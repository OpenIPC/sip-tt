"""The endpoint, exercised against itself over loopback.

No device is involved: one Endpoint answers, another calls, and the assertions
are about the machinery rather than about anyone's conformance. If these fail,
every conformance verdict the tool produces is worthless.
"""

import threading

import pytest

from sip_tt.runtime import sdp
from sip_tt.runtime.endpoint import Endpoint


@pytest.fixture
def pair():
    a = Endpoint("127.0.0.1", 0, advertise_ip="127.0.0.1", username="tester")
    b = Endpoint("127.0.0.1", 0, advertise_ip="127.0.0.1", username="1001")
    yield a, b
    a.close()
    b.close()


def _tester_port(pair):
    return pair[0].port


def _tester_recv(pair, timeout=2.0):
    """Read one message on the tester's socket."""
    return pair[0].pump(timeout, until=lambda m: True)


def _answering_device(dut, answer_body=None, status=200):
    """Run a minimal UAS in a thread: 100, 180, then a final response."""
    def run():
        inv = dut.expect_call(timeout=10)
        if inv is None:
            return
        dut.respond(inv, 100, "Trying")
        dut.respond(inv, 180, "Ringing")
        dut.auto_answer_simple()
        if status != 200:
            dut.respond(inv, status)
            return
        body = answer_body
        if body is None:
            offer = sdp.parse(inv.body) if inv.body else None
            apt = offer.audio.pt_for("PCMU") if offer and offer.audio else 0
            body = sdp.build(address="127.0.0.1", media=[
                sdp.Media("audio", 40000, [sdp.Format(apt)],
                          sdp.Direction.SENDRECV)])
        dut.accept_call(inv, body)
        dut.drain(3)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_a_call_completes_end_to_end(pair):
    tester, dut = pair
    _answering_device(dut)
    call = tester.place_call("sip:1001@127.0.0.1", ("127.0.0.1", dut.port),
                             body=sdp.g711_offer("127.0.0.1", 7078))
    assert call.last_response.status == 200
    assert [m.status for m in tester.received if m.is_response and m.status < 200] \
        == [100, 180]
    call.ack()
    assert call.bye(timeout=3).status == 200


def test_the_remote_target_comes_from_the_contact_not_the_request_uri(pair):
    """In-dialog requests go to the Contact, per RFC 3261 §12.2.1.1.

    Sending them to the original Request-URI works only when the peer happens
    to answer on the port it was called on.
    """
    tester, dut = pair
    _answering_device(dut)
    call = tester.place_call("sip:1001@127.0.0.1", ("127.0.0.1", dut.port),
                             body=sdp.g711_offer("127.0.0.1", 7078))
    call.ack()
    assert call.remote_target == f"sip:1001@127.0.0.1:{dut.port}"
    call.bye(timeout=3)


def test_the_offers_payload_numbers_survive_the_round_trip(pair):
    """The tool must not renumber anything itself on the way through."""
    tester, dut = pair
    _answering_device(dut)
    call = tester.place_call("sip:1001@127.0.0.1", ("127.0.0.1", dut.port),
                             body=sdp.linphone_offer("127.0.0.1", 7078, 9078))
    sent = sdp.parse(call.local_sdp)
    assert sent.video.pt_for("H264") == 97
    assert call.remote_sdp.audio.formats[0].pt == 0
    call.ack()
    call.bye(timeout=3)


def test_a_failure_response_is_reported_not_swallowed(pair):
    tester, dut = pair
    _answering_device(dut, status=486)
    call = tester.place_call("sip:1001@127.0.0.1", ("127.0.0.1", dut.port),
                             body=sdp.g711_offer("127.0.0.1", 7078))
    assert call.last_response.status == 486
    assert not call.confirmed


def test_a_response_echoes_every_via_in_order(pair):
    """Two hops' worth of Via must come back, or the response is undeliverable."""
    from sip_tt.runtime.message import parse
    tester, dut = pair
    req = parse("OPTIONS sip:1001@127.0.0.1 SIP/2.0\r\n"
                "Via: SIP/2.0/UDP proxy;branch=z9hG4bKtop\r\n"
                "Via: SIP/2.0/UDP caller;branch=z9hG4bKbottom\r\n"
                "From: <sip:a@b>;tag=1\r\nTo: <sip:1001@c>\r\n"
                "Call-ID: via-test\r\nCSeq: 1 OPTIONS\r\n\r\n")
    req.source = ("127.0.0.1", tester.port)
    resp = dut.respond(req, 200, "OK")
    assert resp.headers.all("via") == ["SIP/2.0/UDP proxy;branch=z9hG4bKtop",
                                       "SIP/2.0/UDP caller;branch=z9hG4bKbottom"]


def test_a_to_tag_is_added_once_and_only_once(pair):
    """Appending a second tag to an already-tagged To is what a replay does."""
    from sip_tt.runtime.message import parse
    _, dut = pair
    req = parse("BYE sip:1001@127.0.0.1 SIP/2.0\r\n"
                "Via: SIP/2.0/UDP c;branch=z9hG4bKx\r\n"
                "From: <sip:a@b>;tag=1\r\nTo: <sip:1001@c>;tag=already\r\n"
                "Call-ID: tag-test\r\nCSeq: 2 BYE\r\n\r\n")
    req.source = ("127.0.0.1", 9)
    resp = dut.respond(req, 200, "OK")
    assert resp.headers.get("to").count("tag=") == 1
    assert "tag=already" in resp.headers.get("to")


def test_a_2xx_that_forms_a_dialog_always_carries_contact(pair):
    """RFC 3261 §12.1.1 — Contact is where the ACK and BYE are addressed.

    respond() adds one when there is a body, which is a different rule; a
    bodyless answer would otherwise form a dialog nobody can address.
    """
    from sip_tt.runtime.message import parse
    _, dut = pair
    inv = parse("INVITE sip:1001@127.0.0.1 SIP/2.0\r\n"
                "Via: SIP/2.0/UDP c;branch=z9hG4bKx\r\n"
                "From: <sip:a@b>;tag=1\r\nTo: <sip:1001@c>\r\n"
                "Call-ID: contact-test\r\nCSeq: 1 INVITE\r\n\r\n")
    inv.source = ("127.0.0.1", _tester_port(pair))
    dut.accept_call(inv, "")
    got = _tester_recv(pair)
    assert got is not None, "the 200 OK never arrived"
    assert got.status == 200
    assert got.headers.get("contact"), (
        "the 200 OK that formed the dialog carried no Contact, so the caller "
        "has nothing to address its ACK and BYE to (RFC 3261 §12.1.1)")


def test_an_answer_uses_the_numbers_the_offer_bound(pair):
    """RFC 3264 §6.1 binds the answerer to the offer's numbering.

    The rule this whole tool exists to police, so the answers it builds itself
    had better follow it — a tool that renumbered would agree with every
    device that does.
    """
    tester, _ = pair
    call = tester.new_call("sip:x@y", ("127.0.0.1", 1))
    offered = sdp.build(address="10.0.0.5", media=[
        sdp.Media("audio", 5004, [sdp.Format(8)], sdp.Direction.SENDRECV),
        sdp.Media("video", 5006, [sdp.Format(99, "H264", 90000)],
                  sdp.Direction.SENDONLY)])
    answer = sdp.parse(call.answer_to(offered))
    assert answer.audio.formats[0].pt == 8
    assert answer.video.formats[0].pt == 99
    assert answer.direction_of(answer.video) == sdp.Direction.RECVONLY, (
        "the mirror of sendonly is recvonly")


def test_a_refused_medium_stays_refused_in_our_answer(pair):
    """Port zero is a refusal, and the answer keeps the m-line in place."""
    tester, _ = pair
    call = tester.new_call("sip:x@y", ("127.0.0.1", 1))
    offered = sdp.build(address="10.0.0.5", media=[
        sdp.Media("audio", 5004, [sdp.Format(0)]),
        sdp.Media("video", 0, [sdp.Format(99, "H264", 90000)])])
    answer = sdp.parse(call.answer_to(offered))
    assert len(answer.media) == 2, "an answer has one m-line per offered one"
    assert answer.video.active is False


def test_an_ack_can_carry_the_answer(pair):
    """§13.2.1 — the only request in SIP whose body answers a response."""
    tester, dut = pair
    _answering_device(dut)
    call = tester.place_call("sip:1001@127.0.0.1", ("127.0.0.1", dut.port),
                             body=sdp.g711_offer("127.0.0.1", 7078))
    ack = call.ack(body="v=0\r\n")
    assert ack.body == "v=0\r\n"
    assert "Content-Length: 5" in ack.render()
    call.bye(timeout=3)
