# Does sip-tt actually detect anything?

A conformance tool that has never caught a defect is not verified, it is
merely green. This is the record of what sip-tt has found, on what, and how to
reproduce it.

## The reference device

[majestic](https://github.com/OpenIPC/majestic), OpenIPC's camera streamer, at
`master` (`0575f652`), on a HiSilicon hi3516ev300. It is a good reference
precisely because its SIP stack is young and six interoperability defects were
found in it by hand in the first week of September 2026 — the tool exists so
that the seventh is found by CI instead.

## Run of 2026-09-04

Twelve implementations, against the camera, media included:

```sh
sip-tt run --target 10.216.128.34:5060 --user 1001 \
          --local-ip 10.216.135.2 --local-port 5062 --with-media \
          --json-report results.json
```

`7 passed, 5 failed`.

### The six known defects, all of which now pass

Each of these was a real shipped bug, fixed between 1 and 4 September 2026.
They are the tool's regression floor: if one of them goes red again, something
regressed.

| Purpose | The defect it re-detects |
| --- | --- |
| `LOCAL-SDP-ANSWER-KEEPS-OFFERED-PAYLOAD-NUMBERS` | Answered H.264 as 96 when the offer bound it to 97. Video connected and stayed black behind a PBX |
| `LOCAL-MEDIA-RTCP-SENDER-REPORTS` | No RTCP SR at all, so no lip-sync and no RTT |
| `LOCAL-MEDIA-RTP-TIMESTAMPS-SURVIVE-A-REINVITE` | Media clock restarted on a direct-media handoff; ortp read the span as millions of ms of jitter and dropped the call at 4–6 s |
| `SIP_CC_TE_SM_V_001` | re-INVITE answered from the cached first 200 OK, carrying the first INVITE's CSeq |
| `LOCAL-SDP-HOLD-IS-HONOURED` | `a=sendonly` ignored; audio kept flowing into a held call |
| `SIP_CC_TE_CE_V_001` | (Never broken, but the common cause when everything else fails) |

### Four findings the tool made on its own

All against current `master`, none previously reported.

| Purpose | Status | What the device does |
| --- | --- | --- |
| `SIP_CC_TE_CE_V_006` | Mandatory | An INVITE with no message body is refused **488**. RFC 3261 §13.2.1 allows a bodyless INVITE and requires the answerer to make the offer in its 2xx |
| `SIP_CC_TE_SM_V_002` | Mandatory | A bodyless **re**-INVITE is refused **488**. §14 makes it a request to re-offer |
| `SIP_CC_TE_SM_I_001` | Mandatory | A re-INVITE sent while an earlier one is still unanswered is answered **200 OK**. §14.2 asks for 500 with a `Retry-After` between 0 and 10 s, or 491 Request Pending |
| `SIP_CC_TE_SM_V_003` | Recommended | 45 s after a 200 OK that was never ACKed, no BYE. §14.1 requires the dialog to be terminated rather than the call leg held |

The first two share a cause — the device requires usable audio SDP in any
INVITE it accepts, and logs `inbound INVITE without usable audio SDP — 488`.
The third means two re-INVITEs in flight both get 200 OK, which leaves the
two ends disagreeing about which offer is in force.

### One advisory finding, and why it is only advisory

`LOCAL-MEDIA-RTP-TIMESTAMP-BASE-IS-PER-SESSION` failed: two consecutive calls
were 24 800 ticks apart at 8 kHz over a 3.1 s gap — two ticks off a clock
running continuously through both.

That is real and measurable, and it is **not** the serious version of the
defect. majestic draws its base once per process from `/dev/urandom`, and says
so in a comment: *"One draw for the life of the process, so every stream
shares it."* The observed base was 1 282 812 300 against a device uptime of
49 441 s, which at 8 kHz would put an uptime-derived base near 395 528 000 —
so the base is genuinely random and leaks nothing.

RFC 3550 §5.1 is a SHOULD, and one timeline per device makes a device's own
audio and video trivially comparable. What it costs is that consecutive calls
are correlated and the offset between them is the real elapsed time. So the
purpose is registered non-mandatory, and its assertion says which of the two
things it saw. This distinction is the point of CLAUDE.md §8.

## Reproducing without hardware

Most of the signalling findings reproduce against a host build. majestic reads
`/etc/majestic.yaml` and has no flag for another path, so it wants a container:

```sh
cmake -B /tmp/mjbuild -S <majestic> -DVENDOR=Video4Linux -DENABLE_SIP=ON \
      -DDISABLE_AUDIO=ON -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON
cmake --build /tmp/mjbuild -j8

# same distro as the host, with the host's libraries, because majestic links
# a forked libevent and an mbedTLS built with DTLS-SRTP
docker run -d --name mj --network host -v /tmp/mj:/mj \
    -v /usr/lib/x86_64-linux-gnu:/hostlib:ro ubuntu:26.04 \
    bash -c 'export LD_LIBRARY_PATH=/hostlib
             cp /mj/majestic.yaml /etc/majestic.yaml && exec /mj/majestic'

sip-tt run --target 127.0.0.1:5070 --user 1001 --local-ip 127.0.0.1
```

`sip.doRegister: false` still resolves `sip.server`, so point it somewhere
that resolves or start-up fails with `sip uac: cannot resolve`.

The four signalling findings all reproduce this way. The media purposes do
not: a host build with no SDK has nothing to send, and `answered audio and
sent none` in that configuration is the rig, not the device.

## Two defects the first run found in sip-tt itself

Worth recording, because both are the kind that make a *tool* lie rather than
a device.

**A leaked call leg cascades.** A test that failed before its BYE left a call
standing; majestic allows one at a time and answered 486 to the next purpose,
so one failure turned three passing purposes red with a message about
busy-ness that had nothing to do with them. Teardown is now structural
(CLAUDE.md §2).

**The report threw the finding away.** `longrepr` was truncated to its first
4000 characters, and pytest puts the assertion message at the *end*, after a
fixture dump. Every record kept the useless half. Records now carry an
extracted `message`, and keep longrepr's tail.
