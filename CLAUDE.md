# CLAUDE.md

A cold-start brief for anyone — human or agent — working on sip-tt. Not a
tutorial: these are the conventions that, if you get them wrong, produce
something that looks right and is not.

## What this is

sip-tt points a scriptable SIP user agent at a device and scores it against
the numbered test purposes of ETSI TS 102 027-2. It is the SIP counterpart of
[onvif-tt](https://github.com/OpenIPC/onvif-tt) and copies its structure
deliberately, so a fix in one can be carried to the other.

```
src/sip_tt/
  cli.py          list | show | corpus refresh|verify|stats | run
  registry.py     @register, REGISTRY, discover(), match_xfail
  specs/          the corpus: models, parser, catalogue
  runtime/        the SIP stack we are: message, sdp, transaction, auth,
                  media, endpoint, profile — plus registrar (so a registrant
                  has a peer) and trigger (so an originator can be asked)
  cases/          the tests themselves, one module per role
  runner/         pytest dispatch + plugin (JSON report)
corpus/           EXPECTED.json (committed) and parsed.json (never committed)
ci/dut/           baresip as a device under test, for the integration job
fixtures/         two Asterisk generations, and a STUN responder
tests/            the tool's own tests; no device involved
```

Three roles, and a device may play any subset: **registrant** (it sends
REGISTER, so sip-tt runs a registrar), **terminating** (it answers calls),
**originating** (it places them, so the profile must declare a trigger — a
SIP UA has no control channel and something outside SIP has to press the
button).

## Conventions that matter

Each of these is a mistake that has already been made here.

### 1. A skip means "not applicable", and nothing else

This is the whole ethic. onvif-tt learnt it the expensive way: twelve of its
tests sat green for weeks having never executed, because "we could not reach
the service" was reported as a skip.

- **Skip** — the device does not play this role, the profile answers the PICS
  condition "no", the corpus marks the purpose Void.
- **Fail** — anything that stopped us finding out: no trigger where the
  purpose needs the device to originate, a fixture that did not start, the
  device unreachable, a call that could not be established. That is not the
  device's verdict and must never be recorded as one.

`_common.establish()` exists so that "the call would not set up" is reported
the second way, in every test, without each test remembering.

### 2. Never leave a call standing

Most devices allow one call at a time and answer 486 Busy Here to the next.
One test that fails before its own BYE therefore turns *every later purpose*
red for a reason that has nothing to do with what it was checking — a leaked
leg once made three passing purposes fail.

Teardown is structural: the `_leave_no_call_standing` autouse fixture calls
`Endpoint.teardown_calls()` after every purpose. Do not rely on a `try/finally`
in your test — that is exactly the discipline that fails under an assertion.
A `try/finally` is still good manners for prompt cleanup; it is not the
safety net.

### 3. The payload numbers in a canned offer are the test vector

`sdp.linphone_offer()` numbers H.264 as **97**, because that is what a real
Linphone puts on the wire. An answerer must reply with 97. If you "tidy" that
to the conventional 96, the tool starts agreeing with a broken answerer and
the defect it was written for goes unseen.

Related, and the reason a PBX belongs in the rig at all: **liblinphone
forgives a renumbered answer.** It logs `proposed number was 97 but the remote
phone answered 96`, rebuilds its decoder and plays the video. A
peer-to-peer test therefore passes while the answer is malformed. A B2BUA does
not forgive it, and drops every packet silently.

### 4. Headers repeat, and parameters are not substrings

`Via`, `Route` and `Contact` may appear more than once and the order is load
bearing — a response echoes the whole Via stack. Use `headers.all()`, not
`headers.get()`, whenever more than one is possible.

And never test for a tag with `"tag=" in header`: it matches
`To: "tag=trap" <sip:1001@cam>`. Use `msg.tag("to")`, which parses the
parameter list respecting quotes and angle brackets.

### 5. Identifiers come from the catalogue, never from memory

`@register("SIP_CC_TE_SM_V_001")` is checked against `catalog.json` by
`tests/test_registry.py`. An invented identifier reads as coverage of a
purpose nobody wrote, and `sip-tt list --missing` stops telling the truth.
Use `LOCAL-` for tests with no counterpart in the corpus, and mean it.

### 6. Do not commit ETSI's prose

ETSI deliverables carry "No part may be reproduced except as authorized by
written permission". Identifiers, applicability conditions and RFC citations
are facts and ship in `catalog.json`; the purpose *text* is ETSI's expression
and lives only in `corpus/parsed.json`, which is gitignored.
`tests/test_corpus.py` asserts this rather than leaving it to memory. If you
regenerate the catalogue, check the assertion still passes.

### 7. The corpus parser has four traps, all of them already sprung

- `TPId:` is itself a field label. Test it as a **boundary before** matching
  it as a field, or the whole document folds into one record — that parsed
  two purposes out of 609 and looked like a bad regex.
- The class token has **four** values: V, I, O and **TI**. The timer family
  alone is 234 identifiers.
- The function part **repeats** (`SIP_CC_PR_MP_RQ_V_001`).
- `pdftotext` emits stray spaces inside identifiers (`SIP_QC_OE_ V_001`) and
  interleaves page furniture with content, including mid-sentence. Both
  extraction modes must yield the same 609 purposes; `sip-tt corpus verify`
  is the check.

### 8. Say what you measured, not what you suspect

A conformance tool that overstates a finding gets ignored. If a purpose is a
SHOULD, register it non-mandatory and say so in the message.
`LOCAL-MEDIA-RTP-TIMESTAMP-BASE-IS-PER-SESSION` is the worked example: it
detects one media clock running through two calls, which is real and
checkable, but a per-process random draw is a deliberate design choice in at
least one implementation — so the assertion states what it saw, names the
weaker claim it is making, and tells the reader how to distinguish it from
the serious version of the same defect.

### 9. Establish the precondition, or say you could not

Many purposes are conditional on a state — "while a session has been
established", "when the INVITE server transaction is in the Proceeding
state". A test that assumes the state rather than creating and checking it
reports healthy devices as broken.

`SIP_CC_TE_SM_I_001` is the worked example and it cost a retraction. It
originally sent two re-INVITEs back to back and asserted the second must be
refused; on a fast path the device answers the first in microseconds, so the
second arrives with nothing outstanding and 200 OK is correct. It failed
majestic and then failed baresip, and two independent stacks failing the same
mandatory purpose is a reason to distrust the test.

If the precondition cannot be created, **skip and name the measurement** —
"answered 200 in 0 ms without a provisional, so it never enters Proceeding".
That is a genuine not-applicable. Where a race could go either way, check
afterwards which way it went (arrival order usually says) and skip when the
answer is "we do not know".

### 10. Advertise the right address

`--local-ip` is not guessed. On a host with a public interface and a tunnel,
the default route's address is often the public one; the device then sends its
media somewhere it cannot arrive from, and the symptom — a connected call with
no media — imitates the very bug under test.

### 11. Watch the path MTU

A full-codec Linphone INVITE is about 1410 bytes of SDP, which is over 1420
with an IP header, and a tunnelled lab path drops it without a word. The call
then produces *no response at all* rather than a bad one. `g711_offer()` is
small for this reason; reach for it when a device appears not to answer.

## Ask before

- Changing a device's configuration, or restarting anything on shared lab
  hardware. Back it up first and put it back afterwards.
- Adding a dependency. The runtime is stdlib sockets on purpose: a
  conformance tool must be able to send what a library would refuse to build.
- Pushing branches or opening pull requests.
