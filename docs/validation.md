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

### Three findings in the terminating family

All against `master` at `b957ff3c`, none previously reported. **All three are
fixed by widgetii/majestic#577**, which is also the first evidence that this
tool changes anything.

| Purpose | Status | What the device does |
| --- | --- | --- |
| `SIP_CC_TE_CE_V_006` | Mandatory | An INVITE with no message body is refused **488**. RFC 3261 §13.2.1 allows a bodyless INVITE and requires the answerer to make the offer in its 2xx |
| `SIP_CC_TE_SM_V_002` | Mandatory | A bodyless **re**-INVITE is refused **488**. §14 makes it a request to re-offer |
| `SIP_CC_TE_SM_V_003` | Recommended | 45 s after a 200 OK that was never ACKed, no BYE. §14.1 requires the dialog to be terminated rather than the call leg held |

The first two share a cause — the device requires usable audio SDP in any
INVITE it accepts, and logs `inbound INVITE without usable audio SDP — 488`.

### A fourth, in the registrant family

`SIP_RG_RT_V_012` [Recommended] — **the device refreshes on its own schedule,
not on the one the registrar granted.**

RFC 3261 §10.2.4 makes the expiry in the 200 OK authoritative: the registrant
must refresh within *that* window, whatever it asked for. Registrars routinely
grant less than requested, to keep bindings fresh behind NAT.

Measured, against a majestic configured with `registerExpires: 120`:

| registrar granted | allowed window | device refreshed after | verdict |
| --- | --- | --- | --- |
| 45 s | 90 s | 60 s | passes |
| 20 s | 40 s | 60 s | **fails** |

The device refreshed at 60 s in both runs — it did not react to the grant at
all. `src/sip/uac.c` confirms it: the refresh timer is armed from
`u->cfg.register_expires_s`, the value in the camera's own config, and the 200
OK's `Expires` header is never parsed. The log line prints the configured
value as though it were the granted one.

The consequence is a camera that goes unreachable between refreshes whenever a
registrar grants less than it asked for — with the stock `registerExpires:
3600` against a registrar granting 60 s, the binding lapses within a minute
and stays lapsed for the best part of an hour.

Advisory rather than mandatory: the corpus marks the purpose Recommended, and
the failure mode is unreachability rather than a broken call.

**Fixed by widgetii/majestic#577**: the camera now reads the granted lifetime
— Contact parameter first, then the Expires header, which is the precedence
§10.2.4 states — and refreshes every 10 s against a 20 s grant.

The second row of that table is also the negative control for the test itself.
A conformance check that has only ever passed proves nothing, so the interval
assertion was deliberately given a window the device could not meet, and it
failed with the numbers in it.

### And one retracted

`SIP_CC_TE_SM_I_001` was reported here as a defect and was not one. The first
version of that test sent two re-INVITEs back to back inside an established
dialog and asserted that the second must be refused 500 or 491 per RFC 3261
§14.2. It reported majestic as non-conformant, and then reported baresip as
non-conformant too — two independent stacks failing the same mandatory purpose
is a reason to distrust the test, not the stacks.

The purpose is conditional on the device's INVITE server transaction being in
the **Proceeding** state: a provisional sent, no final yet. On a fast path the
device answers the first re-INVITE in microseconds, so by the time the second
arrives there is no outstanding transaction and 200 OK is exactly right. The
test was asserting a precondition it had not created.

It now drives an initial INVITE, waits for a provisional, and only then sends
the second — and it checks the arrival order afterwards, because the device
may still leave Proceeding in the microseconds between. Where the precondition
cannot be created it skips, naming the measurement: majestic answers 200 in
0 ms with no provisional, so it never enters Proceeding and the purpose does
not apply to it while it auto-answers.

The lesson is the one in CLAUDE.md §8, learnt the hard way rather than
inherited: a test that cannot establish its own precondition must say so, not
guess. Two false failures against two different implementations is what
overstating a finding looks like from the outside.

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

## A second implementation

Running only against the device the tool was written for proves very little,
so CI runs the same suite against **baresip**, a wholly independent stack.
It is also the reason the `SIP_CC_TE_SM_I_001` retraction above was caught:
a purpose that fails on two unrelated implementations is usually the test.

`10 passed, 15 skipped` on the terminating role, with the advisory purposes
left out by `--skip-tag advisory` and named in the output. Every skip is a
genuine not-applicable — fifteen of them are the originating and registrant
purposes, which that DUT profile does not claim.

Two notes worth keeping:

* baresip fails `LOCAL-MEDIA-RTP-TIMESTAMP-BASE-IS-PER-SESSION` in exactly the
  way majestic does. Two independent implementations sharing one media clock
  across consecutive calls is good evidence that the advisory registration is
  the right one, and that a mandatory one would have been wrong.
* It is baresip and not linphonec because liblinphone answers **503 Service
  unavailable** to every inbound INVITE when its core has not reached the On
  state, which in a container it does not. The log line is "Linphone core
  global state is not on", buried under a wall of ALSA errors that look like
  the cause and are not — and from the outside it is indistinguishable from a
  device that refuses calls. baresip is built for headless operation and needs
  a paired `aubridge` device (`audio_player aubridge,dut` and `audio_source
  aubridge,dut`); an unpaired one answers 200 and then fails media setup with
  "audio_decoder_set error: No such device".

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
