"""Originating endpoint: what a device must put in the INVITE it sends.

These need the device to place a call, and a SIP user agent answers to nobody
— so every purpose here carries `requires={"trigger"}` and the profile has to
declare an out-of-band lever. Where it declares none, the runner fails them as
not-exercised rather than skipping, because "we could not ask" is not a
verdict about the device.

The whole family shares one setup, so the module places a single call and the
purposes assert different things about the same INVITE. That keeps a run to
one call rather than eleven, which matters on a device that rings a real
handset.
"""

from __future__ import annotations

import pytest

from ..registry import register
from ..runtime import sdp as _sdp
from ..runtime import trigger as _trigger
from ..runtime.message import split_params

_CACHE: dict[str, object] = {}


def outbound_invite(endpoint, profile, timeout: float = 30.0):
    """Make the device call us, and return the INVITE it sent.

    The *message* is cached for the session, because the header purposes all
    inspect one INVITE and ringing a device eight times to read eight headers
    is antisocial on real hardware. The *call* is not cached and must not be:
    teardown runs after every purpose, so by the second test the dialog this
    INVITE created is long gone. A purpose that needs a live call calls
    `fresh_outbound_call` instead — which is what SIP_CC_OE_CR_V_003 has to
    do, and getting that wrong made a trigger that toggles place a second
    call where a hangup was intended.
    """
    cached = _CACHE.get("invite")
    if cached is not None:
        return cached
    invite, _ = fresh_outbound_call(endpoint, profile, timeout)
    _CACHE["invite"] = invite
    return invite


def fresh_outbound_call(endpoint, profile, timeout: float = 30.0):
    """Trigger a call, answer it, and return (invite, call) with it live."""
    try:
        what = _trigger.fire(profile.trigger)
    except _trigger.TriggerError as e:
        pytest.fail(f"could not ask the device to place a call: {e}")

    invite = endpoint.wait_for_request("INVITE", timeout=timeout)
    if invite is None:
        pytest.fail(
            f"the trigger succeeded ({what}) but no INVITE reached "
            f"{endpoint.advertise_ip}:{endpoint.port} within {timeout:.0f}s. "
            f"Check the device's call target points here, and that the "
            f"address we advertise is one it can route to")

    # Answer it, so the device reaches a confirmed dialog and the release
    # purposes have something to release.
    ip = profile.local_ip or endpoint.advertise_ip
    offer = _sdp.parse(invite.body) if invite.body else None
    body = ""
    if offer is not None:
        media = []
        for m in offer.media:
            if not m.active:
                continue
            pt = m.formats[0].pt if m.formats else 0
            fmt = _sdp.Format(pt, m.formats[0].name if m.formats else "",
                              m.formats[0].clock if m.formats else 0)
            media.append(_sdp.Media(m.kind, 40000 + 2 * len(media), [fmt],
                                    _sdp.Direction.SENDRECV))
        body = _sdp.build(address=ip, media=media)

    endpoint.respond(invite, 180, "Ringing")
    call = endpoint.accept_call(invite, body)
    endpoint.wait_for(lambda m: m.is_request and m.method == "ACK"
                      and m.call_id == invite.call_id, timeout=5.0)
    return invite, call


@register("SIP_CC_OE_CE_V_001", roles={"originating"}, mandatory=True,
          requires={"trigger"})
def invite_carries_the_mandatory_headers(endpoint, profile):
    """An INVITE carries To, From, CSeq, Call-ID, Max-Forwards, Contact, Via.

    RFC 3261 §8.1.1 makes these the minimum set. Contact is the one devices
    forget, and its absence is not cosmetic: without it the callee has no
    target for in-dialog requests, so the BYE that ends the call goes to the
    Request-URI and may never arrive.
    """
    inv = outbound_invite(endpoint, profile)
    missing = [h for h in ("to", "from", "cseq", "call-id", "max-forwards",
                           "contact", "via") if not inv.headers.get(h)]
    assert not missing, (
        f"the INVITE omits {', '.join(sorted(missing))}. RFC 3261 §8.1.1 "
        f"makes all of To, From, CSeq, Call-ID, Max-Forwards, Contact and Via "
        f"mandatory in a request")


@register("SIP_CC_OE_CE_V_002", roles={"originating"}, mandatory=False,
          requires={"trigger"})
def request_uri_matches_the_to_header(endpoint, profile):
    """The Request-URI should be the URI in To.

    RFC 3261 §8.1.1. They diverge legitimately once a route set is in play,
    but on a first INVITE from a device with a pre-configured target they are
    the same, and a mismatch usually means the device dialled its proxy while
    naming the callee.
    """
    inv = outbound_invite(endpoint, profile)
    to_uri = _uri_of(inv.headers.get("to"))
    if inv.headers.get("route"):
        pytest.skip("the INVITE carries a Route header, so the Request-URI "
                    "legitimately differs from To")
    assert inv.uri.lower() == to_uri.lower(), (
        f"the Request-URI is {inv.uri!r} and To is {to_uri!r}; §8.1.1 makes "
        f"them the same on a request outside a route set")


@register("SIP_CC_OE_CE_V_003", roles={"originating"}, mandatory=True,
          requires={"trigger"})
def invite_to_header_has_no_tag(endpoint, profile):
    """The To header of an initial INVITE carries no tag.

    RFC 3261 §8.1.1.2. The callee supplies the To tag in its response, and
    that is what completes the dialog identifier. A caller that invents one
    has named a dialog that does not exist, and the callee's own tag then
    disagrees with it.
    """
    inv = outbound_invite(endpoint, profile)
    assert inv.tag("to") == "", (
        f"the initial INVITE carries To tag {inv.tag('to')!r}. §8.1.1.2 "
        f"leaves it out — the callee's response supplies it")


@register("SIP_CC_OE_CE_V_004", roles={"originating"}, mandatory=True,
          requires={"trigger"})
def invite_from_header_has_a_tag(endpoint, profile):
    """The From header of an INVITE carries a tag.

    RFC 3261 §8.1.1.3. It is half the dialog identifier and must be random
    enough not to collide; a missing one leaves the dialog unidentifiable.
    """
    inv = outbound_invite(endpoint, profile)
    tag = inv.tag("from")
    assert tag, ("the INVITE's From header carries no tag parameter; §8.1.1.3 "
                 "requires one, and it is half the dialog identifier")
    assert len(tag) >= 4, (
        f"the From tag is {tag!r}, which is too short to be the 32 bits of "
        f"randomness §19.3 asks for — collisions between two calls become "
        f"likely")


@register("SIP_CC_OE_CE_V_005", roles={"originating"}, mandatory=True,
          requires={"trigger"})
def invite_cseq_method_matches(endpoint, profile):
    """The CSeq method field says INVITE.

    RFC 3261 §8.1.1.5. It is how a response is matched to the request that
    caused it when a CANCEL shares the transaction, so a wrong one makes the
    two indistinguishable.
    """
    inv = outbound_invite(endpoint, profile)
    assert inv.cseq_method == "INVITE", (
        f"the CSeq header reads {inv.headers.get('cseq')!r}; its method field "
        f"must match the request method (§8.1.1.5)")
    assert inv.cseq_number >= 0, (
        f"the CSeq sequence number in {inv.headers.get('cseq')!r} is not a "
        f"number")


@register("SIP_CC_OE_CE_V_006", roles={"originating"}, mandatory=False,
          requires={"trigger"})
def invite_max_forwards_is_seventy(endpoint, profile):
    """Max-Forwards should start at 70.

    RFC 3261 §8.1.1.6. Lower values are legal and shrink the number of proxies
    a call can traverse; 70 is the value the RFC names, and a device shipping
    something small fails only in deployments with a long proxy chain, which
    is the worst kind of bug to find in the field.
    """
    inv = outbound_invite(endpoint, profile)
    value = inv.headers.get("max-forwards")
    assert value.strip().isdigit(), (
        f"Max-Forwards is {value!r}, which is not a number")
    assert int(value) == 70, (
        f"Max-Forwards is {value}; §8.1.1.6 names 70")


@register("SIP_CC_OE_CE_V_007", roles={"originating"}, mandatory=True,
          requires={"trigger"})
def invite_via_is_well_formed(endpoint, profile):
    """Via names SIP/2.0 and a branch beginning z9hG4bK.

    RFC 3261 §8.1.1.7. The magic cookie is what tells a peer the branch is
    globally unique rather than RFC 2543's non-unique one; without it a proxy
    falls back to comparing whole headers to match transactions, and a
    retransmission can be taken for a new request.
    """
    inv = outbound_invite(endpoint, profile)
    via = inv.headers.get("via")
    assert via, "the INVITE carried no Via header"
    sent_protocol = via.split()[0] if via.split() else ""
    assert sent_protocol.upper().startswith("SIP/2.0/"), (
        f"the Via sent-protocol is {sent_protocol!r}; §8.1.1.7 wants "
        f"SIP/2.0/<transport>")
    branch = inv.branch
    assert branch, "the Via header carries no branch parameter"
    assert branch.startswith("z9hG4bK"), (
        f"the branch is {branch!r}. §8.1.1.7 requires the magic cookie "
        f"z9hG4bK, which is what marks the branch as globally unique")
    assert len(branch) > len("z9hG4bK"), (
        "the branch is the magic cookie and nothing else, so it identifies "
        "no transaction")


@register("SIP_CC_OE_CR_V_003", roles={"originating"}, mandatory=True,
          requires={"trigger"})
def bye_reuses_the_call_id_and_from(endpoint, profile):
    """A BYE stays inside the dialog its INVITE created.

    RFC 3261 §12.2.1.1: same Call-ID, same From (tag included). A BYE that
    draws fresh ones names a dialog nobody has, and the callee answers 481
    Call/Transaction Does Not Exist while the caller believes the call ended.
    """
    # A live call of its own: the cached INVITE's dialog was torn down after
    # whichever purpose established it.
    inv, _call = fresh_outbound_call(endpoint, profile)
    try:
        _trigger.fire(profile.trigger, hangup=True)
    except _trigger.TriggerError as e:
        pytest.fail(f"could not ask the device to hang up: {e}")

    bye = endpoint.wait_for(
        lambda m: m.is_request and m.method == "BYE"
        and m.call_id == inv.call_id, timeout=15.0)
    if bye is None:
        any_bye = endpoint.wait_for(
            lambda m: m.is_request and m.method == "BYE", timeout=0.1)
        if any_bye is not None:
            pytest.fail(
                f"the device sent a BYE with Call-ID {any_bye.call_id!r}, but "
                f"the dialog's is {inv.call_id!r}. §12.2.1.1 keeps the "
                f"Call-ID; a fresh one names a dialog the callee does not "
                f"have, and is answered 481")
        pytest.fail("the device was asked to hang up and sent no BYE within "
                    "15s")

    assert bye.tag("from") == inv.tag("from"), (
        f"the BYE's From tag is {bye.tag('from')!r} and the INVITE's was "
        f"{inv.tag('from')!r}; §12.2.1.1 keeps the local tag for the life of "
        f"the dialog")
    assert bye.cseq_method == "BYE", (
        f"the BYE's CSeq method reads {bye.cseq_method!r}")
    assert bye.cseq_number > inv.cseq_number, (
        f"the BYE carries CSeq {bye.cseq_number} and the INVITE carried "
        f"{inv.cseq_number}; §12.2.1.1 increments the local sequence number "
        f"for each new request in the dialog")
    endpoint.respond(bye, 200, "OK")


def _uri_of(header_value: str) -> str:
    start = header_value.find("<")
    if start >= 0:
        end = header_value.find(">", start)
        if end > start:
            return header_value[start + 1:end]
    body, _ = split_params(header_value)
    return body.strip()
