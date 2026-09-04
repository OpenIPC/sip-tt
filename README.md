# sip-tt

Open-source, headless, CI- and AI-friendly **SIP conformance test tool**.

sip-tt points a scriptable SIP user agent at a device that speaks SIP — an IP
camera, a doorbell, a softphone — and reports, per numbered test purpose,
whether it behaves the way RFC 3261 and RFC 3264 say it must. It emits JUnit
XML and JSON, needs no GUI, and is meant to be run by CI and read by agents as
readily as by people.

It is the SIP counterpart of [onvif-tt](https://github.com/OpenIPC/onvif-tt),
and it exists for the same reason: interoperability defects are behavioural,
they hide from unit tests, and finding them by hand does not scale.

## Why

Six SIP defects shipped from one camera firmware in a single week. Every one
was a state-machine or offer/answer decision, every one was invisible to that
project's own in-process unit tests, and every one was found by a person with
a PBX and a packet capture:

| Defect | What the device did | What the user saw |
| --- | --- | --- |
| Payload renumbering | Answered H.264 as 96 when the offer bound it to 97 | Video connects, stays black |
| No RTCP sender reports | Never sent SR | No lip sync, no RTT |
| RTP timestamps from uptime | Media clock not randomised (RFC 3550 §5.1) | Call drops 4–6 s in |
| re-INVITE answered from cache | Replayed the first 200 OK, with its CSeq | Call drops on PBX handoff |
| Hold ignored | Kept sending through `a=sendonly` | Audio during hold |
| Ringing counted as busy | 486 while dialling out | Cannot be called back |

Every one of those has a numbered test purpose in a public specification that
predates the bug by nearly twenty years.

## Corpus

sip-tt scores against **ETSI TS 102 027-2**, "SIP conformance test
specification; Part 2: Test Suite Structure and Test Purposes" — 548 numbered
purposes, each keyed to an RFC 3261 section and carrying its own applicability
condition.

The document is **free to download but not free to redistribute**, so it is
not vendored here. `sip-tt corpus refresh` fetches and parses it on your
machine; the parsed output is gitignored. What *is* committed is
`corpus/EXPECTED.json` — the document checksum, the purpose count and the
sorted list of identifiers, all of which are facts — so CI can prove the parse
is complete and byte-stable without the repository carrying a word of ETSI's
prose.

Identifiers look like `SIP_CC_TE_SM_V_001`: call control, terminating
endpoint, session modification, valid behaviour. Tests we write ourselves,
with no counterpart in the corpus, are prefixed `LOCAL-`.

## Status

Early, and already useful. The runtime, the corpus pipeline, the runner and
twelve implementations are in; the remaining purpose families are landing
next.

On its first run against a real device — OpenIPC's majestic at `master`, on a
HiSilicon hi3516ev300 — it reproduced all six of the defects above and found
four more that nobody had reported:

| Purpose | | What the device does |
| --- | --- | --- |
| `SIP_CC_TE_CE_V_006` | Mandatory | Refuses a bodyless INVITE with 488; RFC 3261 §13.2.1 requires the answerer to make the offer in its 2xx |
| `SIP_CC_TE_SM_V_002` | Mandatory | Refuses a bodyless re-INVITE with 488; §14 makes it a request to re-offer |
| `SIP_CC_TE_SM_I_001` | Mandatory | Answers 200 OK to a re-INVITE sent while an earlier one is unanswered; §14.2 asks for 500 + `Retry-After`, or 491 |
| `SIP_CC_TE_SM_V_003` | Recommended | Never sends BYE after a 200 OK that was not ACKed (§14.1) |

`docs/validation.md` has the full record, including how to reproduce the
signalling findings on a host build with no camera, and the two bugs the first
run found in sip-tt itself.

## Scope, honestly

- sip-tt is **not** an ETSI conformance assessment and produces no declaration
  of conformance. It is a regression and interoperability harness that borrows
  a good public catalogue.
- It cannot test what the device will not let it trigger. Exercising a
  device's *originating* behaviour needs a way to make it place a call; where
  none is configured, those purposes are reported as not-run, never as passed.
- UDP and IPv4 only for now.

## Licence

MIT. See `LICENSE`.
