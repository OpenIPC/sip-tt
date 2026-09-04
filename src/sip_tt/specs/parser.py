"""Parse ETSI TS 102 027-2 into a machine-readable catalogue.

**The document is not redistributable.** ETSI deliverables carry "No part may
be reproduced except as authorized by written permission", so neither the PDF
nor the text it yields nor the parsed JSON is ever committed here. This module
runs on the user's machine, against a copy the user downloaded, and writes
into a gitignored file.

What *is* committed is `corpus/EXPECTED.json` — the document's checksum, the
purpose count and the sorted list of identifiers. Identifiers and counts are
facts, not expression, and they give the same guarantee onvif-tt gets from
vendoring its parsed corpus: CI can prove the parse is complete and stable
without the repository carrying a word of ETSI's prose.

The parse itself is against `pdftotext` output. Each purpose is a run of

    TPId:
    <identifier>
    Status:
    <applicability>
    Ref:
    <RFC 3261 sections>
    Purpose:
    <one or more lines>

with the page furniture — a running header, the page number, a bare "ETSI" —
interleaved anywhere, including in the middle of a purpose. Stripping that
first is what takes the yield from 362 blocks to all of them.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from .models import TP_ID, TestPurpose, normalise_id

PDF_URL = ("https://www.etsi.org/deliver/etsi_ts/102000_102099/10202702/"
           "04.01.01_60/ts_10202702v040101p.pdf")
PDF_SHA256 = "034ecd9cd8183701b4a263e4ae5bbe04b24a975e9ad51f69c3970acd12adce1f"
DOC_VERSION = "V4.1.1 (2006-07)"

# Page furniture, which pdftotext interleaves with the content — including in
# the middle of a purpose, which is how "48   ETSI TS 102 027-2 V4.1.1
# (2006-07)" ends up appended to a test purpose's text. In `-layout` mode the
# page number shares a line with the running header, on either side of it, so
# matching the header alone is not enough.
_HEADER = r"ETSI TS 102 027-2\s+V[\d.]+\s+\(\d{4}-\d{2}\)"
_FURNITURE = re.compile(
    rf"^(?:\d{{1,3}}\s+)?(?:{_HEADER}|ETSI)(?:\s+\d{{1,3}})?$|^\d{{1,3}}$")

# A field label, with or without its value on the same line. `pdftotext
# -layout` keeps the document's two columns, so a field reads
# "TPId:              SIP_RG_RT_V_001"; plain `pdftotext` puts the label and
# the value on separate lines. Both are accepted, because a user who runs the
# obvious command should still get a catalogue rather than an empty one.
_FIELD = re.compile(r"^(TPId|Status|Ref|Purpose):\s*(.*)$")

# A heading ends whatever purpose was being read. Without it the prose that
# follows is folded into the last purpose — and the very last purpose in the
# document has no next TPId to stop it, so it absorbed the bibliography.
# Annexes and the change history are headings too, and are not numbered.
_HEADING = re.compile(
    r"^\d+(?:\.\d+)+\s+\S"
    r"|^Annex\s+[A-Z]\b"
    r"|^History\b"
    r"|^Bibliography\b")


def strip_furniture(text: str) -> list[str]:
    """Drop page headers, footers and page numbers, keeping content order."""
    out = []
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if not s or _FURNITURE.match(s):
            continue
        out.append(s)
    return out


def parse_text(text: str) -> list[TestPurpose]:
    """Parse pdftotext output into purposes, sorted by identifier.

    Deterministic by construction: the input order is the document's, and the
    output is sorted, so re-parsing the same PDF yields byte-identical JSON.
    That is what makes EXPECTED.json a usable guard.
    """
    lines = strip_furniture(text)
    purposes: dict[str, TestPurpose] = {}

    i = 0
    n = len(lines)
    while i < n:
        m = _FIELD.match(lines[i])
        if not m or m.group(1) != "TPId":
            i += 1
            continue

        fields: dict[str, list[str]] = {"TPId": []}
        current = "TPId"
        if m.group(2):
            fields["TPId"].append(m.group(2))
        i += 1

        while i < n:
            m = _FIELD.match(lines[i])
            if m:
                # The next TPId ends this block, and the label has to be
                # tested before anything else is done with it: "TPId:" is
                # itself a field label, so treating it as just another field
                # folds the whole document into one record. That bug parsed
                # two purposes out of 609 and looked like a bad regex.
                if m.group(1) == "TPId":
                    break
                current = m.group(1)
                fields[current] = [m.group(2)] if m.group(2) else []
                i += 1
                continue
            if _HEADING.match(lines[i]):
                break
            fields.setdefault(current, []).append(lines[i])
            i += 1

        tp_id = normalise_id(" ".join(fields.get("TPId", [])))
        if not TP_ID.match(tp_id):
            continue
        tp = TestPurpose.from_id(
            tp_id,
            status=" ".join(fields.get("Status", [])).strip(),
            ref=" ".join(fields.get("Ref", [])).strip(),
            purpose=" ".join(fields.get("Purpose", [])).strip(),
        )
        if tp is None:
            continue
        # The document lists each identifier once in a table of contents and
        # once in full. Keep whichever carries the most detail.
        prev = purposes.get(tp.id)
        if prev is None or len(tp.purpose) > len(prev.purpose):
            purposes[tp.id] = tp

    return [purposes[k] for k in sorted(purposes)]


def pdf_to_text(pdf: Path) -> str:
    """Run pdftotext, with an error that says how to get it."""
    if shutil.which("pdftotext") is None:
        raise RuntimeError(
            "pdftotext is not installed. It ships in poppler-utils:\n"
            "  Debian/Ubuntu: sudo apt-get install poppler-utils\n"
            "  Fedora:        sudo dnf install poppler-utils\n"
            "  macOS:         brew install poppler")
    proc = subprocess.run(["pdftotext", "-layout", str(pdf), "-"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {proc.stderr.strip()}")
    return proc.stdout


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_pdf(pdf: Path) -> list[TestPurpose]:
    return parse_text(pdf_to_text(pdf))


def expectation(purposes: list[TestPurpose], digest: str) -> dict:
    """The committed guard: facts about the parse, not the parsed text."""
    return {
        "document": "ETSI TS 102 027-2",
        "version": DOC_VERSION,
        "url": PDF_URL,
        "sha256": digest,
        "count": len(purposes),
        "ids": [p.id for p in purposes],
    }


def check(purposes: list[TestPurpose], expected: dict) -> list[str]:
    """Compare a fresh parse with the committed expectation."""
    problems = []
    got = [p.id for p in purposes]
    if len(got) != expected.get("count"):
        problems.append(f"parsed {len(got)} purposes, expected "
                        f"{expected.get('count')}")
    missing = sorted(set(expected.get("ids", [])) - set(got))
    extra = sorted(set(got) - set(expected.get("ids", [])))
    if missing:
        problems.append(f"{len(missing)} identifiers no longer parse: "
                        f"{', '.join(missing[:5])}"
                        + (" ..." if len(missing) > 5 else ""))
    if extra:
        problems.append(f"{len(extra)} identifiers appeared: "
                        f"{', '.join(extra[:5])}"
                        + (" ..." if len(extra) > 5 else ""))
    return problems


def to_json(purposes: list[TestPurpose]) -> str:
    return json.dumps([p.to_dict() for p in purposes], indent=2, sort_keys=True)
