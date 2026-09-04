# sip-tt for agents

Everything here is stable, machine-readable and safe to script against.

## Identifiers

ETSI purposes match
`^SIP_[A-Z]{2}_[A-Z]{2}(_[A-Z]{2})*_(V|I|O|TI)_\d+$` — area, entity, zero or
more function tokens, class, number. Tests with no counterpart in the corpus
are prefixed `LOCAL-`. The classes are valid, invalid, inopportune and timer.

## Enumerating work

```sh
sip-tt list --format json --compact          # everything
sip-tt list --missing --ua-only --format json  # what is left to implement
sip-tt list --role terminating --implemented
sip-tt show SIP_CC_TE_SM_V_001 --format json
```

`show` prints ETSI's own wording only when a local `corpus/parsed.json`
exists; the shipped catalogue carries identifiers, applicability and RFC
citations only.

## Running

```sh
sip-tt run --target HOST[:PORT] --user <the DUT's SIP user> \
          --local-ip <address the DUT can route back to> \
          [--id-glob 'SIP_CC_TE_*'] [--mandatory-only] \
          [--with-media] [--with-registrar] [--with-pbx] \
          [--junit-xml junit.xml] [--json-report results.json]
```

Exit codes are pytest's: 0 all clear, 1 something failed.

## results.json

```json
{
  "device":  { "host": "...", "roles": ["terminating"], "trigger": "none" },
  "summary": { "total": 12, "passed": 7, "failed": 5 },
  "results": [
    { "id": "SIP_CC_TE_CE_V_006",
      "status": "failed",
      "mandatory": true,
      "roles": ["terminating"],
      "requires": [],
      "duration_s": 0.31,
      "message": "AssertionError: a bodyless INVITE was refused 488 ...",
      "longrepr": "..." }
  ]
}
```

`message` is the assertion text on its own — read that first. `status` is one
of `passed`, `failed`, `skipped`, `xfailed`, `xpassed`.

**`skipped` means the purpose does not apply to this device** — wrong role,
PICS answered no, corpus says Void. It never means "could not run": that is a
`failed` whose message says so. Do not report a skip as a pass, and do not
report it as a defect.

## A typical loop

1. `sip-tt list --missing --ua-only --format json` — pick a purpose.
2. `sip-tt show <id>` — read what it requires. Without a local corpus you get
   the classification, the status and the RFC section, which is usually
   enough; `sip-tt corpus refresh` gets you the wording.
3. Implement it in the matching module under `src/sip_tt/cases/`, with
   `@register("<id>", roles=..., mandatory=...)`.
4. `sip-tt run --target ... --id-glob '<id>'` against a device.
5. Read `message`, and the device's own log for the same seconds.

Read CLAUDE.md before writing a case. The two rules that will otherwise bite
you: a skip means not-applicable and nothing else, and the payload numbers in
a canned offer are the test vector rather than a detail to tidy.
