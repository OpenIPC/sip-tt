"""RTP/RTCP observation, driven by synthetic packets."""

import struct
import time

from sip_tt.runtime.media import MediaEndpoint


def rtp(pt, seq, ts, ssrc, payload=b"x" * 160):
    return struct.pack("!BBHII", 0x80, pt, seq, ts, ssrc) + payload


def test_counts_are_per_payload_type():
    """Aggregate counts hide the defect: which number arrived is the point."""
    e = MediaEndpoint("audio")
    try:
        e.on_rtp(rtp(0, 1, 1000, 7), ("10.0.0.1", 5004))
        e.on_rtp(rtp(8, 2, 1160, 7), ("10.0.0.1", 5004))
        assert e.stats.packets_for(0) == 1
        assert e.stats.packets_for(8) == 1
        assert e.stats.payload_types == {0, 8}
    finally:
        e.close()


def test_the_marker_bit_is_not_part_of_the_payload_type():
    e = MediaEndpoint("video")
    try:
        marked = bytearray(rtp(96, 1, 1000, 7))
        marked[1] |= 0x80
        e.on_rtp(bytes(marked), ("10.0.0.1", 5004))
        assert e.stats.payload_types == {96}
    finally:
        e.close()


def test_a_source_change_is_reported_with_its_timestamp_jump():
    """The direct-media handoff, rendered in the unit the symptom appears in."""
    e = MediaEndpoint("audio")
    try:
        e.on_rtp(rtp(0, 1, 1000, 111), ("10.0.0.9", 5004))
        e.on_rtp(rtp(0, 2, 1160, 111), ("10.0.0.9", 5004))
        e.on_rtp(rtp(0, 3, 999000, 222), ("10.0.0.7", 5004))
        assert len(e.stats.source_changes) == 1
        ch = e.stats.source_changes[0]
        assert ch.old_ssrc == 111 and ch.new_ssrc == 222
        assert ch.ts_jump_ticks == 999000 - 1160
        assert abs(ch.ts_jump_ms - (999000 - 1160) / 8.0) < 1
    finally:
        e.close()


def test_sender_reports_are_decoded():
    e = MediaEndpoint("audio")
    try:
        e.on_rtp(rtp(0, 1, 1160, 111), ("10.0.0.9", 5004))
        now = int(time.time()) + 2208988800
        sr = struct.pack("!BBH", 0x80, 200, 6) + struct.pack(
            "!IIIIII", 111, now, 0, 1160, 2, 320)
        e.on_rtcp(sr, ("10.0.0.9", 5005))
        assert len(e.stats.sender_reports) == 1
        r = e.stats.sender_reports[0]
        assert (r.ssrc, r.rtp_ts, r.packets, r.octets) == (111, 1160, 2, 320)
        assert r.last_data_ts == 1160
        assert abs(r.clock_skew_s) < 5
    finally:
        e.close()


def test_a_compound_packet_is_walked_to_the_end():
    """SR + SDES arrives as one datagram; counting only the first misses SDES."""
    e = MediaEndpoint("audio")
    try:
        sr = struct.pack("!BBH", 0x80, 200, 6) + struct.pack(
            "!IIIIII", 5, 0, 0, 0, 0, 0)
        sdes = struct.pack("!BBH", 0x81, 202, 1) + b"\x00\x00\x00\x05"
        e.on_rtcp(sr + sdes, ("10.0.0.9", 5005))
        assert e.stats.rtcp_types == {200: 1, 202: 1}
    finally:
        e.close()


def test_rtp_and_rtcp_ports_are_an_even_odd_pair():
    """RFC 3550 §11 — peers assume RTCP is RTP+1 without being told."""
    e = MediaEndpoint("audio")
    try:
        assert e.port % 2 == 0
        assert e.rtcp.getsockname()[1] == e.port + 1
    finally:
        e.close()
