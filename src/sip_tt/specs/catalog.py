"""The committed catalogue: what sip-tt knows without the ETSI document.

`corpus/parsed.json` carries ETSI's own wording and is produced locally and
gitignored. This catalogue is what ships, and the line between them is
deliberate: identifiers, applicability conditions and RFC citations are
*facts* — you cannot state which RFC section a purpose tests in a different
way — while the purpose text is ETSI's expression and is theirs.

So the catalogue lets `sip-tt list` work, lets the registry check that every
registered identifier is real, and lets a device profile decide what applies,
all on a machine that has never downloaded the specification. `sip-tt show`
prints the purpose text only when a local parse is present.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .models import TestPurpose

CATALOG_PATH = Path(__file__).resolve().parent.parent / "catalog.json"


@lru_cache(maxsize=1)
def load() -> dict[str, TestPurpose]:
    """Every known purpose, keyed by identifier. No ETSI prose."""
    if not CATALOG_PATH.exists():
        return {}
    raw = json.loads(CATALOG_PATH.read_text())
    out: dict[str, TestPurpose] = {}
    for rec in raw["purposes"]:
        tp = TestPurpose(**rec)
        out[tp.id] = tp
    return out


def meta() -> dict:
    if not CATALOG_PATH.exists():
        return {}
    raw = json.loads(CATALOG_PATH.read_text())
    return {k: v for k, v in raw.items() if k != "purposes"}


def write(purposes: list[TestPurpose], *, document: str, version: str,
          url: str, sha256: str) -> None:
    """Regenerate the catalogue from a fresh parse, dropping ETSI's wording."""
    payload = {
        "document": document,
        "version": version,
        "url": url,
        "sha256": sha256,
        "note": ("Identifiers, applicability conditions and RFC citations "
                 "only. The purpose text is ETSI's and is not redistributed; "
                 "run `sip-tt corpus refresh` to read it locally."),
        "purposes": [
            {k: v for k, v in p.to_dict().items() if k != "purpose"}
            for p in purposes
        ],
    }
    CATALOG_PATH.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")


@lru_cache(maxsize=1)
def local_purposes() -> dict[str, TestPurpose]:
    """The locally parsed corpus, with ETSI's wording, if the user made one."""
    path = _repo_root() / "corpus" / "parsed.json"
    if not path.exists():
        return {}
    return {r["id"]: TestPurpose(**r) for r in json.loads(path.read_text())}


def _repo_root() -> Path:
    # Installed as a package there is no repo; fall back to the working
    # directory, which is where `sip-tt corpus refresh` writes.
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "corpus").is_dir():
            return parent
    return Path.cwd()
