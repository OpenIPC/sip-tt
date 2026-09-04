"""Which test purposes sip-tt actually implements.

A module-level dict populated by import side effects, exactly as onvif-tt does
it, because the property that matters is that a test's *declaration* and its
*code* cannot drift: the identifier is the decorator argument, and the
catalogue is checked against it.

Duplicate registration raises rather than overwrites. Two implementations of
one purpose means one of them is dead code that will never run and never be
noticed, which is worse than a loud failure at import.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable

REGISTRY: dict[str, "Implementation"] = {}

# Roles a device can be asked to play. A purpose is filtered out — skipped as
# not-applicable — when the profile says the device does not play its role.
ROLES = {"registrant", "originating", "terminating"}

# Fixtures a test can require. `trigger` is the one with no ONVIF counterpart:
# a SIP UA has no control channel, so exercising its originating behaviour
# needs some out-of-band way to make it place a call.
FIXTURES = {"registrar", "pbx", "media", "trigger"}


@dataclass(slots=True)
class Implementation:
    test_id: str
    func: Callable[..., Any]
    roles: set[str] = field(default_factory=set)
    mandatory: bool = False
    requires: set[str] = field(default_factory=set)
    pbx: str | None = None            # None | "asterisk13" | "asterisk18"
    tags: set[str] = field(default_factory=set)
    xfail_on: list[dict[str, Any]] = field(default_factory=list)

    @property
    def qualname(self) -> str:
        return f"{self.func.__module__}.{self.func.__qualname__}"


def register(test_id: str, *, roles: set[str] | None = None,
             mandatory: bool = False, requires: set[str] | None = None,
             pbx: str | None = None, tags: set[str] | None = None,
             xfail_on: list[dict[str, Any]] | None = None):
    """Register an implementation of `test_id`. Returns the function unchanged."""
    def deco(func):
        if test_id in REGISTRY:
            existing = REGISTRY[test_id]
            raise RuntimeError(
                f"Duplicate implementation for {test_id!r}: "
                f"{existing.qualname} vs {func.__module__}.{func.__qualname__}")
        bad_roles = (roles or set()) - ROLES
        if bad_roles:
            raise ValueError(f"{test_id}: unknown role(s) {sorted(bad_roles)}")
        bad_fixtures = (requires or set()) - FIXTURES
        if bad_fixtures:
            raise ValueError(f"{test_id}: unknown fixture(s) "
                             f"{sorted(bad_fixtures)}")
        REGISTRY[test_id] = Implementation(
            test_id=test_id, func=func, roles=set(roles or ()),
            mandatory=mandatory, requires=set(requires or ()), pbx=pbx,
            tags=set(tags or ()), xfail_on=list(xfail_on or ()))
        return func
    return deco


def discover() -> None:
    """Import every module under `sip_tt.cases` so registration happens."""
    from . import cases as _cases
    for m in pkgutil.iter_modules(_cases.__path__):
        importlib.import_module(f"{_cases.__name__}.{m.name}")


def match_xfail(impl: Implementation, device: dict[str, Any]) -> str | None:
    """The reason this device is expected to fail, or None.

    Matchers are ANDed within one dict and ORed across the list. A missing key
    is a non-match, so a matcher can never widen by accident when the device
    fingerprint is incomplete.
    """
    for matcher in impl.xfail_on:
        reason = matcher.get("reason", "known non-conformance")
        keys = [k for k in matcher if k != "reason"]
        if not keys:
            continue
        ok = True
        for k in keys:
            if k not in device:
                ok = False
                break
            want, got = matcher[k], device[k]
            if callable(want):
                if not want(got):
                    ok = False
                    break
            elif want != got:
                ok = False
                break
        if ok:
            return reason
    return None
