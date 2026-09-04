"""Offer/answer, and the payload-numbering rule the whole tool exists for."""

from sip_tt.runtime import sdp


def test_linphone_offer_numbers_h264_as_97():
    """The vector, not an illustration.

    A tool that offered H.264 under the conventional 96 would agree with an
    answerer that renumbers, and the defect this catches would go unseen.
    """
    o = sdp.parse(sdp.linphone_offer("10.0.0.1", 7078, 9078))
    assert o.video.pt_for("H264") == 97
    assert o.video.pt_for("h264") == 97, "encoding names are case-insensitive"
    assert o.video.pt_for("AV1") == -1


def test_static_payload_types_need_no_rtpmap():
    """Asterisk offers `0 8 101` with an rtpmap for 101 alone, and may."""
    o = sdp.parse("v=0\r\nc=IN IP4 1.2.3.4\r\nm=audio 5004 RTP/AVP 0 8 101\r\n"
                  "a=rtpmap:101 telephone-event/8000\r\n")
    assert o.audio.format(0).name == "PCMU"
    assert o.audio.format(8).name == "PCMA"


def test_payload_numbers_are_seven_bits():
    """A number outside 0-127 cannot name a stream (RFC 3550 §5.1)."""
    o = sdp.parse("v=0\r\nm=audio 5004 RTP/AVP 0 200 8\r\n")
    assert [f.pt for f in o.audio.formats] == [0, 8]


def test_port_zero_is_a_refusal_not_an_omission():
    o = sdp.parse("v=0\r\nm=audio 0 RTP/AVP 0\r\nm=video 5006 RTP/AVP 96\r\n")
    assert o.audio.active is False
    assert o.video.active is True
    assert len(o.media) == 2, "a refused medium keeps its place in the list"


def test_media_level_connection_overrides_the_session_one():
    o = sdp.parse("v=0\r\nc=IN IP4 1.1.1.1\r\nm=audio 5004 RTP/AVP 0\r\n"
                  "c=IN IP4 2.2.2.2\r\nm=video 5006 RTP/AVP 96\r\n")
    assert o.address_for(o.audio) == "2.2.2.2"
    assert o.address_for(o.video) == "1.1.1.1"


def test_session_direction_applies_where_a_medium_has_none():
    o = sdp.parse("v=0\r\na=sendonly\r\nm=audio 5004 RTP/AVP 0\r\n"
                  "m=video 5006 RTP/AVP 96\r\na=recvonly\r\n")
    assert o.direction_of(o.audio) == sdp.Direction.SENDONLY
    assert o.direction_of(o.video) == sdp.Direction.RECVONLY


def test_direction_mirroring_follows_rfc_3264():
    assert sdp.Direction.SENDONLY.mirror() == sdp.Direction.RECVONLY
    assert sdp.Direction.RECVONLY.mirror() == sdp.Direction.SENDONLY
    assert sdp.Direction.SENDRECV.mirror() == sdp.Direction.SENDRECV
    assert sdp.Direction.INACTIVE.mirror() == sdp.Direction.INACTIVE
    assert not sdp.Direction.SENDONLY.mirror().sends, (
        "the mirror of a hold must not send")


def test_build_is_reproducible():
    """Two identical offers must be byte-identical.

    That is what a retransmission is, and it is how a test tells one from a
    re-INVITE; a session id taken from the clock would make them differ.
    """
    a = sdp.g711_offer("10.0.0.1", 5004)
    b = sdp.g711_offer("10.0.0.1", 5004)
    assert a == b


def test_build_then_parse_round_trip():
    body = sdp.build(address="10.0.0.1", media=[
        sdp.Media("audio", 5004, [sdp.Format(0)], sdp.Direction.SENDRECV),
        sdp.Media("video", 5006, [sdp.Format(99, "H264", 90000, "x=1")],
                  sdp.Direction.SENDONLY)])
    o = sdp.parse(body)
    assert o.video.pt_for("H264") == 99
    assert o.video.format(99).fmtp == "x=1"
    assert o.direction_of(o.video) == sdp.Direction.SENDONLY
