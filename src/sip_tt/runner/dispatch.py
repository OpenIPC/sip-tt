"""The pytest module every conformance run actually executes.

One parametrised node per registered identifier, so a node id is
``dispatch.py::test_sip_purpose[SIP_CC_TE_SM_V_001]`` and `-k` selects by real
identifier.

The gating rules here are the whole ethic of the tool, and they are inherited
from a mistake onvif-tt made and documented: twelve of its tests once sat green
without ever executing, because "we could not reach the service" was reported
as a skip and a skip reads as "not applicable". So:

* **Skip** means the purpose does not apply to *this device*: it does not play
  the role, or the profile answers its PICS condition "no", or the corpus marks
  it Void.
* **Fail** means we could not find out whether the device conforms — no
  trigger where one is needed, a fixture that did not come up, the device
  unreachable. That is not the device's verdict, and it must not be recorded
  as one.

The distinction costs nothing to honour and is the difference between a report
that means something and a green wall.
"""

from __future__ import annotations

import inspect
import warnings

import pytest

from ..registry import REGISTRY, discover, match_xfail
from ..specs import catalog

discover()


def _all_ids() -> list[str]:
    return sorted(REGISTRY)


@pytest.fixture(scope="session")
def profile(request):
    from .plugin import build_profile
    return build_profile(request.config)


@pytest.fixture(scope="session")
def endpoint(profile):
    """sip-tt's own user agent, for the whole session."""
    from ..runtime.endpoint import Endpoint
    ep = Endpoint(bind_ip=profile.local_ip or "0.0.0.0",
                  port=profile.local_port,
                  advertise_ip=profile.local_ip or None,
                  username="sip-tt",
                  domain=profile.domain or profile.host,
                  password=profile.password)
    yield ep
    ep.close()


@pytest.fixture(scope="session")
def device(profile):
    """The fingerprint `xfail_on` matches against."""
    return dict(profile.fingerprint)


@pytest.fixture(autouse=True)
def _leave_no_call_standing(endpoint):
    """Guarantee the device is idle again, whatever the test did.

    Structural rather than a convention, because the failure mode is silent
    and cascading: a leaked call leg makes the *next* purpose fail with 486,
    and the report then blames a device behaviour that was never tested.
    """
    yield
    endpoint.teardown_calls()


@pytest.fixture(scope="session")
def registrar(endpoint, profile, request):
    """A registrar for the device to register with, when this run provides one.

    Session-scoped and marked as a persistent responder, because a registrant
    refreshes on its own schedule: a registrar that vanished between purposes
    would leave every REGISTER after the first one unanswered.
    """
    if not request.config.getoption("--with-registrar", default=False):
        return None
    from ..runtime.registrar import Registrar
    reg = Registrar(endpoint, realm=profile.domain or "sip-tt",
                    password=profile.password,
                    grant_expires=request.config.getoption("--grant-expires"))
    endpoint.keep_responders()
    return reg


@pytest.fixture
def spec(request):
    """The catalogue entry for the purpose being run."""
    tp_id = request.node.callspec.params["test_id"]
    entry = catalog.load().get(tp_id)
    local = catalog.local_purposes().get(tp_id)
    if entry is not None and local is not None and not entry.purpose:
        entry.purpose = local.purpose
    return entry


@pytest.mark.parametrize("test_id", _all_ids() or ["__none_registered__"])
def test_sip_purpose(test_id, profile, endpoint, device, spec, request):
    if test_id == "__none_registered__":
        pytest.skip("no implementations registered")

    impl = REGISTRY[test_id]
    entry = catalog.load().get(test_id)

    # -- not applicable to this device: skip -------------------------------
    if entry is not None and entry.void:
        pytest.skip(f"{test_id} is marked Void in the corpus")

    if impl.roles and not (impl.roles & profile.roles):
        pytest.skip(f"device does not play {'/'.join(sorted(impl.roles))}; "
                    f"profile says {'/'.join(sorted(profile.roles))}")

    if entry is not None and entry.pics:
        allowed = profile.pics_allows(entry.pics)
        if allowed is False:
            pytest.skip(f"profile answers PICS {entry.pics} as not supported")

    # -- we could not find out: fail ---------------------------------------
    if "trigger" in impl.requires and not profile.trigger.available:
        pytest.fail(
            f"{test_id} needs the device to place a call, and the profile "
            f"declares no trigger, so this purpose was not exercised. This is "
            f"not a skip: a skip would claim the purpose does not apply to "
            f"this device, when in fact we simply could not ask it. Configure "
            f"`trigger` in the profile, or drop the originating role.")

    for fixture in sorted(impl.requires - {"trigger"}):
        if not request.config.getoption(f"--with-{fixture}", default=False):
            pytest.fail(
                f"{test_id} needs the {fixture!r} fixture, which this run did "
                f"not start (pass --with-{fixture}). Reported as a failure "
                f"rather than a skip because the purpose was never put to the "
                f"device.")

    # -- run ---------------------------------------------------------------
    kwargs = {}
    sig = inspect.signature(impl.func)
    for name, value in (("endpoint", endpoint), ("profile", profile),
                        ("spec", spec), ("device", device),
                        ("request", request)):
        if name in sig.parameters:
            kwargs[name] = value
    if "registrar" in sig.parameters:
        kwargs["registrar"] = request.getfixturevalue("registrar")

    xfail_reason = match_xfail(impl, device)
    if xfail_reason is None:
        impl.func(**kwargs)
        return

    # xfail is decided at runtime from the device fingerprint, so it cannot be
    # a mark. BaseException, not Exception: pytest.fail and pytest.skip raise
    # BaseException subclasses.
    try:
        impl.func(**kwargs)
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        pytest.xfail(f"{xfail_reason} | {type(exc).__name__}: {exc}")
    warnings.warn(
        f"XPASS for {test_id}: expected failure on this device "
        f"({xfail_reason!r}) but it passed — the matcher may be stale.",
        stacklevel=2)
