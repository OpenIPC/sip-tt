# Contributing to sip-tt

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/pytest tests/            # the tool's own tests; no device needed
.venv/bin/sip-tt corpus stats
```

To read the ETSI purposes locally — the repository does not carry them:

```sh
sudo apt-get install poppler-utils      # for pdftotext
sip-tt corpus refresh                   # downloads, parses, writes a
                                        # gitignored corpus/parsed.json
sip-tt corpus verify                    # checks it against EXPECTED.json
```

## Running against a device

```sh
sip-tt run --target 192.0.2.10:5060 --user 1001 --local-ip 192.0.2.1 \
          --with-media --json-report results.json
```

`--local-ip` is required in practice: see CLAUDE.md §9.

## Pull requests

- CI green on Python 3.10–3.13.
- New implementations use the **real** identifier from the catalogue, or a
  `LOCAL-` one. `sip-tt list --missing` shows what is left.
- Mark `mandatory` honestly — it comes from the corpus's own `Status`, and a
  SHOULD is not a MUST.
- `xfail_on` is for a device behaviour you have actually observed, with the
  observation written into the reason. It is **not** for a flaky test; if a
  test is flaky, fix the test.
- If you change the parser, re-run `sip-tt corpus verify` and say in the PR
  what the counts were before and after.

## Bug reports

Include the device's make, model and firmware, the failing identifier, and the
`results.json` record — especially its `message` field. A `sip-tt run` with
`--id-glob` narrowed to the one purpose, plus the device's own SIP log for the
same seconds, is usually enough to diagnose without the hardware.

## Style

Python ≥ 3.10, stdlib only in `runtime/`. Type hints encouraged, not
mandatory. Module docstrings should argue why the module exists and why the
obvious alternative was not taken; comments should pin down ordering and
failure modes. Assertion messages state observed versus expected, and name the
next action.

By contributing you agree your work is licensed under the MIT licence.
