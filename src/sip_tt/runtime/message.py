"""SIP message parsing and building.

Why this is hand-rolled rather than taken from a library: a conformance tool
has to be able to send messages a library would refuse to build. Half the
interesting test purposes are malformed-input cases — an INVITE with no body,
a CSeq that goes backwards, a Via with no branch — and a stack that helpfully
corrects them tests nothing.

The two things this gets right that every quick SIP script gets wrong:

*Headers repeat.* ``Via``, ``Route``, ``Contact`` and friends may appear more
than once, and the order is load-bearing: a response is routed by echoing the
request's Via stack *in full and in order*. A dict of ``name -> value`` keeps
the first and silently drops the rest, which produces a response that works in
a one-hop lab and vanishes the moment a proxy is involved. ``Headers`` here is
an ordered list of pairs, and ``get`` returns the first while ``all`` returns
every one.

*Parameters are not substrings.* ``"tag=" in to_header`` finds the tag in
``To: "tag=me" <sip:x@y>``, and ``split("tag=")[1]`` then returns rubbish.
Parameters are parsed off the end of the field, respecting quoted strings and
angle brackets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CRLF = "\r\n"

# RFC 3261 §7.3.3 — the compact forms. Mapped to their long names on parse so
# a test never has to ask twice; a peer is free to use either.
_COMPACT = {
    "i": "call-id", "m": "contact", "e": "content-encoding",
    "l": "content-length", "c": "content-type", "f": "from",
    "s": "subject", "k": "supported", "t": "to", "v": "via",
    "x": "session-expires", "r": "refer-to", "b": "referred-by",
    "o": "event", "u": "allow-events", "y": "identity",
}

_STATUS_LINE = re.compile(r"^SIP/(\d+\.\d+)\s+(\d{3})\s*(.*)$")
_REQUEST_LINE = re.compile(r"^([A-Za-z0-9.!%*_+`'~-]+)\s+(\S+)\s+SIP/(\d+\.\d+)$")


class ParseError(ValueError):
    """The bytes on the wire are not a SIP message we can read at all."""


def _canon(name: str) -> str:
    n = name.strip().lower()
    return _COMPACT.get(n, n)


def split_params(value: str) -> tuple[str, dict[str, str]]:
    """Split a header field into its body and its ``;``-separated parameters.

    Semicolons inside a quoted string or inside angle brackets belong to the
    body, not to the parameter list: ``<sip:a@b;user=phone>;tag=1`` has exactly
    one parameter. Getting this wrong is how ``To: "tag=x" <sip:...>`` grows a
    tag that was never there.
    """
    body_end = None
    depth = 0
    quoted = False
    escaped = False
    params: dict[str, str] = {}
    i = 0
    parts: list[str] = []
    start = 0
    for i, ch in enumerate(value):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quoted:
            escaped = True
        elif ch == '"':
            quoted = not quoted
        elif quoted:
            continue
        elif ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif ch == ";" and depth == 0:
            if body_end is None:
                body_end = i
            else:
                parts.append(value[start:i])
            start = i + 1
    if body_end is None:
        return value.strip(), {}
    parts.append(value[start:])
    for p in parts:
        if not p.strip():
            continue
        k, sep, v = p.partition("=")
        params[k.strip().lower()] = v.strip() if sep else ""
    return value[:body_end].strip(), params


class Headers:
    """An ordered multimap of header fields, case-insensitive by name."""

    __slots__ = ("_items",)

    def __init__(self, items: list[tuple[str, str]] | None = None) -> None:
        # (canonical-name, raw-value); insertion order preserved, which is
        # what makes the Via stack echo correctly.
        self._items: list[tuple[str, str]] = list(items or [])

    def add(self, name: str, value: str) -> None:
        self._items.append((_canon(name), str(value)))

    def set(self, name: str, value: str) -> None:
        """Replace every instance of `name` with one carrying `value`."""
        n = _canon(name)
        self._items = [(k, v) for k, v in self._items if k != n]
        self._items.append((n, str(value)))

    def get(self, name: str, default: str = "") -> str:
        n = _canon(name)
        for k, v in self._items:
            if k == n:
                return v
        return default

    def all(self, name: str) -> list[str]:
        n = _canon(name)
        return [v for k, v in self._items if k == n]

    def remove(self, name: str) -> None:
        n = _canon(name)
        self._items = [(k, v) for k, v in self._items if k != n]

    def __contains__(self, name: str) -> bool:
        n = _canon(name)
        return any(k == n for k, _ in self._items)

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def render(self) -> str:
        return "".join(f"{_title(k)}: {v}{CRLF}" for k, v in self._items)


def _title(canon_name: str) -> str:
    """Render a canonical lower-case name the conventional way.

    Purely cosmetic — SIP header names are case-insensitive — but a trace a
    human has to read is part of the product, and ``Call-ID`` and ``CSeq`` are
    not what ``str.title()`` produces.
    """
    special = {"call-id": "Call-ID", "cseq": "CSeq", "www-authenticate":
               "WWW-Authenticate", "mime-version": "MIME-Version"}
    if canon_name in special:
        return special[canon_name]
    return "-".join(p.capitalize() for p in canon_name.split("-"))


@dataclass
class Message:
    """One SIP message, request or response."""

    # Request half
    method: str = ""
    uri: str = ""
    # Response half
    status: int = 0
    reason: str = ""
    version: str = "2.0"

    headers: Headers = field(default_factory=Headers)
    body: str = ""

    # Populated by the endpoint, not by the parser: who this arrived from.
    source: tuple[str, int] | None = None

    @property
    def is_request(self) -> bool:
        return bool(self.method)

    @property
    def is_response(self) -> bool:
        return self.status != 0

    # -- convenience accessors over the headers a test reaches for constantly --

    @property
    def call_id(self) -> str:
        return self.headers.get("call-id")

    @property
    def cseq_number(self) -> int:
        """The numeric half of CSeq, or -1 when it is absent or unreadable.

        -1 rather than an exception: "the peer sent a CSeq we cannot read" is
        a finding a test wants to assert on, not an error that aborts it.
        """
        n, _, _ = self.headers.get("cseq").strip().partition(" ")
        try:
            return int(n)
        except ValueError:
            return -1

    @property
    def cseq_method(self) -> str:
        _, _, m = self.headers.get("cseq").strip().partition(" ")
        return m.strip().upper()

    def tag(self, which: str) -> str:
        """The ``tag`` parameter of the From or To header, or ''."""
        _, params = split_params(self.headers.get(which))
        return params.get("tag", "")

    @property
    def branch(self) -> str:
        """The topmost Via's branch parameter, or ''."""
        via = self.headers.get("via")
        if not via:
            return ""
        _, params = split_params(via)
        return params.get("branch", "")

    def start_line(self) -> str:
        if self.is_request:
            return f"{self.method} {self.uri} SIP/{self.version}"
        return f"SIP/{self.version} {self.status} {self.reason}"

    def render(self) -> str:
        """Serialise, fixing up Content-Length to match the body.

        Deliberately the *only* automatic correction made here. A test that
        wants to send a wrong Content-Length sets it after rendering, or uses
        the raw send path; everything else would rather not have to remember.
        """
        h = Headers(list(self.headers))
        h.set("content-length", str(len(self.body.encode())))
        return f"{self.start_line()}{CRLF}{h.render()}{CRLF}{self.body}"

    def __bytes__(self) -> bytes:
        return self.render().encode()


def parse(data: str | bytes) -> Message:
    """Parse one SIP message. Raises ParseError if the start line is unusable.

    Lenient about everything else on purpose: a malformed header is something
    a test asserts on, so it is preserved rather than rejected. Bare-LF line
    endings are accepted because peers send them and RFC 3261 §7.5 asks
    receivers to cope.
    """
    if isinstance(data, bytes):
        text = data.decode("utf-8", errors="replace")
    else:
        text = data

    head, sep, body = text.partition(CRLF + CRLF)
    if not sep:
        head, sep, body = text.partition("\n\n")
        if not sep:
            head, body = text, ""

    lines = head.replace(CRLF, "\n").split("\n")
    if not lines or not lines[0].strip():
        raise ParseError("empty message")

    msg = Message(body=body)
    start = lines[0].strip()

    m = _STATUS_LINE.match(start)
    if m:
        msg.version, msg.status, msg.reason = m.group(1), int(m.group(2)), m.group(3)
    else:
        m = _REQUEST_LINE.match(start)
        if not m:
            raise ParseError(f"unparseable start line: {start!r}")
        msg.method, msg.uri, msg.version = m.group(1).upper(), m.group(2), m.group(3)

    # Unfold continuation lines (RFC 3261 §7.3.1) before splitting on ':'.
    unfolded: list[str] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        if line[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += " " + line.strip()
        else:
            unfolded.append(line)

    for line in unfolded:
        name, sep2, value = line.partition(":")
        if not sep2:
            continue
        # A comma-separated Via list is the same thing as repeated Via headers
        # (RFC 3261 §7.3.1). Splitting it here means a test can count hops
        # without caring which form the peer chose.
        if _canon(name) in ("via", "route", "record-route"):
            for part in _split_commas(value):
                msg.headers.add(name, part)
        else:
            msg.headers.add(name, value.strip())

    return msg


def _split_commas(value: str) -> list[str]:
    """Split on commas that are not inside quotes or angle brackets."""
    out, depth, quoted, escaped, start = [], 0, False, False, 0
    for i, ch in enumerate(value):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quoted:
            escaped = True
        elif ch == '"':
            quoted = not quoted
        elif quoted:
            continue
        elif ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            out.append(value[start:i].strip())
            start = i + 1
    tail = value[start:].strip()
    if tail:
        out.append(tail)
    return out or [value.strip()]
