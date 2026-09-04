"""SDP offer/answer, from the test tool's side.

The rule this file exists to police is RFC 3264 §6.1: *the offerer chooses the
payload numbers*, and an answer may only name formats the offer named, under
the numbers the offer bound to them. It reads like a formality and is not —
a B2BUA holds each leg to the numbers negotiated on that leg and silently
drops every packet carrying any other one, which reaches the user as a video
stream that connects, reports zero packets received, and stays black. That is
majestic#560, and it survived a peer-to-peer test because liblinphone forgives
a renumbered answer and rebuilds its decoder around whatever arrived.

So the offers built here are *specific*: `linphone_offer()` reproduces the
numbering a real Linphone puts on the wire (H.264 as 97, not 96) precisely
because that asymmetry is what catches the bug. A tool that always offered
codecs under their conventional numbers would agree with a broken answerer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Direction(str, Enum):
    SENDRECV = "sendrecv"
    SENDONLY = "sendonly"
    RECVONLY = "recvonly"
    INACTIVE = "inactive"

    def mirror(self) -> "Direction":
        """What an answer to this direction must say (RFC 3264 §6.1).

        Each side describes what *it* will do, so the two are mirror images:
        an offer of sendonly — a hold — is answered recvonly, and nothing is
        sent towards the holder until it says otherwise.
        """
        return {
            Direction.SENDRECV: Direction.SENDRECV,
            Direction.SENDONLY: Direction.RECVONLY,
            Direction.RECVONLY: Direction.SENDONLY,
            Direction.INACTIVE: Direction.INACTIVE,
        }[self]

    @property
    def sends(self) -> bool:
        return self in (Direction.SENDRECV, Direction.SENDONLY)

    @property
    def receives(self) -> bool:
        return self in (Direction.SENDRECV, Direction.RECVONLY)


# RFC 3551 §6 static assignments a peer may leave unmapped. Asterisk offers
# "0 8 101" with an a=rtpmap for 101 alone and is entitled to.
STATIC_PT = {0: ("PCMU", 8000), 8: ("PCMA", 8000), 9: ("G722", 8000),
             18: ("G729", 8000), 26: ("JPEG", 90000), 34: ("H263", 90000)}


@dataclass
class Format:
    """One entry of an m-line's format list, with whatever rtpmap bound to it."""

    pt: int
    name: str = ""
    clock: int = 0
    fmtp: str = ""

    def __post_init__(self) -> None:
        if not self.name and self.pt in STATIC_PT:
            self.name, self.clock = STATIC_PT[self.pt]


@dataclass
class Media:
    """One m-line."""

    kind: str                       # "audio" | "video" | ...
    port: int
    formats: list[Format] = field(default_factory=list)
    direction: Direction | None = None   # None = inherit the session's
    connection: str = ""                 # media-level c=, if any
    attributes: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        """Whether there is a stream here to talk to.

        Port zero is a refusal, not an omission: RFC 3264 §6 pairs the offer's
        and answer's m-lines *by position*, so a medium that cannot be
        accepted is answered with port 0 rather than dropped. A dropped line
        shifts every line after it onto the wrong offer.
        """
        return self.port != 0

    def pt_for(self, name: str) -> int:
        """The number this list bound to `name`, or -1 if it never offered it.

        Case-insensitive: encoding names are, and peers disagree about "H264"
        versus "h264".
        """
        for f in self.formats:
            if f.name.upper() == name.upper():
                return f.pt
        return -1

    def format(self, pt: int) -> Format | None:
        for f in self.formats:
            if f.pt == pt:
                return f
        return None


@dataclass
class Sdp:
    """A parsed session description."""

    connection: str = ""
    session_name: str = "-"
    origin: str = ""
    direction: Direction = Direction.SENDRECV   # session-level default
    media: list[Media] = field(default_factory=list)
    raw: str = ""

    def get(self, kind: str) -> Media | None:
        """The first m-line of this kind, active or refused."""
        for m in self.media:
            if m.kind == kind:
                return m
        return None

    @property
    def audio(self) -> Media | None:
        return self.get("audio")

    @property
    def video(self) -> Media | None:
        return self.get("video")

    def direction_of(self, m: Media) -> Direction:
        """A medium's effective direction: its own, else the session's."""
        return m.direction if m.direction is not None else self.direction

    def address_for(self, m: Media) -> str:
        """A medium's connection address: media-level c=, else session-level."""
        return m.connection or self.connection


def parse(body: str) -> Sdp:
    """Parse an SDP body. Lenient — unknown lines are kept, not rejected."""
    sdp = Sdp(raw=body)
    current: Media | None = None
    for raw_line in body.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if len(line) < 2 or line[1] != "=":
            continue
        kind, value = line[0], line[2:]
        if kind == "o":
            sdp.origin = value
        elif kind == "s":
            sdp.session_name = value
        elif kind == "c":
            addr = value.split()[-1] if value.split() else ""
            if current is None:
                sdp.connection = addr
            else:
                current.connection = addr
        elif kind == "m":
            current = _parse_m(value)
            if current:
                sdp.media.append(current)
        elif kind == "a":
            _parse_a(value, sdp, current)
    return sdp


def _parse_m(value: str) -> Media | None:
    parts = value.split()
    if len(parts) < 3:
        return None
    try:
        port = int(parts[1].split("/")[0])
    except ValueError:
        return None
    formats = []
    for tok in parts[3:]:
        try:
            pt = int(tok)
        except ValueError:
            continue
        # Payload types are seven bits (RFC 3550 §5.1). A number outside that
        # cannot name a stream, so it is dropped rather than carried around.
        if 0 <= pt <= 127:
            formats.append(Format(pt=pt))
    return Media(kind=parts[0], port=port, formats=formats)


def _parse_a(value: str, sdp: Sdp, current: Media | None) -> None:
    name, _, rest = value.partition(":")
    name = name.strip().lower()

    if name in ("sendrecv", "sendonly", "recvonly", "inactive"):
        d = Direction(name)
        if current is None:
            sdp.direction = d
        else:
            current.direction = d
        return

    if name == "rtpmap" and current is not None:
        num, _, enc = rest.strip().partition(" ")
        try:
            pt = int(num)
        except ValueError:
            return
        bits = enc.split("/")
        f = current.format(pt)
        if f is None:
            return
        f.name = bits[0]
        if len(bits) > 1:
            try:
                f.clock = int(bits[1])
            except ValueError:
                pass
        return

    if name == "fmtp" and current is not None:
        num, _, params = rest.strip().partition(" ")
        try:
            f = current.format(int(num))
        except ValueError:
            return
        if f is not None:
            f.fmtp = params
        return

    if current is not None:
        current.attributes.append(value)


def build(*, address: str, media: list[Media], session_name: str = "sip-tt",
          session_id: int = 0, direction: Direction | None = None) -> str:
    """Render an SDP body.

    `session_id` is passed in rather than taken from the clock so that a test
    can produce byte-identical offers twice — which is exactly what a
    retransmission is, and what distinguishes it from a re-INVITE.
    """
    lines = [
        "v=0",
        f"o=- {session_id} {session_id} IN IP4 {address}",
        f"s={session_name}",
        f"c=IN IP4 {address}",
        "t=0 0",
    ]
    if direction is not None:
        lines.append(f"a={direction.value}")
    for m in media:
        pts = " ".join(str(f.pt) for f in m.formats)
        lines.append(f"m={m.kind} {m.port} RTP/AVP {pts}")
        if m.connection:
            lines.append(f"c=IN IP4 {m.connection}")
        for f in m.formats:
            # A static number needs no rtpmap, but sending one is legal and
            # makes a trace readable. Dynamic numbers must have one.
            if f.name:
                clock = f.clock or (90000 if m.kind == "video" else 8000)
                lines.append(f"a=rtpmap:{f.pt} {f.name}/{clock}")
            if f.fmtp:
                lines.append(f"a=fmtp:{f.pt} {f.fmtp}")
        for attr in m.attributes:
            lines.append(f"a={attr}")
        if m.direction is not None:
            lines.append(f"a={m.direction.value}")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# Canned offers
#
# These are not illustrative: each reproduces a real peer's numbering, and the
# numbering is the test vector. See the module docstring.
# ---------------------------------------------------------------------------

def linphone_offer(address: str, audio_port: int, video_port: int) -> str:
    """What linphonec 5.x puts on the wire, trimmed to fit a 1420-byte path.

    The load-bearing detail is that H.264 is 97 and *not* 96. An answerer that
    replies "96 H264" has renumbered the format, which a PBX will not forgive
    even though liblinphone does.
    """
    audio = Media("audio", audio_port, [
        Format(96, "opus", 48000, "useinbandfec=1"),
        Format(0), Format(8),
        Format(101, "telephone-event", 8000),
    ], Direction.SENDRECV)
    video = Media("video", video_port, [
        Format(96, "VP8", 90000),
        Format(97, "H264", 90000, "profile-level-id=42801F;packetization-mode=1"),
        Format(98, "H265", 90000),
    ], Direction.SENDRECV)
    return build(address=address, media=[audio, video])


def g711_offer(address: str, audio_port: int, video_port: int = 0,
               video_codec: str = "H264", video_pt: int = 99) -> str:
    """A small offer that fits any path: G.711 plus one video codec.

    `video_pt` defaults to 99 because that is what Asterisk offers H.264 as,
    and answering it under 96 is majestic#560.
    """
    media = [Media("audio", audio_port,
                   [Format(0), Format(8), Format(101, "telephone-event", 8000)],
                   Direction.SENDRECV)]
    if video_port:
        media.append(Media("video", video_port,
                           [Format(video_pt, video_codec, 90000)],
                           Direction.SENDRECV))
    return build(address=address, media=media)


def hold_offer(address: str, audio_port: int, video_port: int = 0) -> str:
    """The same session, put on hold: every medium sendonly (RFC 3264 §8.4)."""
    media = [Media("audio", audio_port, [Format(0), Format(8)],
                   Direction.SENDONLY)]
    if video_port:
        media.append(Media("video", video_port, [Format(99, "H264", 90000)],
                           Direction.SENDONLY))
    return build(address=address, media=media)
