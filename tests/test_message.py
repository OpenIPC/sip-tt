"""The message layer, and specifically the ways a naive one goes wrong."""

import pytest

from sip_tt.runtime.message import Message, ParseError, parse, split_params


def test_repeated_via_headers_are_all_kept():
    """A response is routed by echoing the Via stack in full and in order.

    A dict of name->value keeps the first and drops the rest, which works in a
    one-hop lab and vanishes the moment a proxy is in the path.
    """
    m = parse("INVITE sip:a@b SIP/2.0\r\n"
              "Via: SIP/2.0/UDP one;branch=z1\r\n"
              "Via: SIP/2.0/UDP two;branch=z2\r\n\r\n")
    assert m.headers.all("via") == ["SIP/2.0/UDP one;branch=z1",
                                    "SIP/2.0/UDP two;branch=z2"]
    assert m.branch == "z1", "the branch is the topmost Via's"


def test_comma_separated_via_is_the_same_as_repeated():
    """RFC 3261 §7.3.1 makes the two forms equivalent; peers use both."""
    m = parse("INVITE sip:a@b SIP/2.0\r\n"
              "Via: SIP/2.0/UDP one;branch=z1, SIP/2.0/UDP two;branch=z2\r\n\r\n")
    assert len(m.headers.all("via")) == 2


def test_compact_forms_resolve_to_long_names():
    m = parse("INVITE sip:a@b SIP/2.0\r\nv: SIP/2.0/UDP h;branch=z\r\n"
              "i: call-1\r\nl: 0\r\n\r\n")
    assert m.headers.get("via").startswith("SIP/2.0/UDP")
    assert m.call_id == "call-1"


def test_a_display_name_containing_tag_is_not_a_tag():
    """`"tag=" in header` and `split("tag=")[1]` both get this wrong.

    Every quick SIP script in the lab had this bug. The parameter list starts
    after the name-addr, so a display name is not searched at all.
    """
    m = parse('INVITE sip:a@b SIP/2.0\r\nTo: "tag=trap" <sip:1001@cam>\r\n\r\n')
    assert m.tag("to") == ""


def test_parameters_inside_angle_brackets_belong_to_the_uri():
    body, params = split_params("<sip:a@b;user=phone>;tag=1")
    assert body == "<sip:a@b;user=phone>"
    assert params == {"tag": "1"}


def test_folded_headers_are_unfolded():
    m = parse("INVITE sip:a@b SIP/2.0\r\nSubject: one\r\n  two\r\n\r\n")
    assert m.headers.get("subject") == "one two"


def test_cseq_that_cannot_be_read_is_minus_one():
    """A finding to assert on, not an exception that aborts the test."""
    assert parse("INVITE sip:a@b SIP/2.0\r\nCSeq: junk INVITE\r\n\r\n"
                 ).cseq_number == -1


def test_bare_lf_is_accepted():
    """RFC 3261 §7.5 asks receivers to cope, and peers rely on it."""
    m = parse("SIP/2.0 200 OK\nCSeq: 1 INVITE\n\nbody")
    assert m.status == 200 and m.body == "body"


def test_render_fixes_content_length():
    m = Message(method="INVITE", uri="sip:a@b", body="v=0\r\n")
    m.headers.add("Content-Length", "999")
    assert "Content-Length: 5" in m.render()


def test_round_trip():
    m = Message(method="INVITE", uri="sip:a@b")
    m.headers.add("Via", "SIP/2.0/UDP h;branch=z9hG4bKx")
    m.headers.add("CSeq", "42 INVITE")
    back = parse(m.render())
    assert back.method == "INVITE" and back.cseq_number == 42
    assert back.branch == "z9hG4bKx"


def test_unparseable_start_line_raises():
    with pytest.raises(ParseError):
        parse("this is not SIP\r\n\r\n")
