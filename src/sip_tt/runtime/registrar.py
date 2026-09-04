"""A minimal registrar, so a registrant has something to register with.

The registrant purposes (`SIP_RG_RT_*`) are all about what the device *sends*:
the shape of its Request-URI, whether From and To carry the same URI, whether
CSeq increments across refreshes, whether it retries a 401 with credentials.
None of that is observable unless something accepts the REGISTER, so sip-tt
has to be a registrar. There is no onvif-tt counterpart to this: an ONVIF
device is a server and answers when spoken to, while a registrant speaks
first and only to a registrar it has been configured to trust.

Two behaviours here are deliberate levers rather than politeness.

**It challenges by default.** A registrar that accepts an unauthenticated
REGISTER never learns whether the device can compute a digest, and
`SIP_RG_RT_V_007` is exactly that question. The challenge is issued once per
Call-ID so a device that cannot authenticate still makes progress rather than
looping.

**The granted expiry is short and configurable.** RFC 3261 §10.2.4 makes the
registrar's returned expiry authoritative — the registrant must refresh within
*that* window, not within the one it asked for. Granting sixty seconds is how
a test session gets a second REGISTER to compare against the first without
waiting an hour, and it is also the measurement for `SIP_RG_RT_V_012`: a
device that refreshes on its own configured schedule instead is ignoring what
the registrar told it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import auth as _auth
from .message import Message, split_params


@dataclass
class Binding:
    """One contact registered against an address of record."""

    aor: str
    contact: str
    expires: int
    registered_at: float = field(default_factory=time.time)
    call_id: str = ""
    cseq: int = 0

    @property
    def expired(self) -> bool:
        return time.time() > self.registered_at + self.expires


class Registrar:
    """Answers REGISTER on an Endpoint, and records everything it saw."""

    def __init__(self, endpoint, *, realm: str = "sip-tt",
                 password: str = "", challenge: bool = True,
                 grant_expires: int = 60, qop: str = "auth") -> None:
        self.endpoint = endpoint
        self.realm = realm
        self.password = password
        self.challenge = challenge
        self.grant_expires = grant_expires
        self.qop = qop

        # Every REGISTER seen, in arrival order. The tests assert on this, so
        # nothing is discarded — including the ones we rejected, since "did it
        # retry, and with what" is half the registrant suite.
        self.registers: list[Message] = []
        # The ones we answered 200 to. A challenge and its retry are one
        # registration, so counting raw REGISTERs makes an auth retry look
        # like a refresh — which is how a refresh test can pass in 0.0s
        # having waited for nothing.
        self.accepted: list[Message] = []
        self.bindings: dict[str, Binding] = {}
        self.challenged: dict[str, str] = {}     # Call-ID -> nonce
        self.rejected: list[Message] = []

        endpoint.auto_responders.append(self._on_request)

    # -- observation helpers the tests use ---------------------------------

    def wait_for_register(self, timeout: float = 90.0, *,
                          after: int = 0) -> Message | None:
        """Wait until at least `after`+1 REGISTERs have been seen.

        The default timeout is long because a registrant refreshes on its own
        schedule and we cannot ask it to hurry — only grant a short expiry and
        wait. A test that needs two REGISTERs should grant sixty seconds and
        allow at least twice that.
        """
        if len(self.registers) > after:
            return self.registers[after]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.endpoint.pump(min(1.0, deadline - time.monotonic()))
            if len(self.registers) > after:
                return self.registers[after]
        return None

    def wait_for_refresh(self, timeout: float) -> Message | None:
        """Wait for a *second* accepted registration — a genuine refresh.

        Not the second REGISTER: a 401 and its retry are one registration, and
        counting messages instead of registrations is how this test used to
        pass instantly against a device that had refreshed nothing.
        """
        want = len(self.accepted) + 1
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.endpoint.pump(min(1.0, max(0.05, deadline - time.monotonic())))
            if len(self.accepted) >= want:
                return self.accepted[-1]
        return None

    @property
    def authenticated(self) -> list[Message]:
        return [m for m in self.registers if m.headers.get("authorization")]

    def detach(self) -> None:
        if self._on_request in self.endpoint.auto_responders:
            self.endpoint.auto_responders.remove(self._on_request)

    # -- the responder ------------------------------------------------------

    def _on_request(self, msg: Message) -> bool:
        if msg.method != "REGISTER":
            return False
        self.registers.append(msg)

        if self.challenge and self.password:
            authz = msg.headers.get("authorization")
            if not authz:
                nonce = self.challenged.get(msg.call_id)
                if nonce is None:
                    nonce = _auth.make_challenge(self.realm, qop=self.qop)
                    self.challenged[msg.call_id] = nonce
                    self.rejected.append(msg)
                    self.endpoint.respond(
                        msg, 401, "Unauthorized",
                        extra=[("WWW-Authenticate", nonce)])
                    return True
                # Already challenged this Call-ID and it came back without
                # credentials. Accept rather than loop: "cannot authenticate"
                # is a finding for the test to report, not a reason to make
                # the device retry for ever.
            else:
                ok, why = _auth.verify(authz, password=self.password,
                                       method="REGISTER")
                if not ok:
                    self.rejected.append(msg)
                    self.endpoint.respond(
                        msg, 403, "Forbidden",
                        extra=[("Warning", f'399 sip-tt "{why}"')])
                    return True

        aor = _bare_uri(msg.headers.get("to"))
        contacts = msg.headers.all("contact")

        # No Contact at all is a query for the current bindings
        # (RFC 3261 §10.2.1), not a registration.
        if contacts:
            for c in contacts:
                self._apply(aor, c, msg)

        self.accepted.append(msg)
        self.endpoint.respond(msg, 200, "OK",
                              extra=self._binding_headers(aor))
        return True

    def _apply(self, aor: str, contact: str, msg: Message) -> None:
        body, params = split_params(contact)
        header_expires = msg.headers.get("expires")
        if body.strip() == "*":
            # "Contact: *" with Expires: 0 removes every binding (§10.2.2).
            if header_expires.strip() == "0":
                for key in [k for k in self.bindings if k.startswith(aor + "|")]:
                    del self.bindings[key]
            return
        requested = params.get("expires") or header_expires or "3600"
        try:
            requested_i = int(requested)
        except ValueError:
            requested_i = 3600
        key = f"{aor}|{_bare_uri(contact)}"
        if requested_i == 0:
            self.bindings.pop(key, None)
            return
        self.bindings[key] = Binding(
            aor=aor, contact=_bare_uri(contact),
            expires=min(requested_i, self.grant_expires),
            call_id=msg.call_id, cseq=msg.cseq_number)

    def _binding_headers(self, aor: str) -> list[tuple[str, str]]:
        """The current registration list, as §10.3 step 8 requires."""
        out = []
        for b in self.bindings.values():
            if b.aor != aor:
                continue
            out.append(("Contact", f"<{b.contact}>;expires={b.expires}"))
        out.append(("Expires", str(self.grant_expires)))
        out.append(("Date", time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                                          time.gmtime())))
        return out


def _bare_uri(header_value: str) -> str:
    if not header_value:
        return ""
    start = header_value.find("<")
    if start >= 0:
        end = header_value.find(">", start)
        if end > start:
            return header_value[start + 1:end]
    body, _ = split_params(header_value)
    return body.strip()
