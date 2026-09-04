"""Registrant: what a device must put in the REGISTER it sends.

Every purpose here is about a message the *device* originates, so all of them
need sip-tt to be the registrar and the device to be pointed at it. That makes
them the one family with a hard setup requirement — `--with-registrar`, and a
device configured to register with this host — and the runner reports a
missing fixture as a failure to exercise rather than as a skip.

The waits are long by the standards of the rest of the suite. A registrant
refreshes on its own schedule and cannot be asked to hurry; the only lever is
RFC 3261 §10.2.4, which makes the *registrar's* granted expiry authoritative.
sip-tt grants sixty seconds by default so a refresh arrives inside a test
run — and a device that ignores it and keeps to its own configured interval
is what `SIP_RG_RT_V_012` is asking about.
"""

from __future__ import annotations

import pytest

from ..registry import register
from ..runtime.message import split_params

FIRST_TIMEOUT = 120.0


def _first(registrar, timeout: float = FIRST_TIMEOUT):
    m = registrar.wait_for_register(timeout=timeout)
    if m is None:
        pytest.fail(
            f"no REGISTER arrived in {timeout:.0f}s. The device has to be "
            f"configured to register with this host — check its registrar "
            f"address and port, and that nothing else is bound to our "
            f"signalling port. Reported as a failure and not a skip: the "
            f"purpose was never put to the device")
    return m


def _uri_of(header_value: str) -> str:
    start = header_value.find("<")
    if start >= 0:
        end = header_value.find(">", start)
        if end > start:
            return header_value[start + 1:end]
    body, _ = split_params(header_value)
    return body.strip()


@register("SIP_RG_RT_V_001", roles={"registrant"}, mandatory=True,
          requires={"registrar"}, tags={"slow"})
def register_request_uri_is_a_userless_sip_uri(registrar):
    """The Request-URI of a REGISTER names the domain, not the user.

    RFC 3261 §10.2: the Request-URI is "the domain of the location service",
    so `sip:example.com` and never `sip:alice@example.com`. A registrar that
    routes on the Request-URI cannot place a REGISTER that carries a user
    part, and the registration silently never happens.
    """
    m = _first(registrar)
    uri = m.uri
    assert uri.lower().startswith(("sip:", "sips:")), (
        f"the Request-URI is {uri!r}, which is not a SIP URI (§10.2)")
    assert "@" not in uri, (
        f"the Request-URI is {uri!r}. §10.2 makes it the registrar's domain, "
        f"with no user part — the address being registered goes in To")


@register("SIP_RG_RT_V_008", roles={"registrant"}, mandatory=True,
          requires={"registrar"}, tags={"slow"})
def register_to_header_is_the_address_of_record(registrar):
    """The To header carries the address of record, as a SIP URI.

    RFC 3261 §10.2. To is what is being registered; the Request-URI is where
    the registration is being sent. Devices that confuse the two register
    something nobody will ever look up.
    """
    m = _first(registrar)
    to = m.headers.get("to")
    assert to, "the REGISTER carried no To header"
    aor = _uri_of(to)
    assert aor.lower().startswith(("sip:", "sips:")), (
        f"the To header is {to!r}; §10.2 wants an address of record as a SIP "
        f"URI")
    assert "@" in aor, (
        f"the address of record is {aor!r} — it names no user, so there is "
        f"nothing for a caller to reach")


@register("SIP_RG_RT_V_009", roles={"registrant"}, mandatory=True,
          requires={"registrar"}, tags={"slow"})
def register_from_and_to_carry_the_same_uri(registrar):
    """From and To hold the same URI when a device registers itself.

    RFC 3261 §10.2: they differ only when one party registers on another's
    behalf — third-party registration — which a camera does not do. A mismatch
    usually means the device put its proxy in From, and a registrar enforcing
    third-party policy will refuse it.
    """
    m = _first(registrar)
    frm, to = _uri_of(m.headers.get("from")), _uri_of(m.headers.get("to"))
    assert frm and to, "the REGISTER lacked a From or a To header"
    assert frm.lower() == to.lower(), (
        f"From is {frm!r} and To is {to!r}. For a device registering itself "
        f"§10.2 makes these the same URI; differing ones assert a "
        f"third-party registration, which many registrars refuse")


@register("SIP_RG_RT_V_015", roles={"registrant"}, mandatory=False,
          requires={"registrar"}, tags={"slow"})
def register_suggests_an_expiry(registrar):
    """A REGISTER should suggest how long the binding is wanted for.

    RFC 3261 §10.2.1.1 — either an `Expires` header or an `expires` parameter
    on the Contact. Without one the registrar picks, and a device that assumed
    a longer interval than it was given goes unreachable between refreshes.
    """
    m = _first(registrar)
    if m.headers.get("expires"):
        return
    for c in m.headers.all("contact"):
        _, params = split_params(c)
        if "expires" in params:
            return
    pytest.fail(
        "the REGISTER carried neither an Expires header nor an expires "
        "parameter on its Contact, so it makes no suggestion about the "
        "binding's lifetime (§10.2.1.1) and takes whatever the registrar "
        "grants")


@register("SIP_RG_RT_V_007", roles={"registrant"}, mandatory=False,
          requires={"registrar"}, tags={"slow"})
def register_retries_a_challenge_with_credentials(registrar, profile):
    """A 401 must be answered by repeating the REGISTER with credentials.

    RFC 3261 §8.1.3.5 and §22.2, and the retry takes a *new* CSeq: it is a new
    request, not a retransmission of the challenged one, and a registrar that
    treats a repeated CSeq as a duplicate will ignore it.
    """
    if not profile.password:
        pytest.skip("no credential configured, so the registrar does not "
                    "challenge and there is nothing to retry")
    first = _first(registrar)
    registrar.wait_for_register(timeout=30.0)
    retry = None
    for i in range(1, len(registrar.registers)):
        candidate = registrar.registers[i]
        if candidate.headers.get("authorization"):
            retry = candidate
            break
    else:
        pytest.fail(
            f"the device was challenged 401 and did not repeat its REGISTER "
            f"with an Authorization header ({len(registrar.registers)} "
            f"REGISTERs seen). Without it the device never registers and "
            f"cannot be called")
    assert retry.cseq_number > first.cseq_number, (
        f"the retry carries CSeq {retry.cseq_number}, the same as or lower "
        f"than the challenged request's {first.cseq_number}. §8.1.3.5 makes "
        f"the retry a new request, so its CSeq increments")


@register("SIP_RG_RT_V_011", roles={"registrant"}, mandatory=True,
          requires={"registrar"}, tags={"slow"})
def refresh_increments_cseq_on_the_same_call_id(registrar):
    """A refresh reuses the Call-ID and increments CSeq.

    RFC 3261 §10.2. The Call-ID is what tells the registrar this is the same
    registration being renewed rather than a second device claiming the same
    address; the CSeq is what orders them. A device that draws a fresh Call-ID
    each time cannot have its bindings distinguished from a duplicate.
    """
    first = _first(registrar)
    later = registrar.wait_for_refresh(timeout=180.0)
    if later is None:
        pytest.fail(
            f"no refresh arrived in 3 minutes — {len(registrar.accepted)} "
            f"registration(s) accepted in total. We granted "
            f"{registrar.grant_expires}s, which §10.2.4 makes authoritative")
    same_cid = [m for m in registrar.accepted if m.call_id == first.call_id]
    assert len(same_cid) >= 2, (
        f"the refresh used Call-ID {later.call_id!r} where the first "
        f"registration used {first.call_id!r}. §10.2 keeps one Call-ID for "
        f"the life of a registration")
    assert same_cid[-1].cseq_number > same_cid[0].cseq_number, (
        f"CSeq went from {same_cid[0].cseq_number} to "
        f"{same_cid[-1].cseq_number} across a refresh; §10.2 requires it to "
        f"increment")


@register("SIP_RG_RT_V_012", roles={"registrant"}, mandatory=False,
          requires={"registrar"}, tags={"slow", "advisory"})
def refresh_happens_within_the_granted_lifetime(registrar):
    """The refresh interval is the registrar's, not the device's.

    RFC 3261 §10.2.4: the expiry in the 200 OK is authoritative, and the
    registrant refreshes within it. A device that keeps to the interval it
    *asked* for goes unreachable the moment a registrar grants less — which
    registrars do routinely, to keep bindings fresh behind NAT.

    Registered advisory: the corpus marks it Recommended, and the failure mode
    is a device that is unreachable rather than one that is broken.
    """
    _first(registrar)
    granted = registrar.grant_expires
    window = granted * 2.0
    later = registrar.wait_for_refresh(timeout=window)
    if later is None:
        pytest.fail(
            f"we granted a {granted}s registration and no refresh arrived "
            f"within {window:.0f}s. RFC 3261 §10.2.4 makes the granted expiry "
            f"authoritative, so the binding has already lapsed and the device "
            f"is unreachable until it refreshes on its own schedule. Check "
            f"whether it is honouring the Expires in our 200 OK or its own "
            f"configured interval")
