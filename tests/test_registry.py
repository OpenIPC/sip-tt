"""The registry, and the invariant that keeps identifiers honest."""

import pytest

from sip_tt.registry import REGISTRY, discover, match_xfail, register
from sip_tt.specs import catalog


def setup_module(_):
    discover()


def test_something_is_registered():
    assert len(REGISTRY) > 5


def test_every_non_local_id_exists_in_the_catalogue():
    """An invented identifier is worse than a missing test.

    It reads as coverage of a purpose nobody wrote, and `sip-tt list
    --missing` stops telling the truth about what is left to do.
    """
    known = catalog.load()
    assert known, "the shipped catalogue is empty"
    invented = [k for k in REGISTRY
                if not k.startswith("LOCAL-") and k not in known]
    assert not invented, f"identifiers not in the corpus: {invented}"


def test_local_ids_are_clearly_ours():
    for k in REGISTRY:
        if k not in catalog.load():
            assert k.startswith("LOCAL-"), (
                f"{k} is not in the corpus and is not marked LOCAL-")


def test_mandatory_matches_the_corpus():
    """A test may not claim a purpose is optional when the corpus says it is not.

    The other direction is allowed: a LOCAL- test can be mandatory on our own
    judgement, and a Recommended purpose can be run as non-mandatory.
    """
    known = catalog.load()
    for tp_id, impl in REGISTRY.items():
        tp = known.get(tp_id)
        if tp is None:
            continue
        if tp.mandatory and not impl.mandatory:
            pytest.fail(f"{tp_id} is Mandatory in the corpus but registered "
                        f"as optional")


def test_duplicate_registration_raises():
    with pytest.raises(RuntimeError, match="Duplicate"):
        register(next(iter(REGISTRY)))(lambda: None)


def test_unknown_role_is_refused():
    with pytest.raises(ValueError, match="unknown role"):
        register("LOCAL-TEST-ONLY-ROLE", roles={"bystander"})(lambda: None)


def test_unknown_fixture_is_refused():
    with pytest.raises(ValueError, match="unknown fixture"):
        register("LOCAL-TEST-ONLY-FIXTURE", requires={"telepathy"})(lambda: None)


def test_xfail_matchers_are_anded_within_and_ored_across():
    impl = next(iter(REGISTRY.values()))
    saved = impl.xfail_on
    try:
        impl.xfail_on = [
            {"vendor": "A", "firmware": "1.0", "reason": "both"},
            {"vendor": "B", "reason": "either"},
        ]
        assert match_xfail(impl, {"vendor": "A", "firmware": "1.0"}) == "both"
        assert match_xfail(impl, {"vendor": "A", "firmware": "2.0"}) is None
        assert match_xfail(impl, {"vendor": "B"}) == "either"
        assert match_xfail(impl, {"vendor": "C"}) is None
    finally:
        impl.xfail_on = saved


def test_a_missing_fingerprint_key_never_matches():
    """A matcher must not widen because the device told us less than usual."""
    impl = next(iter(REGISTRY.values()))
    saved = impl.xfail_on
    try:
        impl.xfail_on = [{"firmware": "old", "reason": "known"}]
        assert match_xfail(impl, {}) is None
    finally:
        impl.xfail_on = saved
