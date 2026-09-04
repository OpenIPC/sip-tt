"""RTP and RTCP observation.

This is where three of the six defects that motivated sip-tt were actually
visible, and none of them were visible in the signalling:

* **Nothing arrives.** The answer named a payload number the offer did not
  bind to that codec, so the far end drops every packet. The signalling looks
  clean; ``packets == 0`` under the offered number is the whole finding.
* **No sender reports.** Without RTCP SR a receiver has no mapping from RTP
  timestamps to wall clock, so it cannot synchronise audio with video and
  cannot compute round-trip time. ``SR_COUNT == 0`` after several seconds is
  the finding.
* **The media changes identity mid-call.** A PBX with direct media re-INVITEs
  both parties into talking to each other, so the caller's RTP source address
  and SSRC change. If the new stream's timestamps come from an unrelated base,
  the receiver's jitter estimate explodes — ortp reports the *span* of its
  receive queue, so an unrelated base shows up as millions of milliseconds of
  "jitter buffer" and the call is torn down 4-6 s in. That is majestic#563:
  timestamps started from system uptime rather than a random offset (RFC 3550
  §5.1), so every restart produced a different, and adjacent-call-colliding,
  base.

So this counts per payload type (not in aggregate — the whole point is which
number arrived), tracks source and SSRC per stream, and reports a change as a
timestamp discontinuity in milliseconds, which is the unit the symptom appears
in.
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field

# RTP timestamps advance at the codec's clock rate; a discontinuity only means
# something once converted with the right one.
CLOCK_RATE = {"audio": 8000, "video": 90000}

# Seconds between the NTP epoch (1900) and the Unix epoch (1970).
NTP_EPOCH_OFFSET = 2208988800


@dataclass
class SenderReport:
    """One decoded RTCP sender report (RFC 3550 §6.4.1)."""

    ssrc: int
    ntp_unix: float          # the SR's NTP field, as a Unix timestamp
    rtp_ts: int
    packets: int
    octets: int
    received_at: float
    # The RTP timestamp of the last data packet seen before this report. The
    # gap between the two says whether the SR describes the stream actually
    # being sent, or a clock running independently of it.
    last_data_ts: int | None = None

    @property
    def clock_skew_s(self) -> float:
        """How far the SR's idea of now is from ours. Needs a synced host."""
        return self.ntp_unix - self.received_at


@dataclass
class SourceChange:
    """A mid-call change of RTP source address or SSRC."""

    at: float
    kind: str
    old_source: tuple[str, int] | None
    old_ssrc: int | None
    new_source: tuple[str, int]
    new_ssrc: int
    ts_jump_ticks: int
    clock_rate: int

    @property
    def ts_jump_ms(self) -> float:
        return self.ts_jump_ticks * 1000.0 / self.clock_rate

    def __str__(self) -> str:
        old = f"{self.old_source} ssrc={self.old_ssrc}" if self.old_ssrc else "nothing"
        return (f"{self.kind} source changed from {old} to {self.new_source} "
                f"ssrc={self.new_ssrc}; timestamps jump {self.ts_jump_ticks} "
                f"ticks = {self.ts_jump_ms:.0f} ms")


@dataclass
class StreamStats:
    """What arrived on one media stream."""

    kind: str
    packets_by_pt: dict[int, int] = field(default_factory=dict)
    bytes_by_pt: dict[int, int] = field(default_factory=dict)
    source: tuple[str, int] | None = None
    ssrc: int | None = None
    first_ts: int | None = None
    last_ts: int | None = None
    first_seq: int | None = None
    last_seq: int | None = None
    seq_gaps: int = 0
    first_packet_at: float | None = None
    last_packet_at: float | None = None
    sender_reports: list[SenderReport] = field(default_factory=list)
    rtcp_types: dict[int, int] = field(default_factory=dict)
    source_changes: list[SourceChange] = field(default_factory=list)

    @property
    def packets(self) -> int:
        return sum(self.packets_by_pt.values())

    @property
    def payload_types(self) -> set[int]:
        return set(self.packets_by_pt)

    def packets_for(self, pt: int) -> int:
        return self.packets_by_pt.get(pt, 0)

    def describe(self) -> str:
        if not self.packets:
            return f"{self.kind}: NO RTP RECEIVED"
        by_pt = ", ".join(f"pt={pt} packets={n} bytes={self.bytes_by_pt.get(pt, 0)}"
                          for pt, n in sorted(self.packets_by_pt.items()))
        sr = (f"{len(self.sender_reports)} SR" if self.sender_reports
              else "NO RTCP SENDER REPORTS")
        return (f"{self.kind}: {by_pt} from={self.source} ssrc={self.ssrc} "
                f"first_ts={self.first_ts} {sr}")


class MediaEndpoint:
    """One RTP/RTCP socket pair, observed.

    Ports are allocated as an even/odd pair because RFC 3550 §11 says RTCP
    lives on RTP+1 and peers assume it without being told.
    """

    def __init__(self, kind: str, bind: str = "0.0.0.0", port: int = 0) -> None:
        self.kind = kind
        self.stats = StreamStats(kind=kind)
        self.clock_rate = CLOCK_RATE.get(kind, 90000)
        self.rtp, self.rtcp, self.port = _bind_pair(bind, port)
        self.rtp.setblocking(False)
        self.rtcp.setblocking(False)
        self._peer: tuple[str, int] | None = None

    def fileno_pair(self) -> tuple[socket.socket, socket.socket]:
        return self.rtp, self.rtcp

    def close(self) -> None:
        for s in (self.rtp, self.rtcp):
            try:
                s.close()
            except OSError:
                pass

    def send_to(self, peer: tuple[str, int], payload: bytes, pt: int,
                seq: int, ts: int, ssrc: int) -> None:
        """Send one RTP packet. Used when the DUT expects to receive media."""
        header = struct.pack("!BBHII", 0x80, pt & 0x7F, seq & 0xFFFF,
                             ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
        self.rtp.sendto(header + payload, peer)

    # -- receive paths ------------------------------------------------------

    def on_rtp(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 12:
            return
        b1 = data[1]
        pt = b1 & 0x7F           # the marker bit is the top one, not the type
        seq, ts, ssrc = struct.unpack("!HII", data[2:12])
        st = self.stats
        now = time.time()

        if st.source is not None and (addr != st.source or ssrc != st.ssrc):
            jump = (ts - (st.last_ts or 0)) % (1 << 32)
            st.source_changes.append(SourceChange(
                at=now, kind=self.kind, old_source=st.source, old_ssrc=st.ssrc,
                new_source=addr, new_ssrc=ssrc, ts_jump_ticks=jump,
                clock_rate=self.clock_rate))
            # Restart the sequence tracking: the new stream numbers its own.
            st.first_seq = seq

        if st.first_packet_at is None:
            st.first_packet_at = now
            st.first_ts = ts
            st.first_seq = seq
        elif st.last_seq is not None:
            expected = (st.last_seq + 1) & 0xFFFF
            if seq != expected and not st.source_changes:
                st.seq_gaps += 1

        st.source, st.ssrc, st.last_ts, st.last_seq = addr, ssrc, ts, seq
        st.last_packet_at = now
        st.packets_by_pt[pt] = st.packets_by_pt.get(pt, 0) + 1
        st.bytes_by_pt[pt] = st.bytes_by_pt.get(pt, 0) + len(data)

    def on_rtcp(self, data: bytes, addr: tuple[str, int]) -> None:
        """Decode a compound RTCP packet, recording every sender report."""
        st = self.stats
        offset = 0
        while offset + 4 <= len(data):
            ptype = data[offset + 1]
            length = struct.unpack("!H", data[offset + 2:offset + 4])[0]
            size = (length + 1) * 4
            st.rtcp_types[ptype] = st.rtcp_types.get(ptype, 0) + 1
            if ptype == 200 and offset + 28 <= len(data):   # SR
                chunk = data[offset:offset + 28]
                ssrc, ntp_s, ntp_f, rtp_ts, pkts, octets = struct.unpack(
                    "!IIIIII", chunk[4:28])
                st.sender_reports.append(SenderReport(
                    ssrc=ssrc,
                    ntp_unix=(ntp_s - NTP_EPOCH_OFFSET) + ntp_f / (1 << 32),
                    rtp_ts=rtp_ts, packets=pkts, octets=octets,
                    received_at=time.time(), last_data_ts=st.last_ts))
            if size <= 0:
                break
            offset += size


def _bind_pair(bind: str, port: int) -> tuple[socket.socket, socket.socket, int]:
    """Bind an even RTP port and the odd RTCP port above it.

    Scans upward when asked for a specific port that is taken, because a test
    run that fails on "address in use" tells you nothing about the DUT. With
    port 0 the kernel picks, and we retry until it hands out an even one.
    """
    attempts = 200 if port else 40
    candidate = port
    for _ in range(attempts):
        rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            rtp.bind((bind, candidate))
            got = rtp.getsockname()[1]
            if got % 2:
                raise OSError("odd port")
            rtcp.bind((bind, got + 1))
            return rtp, rtcp, got
        except OSError:
            rtp.close()
            rtcp.close()
            if port:
                candidate += 2
        else:  # pragma: no cover - defensive
            break
    raise RuntimeError(f"could not bind an RTP/RTCP pair near {bind}:{port}")
