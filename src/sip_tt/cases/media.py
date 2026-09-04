"""What the media has to do, once the signalling has agreed on it.

Three of the six defects that motivated sip-tt were invisible in the
signalling. The SDP was well-formed, the dialog was correct, and the call was
still broken — because nothing arrived, or nothing said when it was sent, or
what arrived carried a clock unrelated to the one before it.

None of these have a counterpart in ETSI TS 102 027-2, which is a conformance
suite for RFC 3261 and stops at the session description. They are `LOCAL-*`.
"""

from __future__ import annotations

import time

import pytest

from ..registry import register
from ..runtime import sdp as _sdp
from ._common import establish, require_target


def _sampled_call(endpoint, profile, seconds: float):
    """Establish a call with media open and let it run."""
    call = establish(endpoint, profile, media=True)
    endpoint.auto_answer_simple()
    endpoint.drain(seconds)
    return call


@register("LOCAL-MEDIA-ARRIVES-UNDER-THE-NEGOTIATED-NUMBER",
          roles={"terminating"}, mandatory=True, requires={"media"})
def media_arrives_under_the_negotiated_number(endpoint, profile):
    """Packets must carry the payload number the answer named.

    The companion to the SDP check: an answer can name the right number and
    the sender can still mark its packets with another. Either way the
    receiver drops them, and the call is a connected stream of nothing.
    """
    call = _sampled_call(endpoint, profile, 4.0)
    try:
        answer = call.remote_sdp
        assert answer is not None, "no session description in the answer"
        problems = []
        for kind, ep in (("audio", call.audio), ("video", call.video)):
            m = answer.get(kind)
            if m is None or not m.active or ep is None:
                continue
            if not answer.direction_of(m).sends:
                continue          # the device said it would not send this
            expected = {f.pt for f in m.formats}
            got = ep.stats.payload_types
            if not got:
                problems.append(
                    f"{kind}: the device answered it would send "
                    f"{sorted(expected)} on port {ep.port}, and 4s later "
                    f"nothing has arrived")
            elif not (got & expected):
                problems.append(
                    f"{kind}: packets arrived marked {sorted(got)}, but the "
                    f"answer named {sorted(expected)}. A peer holding the "
                    f"session to the negotiated numbers discards all of these")
        assert not problems, "; ".join(problems)
    finally:
        call.bye()


@register("LOCAL-MEDIA-RTCP-SENDER-REPORTS", roles={"terminating"},
          mandatory=True, requires={"media"})
def rtcp_sender_reports_are_sent(endpoint, profile):
    """A sender must send RTCP sender reports.

    RFC 3550 §6.4.1. The SR is what maps a stream's RTP timestamps onto a wall
    clock, so without it a receiver cannot synchronise audio against video and
    cannot measure round-trip time. The symptom users report is lip-sync drift
    that never settles, which sounds like a codec problem and is not.

    Five seconds is generous: the RFC's bandwidth rules put an SR every few
    seconds on a call this size, and a device in a VoIP mode usually sends one
    per second.
    """
    call = _sampled_call(endpoint, profile, 5.5)
    try:
        answer = call.remote_sdp
        assert answer is not None, "no session description in the answer"
        problems = []
        for kind, ep in (("audio", call.audio), ("video", call.video)):
            m = answer.get(kind)
            if m is None or not m.active or ep is None:
                continue
            if not answer.direction_of(m).sends:
                continue
            if not ep.stats.packets:
                continue          # covered by the payload-number test
            if not ep.stats.sender_reports:
                problems.append(
                    f"{kind}: {ep.stats.packets} RTP packets arrived in 5.5s "
                    f"and not one RTCP sender report on port {ep.port + 1}. "
                    f"Without an SR the receiver has no RTP-to-wall-clock "
                    f"mapping, so it cannot lip-sync this stream against any "
                    f"other")
        assert not problems, "; ".join(problems)
    finally:
        call.bye()


@register("LOCAL-MEDIA-RTP-TIMESTAMPS-SURVIVE-A-REINVITE",
          roles={"terminating"}, mandatory=True, requires={"media"})
def timestamps_survive_a_reinvite(endpoint, profile):
    """Moving media with a re-INVITE must not restart the media clock.

    This is what a PBX does a few seconds into every call when it takes itself
    out of the media path. RFC 3550 §5.1 makes the timestamp a property of the
    *source*, not of the transport: the same synchronisation source keeps
    counting across a change of address.

    When it does not, the receiver sees one stream whose timestamps jump by an
    arbitrary amount. ortp — liblinphone's RTP stack — reports its jitter as
    the timestamp *span* of the receive queue, so an unrelated base shows up
    as millions of milliseconds of "jitter" and the call is dropped four to
    six seconds in. That is the bug this test is named for.
    """
    call = _sampled_call(endpoint, profile, 2.0)
    try:
        if call.audio is None or not call.audio.stats.packets:
            pytest.skip("no audio arrived, so there is no clock to check")

        before = call.audio.stats.last_ts
        ip = profile.local_ip or endpoint.advertise_ip

        # Move the media to a fresh pair, the way a direct-media handoff does.
        moved = type(call.audio)("audio", endpoint.bind_ip, 0)
        endpoint.watch_media(moved)
        resp = call.reinvite(_sdp.g711_offer(ip, moved.port), timeout=10.0)
        assert resp is not None and 200 <= resp.status < 300, (
            f"the device would not accept a re-INVITE moving the media "
            f"({resp.status if resp else 'no response'}), so the timestamp "
            f"behaviour across a move could not be observed")

        endpoint.drain(3.0)
        if not moved.stats.packets:
            pytest.fail(
                f"the device accepted a re-INVITE moving audio to port "
                f"{moved.port} and sent nothing there in 3s")

        after = moved.stats.first_ts
        rate = moved.clock_rate
        # The gap the move itself accounts for, generously: the re-INVITE
        # round trip plus the drain.
        step = (after - before) % (1 << 32)
        step_ms = step * 1000.0 / rate
        assert step_ms < 15000, (
            f"audio timestamps jumped {step} ticks ({step_ms:.0f} ms) across "
            f"the re-INVITE, from {before} to {after}. The stream restarted "
            f"its clock instead of carrying it across the move; a receiver "
            f"measuring jitter from the timestamp span sees this as "
            f"{step_ms:.0f} ms of buffer and gives up on the call")
        endpoint.unwatch_media(moved)
        moved.close()
    finally:
        call.bye()


@register("LOCAL-MEDIA-RTP-TIMESTAMP-BASE-IS-RANDOM",
          roles={"terminating"}, mandatory=True, requires={"media"},
          tags={"slow"})
def rtp_timestamp_base_is_random(endpoint, profile):
    """Each session's RTP timestamps must start from a random offset.

    RFC 3550 §5.1: "the initial value of the timestamp SHOULD be random". A
    device that starts from its uptime instead produces two consequences a
    user meets. Consecutive calls have adjacent, predictable bases, so a peer
    that mistakes the second stream for a continuation of the first computes
    nonsense; and the base is a disclosure of how long the device has been up.

    The test is two calls in a row. If the second stream's initial timestamp
    is the first's plus roughly the wall-clock gap converted at the media
    rate, the "random" base is a running clock.
    """
    require_target(profile)
    bases = []
    marks = []
    for _ in range(2):
        call = _sampled_call(endpoint, profile, 2.0)
        try:
            if call.audio is None or call.audio.stats.first_ts is None:
                pytest.skip("no audio arrived, so there is no base to compare")
            bases.append(call.audio.stats.first_ts)
            marks.append(time.monotonic())
        finally:
            call.bye()
            call.close_media()
        time.sleep(1.0)

    gap_s = marks[1] - marks[0]
    rate = 8000
    predicted = (bases[0] + int(gap_s * rate)) % (1 << 32)
    drift = min((bases[1] - predicted) % (1 << 32),
                (predicted - bases[1]) % (1 << 32))
    # One second of slack at the media rate. A genuinely random base lands
    # this close by chance about once in 250 000 runs.
    assert drift > rate, (
        f"the second call's first RTP timestamp was {bases[1]}, and a clock "
        f"running continuously from the first call's base {bases[0]} would "
        f"have reached {predicted} after {gap_s:.1f}s — a difference of only "
        f"{drift} ticks. The media clock is not being randomised per session "
        f"(RFC 3550 §5.1); it is a free-running counter, most often uptime")
