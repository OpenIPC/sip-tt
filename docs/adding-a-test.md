# Adding a test

## 1. Find the real identifier

```sh
sip-tt list --missing --role terminating
sip-tt show SIP_CC_TE_CR_V_002
```

Do not invent one. `tests/test_registry.py` checks every non-`LOCAL-`
identifier against the catalogue, and CI fails on an invented one.

If what you want to check has no counterpart in the corpus — anything about
the media, mostly — use a `LOCAL-` identifier that describes the property, not
the bug: `LOCAL-MEDIA-RTCP-SENDER-REPORTS`, not `LOCAL-BUG-563`.

## 2. Pick the module

| Module | Purposes |
| --- | --- |
| `cases/registration.py` | `SIP_RG_RT_*` |
| `cases/terminating.py` | `SIP_CC_TE_*` |
| `cases/originating.py` | `SIP_CC_OE_*` |
| `cases/message_proc.py` | `SIP_MG_*`, `SIP_QC_*` |
| `cases/media.py` | `LOCAL-MEDIA-*` |

## 3. Write it

```python
@register("SIP_CC_TE_CR_V_002", roles={"terminating"}, mandatory=True)
def bye_is_answered_200(endpoint, profile):
    """One line on what the device must do.

    Then why it matters — what a user sees when it is wrong. If the purpose
    is a SHOULD, say so here and register it non-mandatory.
    """
    call = establish(endpoint, profile)
    resp = call.bye(timeout=5.0)
    assert resp is not None, "no response to BYE within 5s"
    assert resp.status == 200, (
        f"BYE was answered {resp.status} {resp.reason}; RFC 3261 §15.1.2 "
        f"requires 200 to a BYE inside a confirmed dialog")
```

Your function may take any subset of `endpoint`, `profile`, `spec`, `device`
and `request` — they are matched by name.

Decorator options:

| Option | Meaning |
| --- | --- |
| `roles={...}` | `registrant`, `originating`, `terminating`. A device that does not play the role skips |
| `mandatory=` | From the corpus's `Status`. Be honest |
| `requires={...}` | `registrar`, `pbx`, `media`, `trigger`. Absent → the purpose **fails** as not-exercised, not skips |
| `pbx=` | `"asterisk13"` or `"asterisk18"` when the purpose needs one specifically |
| `tags={...}` | Free-form; `slow` and `advisory` are in use |
| `xfail_on=[...]` | A device behaviour you have observed, with the observation in `reason` |

## 4. Rules that are not obvious

- **Start from `establish()`.** It reports "the call would not set up" as a
  failure to exercise the purpose rather than as the purpose failing.
- **Do not clean up by hand.** `Endpoint.teardown_calls()` runs after every
  purpose. A `try/finally` for prompt cleanup is fine; it is not the net.
- **Assert with a message, always.** Observed versus expected, the RFC
  section, and what the reader should do next.
- **Do not renumber a canned offer.** See CLAUDE.md §3.

## 5. Check it

```sh
.venv/bin/pytest tests/                                    # the tool
sip-tt run --target <dut> --id-glob 'SIP_CC_TE_CR_V_002'   # the device
```

In the PR, say which device and firmware you ran against and what it did.
