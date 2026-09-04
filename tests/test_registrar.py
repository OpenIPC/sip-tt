"""The registrar, and the distinctions a refresh test depends on.

Every test here exists because a review found a way for a refresh purpose to
pass without a refresh having happened. They are cheap and they are the only
thing standing between "the device renewed its binding" and "some REGISTER
arrived".
"""

import pytest

from sip_tt.runtime.endpoint import Endpoint
from sip_tt.runtime.message import parse
from sip_tt.runtime.registrar import Registrar


@pytest.fixture
def rig():
    ep = Endpoint("127.0.0.1", 0, advertise_ip="127.0.0.1", username="registrar")
    reg = Registrar(ep, password="", challenge=False, grant_expires=60)
    yield ep, reg
    ep.close()


def _register(contact="<sip:dut@127.0.0.1:5070>", expires=None, cseq=1,
              call_id="reg-1", authz=None):
    lines = ["REGISTER sip:127.0.0.1 SIP/2.0",
             "Via: SIP/2.0/UDP 127.0.0.1:5070;branch=z9hG4bK%d" % cseq,
             "From: <sip:dut@127.0.0.1>;tag=abc",
             "To: <sip:dut@127.0.0.1>",
             f"Call-ID: {call_id}",
             f"CSeq: {cseq} REGISTER"]
    if contact is not None:
        lines.append(f"Contact: {contact}")
    if expires is not None:
        lines.append(f"Expires: {expires}")
    if authz:
        lines.append(f"Authorization: {authz}")
    m = parse("\r\n".join(lines) + "\r\n\r\n")
    m.source = ("127.0.0.1", 5070)
    return m


def test_a_binding_is_recorded_as_a_registration(rig):
    _, reg = rig
    reg._on_request(_register())
    assert len(reg.accepted) == 1
    assert len(reg.registrations) == 1
    assert reg.bindings


def test_a_contactless_query_is_not_a_registration(rig):
    """RFC 3261 §10.2.1: no Contact is a query for the current bindings.

    It is answered 200 and renews nothing, so a device that only ever queried
    must not satisfy a refresh purpose.
    """
    _, reg = rig
    reg._on_request(_register(contact=None))
    assert len(reg.accepted) == 1
    assert reg.registrations == []


def test_a_removal_is_not_a_registration(rig):
    """expires=0 removes a binding (§10.2.2); it does not renew one."""
    _, reg = rig
    reg._on_request(_register())
    reg._on_request(_register(expires=0, cseq=2))
    assert len(reg.registrations) == 1
    assert reg.bindings == {}


def test_a_star_removal_is_not_a_registration(rig):
    _, reg = rig
    reg._on_request(_register())
    reg._on_request(_register(contact="*", expires=0, cseq=2))
    assert len(reg.registrations) == 1
    assert reg.bindings == {}


def test_a_challenge_and_its_retry_are_one_registration():
    """Counting raw REGISTERs makes a 401 retry look like a refresh."""
    ep = Endpoint("127.0.0.1", 0, advertise_ip="127.0.0.1")
    try:
        reg = Registrar(ep, password="secret", challenge=True)
        reg._on_request(_register())                  # challenged, 401
        assert reg.registrations == []
        assert len(reg.rejected) == 1

        from sip_tt.runtime import auth
        ch = auth.Challenge.parse(reg.challenged["reg-1"])
        header = auth.respond(ch, username="dut", password="secret",
                              method="REGISTER", uri="sip:127.0.0.1")
        reg._on_request(_register(cseq=2, authz=header))
        assert len(reg.registrations) == 1, "the retry is the registration"
        assert len(reg.accepted) == 1
    finally:
        ep.close()


def test_a_wrong_credential_is_refused_and_registers_nothing():
    """A retry that fails the digest must not count as having registered."""
    ep = Endpoint("127.0.0.1", 0, advertise_ip="127.0.0.1")
    try:
        reg = Registrar(ep, password="secret", challenge=True)
        reg._on_request(_register())
        from sip_tt.runtime import auth
        ch = auth.Challenge.parse(reg.challenged["reg-1"])
        header = auth.respond(ch, username="dut", password="WRONG",
                              method="REGISTER", uri="sip:127.0.0.1")
        bad = _register(cseq=2, authz=header)
        reg._on_request(bad)
        assert bad in reg.rejected
        assert bad not in reg.accepted
        assert reg.registrations == []
    finally:
        ep.close()


def test_wait_for_registration_counts_absolutely(rig):
    """Relative counting accepts the first registration as though it were a refresh."""
    _, reg = rig
    reg._on_request(_register())
    assert reg.wait_for_registration(1, timeout=0.1) is not None
    assert reg.wait_for_registration(2, timeout=0.1) is None, (
        "one registration must not satisfy a wait for two")
    reg._on_request(_register(cseq=2))
    assert reg.wait_for_registration(2, timeout=0.1) is not None


def test_the_granted_expiry_caps_what_was_asked_for(rig):
    """RFC 3261 §10.2.4 — the registrar's expiry is the authoritative one."""
    _, reg = rig
    reg._on_request(_register(expires=3600))
    assert list(reg.bindings.values())[0].expires == 60
