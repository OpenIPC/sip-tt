"""The scriptable SIP endpoint — sip-tt's side of every test.

onvif-tt can wrap ``python-onvif-zeep``: for ONVIF, a client library already
exists and the tool's job is to drive it. SIP has no equivalent, and could not
use one if it did. Half the interesting test purposes send something a
well-behaved stack refuses to build — an INVITE with no body, a CSeq that goes
backwards, a re-INVITE moving media mid-call — so the tool has to *be* the
peer, at a level where every field is under the test's control.

The design decision that matters here is that **nothing runs in a background
thread**. Everything happens inside ``pump()``, which a test calls (usually
via ``wait_for``) and which returns when its predicate is satisfied or its
deadline passes. Retransmissions, auto-responses and media reads all happen on
that one call stack. A conformance failure is then reproducible and its
transcript is ordered; a threaded design buys nothing here and makes every
timing assertion a race.

The message log is JSONL, one record per message either way, following
``tests/sip_peer.py`` in majestic — a test asserts on the sequence, and a
human reading a failure gets the transcript for free.
"""

from __future__ import annotations

import json
import os
import selectors
import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import auth as _auth
from . import sdp as _sdp
from .media import MediaEndpoint
from .message import Message, parse, split_params
from .transaction import ClientTransaction

DEFAULT_ALLOW = "INVITE, ACK, BYE, CANCEL, OPTIONS"


def new_tag() -> str:
    return os.urandom(8).hex()


def new_call_id(host: str) -> str:
    return f"{os.urandom(12).hex()}@{host}"


def new_branch() -> str:
    # RFC 3261 §8.1.1.7 — the magic cookie is mandatory and is how a peer
    # knows the branch is globally unique rather than RFC 2543 nonsense.
    return "z9hG4bK" + os.urandom(8).hex()


@dataclass
class Call:
    """One dialog, from whichever side we are on.

    A dialog and a call are the same object here because sip-tt never carries
    a dialog that is not a call — no SUBSCRIBE, no REFER. The route set is
    kept even though a two-party lab call has none, because a test that puts a
    proxy in the path needs it and finding out later is expensive.
    """

    endpoint: "Endpoint"
    call_id: str
    local_uri: str
    remote_uri: str
    local_tag: str
    remote_tag: str = ""
    local_cseq: int = 0
    remote_cseq: int = 0
    remote_target: str = ""
    route_set: list[str] = field(default_factory=list)
    dest: tuple[str, int] = ("", 0)
    uas: bool = False               # did the far side start this call?

    invite: Message | None = None   # the INVITE that created it
    answer: Message | None = None   # the 2xx that confirmed it
    last_response: Message | None = None
    confirmed: bool = False
    terminated: bool = False

    audio: MediaEndpoint | None = None
    video: MediaEndpoint | None = None
    local_sdp: str = ""
    remote_sdp: _sdp.Sdp | None = None

    challenge: _auth.Challenge | None = None
    auth_attempts: int = 0

    # -- outgoing requests --------------------------------------------------

    def request(self, method: str, *, body: str = "",
                content_type: str = "application/sdp",
                extra: Iterable[tuple[str, str]] = (),
                cseq: int | None = None, branch: str | None = None,
                uri: str | None = None) -> Message:
        """Build an in-dialog request. Does not send it."""
        if cseq is None:
            self.local_cseq += 1
            cseq = self.local_cseq
        m = Message(method=method, uri=uri or self.remote_target or self.remote_uri)
        h = m.headers
        h.add("Via", f"SIP/2.0/UDP {self.endpoint.contact_host};"
                     f"branch={branch or new_branch()};rport")
        h.add("Max-Forwards", "70")
        for r in self.route_set:
            h.add("Route", r)
        frm, to = (self.local_uri, self.remote_uri)
        ftag, ttag = self.local_tag, self.remote_tag
        h.add("From", f"<{frm}>;tag={ftag}")
        h.add("To", f"<{to}>" + (f";tag={ttag}" if ttag else ""))
        h.add("Call-ID", self.call_id)
        h.add("CSeq", f"{cseq} {method}")
        h.add("Contact", f"<sip:{self.endpoint.username}@{self.endpoint.contact_host}>")
        h.add("User-Agent", self.endpoint.user_agent)
        if method in ("INVITE", "OPTIONS"):
            h.add("Allow", DEFAULT_ALLOW)
        for k, v in extra:
            h.add(k, v)
        if body:
            h.add("Content-Type", content_type)
        m.body = body
        return m

    def send(self, m: Message, *, track: bool = True) -> ClientTransaction | None:
        return self.endpoint.send(m, self.dest, track=track)

    def ack(self, *, for_response: Message | None = None) -> Message:
        """ACK a 2xx. Its own transaction, but the INVITE's CSeq number.

        A 2xx is ACKed end-to-end and separately from the INVITE transaction
        (§17.1.1.3); a non-2xx is ACKed hop-by-hop with the *same* branch, so
        those go through `ack_failure`.
        """
        resp = for_response or self.answer
        cseq = resp.cseq_number if resp else self.local_cseq
        m = self.request("ACK", cseq=cseq)
        self.send(m, track=False)
        return m

    def ack_failure(self, resp: Message) -> Message:
        """ACK a non-2xx final response: same branch as the INVITE it ends."""
        m = self.request("ACK", cseq=resp.cseq_number,
                         branch=self.invite.branch if self.invite else None)
        # The To tag of a failure response belongs on the ACK, and it is often
        # the only place a tagless-error bug shows up.
        m.headers.set("To", resp.headers.get("To"))
        self.send(m, track=False)
        return m

    def bye(self, timeout: float = 5.0) -> Message | None:
        m = self.request("BYE")
        self.send(m)
        r = self.endpoint.wait_for(
            lambda x: x.is_response and x.cseq_method == "BYE"
            and x.call_id == self.call_id and x.status >= 200,
            timeout=timeout)
        self.terminated = True
        return r

    def cancel(self, timeout: float = 5.0) -> Message | None:
        """CANCEL the INVITE. Same branch and CSeq — it names a transaction."""
        if self.invite is None:
            raise RuntimeError("no INVITE to cancel")
        m = self.request("CANCEL", cseq=self.invite.cseq_number,
                         branch=self.invite.branch, uri=self.invite.uri)
        m.headers.set("To", self.invite.headers.get("To"))
        self.send(m)
        return self.endpoint.wait_for(
            lambda x: x.is_response and x.cseq_method == "CANCEL"
            and x.call_id == self.call_id, timeout=timeout)

    def reinvite(self, body: str, timeout: float = 5.0,
                 extra: Iterable[tuple[str, str]] = ()) -> Message | None:
        """Re-INVITE inside the dialog, and wait for its final response.

        The CSeq increments; that is what separates a re-INVITE from a
        retransmission, and answering one from the cached response to the
        first INVITE is majestic#566.
        """
        m = self.request("INVITE", body=body, extra=extra)
        self.invite = m
        self.send(m)
        r = self.endpoint.wait_for(
            lambda x: x.is_response and x.cseq_method == "INVITE"
            and x.call_id == self.call_id and x.cseq_number == m.cseq_number
            and x.status >= 200, timeout=timeout)
        if r is not None:
            self.last_response = r
            if 200 <= r.status < 300:
                self.answer = r
                if r.body:
                    self.remote_sdp = _sdp.parse(r.body)
                self.ack(for_response=r)
        return r

    # -- media --------------------------------------------------------------

    def open_media(self, *, audio: bool = True, video: bool = True,
                   audio_port: int = 0, video_port: int = 0) -> None:
        if audio and self.audio is None:
            self.audio = MediaEndpoint("audio", self.endpoint.bind_ip, audio_port)
            self.endpoint.watch_media(self.audio)
        if video and self.video is None:
            self.video = MediaEndpoint("video", self.endpoint.bind_ip, video_port)
            self.endpoint.watch_media(self.video)

    def close_media(self) -> None:
        for m in (self.audio, self.video):
            if m is not None:
                self.endpoint.unwatch_media(m)
                m.close()
        self.audio = self.video = None


class Endpoint:
    """A SIP user agent under a test's control: UAC, UAS, and observer."""

    def __init__(self, bind_ip: str = "0.0.0.0", port: int = 0, *,
                 advertise_ip: str | None = None,
                 username: str = "sip-tt", domain: str = "",
                 password: str = "", user_agent: str = "sip-tt/0.1",
                 events_path: str | None = None) -> None:
        self.bind_ip = bind_ip
        self.username = username
        self.password = password
        self.domain = domain
        self.user_agent = user_agent

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((bind_ip, port))
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]

        # What we put in Via, Contact and SDP c=. Must be an address the peer
        # can route back to, which on a multi-homed host is not what bind says
        # — that is lab trap #1 and it imitates the bug being hunted.
        self.advertise_ip = advertise_ip or (
            bind_ip if bind_ip not in ("0.0.0.0", "") else _guess_local_ip())

        self.selector = selectors.DefaultSelector()
        self.selector.register(self.sock, selectors.EVENT_READ, ("sip", None))

        self.calls: dict[str, Call] = {}
        self.transactions: dict[str, ClientTransaction] = {}
        self.received: list[Message] = []
        self.events: list[dict] = []
        self._events_file = open(events_path, "w", buffering=1) if events_path else None

        # Predicates that answer inbound requests automatically while we wait
        # for something else. Without these, sampling media for ten seconds
        # means ignoring the BYE that arrives during it.
        self.auto_responders: list[Callable[[Message], bool]] = []
        # Responders that outlive a single purpose — the registrar, mainly.
        # teardown_calls() resets to this list rather than emptying, because a
        # session-scoped registrar that gets detached after the first test
        # makes every later registrant purpose time out waiting for a REGISTER
        # that was answered by nobody.
        self._responder_baseline: list[Callable[[Message], bool]] = []
        self._media: list[MediaEndpoint] = []

    # -- plumbing -----------------------------------------------------------

    @property
    def contact_host(self) -> str:
        return f"{self.advertise_ip}:{self.port}"

    @property
    def uri(self) -> str:
        return f"sip:{self.username}@{self.domain or self.advertise_ip}"

    def teardown_calls(self) -> None:
        """Hang up anything still standing, and forget every dialog.

        Called after each test, because a device that allows one call at a
        time answers 486 to the next one — so a single test that fails before
        its BYE turns every later test red for a reason that has nothing to do
        with the purpose it was checking. Leaving that to each test's own
        try/finally is exactly the discipline that fails under an assertion.
        """
        for call in list(self.calls.values()):
            if call.confirmed and not call.terminated:
                try:
                    call.bye(timeout=2.0)
                except Exception:
                    pass
            try:
                call.close_media()
            except Exception:
                pass
        self.calls.clear()
        self.transactions.clear()
        self.received.clear()
        self.auto_responders[:] = list(self._responder_baseline)

    def close(self) -> None:
        for m in list(self._media):
            self.unwatch_media(m)
            m.close()
        try:
            self.selector.unregister(self.sock)
        except (KeyError, ValueError):
            pass
        self.sock.close()
        if self._events_file:
            self._events_file.close()

    def keep_responders(self) -> None:
        """Treat the responders attached so far as session-scoped."""
        self._responder_baseline = list(self.auto_responders)

    def watch_media(self, m: MediaEndpoint) -> None:
        rtp, rtcp = m.fileno_pair()
        self.selector.register(rtp, selectors.EVENT_READ, ("rtp", m))
        self.selector.register(rtcp, selectors.EVENT_READ, ("rtcp", m))
        self._media.append(m)

    def unwatch_media(self, m: MediaEndpoint) -> None:
        for s in m.fileno_pair():
            try:
                self.selector.unregister(s)
            except (KeyError, ValueError):
                pass
        if m in self._media:
            self._media.remove(m)

    def log(self, direction: str, msg: Message, note: str = "") -> None:
        rec = {"t": round(time.time(), 3), "dir": direction,
               "msg": msg.start_line(), "callid": msg.call_id,
               "cseq": msg.headers.get("cseq"), "branch": msg.branch}
        if note:
            rec["note"] = note
        self.events.append(rec)
        if self._events_file:
            self._events_file.write(json.dumps(rec) + "\n")

    def send(self, m: Message, dest: tuple[str, int], *,
             track: bool = True) -> ClientTransaction | None:
        wire = m.render()
        self.sock.sendto(wire.encode(), dest)
        self.log("tx", m)
        if track and m.is_request and m.method != "ACK":
            tx = ClientTransaction(method=m.method, branch=m.branch,
                                   request=wire, dest=dest)
            self.transactions[m.branch] = tx
            return tx
        return None

    def send_raw(self, data: str | bytes, dest: tuple[str, int]) -> None:
        """Send bytes verbatim. For test purposes about malformed input."""
        payload = data.encode() if isinstance(data, str) else data
        self.sock.sendto(payload, dest)
        self.events.append({"t": round(time.time(), 3), "dir": "tx",
                            "msg": "<raw>", "bytes": len(payload)})

    # -- the one event loop -------------------------------------------------

    def pump(self, timeout: float,
             until: Callable[[Message], bool] | None = None) -> Message | None:
        """Run the endpoint for `timeout` seconds, or until `until` matches.

        Returns the matching message, or None on expiry. Everything the
        endpoint does — retransmit, auto-respond, count RTP — happens here.
        """
        deadline = time.monotonic() + timeout
        while True:
            now = time.monotonic()
            if now >= deadline:
                return None

            self._service_transactions(now)

            for key, _ in self.selector.select(timeout=min(0.05, deadline - now)):
                what, obj = key.data
                try:
                    data, addr = key.fileobj.recvfrom(65535)
                except (BlockingIOError, ConnectionRefusedError):
                    continue
                if what == "rtp":
                    obj.on_rtp(data, addr)
                    continue
                if what == "rtcp":
                    obj.on_rtcp(data, addr)
                    continue

                msg = self._on_sip(data, addr)
                if msg is None:
                    continue
                if until is not None and until(msg):
                    return msg

    def _service_transactions(self, now: float) -> None:
        for branch, tx in list(self.transactions.items()):
            if tx.done:
                continue
            if tx.timed_out(now):
                tx.done = True
                self.events.append({"t": round(time.time(), 3), "dir": "--",
                                    "msg": f"{tx.method} timed out after "
                                           f"{tx.elapsed:.1f}s "
                                           f"({tx.attempts} attempts)"})
                continue
            if tx.due(now):
                self.sock.sendto(tx.request.encode(), tx.dest)
                tx.on_retransmit(now)
                self.events.append({"t": round(time.time(), 3), "dir": "tx",
                                    "msg": f"{tx.method} retransmit "
                                           f"#{tx.attempts}"})

    def _on_sip(self, data: bytes, addr: tuple[str, int]) -> Message | None:
        try:
            msg = parse(data)
        except Exception:
            self.events.append({"t": round(time.time(), 3), "dir": "rx",
                                "msg": "<unparseable>", "bytes": len(data)})
            return None
        msg.source = addr
        self.received.append(msg)
        self.log("rx", msg)

        if msg.is_response:
            tx = self.transactions.get(msg.branch)
            if tx is not None:
                tx.on_response(msg.status)
            call = self.calls.get(msg.call_id)
            if call is not None:
                call.last_response = msg
                if not call.remote_tag:
                    call.remote_tag = msg.tag("to")
                if 200 <= msg.status < 300 and msg.cseq_method == "INVITE":
                    call.answer = msg
                    call.confirmed = True
                    contact = msg.headers.get("contact")
                    if contact:
                        call.remote_target = _uri_of(contact)
                    if msg.body:
                        call.remote_sdp = _sdp.parse(msg.body)
            return msg

        # A request. Give the auto-responders first refusal.
        for responder in self.auto_responders:
            if responder(msg):
                break
        return msg

    def wait_for(self, predicate: Callable[[Message], bool],
                 timeout: float = 5.0) -> Message | None:
        """Wait for a message matching `predicate`, checking the backlog first."""
        for m in self.received:
            if predicate(m):
                return m
        return self.pump(timeout, until=predicate)

    def wait_for_request(self, method: str, timeout: float = 30.0) -> Message | None:
        return self.wait_for(
            lambda m: m.is_request and m.method == method.upper(), timeout)

    def drain(self, seconds: float) -> None:
        """Run the loop for a fixed time, keeping media counters fed."""
        self.pump(seconds)

    # -- responding ---------------------------------------------------------

    def respond(self, req: Message, status: int, reason: str = "", *,
                body: str = "", content_type: str = "application/sdp",
                to_tag: str | None = None,
                extra: Iterable[tuple[str, str]] = (),
                contact: bool = False) -> Message:
        """Answer a request, echoing the whole Via stack in order.

        Echoing *every* Via and not just the top one is the difference between
        a response that works in a one-hop lab and one that survives a proxy.
        """
        m = Message(status=status, reason=reason or _REASONS.get(status, "OK"))
        h = m.headers
        for via in req.headers.all("via"):
            h.add("Via", via)
        for rr in req.headers.all("record-route"):
            h.add("Record-Route", rr)
        h.add("From", req.headers.get("From"))
        to = req.headers.get("To")
        if to_tag is None and status > 100 and "tag=" not in _params_of(to):
            to_tag = new_tag()
        if to_tag and "tag=" not in _params_of(to):
            to = f"{to};tag={to_tag}"
        h.add("To", to)
        h.add("Call-ID", req.call_id)
        h.add("CSeq", req.headers.get("CSeq"))
        if contact or body:
            h.add("Contact", f"<sip:{self.username}@{self.contact_host}>")
        h.add("User-Agent", self.user_agent)
        for k, v in extra:
            h.add(k, v)
        if body:
            h.add("Content-Type", content_type)
        m.body = body
        self.send(m, req.source or ("", 0), track=False)
        return m

    def auto_answer_reinvites(self, sdp_for: Callable[[Message], str]) -> None:
        """Answer in-dialog re-INVITEs with a fresh SDP while we do other work.

        This is what a PBX doing direct media expects, and refusing to play
        along means never observing the source switch that follows.
        """
        def responder(req: Message) -> bool:
            if req.method != "INVITE" or req.call_id not in self.calls:
                return False
            call = self.calls[req.call_id]
            if req.cseq_number <= call.remote_cseq:
                return False
            call.remote_cseq = req.cseq_number
            if req.body:
                call.remote_sdp = _sdp.parse(req.body)
            contact = req.headers.get("contact")
            if contact:
                call.remote_target = _uri_of(contact)
            if req.source:
                call.dest = req.source
            self.respond(req, 200, "OK", body=sdp_for(req),
                         to_tag=call.local_tag)
            return True
        self.auto_responders.append(responder)

    def auto_answer_simple(self, methods: Iterable[str] = ("BYE", "OPTIONS",
                                                           "ACK", "CANCEL")) -> None:
        """200 OK to the housekeeping methods, so a wait is not derailed."""
        wanted = {m.upper() for m in methods}

        def responder(req: Message) -> bool:
            if req.method not in wanted:
                return False
            if req.method == "ACK":
                return True                       # ACK is never answered
            call = self.calls.get(req.call_id)
            self.respond(req, 200, "OK",
                         to_tag=call.local_tag if call else None)
            if req.method == "BYE" and call is not None:
                call.terminated = True
            return True
        self.auto_responders.append(responder)

    # -- placing and receiving calls ----------------------------------------

    def new_call(self, target_uri: str, dest: tuple[str, int], *,
                 from_uri: str = "") -> Call:
        call = Call(endpoint=self, call_id=new_call_id(self.advertise_ip),
                    local_uri=from_uri or self.uri, remote_uri=target_uri,
                    local_tag=new_tag(), dest=dest, remote_target=target_uri)
        self.calls[call.call_id] = call
        return call

    def place_call(self, target_uri: str, dest: tuple[str, int], *,
                   body: str = "", from_uri: str = "",
                   extra: Iterable[tuple[str, str]] = (),
                   timeout: float = 10.0, auth_retry: bool = True) -> Call:
        """INVITE `target_uri`, answering one auth challenge, and wait.

        Returns as soon as a final response arrives; the caller decides
        whether to ACK, and reads `call.last_response` for the verdict.
        """
        call = self.new_call(target_uri, dest, from_uri=from_uri)
        m = call.request("INVITE", body=body, extra=extra, uri=target_uri)
        call.invite = m
        call.local_sdp = body
        self.send(m, dest)

        resp = self.pump(timeout, until=lambda x: (
            x.is_response and x.call_id == call.call_id
            and x.cseq_method == "INVITE" and x.status >= 200))

        if (resp is not None and resp.status in (401, 407) and auth_retry
                and call.auth_attempts == 0):
            ch = _auth.challenge_from(resp)
            if ch is not None:
                call.ack_failure(resp)
                call.auth_attempts += 1
                call.challenge = ch
                # A new branch and CSeq: the challenged transaction is over,
                # and reusing its numbers is how a retry gets mistaken for a
                # retransmission.
                m2 = call.request("INVITE", body=body, extra=list(extra) + [
                    (ch.header_name(), _auth.respond(
                        ch, username=self.username, password=self.password,
                        method="INVITE", uri=target_uri, body=body))],
                    uri=target_uri)
                call.invite = m2
                # The tag survives the retry; it is the same call.
                self.send(m2, dest)
                resp = self.pump(timeout, until=lambda x: (
                    x.is_response and x.call_id == call.call_id
                    and x.cseq_method == "INVITE" and x.status >= 200))

        call.last_response = resp
        return call

    def expect_call(self, timeout: float = 30.0) -> Message | None:
        """Wait for an inbound INVITE, without answering it."""
        return self.wait_for_request("INVITE", timeout)

    def accept_call(self, invite: Message, body: str) -> Call:
        """Answer an inbound INVITE with 200 OK and register the dialog."""
        call = Call(endpoint=self, call_id=invite.call_id,
                    local_uri=_uri_of(invite.headers.get("To")),
                    remote_uri=_uri_of(invite.headers.get("From")),
                    local_tag=new_tag(), remote_tag=invite.tag("from"),
                    dest=invite.source or ("", 0), uas=True, invite=invite,
                    remote_cseq=invite.cseq_number,
                    remote_target=_uri_of(invite.headers.get("Contact"))
                    or _uri_of(invite.headers.get("From")))
        if invite.body:
            call.remote_sdp = _sdp.parse(invite.body)
        self.calls[call.call_id] = call
        # contact=True and not merely "there is a body": RFC 3261 §12.1.1
        # makes Contact mandatory in a 2xx that establishes a dialog, and it
        # is what the caller addresses its ACK and BYE to. respond() adds one
        # for a body as a convenience, which is not the same rule and leaves a
        # bodyless answer forming a dialog nobody can address.
        self.respond(invite, 200, "OK", body=body, to_tag=call.local_tag,
                     contact=True)
        call.confirmed = True
        return call

    # -- registration -------------------------------------------------------

    def register(self, registrar: tuple[str, int], *, aor: str = "",
                 expires: int = 3600, timeout: float = 10.0) -> Message | None:
        """REGISTER, answering one challenge. Returns the final response."""
        aor = aor or self.uri
        domain = aor.split("@")[-1] if "@" in aor else self.advertise_ip
        call_id = new_call_id(self.advertise_ip)
        tag = new_tag()
        cseq = 1

        def build(auth_header: str = "") -> Message:
            m = Message(method="REGISTER", uri=f"sip:{domain}")
            h = m.headers
            h.add("Via", f"SIP/2.0/UDP {self.contact_host};"
                         f"branch={new_branch()};rport")
            h.add("Max-Forwards", "70")
            h.add("From", f"<{aor}>;tag={tag}")
            h.add("To", f"<{aor}>")
            h.add("Call-ID", call_id)
            h.add("CSeq", f"{cseq} REGISTER")
            h.add("Contact", f"<sip:{self.username}@{self.contact_host}>")
            h.add("Expires", str(expires))
            h.add("User-Agent", self.user_agent)
            if auth_header:
                h.add("Authorization", auth_header)
            return m

        m = build()
        self.send(m, registrar)
        resp = self.pump(timeout, until=lambda x: (
            x.is_response and x.call_id == call_id and x.status >= 200))

        if resp is not None and resp.status in (401, 407):
            ch = _auth.challenge_from(resp)
            if ch is not None:
                cseq += 1
                m2 = build(_auth.respond(ch, username=self.username,
                                         password=self.password,
                                         method="REGISTER", uri=f"sip:{domain}"))
                if ch.is_proxy:
                    m2.headers.remove("Authorization")
                    m2.headers.add("Proxy-Authorization",
                                   _auth.respond(ch, username=self.username,
                                                 password=self.password,
                                                 method="REGISTER",
                                                 uri=f"sip:{domain}"))
                self.send(m2, registrar)
                resp = self.pump(timeout, until=lambda x: (
                    x.is_response and x.call_id == call_id and x.status >= 200))
        return resp


_REASONS = {
    100: "Trying", 180: "Ringing", 183: "Session Progress", 200: "OK",
    202: "Accepted", 400: "Bad Request", 401: "Unauthorized",
    403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
    407: "Proxy Authentication Required", 408: "Request Timeout",
    415: "Unsupported Media Type", 420: "Bad Extension",
    481: "Call/Transaction Does Not Exist", 486: "Busy Here",
    487: "Request Terminated", 488: "Not Acceptable Here",
    491: "Request Pending", 500: "Server Internal Error",
    503: "Service Unavailable", 603: "Decline",
}


def _params_of(header_value: str) -> str:
    """The parameter tail of a header, for a safe 'is there a tag' check."""
    _, params = split_params(header_value)
    return ";".join(f"{k}={v}" for k, v in params.items())


def _uri_of(header_value: str) -> str:
    """The bare URI inside a name-addr, or the whole field if it is not one."""
    if not header_value:
        return ""
    start = header_value.find("<")
    if start >= 0:
        end = header_value.find(">", start)
        if end > start:
            return header_value[start + 1:end]
    body, _ = split_params(header_value)
    return body.strip()


def _guess_local_ip() -> str:
    """The address we would use to reach the outside world.

    Not always the right one to advertise — on a host with a public interface
    and a tunnel it is often the public one, which sends the DUT's media
    somewhere it cannot arrive from and imitates the very bug under test.
    Tests that care pass `advertise_ip` explicitly.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
