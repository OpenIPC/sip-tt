"""sip-tt command line."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from . import __version__
from .registry import REGISTRY, discover
from .specs import catalog, parser as specs_parser


def _corpus_dir() -> Path:
    for parent in [Path.cwd(), *Path(__file__).resolve().parents]:
        if (parent / "corpus" / "EXPECTED.json").exists():
            return parent / "corpus"
    return Path.cwd() / "corpus"


# -- list / show ------------------------------------------------------------

def _cmd_list(args) -> int:
    discover()
    entries = catalog.load()
    if not entries:
        print("catalogue is empty — reinstall the package", file=sys.stderr)
        return 2

    rows = []
    for tp_id, tp in sorted(entries.items()):
        implemented = tp_id in REGISTRY
        if args.implemented and not implemented:
            continue
        if args.missing and implemented:
            continue
        if args.role and not _role_matches(tp, args.role):
            continue
        if args.area and tp.area != args.area.upper():
            continue
        if args.ua_only and not tp.applies_to_ua:
            continue
        rows.append({"id": tp_id, "status": tp.status, "ref": tp.ref,
                     "area": tp.area, "entity": tp.entity,
                     "klass": tp.klass, "implemented": implemented})

    if args.format == "json":
        print(json.dumps(rows, indent=None if args.compact else 2))
        return 0

    for r in rows:
        mark = "x" if r["implemented"] else " "
        print(f"[{mark}] {r['id']:24s} {r['status'][:22]:22s} {r['ref']}")
    done = sum(1 for r in rows if r["implemented"])
    print(f"\n{done}/{len(rows)} listed purposes implemented "
          f"({len(REGISTRY)} implementations registered in total)")
    return 0


_ROLE_ENTITY = {"registrant": "RT", "originating": "OE", "terminating": "TE"}


def _role_matches(tp, role: str) -> bool:
    return tp.entity == _ROLE_ENTITY.get(role, role.upper())


def _cmd_show(args) -> int:
    discover()
    tp = catalog.load().get(args.test_id)
    if tp is None:
        print(f"unknown test purpose: {args.test_id}", file=sys.stderr)
        return 2
    local = catalog.local_purposes().get(args.test_id)
    impl = REGISTRY.get(args.test_id)

    if args.format == "json":
        d = tp.to_dict()
        d["implemented"] = impl is not None
        if impl is not None:
            d["implementation"] = impl.qualname
            d["roles"] = sorted(impl.roles)
        if local is not None:
            d["purpose"] = local.purpose
        print(json.dumps(d, indent=2))
        return 0

    print(f"{tp.id}")
    print(f"  classification : {tp.describe()}")
    print(f"  status         : {tp.status}")
    print(f"  reference      : {tp.ref}")
    print(f"  applies to a UA: {'yes' if tp.applies_to_ua else 'no'}")
    if impl is not None:
        print(f"  implemented by : {impl.qualname}")
        print(f"  roles          : {', '.join(sorted(impl.roles)) or '-'}")
        if impl.requires:
            print(f"  requires       : {', '.join(sorted(impl.requires))}")
    else:
        print("  implemented by : (not implemented)")
    if local is not None and local.purpose:
        print(f"\n  Purpose (from your local copy of {catalog.meta().get('document','the spec')}):")
        print(f"    {local.purpose}")
    else:
        print("\n  The purpose text is ETSI's and is not redistributed here.")
        print("  Run `sip-tt corpus refresh` to read it from your own copy.")
    return 0


# -- corpus -----------------------------------------------------------------

def _cmd_corpus(args) -> int:
    corpus = _corpus_dir()
    corpus.mkdir(parents=True, exist_ok=True)
    expected_path = corpus / "EXPECTED.json"

    if args.corpus_cmd == "stats":
        entries = catalog.load()
        meta = catalog.meta()
        print(f"{meta.get('document', '?')} {meta.get('version', '')}")
        print(f"  purposes            : {len(entries)}")
        print(f"  applicable to a UA  : "
              f"{sum(1 for t in entries.values() if t.applies_to_ua)}")
        by_entity: dict[str, int] = {}
        for t in entries.values():
            by_entity[t.entity] = by_entity.get(t.entity, 0) + 1
        for k in sorted(by_entity):
            print(f"    {k}: {by_entity[k]}")
        local = catalog.local_purposes()
        print(f"  local parse present : "
              f"{'yes, ' + str(len(local)) + ' purposes' if local else 'no'}")
        return 0

    pdf = Path(args.pdf) if args.pdf else corpus / "ts102027-2.pdf"
    if args.corpus_cmd == "refresh" and not pdf.exists():
        print(f"downloading {specs_parser.PDF_URL}")
        req = urllib.request.Request(
            specs_parser.PDF_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r, open(pdf, "wb") as fh:
            fh.write(r.read())

    if not pdf.exists():
        print(f"no document at {pdf}. `sip-tt corpus refresh` downloads it, "
              f"or pass --pdf.", file=sys.stderr)
        return 2

    digest = specs_parser.sha256(pdf)
    if digest != specs_parser.PDF_SHA256:
        print(f"note: {pdf.name} hashes {digest[:16]}..., not the "
              f"{specs_parser.PDF_SHA256[:16]}... this catalogue was built "
              f"from. A newer revision may add or renumber purposes.")

    purposes = specs_parser.parse_pdf(pdf)
    print(f"parsed {len(purposes)} test purposes")

    if expected_path.exists():
        expected = json.loads(expected_path.read_text())
        problems = specs_parser.check(purposes, expected)
        if problems:
            for p in problems:
                print(f"  ! {p}", file=sys.stderr)
            if args.corpus_cmd == "verify":
                return 1
        else:
            print(f"  matches corpus/EXPECTED.json ({expected['count']} ids)")

    if args.corpus_cmd == "verify":
        return 0

    out = corpus / "parsed.json"
    out.write_text(specs_parser.to_json(purposes) + "\n")
    print(f"wrote {out} (gitignored — it carries ETSI's wording)")
    if args.write_expected:
        expected_path.write_text(
            json.dumps(specs_parser.expectation(purposes, digest), indent=2) + "\n")
        print(f"wrote {expected_path}")
    if args.write_catalog:
        catalog.write(purposes, document="ETSI TS 102 027-2",
                      version=specs_parser.DOC_VERSION,
                      url=specs_parser.PDF_URL, sha256=digest)
        print(f"wrote {catalog.CATALOG_PATH}")
    return 0


# -- run --------------------------------------------------------------------

def _cmd_run(args, extra: list[str]) -> int:
    import pytest
    from .runner import dispatch as dispatch_mod

    argv = [dispatch_mod.__file__, f"--target={args.target}"]
    for flag, value in (("--profile-file", args.profile),
                        ("--sip-user", args.user),
                        ("--sip-password", args.password),
                        ("--sip-domain", args.domain),
                        ("--local-ip", args.local_ip),
                        ("--roles", args.roles)):
        if value:
            argv.append(f"{flag}={value}")
    if args.local_port:
        argv.append(f"--local-port={args.local_port}")
    for g in args.id_glob:
        argv.append(f"--id-glob={g}")
    if args.mandatory_only:
        argv.append("--mandatory-only")
    for fixture in ("registrar", "pbx", "media"):
        if getattr(args, f"with_{fixture}"):
            argv.append(f"--with-{fixture}")
    if args.junit_xml:
        argv.append(f"--junit-xml={args.junit_xml}")
    if args.json_report:
        argv.append(f"--json-report={args.json_report}")
    argv.extend(extra)
    return pytest.main(argv)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="sip-tt",
        description="SIP conformance test tool. Scores a device against the "
                    "numbered test purposes of ETSI TS 102 027-2.")
    ap.add_argument("--version", action="version", version=f"sip-tt {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list test purposes")
    p.add_argument("--implemented", action="store_true")
    p.add_argument("--missing", action="store_true")
    p.add_argument("--role", choices=sorted(_ROLE_ENTITY))
    p.add_argument("--area", help="RG, CC, MG or QC")
    p.add_argument("--ua-only", action="store_true",
                   help="only purposes a user agent can be asked to meet")
    p.add_argument("--format", choices=("human", "json"), default="human")
    p.add_argument("--compact", action="store_true")
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("show", help="show one test purpose")
    p.add_argument("test_id")
    p.add_argument("--format", choices=("human", "json"), default="human")
    p.set_defaults(func=_cmd_show)

    p = sub.add_parser("corpus", help="fetch, parse and check the corpus")
    p.add_argument("corpus_cmd", choices=("refresh", "verify", "stats"))
    p.add_argument("--pdf", help="use this document instead of downloading")
    p.add_argument("--write-expected", action="store_true",
                   help="regenerate corpus/EXPECTED.json (maintainers)")
    p.add_argument("--write-catalog", action="store_true",
                   help="regenerate the shipped catalogue (maintainers)")
    p.set_defaults(func=_cmd_corpus)

    p = sub.add_parser("run", help="run the suite against a device")
    p.add_argument("--target", required=True, help="host[:port] of the DUT")
    p.add_argument("--profile", help="JSON device profile")
    p.add_argument("--user", default="")
    p.add_argument("--password", default="")
    p.add_argument("--domain", default="")
    p.add_argument("--local-ip", default="")
    p.add_argument("--local-port", type=int, default=0)
    p.add_argument("--roles", default="terminating")
    p.add_argument("--id-glob", action="append", default=[])
    p.add_argument("--mandatory-only", action="store_true")
    p.add_argument("--with-registrar", action="store_true")
    p.add_argument("--with-pbx", action="store_true")
    p.add_argument("--with-media", action="store_true")
    p.add_argument("--junit-xml", default="")
    p.add_argument("--json-report", default="")
    p.set_defaults(func=None)

    args, extra = ap.parse_known_args(argv)
    if args.cmd == "run":
        return _cmd_run(args, extra)
    if extra:
        ap.error(f"unrecognised arguments: {' '.join(extra)}")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
