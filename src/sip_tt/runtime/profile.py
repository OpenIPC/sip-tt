"""How a device under test is described.

Two things live here that onvif-tt has no need for.

**Roles.** An ONVIF device is a server; you point the tool at it and it
answers. A SIP user agent may register, may place calls, may receive them, and
which of those it does is a property of the deployment, not of the protocol.
A purpose written for an originating endpoint is *not applicable* to a device
configured only to answer — not a failure, and not a pass either.

**Trigger.** A SIP UA has no control channel. To exercise originating
behaviour something has to make the device place a call, and that something is
outside SIP: an HTTP API, a GPIO button, a person. The profile says which. Where
there is none, those purposes are reported as not-run, and the report says so
rather than quietly showing green.

**PICS answers.** The corpus gates many purposes on conditions like
`PICS: A.77/1.2`, which index the ICS tables of TS 102 027-1. A profile answers
the ones the device knows about; an unanswered condition leaves the purpose
applicable, because assuming a device lacks a capability is how a test suite
shrinks without anyone noticing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Trigger:
    """How to make the device originate a call."""

    kind: str = "none"          # "none" | "http" | "manual"
    url: str = ""               # for kind="http"
    method: str = "POST"
    username: str = ""
    password: str = ""
    hangup_url: str = ""

    @property
    def available(self) -> bool:
        return self.kind != "none"


@dataclass
class Profile:
    """Everything sip-tt needs to know about one device."""

    host: str = ""
    port: int = 5060
    # What the device is called, and what it will accept from us.
    username: str = ""
    password: str = ""
    domain: str = ""
    aor: str = ""

    # Which of the three UA roles this device plays. Purposes for a role it
    # does not play are skipped as not applicable.
    roles: set[str] = field(default_factory=lambda: {"terminating"})

    # The address the device must be able to route back to. Not guessable on a
    # multi-homed host: guessing wrong sends the device's media somewhere it
    # cannot arrive from, which imitates the bug under test.
    local_ip: str = ""
    local_port: int = 0

    trigger: Trigger = field(default_factory=Trigger)
    pics: dict[str, bool] = field(default_factory=dict)

    # Free-form identity, matched by `xfail_on`. Anything a report should
    # carry: vendor, model, firmware.
    fingerprint: dict[str, str] = field(default_factory=dict)

    @property
    def target(self) -> tuple[str, int]:
        return (self.host, self.port)

    @property
    def uri(self) -> str:
        return self.aor or f"sip:{self.username}@{self.domain or self.host}"

    def plays(self, role: str) -> bool:
        return role in self.roles

    def pics_allows(self, expression: str) -> bool | None:
        """Whether a PICS expression is satisfied. None means 'not answered'.

        Only the AND form the corpus actually uses is understood. An
        expression with anything else returns None — unanswered, therefore
        applicable — because silently reading a condition wrong is how a
        purpose stops running while the report still says it was considered.
        """
        expr = expression.strip()
        if not expr:
            return True
        if " OR " in expr.upper():
            return None
        parts = [p.strip() for p in expr.replace(" and ", " AND ").split(" AND ")]
        answers = []
        for p in parts:
            key = p.strip().rstrip(".")
            if key not in self.pics:
                return None
            answers.append(self.pics[key])
        return all(answers)

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        data = json.loads(Path(path).read_text())
        trig = Trigger(**data.pop("trigger", {}))
        roles = set(data.pop("roles", ["terminating"]))
        return cls(trigger=trig, roles=roles, **data)

    def to_dict(self) -> dict:
        return {
            "host": self.host, "port": self.port, "username": self.username,
            "domain": self.domain, "aor": self.aor,
            "roles": sorted(self.roles), "local_ip": self.local_ip,
            "trigger": self.trigger.kind, "fingerprint": self.fingerprint,
            "pics_answered": len(self.pics),
        }
