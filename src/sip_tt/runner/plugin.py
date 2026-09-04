"""pytest plugin: the run's options, and the JSON report.

Registered through the ``pytest11`` entry point, so the options exist as soon
as the package is installed and ``sip-tt run`` is a thin translator into
``pytest.main()``.
"""

from __future__ import annotations

import fnmatch
import json

import pytest

from ..registry import REGISTRY
from ..runtime.profile import Profile, Trigger

_results: list[dict] = []
_meta: dict = {}


def pytest_addoption(parser):
    g = parser.getgroup("sip-tt")
    g.addoption("--target", default="", help="DUT signalling address host[:port]")
    g.addoption("--profile-file", default="", help="JSON device profile")
    g.addoption("--sip-user", default="",
                help="the DUT's SIP user — the user part of the URI we call")
    g.addoption("--sip-password", default="",
                help="credential to answer a challenge from the DUT with")
    g.addoption("--sip-domain", default="")
    g.addoption("--local-ip", default="",
                help="address the DUT must route back to; not guessed, "
                     "because guessing wrong imitates the bug under test")
    g.addoption("--local-port", type=int, default=0)
    g.addoption("--roles", default="terminating",
                help="comma-separated: registrant,originating,terminating")
    g.addoption("--id-glob", action="append", default=[],
                help="only purposes matching this glob (repeatable)")
    g.addoption("--mandatory-only", action="store_true", default=False)
    g.addoption("--skip-tag", action="append", default=[],
                help="do not run purposes carrying this tag (repeatable). "
                     "'advisory' is the one CI usually wants: those purposes "
                     "measure a SHOULD and are reported for information, so "
                     "gating a build on them is noise. What was left out is "
                     "printed, never silently dropped")
    g.addoption("--json-report", default="",
                help="write results.json here")
    for fixture in ("registrar", "pbx", "media"):
        g.addoption(f"--with-{fixture}", action="store_true", default=False,
                    help=f"this run provides the {fixture} fixture")
    g.addoption("--grant-expires", type=int, default=60,
                help="registration lifetime our registrar grants. RFC 3261 "
                     "§10.2.4 makes this authoritative, so a short one is how "
                     "a test session sees a refresh without waiting an hour")


def build_profile(config) -> Profile:
    """A profile from the file, the flags, or both — flags win."""
    if config.getoption("--profile-file"):
        prof = Profile.load(config.getoption("--profile-file"))
    else:
        prof = Profile()

    target = config.getoption("--target")
    if target:
        host, _, port = target.partition(":")
        prof.host = host
        prof.port = int(port) if port else 5060
    for opt, attr in (("--sip-user", "username"), ("--sip-password", "password"),
                      ("--sip-domain", "domain"), ("--local-ip", "local_ip")):
        val = config.getoption(opt)
        if val:
            setattr(prof, attr, val)
    if config.getoption("--local-port"):
        prof.local_port = config.getoption("--local-port")
    if config.getoption("--roles") != "terminating" or not prof.roles:
        prof.roles = {r.strip() for r in config.getoption("--roles").split(",")
                      if r.strip()}
    if not isinstance(prof.trigger, Trigger):
        prof.trigger = Trigger(**prof.trigger)
    return prof


def pytest_collection_modifyitems(config, items):
    globs = config.getoption("--id-glob")
    mandatory_only = config.getoption("--mandatory-only")
    skip_tags = set(config.getoption("--skip-tag"))
    if not globs and not mandatory_only and not skip_tags:
        return
    kept, dropped = [], []
    for item in items:
        tp_id = _id_of(item)
        if tp_id is None:
            kept.append(item)
            continue
        if globs and not any(fnmatch.fnmatch(tp_id, g) for g in globs):
            dropped.append(tp_id)
            continue
        if mandatory_only and not REGISTRY[tp_id].mandatory:
            dropped.append(tp_id)
            continue
        if skip_tags & REGISTRY[tp_id].tags:
            dropped.append(tp_id)
            continue
        kept.append(item)
    items[:] = kept
    if dropped:
        # Named, not counted. A run that quietly leaves purposes out reads as
        # coverage it does not have.
        _meta["not_run"] = sorted(dropped)


def _id_of(item) -> str | None:
    params = getattr(getattr(item, "callspec", None), "params", {})
    tp_id = params.get("test_id")
    return tp_id if tp_id in REGISTRY else None


def pytest_sessionstart(session):
    # pytest.main() runs in-process, so globals survive between invocations.
    _results.clear()
    _meta.clear()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    # The stash has to happen before the yield: TestReport.__init__ copies
    # user_properties when the report is built during the yield, so appending
    # afterwards mutates a list nothing reads again.
    if call.when == "call":
        tp_id = _id_of(item)
        if tp_id is not None:
            impl = REGISTRY[tp_id]
            item.user_properties.append(("sip_tt", {
                "id": tp_id,
                "mandatory": impl.mandatory,
                "roles": sorted(impl.roles),
                "requires": sorted(impl.requires),
            }))
    yield


def pytest_runtest_logreport(report):
    tp_id = None
    payload = {}
    for name, value in report.user_properties:
        if name == "sip_tt":
            payload = value
            tp_id = value["id"]
    if tp_id is None:
        # Skipped or failed before the call phase: recover the id from the
        # node name so a not-applicable purpose still appears in the report.
        if "[" in report.nodeid and report.when == "setup":
            candidate = report.nodeid.rsplit("[", 1)[1].rstrip("]")
            if candidate in REGISTRY:
                tp_id = candidate
                impl = REGISTRY[candidate]
                payload = {"id": candidate, "mandatory": impl.mandatory,
                           "roles": sorted(impl.roles),
                           "requires": sorted(impl.requires)}
    if tp_id is None:
        return
    if report.when not in ("call", "setup"):
        return
    if report.when == "setup" and report.passed:
        return

    if hasattr(report, "wasxfail"):
        status = "xfailed" if report.skipped else "xpassed"
    elif report.passed:
        status = "passed"
    elif report.skipped:
        status = "skipped"
    else:
        status = "failed"

    rec = dict(payload)
    rec["status"] = status
    rec["duration_s"] = round(report.duration, 3)
    if report.longrepr is not None and status in ("failed", "skipped", "xfailed"):
        # A skip's longrepr is a (path, lineno, reason) tuple, not a
        # traceback, and str() on it buries the reason in a repr.
        if isinstance(report.longrepr, tuple) and len(report.longrepr) == 3:
            rec["message"] = str(report.longrepr[2])
            _results.append(rec)
            return
        text = str(report.longrepr)
        # The assertion message is the point of the record, and pytest puts it
        # at the *end* of the traceback after a fixture dump that can run to
        # thousands of characters. Truncating from the front therefore keeps
        # the least useful half and throws the finding away.
        rec["message"] = _assertion_text(text)
        rec["longrepr"] = text if len(text) <= 4000 else "..." + text[-4000:]
    _results.append(rec)


def _assertion_text(longrepr: str) -> str:
    """The assertion message pytest marks with a leading 'E'."""
    lines = [ln.lstrip()[1:].strip()
             for ln in longrepr.splitlines() if ln.lstrip().startswith("E ")]
    if not lines:
        # A skip or an explicit fail outside an assert: pytest renders those
        # as a trailing "Failed: ..." / "Skipped: ..." line instead.
        for ln in reversed(longrepr.splitlines()):
            if ln.strip().startswith(("Failed:", "Skipped:")):
                return ln.strip()
        return ""
    return " ".join(lines)


def pytest_sessionfinish(session, exitstatus):
    path = session.config.getoption("--json-report")
    if not path:
        return
    prof = build_profile(session.config)
    counts: dict[str, int] = {}
    for r in _results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    payload = {
        "device": prof.to_dict(),
        "summary": {"total": len(_results), **counts},
        "results": sorted(_results, key=lambda r: r["id"]),
    }
    if _meta:
        payload["meta"] = _meta
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if _meta.get("not_run"):
        names = _meta["not_run"]
        terminalreporter.write_sep(
            "-", f"{len(names)} purpose(s) not run by this invocation")
        for n in names:
            terminalreporter.write_line(f"  - {n}")
    failed = [r for r in _results if r["status"] == "failed"]
    if not failed:
        return
    mand = sum(1 for r in failed if r.get("mandatory"))
    terminalreporter.write_sep(
        "-", f"{len(failed)} purpose(s) failed, {mand} of them mandatory")
    for r in failed:
        flag = "M" if r.get("mandatory") else " "
        terminalreporter.write_line(f"  [{flag}] {r['id']}")
