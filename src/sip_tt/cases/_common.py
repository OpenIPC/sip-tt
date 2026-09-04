"""Shared scaffolding for the case modules."""

from __future__ import annotations

import pytest

from ..runtime import sdp as _sdp
from ..runtime.endpoint import Call, Endpoint
from ..runtime.profile import Profile


def require_target(profile: Profile) -> None:
    if not profile.host:
        pytest.fail("no --target given, so nothing was put to any device. "
                    "Not a skip: a skip would claim this purpose does not "
                    "apply.")


def call_uri(profile: Profile) -> str:
    return profile.uri


def establish(endpoint: Endpoint, profile: Profile, *, body: str | None = None,
              media: bool = False, timeout: float = 10.0) -> Call:
    """Place a call and take it to the confirmed state, or fail saying why.

    Returns an ACKed dialog. Every test that needs "a session has been
    established" starts here, and a failure to get one is reported as an
    inability to run the purpose rather than as the purpose failing.
    """
    require_target(profile)
    ip = profile.local_ip or endpoint.advertise_ip

    audio_port = video_port = 0
    call_media = None
    if media or body is None:
        call_media = True

    if body is None:
        # Open real sockets first so the offer advertises ports we are
        # actually listening on — an offer naming a closed port is a test of
        # our own plumbing, not of the device.
        tmp = Call(endpoint=endpoint, call_id="", local_uri="", remote_uri="",
                   local_tag="")
        tmp.open_media()
        audio_port, video_port = tmp.audio.port, tmp.video.port
        body = _sdp.linphone_offer(ip, audio_port, video_port)

    call = endpoint.place_call(call_uri(profile), profile.target, body=body,
                               timeout=timeout)
    resp = call.last_response
    if resp is None:
        pytest.fail(f"no final response to INVITE within {timeout:.0f}s from "
                    f"{profile.host}:{profile.port}; the device may be "
                    f"unreachable or the offer may exceed the path MTU "
                    f"({len(body)} bytes of SDP)")
    if not (200 <= resp.status < 300):
        pytest.fail(f"INVITE was answered {resp.status} {resp.reason}, so no "
                    f"session was established and the purpose could not be "
                    f"exercised")

    if call_media and call.audio is None:
        call.audio = tmp.audio
        call.video = tmp.video
    call.ack()
    return call


def sdp_of(msg) -> _sdp.Sdp:
    if not msg.body:
        pytest.fail(f"{msg.start_line()} carried no session description")
    return _sdp.parse(msg.body)


def remote_media_target(call: Call, kind: str) -> tuple[str, int] | None:
    """Where the device asked us to send `kind`, per its answer."""
    if call.remote_sdp is None:
        return None
    m = call.remote_sdp.get(kind)
    if m is None or not m.active:
        return None
    return (call.remote_sdp.address_for(m), m.port)
