"""The corpus pipeline, and the guard that stands in for a vendored copy.

onvif-tt commits its parsed corpus and asserts a fresh parse matches it byte
for byte. ETSI's terms forbid that here, so the committed artefact is
`corpus/EXPECTED.json` — a checksum, a count and the sorted identifiers, which
are facts rather than expression. These tests hold it to the same contract.
"""

import json
from pathlib import Path

import pytest

from sip_tt.specs import catalog, parser
from sip_tt.specs.models import TP_ID, TestPurpose, normalise_id

REPO = Path(__file__).resolve().parent.parent
EXPECTED = REPO / "corpus" / "EXPECTED.json"
PARSED = REPO / "corpus" / "parsed.json"


def test_expected_is_self_consistent():
    exp = json.loads(EXPECTED.read_text())
    assert exp["count"] == len(exp["ids"])
    assert exp["ids"] == sorted(exp["ids"]), "ids must be sorted to diff cleanly"
    assert len(set(exp["ids"])) == len(exp["ids"]), "duplicate identifiers"
    assert exp["sha256"] == parser.PDF_SHA256


def test_the_shipped_catalogue_covers_every_expected_id():
    exp = json.loads(EXPECTED.read_text())
    known = catalog.load()
    assert sorted(known) == exp["ids"]


def test_the_catalogue_carries_no_etsi_prose():
    """The licence line, asserted rather than remembered.

    Identifiers, applicability conditions and RFC citations are facts. The
    purpose text is ETSI's expression and may not be redistributed, so it must
    not be in the file that ships.
    """
    raw = catalog.CATALOG_PATH.read_text()
    assert "Ensure that" not in raw
    for tp in catalog.load().values():
        assert tp.purpose == ""


def test_identifier_grammar_covers_every_shipped_id():
    for tp_id in catalog.load():
        assert TP_ID.match(tp_id), f"{tp_id} does not match the grammar"


def test_the_timer_class_is_recognised():
    """TI is 234 identifiers. A grammar without it drops them silently."""
    tp = TestPurpose.from_id("SIP_QC_OE_TI_001")
    assert tp is not None and tp.klass == "TI"


def test_stray_spaces_from_pdftotext_are_squeezed():
    """pdftotext emits the OPTIONS family as "SIP_QC_OE_ V_001"."""
    assert normalise_id("SIP_QC_OE_ V_001") == "SIP_QC_OE_V_001"
    assert TestPurpose.from_id("SIP_QC_OE _V_002").id == "SIP_QC_OE_V_002"


def test_status_is_read_as_an_applicability_predicate():
    tp = TestPurpose(id="X", status="PICS: A.77/1.2 AND A.77/3.2")
    assert tp.pics == "A.77/1.2 AND A.77/3.2"
    assert not tp.mandatory
    assert TestPurpose(id="X", status="Mandatory").pics == ""
    assert TestPurpose(id="X", status="Void").void


def test_only_user_agent_entities_apply_to_a_ua():
    assert TestPurpose.from_id("SIP_CC_TE_CE_V_001").applies_to_ua
    assert TestPurpose.from_id("SIP_RG_RT_V_001").applies_to_ua
    assert not TestPurpose.from_id("SIP_RG_RR_V_001").applies_to_ua
    assert not TestPurpose.from_id("SIP_CC_PR_MP_RQ_V_001").applies_to_ua


@pytest.mark.skipif(not PARSED.exists(),
                    reason="no local parse; run `sip-tt corpus refresh`")
def test_a_local_parse_matches_the_expectation():
    parsed = [TestPurpose(**r) for r in json.loads(PARSED.read_text())]
    exp = json.loads(EXPECTED.read_text())
    assert parser.check(parsed, exp) == []


@pytest.mark.skipif(not PARSED.exists(), reason="no local parse")
def test_the_parse_is_deterministic():
    """Without this, EXPECTED.json cannot be trusted as a guard."""
    records = json.loads(PARSED.read_text())
    text = "\n".join(f"TPId: {r['id']}\nStatus: {r['status']}\n"
                     f"Ref: {r['ref']}\nPurpose: {r['purpose']}"
                     for r in records)
    first = parser.to_json(parser.parse_text(text))
    second = parser.to_json(parser.parse_text(text))
    assert first == second


@pytest.mark.skipif(not PARSED.exists(), reason="no local parse")
def test_no_page_furniture_survives_into_a_purpose():
    """The running header interleaves with content, including mid-sentence."""
    for r in json.loads(PARSED.read_text()):
        assert "ETSI TS 102 027-2" not in r["purpose"], r["id"]
