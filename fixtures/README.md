# Fixtures

Two PBXs and a STUN responder. They are here because some defects only appear
when a device is talking to a real B2BUA, and because the two Asterisk
generations find *different* defects — shipping only one would quietly halve
the coverage.

## Which rig finds what

| | `asterisk13` | `asterisk18` |
| --- | --- | --- |
| Channel driver | `chan_sip` | `res_pjsip` |
| Direct media | **on** (`canreinvite` left at its default) | **off** (`direct_media=no`) |
| Finds | Everything that happens when the PBX takes itself out of the media path a few seconds in: re-INVITE handling, media re-pointing, timestamp and SSRC continuity | Payload-number bugs, because a B2BUA holds each leg to what it negotiated on that leg and silently drops anything else |

`chan_sip` was removed from Asterisk after 16, and no current distribution
ships it, so `asterisk13` builds **13.19.1 from source on ubuntu:18.04**. That
is deliberate and slow (a few minutes); it is also the configuration a great
many deployed PBXs are still running, and `canreinvite=yes` is its default.

## Why a PBX at all

Because a peer-to-peer test passes anyway. liblinphone forgives a renumbered
answer — it logs `proposed number was 97 but the remote phone answered 96`,
rebuilds its decoder and plays the video. A direct softphone-to-device call
therefore looks perfect while the answer is malformed, and the same device
goes black the moment a B2BUA is in the path.

## Running them

```sh
docker build -t sip-tt-ast13 fixtures/asterisk13
docker run -d --name ast13 --network host \
    -v "$PWD/fixtures/asterisk13/conf:/etc/asterisk" sip-tt-ast13

docker build -t sip-tt-ast18 fixtures/asterisk18
docker run -d --name ast18 --network host \
    -v "$PWD/fixtures/asterisk18/conf:/etc/asterisk" sip-tt-ast18
```

Both configs define peer `1001` for the device under test (password
`camera123`) and `223` for sip-tt. Change the addresses marked with a comment
before use — they are `192.0.2.x` placeholders, not working values.

Watch what is actually on the wire:

```sh
docker exec ast13 asterisk -rx "sip set debug on"
docker exec ast18 asterisk -rx "pjsip set logger on"
docker exec ast18 asterisk -rx "pjsip show contacts"   # did it register?
```

## ministun.py

A 25-line RFC 5389 binding responder. liblinphone otherwise advertises the
address of its default route, which on a host with both a public interface and
a tunnel is the public one; the device then sends media somewhere it cannot
arrive from, and the symptom — a connected call with no media — imitates the
bug being hunted. Only needed when a softphone is in the rig; sip-tt itself
takes its address from `--local-ip`.
