"""Digest, checked against the vector RFC 7616 publishes."""

import re

from sip_tt.runtime import auth


def test_matches_the_rfc_7616_md5_vector():
    """RFC 7616 §3.9.1. If this drifts, every authenticated test lies."""
    c = auth.Challenge.parse(
        'Digest realm="http-auth@example.org", qop="auth, auth-int", '
        'algorithm=MD5, nonce="7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v", '
        'opaque="FQhe/qaU925kfnzjCev0ciny7QMkPqMAFRtzCUYo5tdS"')
    header = auth.respond(
        c, username="Mufasa", password="Circle of Life", method="GET",
        uri="/dir/index.html", nc=1,
        cnonce="f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ")
    got = re.search(r'response="([0-9a-f]+)"', header).group(1)
    assert got == "8ca523f5e9506fed4657c9700eebdbec"


def test_legacy_rfc_2069_form_when_no_qop_is_offered():
    c = auth.Challenge.parse('Digest realm="test", nonce="abc"')
    h = auth.respond(c, username="u", password="p", method="REGISTER",
                     uri="sip:example.org")
    assert "qop=" not in h and "cnonce=" not in h
    assert auth.verify(h, password="p", method="REGISTER")[0]


def test_a_proxy_challenge_stays_a_proxy_challenge():
    """Answering a 407 with `Authorization` is a real and common bug.

    It is what majestic does today, so the proxy-ness has to survive parsing
    rather than being flattened into one code path.
    """
    from sip_tt.runtime.message import parse
    resp = parse("SIP/2.0 407 Proxy Authentication Required\r\n"
                 'Proxy-Authenticate: Digest realm="r", nonce="n"\r\n\r\n')
    c = auth.challenge_from(resp)
    assert c.is_proxy and c.header_name() == "Proxy-Authorization"

    resp2 = parse("SIP/2.0 401 Unauthorized\r\n"
                  'WWW-Authenticate: Digest realm="r", nonce="n"\r\n\r\n')
    assert auth.challenge_from(resp2).header_name() == "Authorization"


def test_verify_rejects_a_wrong_password_and_says_so():
    c = auth.Challenge.parse('Digest realm="r", nonce="n", qop="auth"')
    h = auth.respond(c, username="u", password="right", method="INVITE",
                     uri="sip:a@b")
    ok, why = auth.verify(h, password="wrong", method="INVITE")
    assert not ok and why == "digest mismatch"


def test_verify_names_the_missing_field():
    ok, why = auth.verify('Digest username="u", realm="r"', password="p",
                          method="INVITE")
    assert not ok and "nonce" in why


def test_stale_is_parsed():
    c = auth.Challenge.parse('Digest realm="r", nonce="n", stale=true')
    assert c.stale is True
