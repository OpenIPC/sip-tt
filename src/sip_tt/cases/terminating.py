"""Terminating endpoint: what a device must do when someone calls it.

This family is where every defect that motivated sip-tt lived. That is not a
coincidence — a camera or a doorbell is the *answerer* far more often than the
caller, and the answerer is the side with fewer degrees of freedom: RFC 3264
§6.1 lets the offerer choose the payload numbers, the directions and the
session's shape, and the answer has to follow.
"""

from __future__ import annotations

import time

import pytest

from ..registry import register
from ..runtime import sdp as _sdp
from ._common import establish, require_target, sdp_of


@register("SIP_CC_TE_CE_V_001", roles={"terminating"}, mandatory=True)
def invite_gets_a_response(endpoint, profile):
    """On receipt of an INVITE, answer 2xx or 1xx.

    The most basic thing a terminating endpoint does, and worth having first:
    when it fails, every other terminating purpose fails too and the report
    should make the common cause obvious.
    """
    require_target(profile)
    ip = profile.local_ip or endpoint.advertise_ip
    call = endpoint.place_call(profile.uri, profile.target,
                               body=_sdp.g711_offer(ip, 40000), timeout=10.0)
    got = [m.status for m in endpoint.received
           if m.is_response and m.call_id == call.call_id]
    assert got, (f"no response of any kind to an INVITE within 10s from "
                 f"{profile.host}:{profile.port}")
    assert any(100 <= s < 300 for s in got), (
        f"the device answered {got} — neither a provisional nor a success. "
        f"RFC 3261 §8.2.6 wants a 1xx or a 2xx to a well-formed INVITE it can "
        f"serve")
    if call.last_response is not None and 200 <= call.last_response.status < 300:
        call.ack()
        call.bye()


@register("SIP_CC_TE_CE_V_006", roles={"terminating"}, mandatory=True)
def invite_without_body_gets_an_offer(endpoint, profile):
    """An INVITE with no body must be answered with an offer in the 2xx.

    RFC 3261 §13.2.1: a UAC may leave the offer out of the INVITE, in which
    case the *answerer* makes the offer and the caller answers it in the ACK.
    A device that requires an offer to arrive first cannot be called by
    anything that defers its own — several PBXs do this when bridging, and a
    488 here means the call simply never connects.
    """
    require_target(profile)
    call = endpoint.place_call(profile.uri, profile.target, body="",
                               timeout=10.0)
    resp = call.last_response
    assert resp is not None, "no final response to a bodyless INVITE within 10s"
    assert 200 <= resp.status < 300, (
        f"a bodyless INVITE was refused {resp.status} {resp.reason}. "
        f"RFC 3261 §13.2.1 allows an INVITE with no offer, and requires the "
        f"answerer to make one in its 2xx")
    assert resp.body.strip(), (
        f"the {resp.status} carried no session description. With no offer in "
        f"the INVITE there is nothing for the caller to answer in its ACK, so "
        f"the session has no media")
    offer = sdp_of(resp)
    assert offer.media, "the 2xx body contained no m= line"
    call.ack()
    call.bye()


@register("SIP_CC_TE_SM_V_001", roles={"terminating"}, mandatory=True)
def reinvite_is_answered_on_its_own_cseq(endpoint, profile):
    """A re-INVITE must be answered, with the CSeq it carried.

    This is the shape of a real, shipped defect. The device cached the 200 OK
    it sent for the first INVITE and replayed it verbatim when a re-INVITE
    arrived — same CSeq, same SDP, same everything. It looks like an answer
    and is not one: the caller sees a response naming a transaction it has
    already completed, the media never moves, and a PBX that re-INVITEs to set
    up direct media tears the call down a few seconds later.

    Checking the CSeq is what distinguishes a real answer from a replay, and
    it is checkable without knowing anything about the device's media.
    """
    call = establish(endpoint, profile)
    first_cseq = call.invite.cseq_number

    ip = profile.local_ip or endpoint.advertise_ip
    resp = call.reinvite(_sdp.g711_offer(ip, 41000, 41002), timeout=10.0)

    assert resp is not None, (
        "the device never answered a re-INVITE inside an established dialog")
    assert 200 <= resp.status < 300, (
        f"the re-INVITE was refused {resp.status} {resp.reason}; RFC 3261 §14 "
        f"requires an established dialog to accept a well-formed re-INVITE it "
        f"can honour")
    assert resp.cseq_number != first_cseq, (
        f"the answer carries CSeq {resp.cseq_number}, which is the *first* "
        f"INVITE's. This is the cached response to that INVITE replayed, so "
        f"the re-INVITE was never really answered")
    assert resp.cseq_number == call.invite.cseq_number, (
        f"the answer carries CSeq {resp.cseq_number}, but the re-INVITE was "
        f"CSeq {call.invite.cseq_number}")
    to_tags = resp.headers.get("To").count("tag=")
    assert to_tags == 1, (
        f"the To header of the answer carries {to_tags} tag parameters; a "
        f"second tag appended to an already-tagged To is what a naive replay "
        f"produces")
    call.bye()


@register("SIP_CC_TE_SM_V_002", roles={"terminating"}, mandatory=True)
def reinvite_without_sdp_is_accepted(endpoint, profile):
    """A re-INVITE with no session description must still get a 200 OK.

    RFC 3261 §14: a re-INVITE carrying no body is how a peer asks the other
    side to re-offer — commonly a session refresh, or a PBX taking a call back
    from direct media. Refusing it with 488 ends a call that was working.
    """
    call = establish(endpoint, profile)
    resp = call.reinvite("", timeout=10.0)
    assert resp is not None, "no answer to a bodyless re-INVITE"
    assert 200 <= resp.status < 300, (
        f"a bodyless re-INVITE was refused {resp.status} {resp.reason}. It is "
        f"not malformed: RFC 3261 §14 makes it a request to re-offer, and the "
        f"answerer supplies the offer in its 2xx")
    assert resp.body.strip(), (
        "the 200 OK to a bodyless re-INVITE carried no session description, "
        "so neither side has made an offer and the session's media is "
        "undefined")
    call.bye()


@register("SIP_CC_TE_SM_V_003", roles={"terminating"}, mandatory=False,
          tags={"slow"})
def unacked_reinvite_is_ended(endpoint, profile):
    """A 200 OK to a re-INVITE that is never ACKed must be followed by BYE.

    RFC 3261 §14.1: the answerer retransmits its 2xx on the usual ladder and,
    if no ACK arrives, must terminate the dialog. A device that waits instead
    keeps a call leg — and its media ports, and on a small camera its only
    call slot — for as long as the peer stays silent.

    Marked Recommended in the corpus, and slow: the ladder runs to 64*T1 = 32s
    before the BYE is due.
    """
    call = establish(endpoint, profile)
    ip = profile.local_ip or endpoint.advertise_ip

    # Send the re-INVITE by hand: `reinvite` ACKs the answer, and the whole
    # point here is not to.
    m = call.request("INVITE", body=_sdp.g711_offer(ip, 41010, 41012))
    call.invite = m
    call.send(m)
    ok = endpoint.wait_for(
        lambda x: (x.is_response and x.call_id == call.call_id
                   and x.cseq_number == m.cseq_number and x.status >= 200),
        timeout=10.0)
    if ok is None or not (200 <= ok.status < 300):
        pytest.fail(f"the re-INVITE was not accepted "
                    f"({ok.status if ok else 'no response'}), so the "
                    f"no-ACK behaviour could not be exercised")

    bye = endpoint.wait_for(
        lambda x: x.is_request and x.method == "BYE"
        and x.call_id == call.call_id, timeout=45.0)
    assert bye is not None, (
        "45s after a 200 OK that was never ACKed, the device has still not "
        "sent BYE. RFC 3261 §14.1 requires it to terminate the dialog rather "
        "than hold the call leg open indefinitely")
    endpoint.respond(bye, 200, "OK")


@register("SIP_CC_TE_SM_I_001", roles={"terminating"}, mandatory=True,
          tags={"invalid"})
def reinvite_during_proceeding_is_refused(endpoint, profile):
    """A second INVITE arriving before the first is finally answered gets 500.

    RFC 3261 §14.2, and the precondition is the whole difficulty: the device's
    INVITE server transaction has to actually *be* in the Proceeding state —
    a provisional sent, no final yet. Only then is a second INVITE
    unanswerable and the required reply 500 with a Retry-After between 0 and
    10 seconds.

    An earlier version of this test sent two re-INVITEs back to back inside an
    established dialog and asserted on the answer to the second. That is not
    the same thing, and on a fast path it is not even close: the device
    answers the first in microseconds, so by the time the second arrives there
    is no outstanding transaction and 200 OK is the correct reply. It reported
    both majestic and baresip as non-conformant, and both were right. A test
    that cannot create its own precondition must say so rather than guess.

    So this drives an *initial* INVITE and waits for a provisional. A device
    that answers immediately — every auto-answering camera and softphone —
    never enters Proceeding, and the purpose does not apply to it in that
    configuration. That is reported as a skip naming the measured time, not as
    a pass and not as a failure.
    """
    require_target(profile)
    ip = profile.local_ip or endpoint.advertise_ip
    call = endpoint.new_call(profile.uri, profile.target)
    first = call.request("INVITE", body=_sdp.g711_offer(ip, 41020),
                         uri=profile.uri)
    call.invite = first
    started = time.monotonic()
    call.send(first)

    reply = endpoint.pump(5.0, until=lambda x: (
        x.is_response and x.call_id == call.call_id
        and x.cseq_number == first.cseq_number))
    if reply is None:
        pytest.fail("no response at all to an INVITE within 5s")
    if reply.status >= 200:
        took = (time.monotonic() - started) * 1000
        if 200 <= reply.status < 300:
            call.ack()
            call.bye()
        else:
            call.ack_failure(reply)
        pytest.skip(
            f"the device answered {reply.status} in {took:.0f} ms without "
            f"sending a provisional, so its INVITE server transaction is "
            f"never in the Proceeding state and this purpose has no "
            f"precondition to test. Expected of anything that auto-answers")

    # Proceeding: a provisional, no final. Now the second INVITE is the one
    # §14.2 is about.
    second = call.request("INVITE", body=_sdp.g711_offer(ip, 41022),
                          uri=profile.uri)
    call.send(second)
    resp = endpoint.pump(10.0, until=lambda x: (
        x.is_response and x.call_id == call.call_id
        and x.cseq_number == second.cseq_number and x.status >= 200))
    assert resp is not None, (
        "no final response to a second INVITE sent while the first was still "
        "in Proceeding")

    # The device may have left Proceeding between our two requests — it was
    # about to answer the first anyway, and on loopback that window is
    # microseconds wide. Then a 200 to the second is simply correct, and
    # calling it a violation would be the same mistake as before, one race
    # further down. The arrival order says which happened.
    order = [m for m in endpoint.received
             if m.is_response and m.call_id == call.call_id]
    finals_for_first = [i for i, m in enumerate(order)
                        if m.cseq_number == first.cseq_number and m.status >= 200]
    try:
        idx_resp = order.index(resp)
    except ValueError:
        idx_resp = len(order)
    if finals_for_first and finals_for_first[0] < idx_resp:
        first_final = order[finals_for_first[0]]
        call.ack(for_response=first_final) if 200 <= first_final.status < 300 \
            else call.ack_failure(first_final)
        call.bye(timeout=3.0)
        pytest.skip(
            f"the device answered the first INVITE {first_final.status} "
            f"before answering the second, so it had already left the "
            f"Proceeding state and 200 to the second is correct. The race is "
            f"inherent on a fast path; retry on a device that rings, or "
            f"through a PBX that adds latency")
    try:
        assert resp.status in (500, 491), (
            f"a second INVITE sent while the first was still unanswered "
            f"(a {reply.status} had been sent, no final) was given "
            f"{resp.status} {resp.reason}; RFC 3261 §14.2 asks for 500 with a "
            f"Retry-After, or 491 Request Pending")
        if resp.status == 500:
            retry = resp.headers.get("retry-after")
            assert retry, ("the 500 carried no Retry-After, so the caller has "
                           "no idea when to try again (§14.2)")
            assert 0 <= int(retry.split(";")[0].strip()) <= 10, (
                f"Retry-After is {retry}; §14.2 asks for 0 to 10 seconds")
    finally:
        call.cancel(timeout=3.0)


# ---------------------------------------------------------------------------
# LOCAL-* — no counterpart in the corpus.
#
# The ETSI purposes predate the practice they would need to cover: they check
# that an answer *exists* and that its CSeq and dialog identifiers are right,
# but not that its payload numbers follow the offer's, and not that a hold is
# honoured in the media. Both were shipped defects, so both get a test.
# ---------------------------------------------------------------------------

@register("LOCAL-SDP-ANSWER-KEEPS-OFFERED-PAYLOAD-NUMBERS",
          roles={"terminating"}, mandatory=True)
def answer_keeps_offered_payload_numbers(endpoint, profile):
    """The answer may only use the numbers the offer bound to each codec.

    RFC 3264 §6.1 gives the answerer no say in this. It reads like a
    formality; it is not. A B2BUA holds each leg to the numbers negotiated on
    that leg and silently drops every packet carrying any other one, so a
    renumbered answer reaches the user as a video stream that connects,
    reports zero packets received, and stays black.

    It survives peer-to-peer testing because liblinphone forgives it — it logs
    "proposed number was 97 but the remote phone answered 96" and rebuilds its
    decoder. That forgiveness is why this test offers Linphone's numbering
    rather than the conventional one: H.264 as 97 is the vector.
    """
    call = establish(endpoint, profile)
    try:
        offer = _sdp.parse(call.local_sdp)
        answer = call.remote_sdp
        assert answer is not None, "the 200 OK carried no session description"

        problems = []
        for kind in ("audio", "video"):
            om, am = offer.get(kind), answer.get(kind)
            if om is None or am is None or not am.active:
                continue
            for fmt in am.formats:
                offered = om.format(fmt.pt)
                if offered is None:
                    named = ", ".join(f"{f.pt}={f.name or '?'}"
                                      for f in om.formats)
                    problems.append(
                        f"{kind}: the answer names payload {fmt.pt}"
                        f"{' (' + fmt.name + ')' if fmt.name else ''}, which "
                        f"the offer never bound to anything. Offered: {named}")
                elif (fmt.name and offered.name
                      and fmt.name.upper() != offered.name.upper()):
                    problems.append(
                        f"{kind}: the answer maps payload {fmt.pt} to "
                        f"{fmt.name}, but the offer bound {fmt.pt} to "
                        f"{offered.name}. The correct number for {fmt.name} "
                        f"in this offer is {om.pt_for(fmt.name)}")
        assert not problems, "; ".join(problems)
    finally:
        call.bye()


@register("LOCAL-SDP-HOLD-IS-HONOURED", roles={"terminating"}, mandatory=True)
def hold_is_honoured(endpoint, profile):
    """An offer of sendonly must be answered recvonly, and media must stop.

    RFC 3264 §6.1 and §8.4: each side describes what *it* will do, so the
    mirror of sendonly is recvonly. A device that answers sendrecv to a hold —
    or answers correctly and keeps sending anyway — puts audio into a call the
    other end has parked.
    """
    call = establish(endpoint, profile, media=True)
    try:
        ip = profile.local_ip or endpoint.advertise_ip
        ports = (call.audio.port if call.audio else 41030,
                 call.video.port if call.video else 0)
        resp = call.reinvite(_sdp.hold_offer(ip, *ports), timeout=10.0)
        assert resp is not None and 200 <= resp.status < 300, (
            f"the hold re-INVITE was not accepted "
            f"({resp.status if resp else 'no response'})")
        answer = call.remote_sdp
        assert answer is not None, "the answer to the hold carried no SDP"

        for kind in ("audio", "video"):
            m = answer.get(kind)
            if m is None or not m.active:
                continue
            d = answer.direction_of(m)
            assert not d.sends, (
                f"we offered {kind} sendonly — a hold — and the device "
                f"answered {d.value}, claiming it will keep sending. RFC 3264 "
                f"§6.1 makes the answer the mirror of the offer, so the only "
                f"answers that do not send are recvonly and inactive")

        if call.audio is not None:
            before = call.audio.stats.packets
            endpoint.drain(3.0)
            after = call.audio.stats.packets
            assert after == before, (
                f"{after - before} audio packets arrived in 3s after the call "
                f"was put on hold. The direction was answered correctly, but "
                f"the media path did not follow it")
    finally:
        call.bye()
