# jevs-home-assistant-sentinel v1.5.0

Three ways the safety boundary reported a success it had not earned. All three ran
on the ordinary path: the verifier on every case, the dispatcher on every
documented device action, the redactor on every provider call and every event it
fired.

The v1.4.2 HACS fix is merged to `main`, so the integration is installable again.
This release assumes that and fixes the boundary underneath it.

| | v1.4.2 | v1.5.0 |
| --- | --- | --- |
| test suite | 218 passed, 3 skipped | **251 passed, 3 skipped** |

---

## A readback that returned nothing was reported as a successful readback

`verifier.verify()` compared its two arguments and, on equality, returned
`{"verified": True, "status": "matched", "next_step": "close_case"}`.

The expectation arrives from a magic key inside an untyped facts dict —
`case.facts.get("expected_state")` — and `Case.create(..., facts=None)` yields
`facts={}`. So a case built without an explicit expectation had no verification
requirement at all. An entity renamed or removed between dispatch and readback
makes the readback `None` as well. `None == None` compares equal:

```python
verify(None, None)
# -> {'verified': True, 'status': 'matched', 'next_step': 'close_case'}
```

An AI-recommended `light.turn_off` is dispatched, the entity is renamed by an
unrelated automation before readback, and the case is closed as a successfully
verified device action **with zero state readback performed**.

**What changed.** Neither side being known is no longer a match. The two causes
are reported apart, because they mean different things to an operator:

```python
verify(None, None)     -> status 'no_expectation'  next_step 'notify_and_retry'
verify('off', None)    -> status 'unavailable'     next_step 'notify_and_retry'
verify('off', 'off')   -> status 'matched'         next_step 'close_case'   (unchanged)
verify('off', 'on')    -> status 'mismatch'        next_step 'reopen_case'  (unchanged)
```

---

## An async dispatcher was recorded as sent, and the device never moved

`SentinelWorkflow.execute()` accepted any callable and recorded whatever came
back:

```python
dispatch_result = dispatch(decision.action or "")
...
result["dispatch"] = {"status": "sent", "result_type": type(dispatch_result).__name__}
```

In Home Assistant every service call is async, so `dispatch` returned a
coroutine. The record read `{'status': 'sent', 'result_type': 'coroutine'}`, the
coroutine was collected without ever being awaited, and **the device never
moved**. The readback then compared the state the device was already in, and the
case closed as a successful verified action that physically did not happen.

`docs/integrations.md:66` is the documented wiring and shows exactly this shape
with `dispatch=dispatch_home_assistant_action`. `docs/faq.md:5` is also correct
that the shipped integration itself does not dispatch — the events have no
active dispatch path — so the practical exposure is documented integrators rather
than the default install. Both are recorded here rather than one being implied.

**What changed.** An awaitable result is refused rather than reported as sent:

```python
{'status': 'unsupported_dispatch', 'result_type': 'awaitable'}
verification: {'status': 'dispatch_not_performed', 'next_step': 'notify_and_retry'}
```

The coroutine is closed so it is not left for the collector. A synchronous
dispatcher still works, and a raising dispatcher still reports `status: 'error'` —
both pinned. A synchronous boundary cannot honour an awaitable, so it refuses;
escalating to an async caller is the fix, and refusing is what makes it visible.

---

## Redaction missed nine spellings and every structured form

`_SECRET_WORDS` held six words and matched them as substrings against a flattened
key. 13 spellings passed through **unredacted**, carrying their values to
OpenRouter/Cloudflare and onto the Home Assistant event bus, where every listener
can read them:

**Keys.** `private_key`, `access_key`, `key`, `passwd`, `passphrase`,
`authorisation` (British), `cookie`, `clientid`, `bearer`.

**Strings.** Every JSON form — `{"password": "hunter2"}` — because the pattern
required the colon to follow the word immediately and JSON puts a quote first.
Both `Authorization: Bearer sk-live-…` and bare `Bearer sk-live-…`, because
`bearer` was not in the pattern at all.

**What changed.** The key list is widened, and matched on *segments* rather than
substrings. That distinction is the whole design: `private_key` yields the segment
`key` while `door_pin` yields `pin`, and a door's PIN code is household data the
policy is allowed to see. Substring matching cannot express the difference —
`doorpin` contains `pin` exactly as `privatekey` contains `key` — so the suite
pins both directions, including the `NON_SECRET_KEYS` list it already carried.

The string branch allows a quoted value and adds a `Bearer` form. It runs before
the key-value form, whose wider match would otherwise consume the word `Bearer`
and leave the token itself behind it.

A prose form was tried and **removed**. Matching `the password is X` also matched
`the api key is stored in the vault`, which names no credential and which the
suite had already pinned as text that must pass through untouched. A redactor
that damages legitimate household text is its own failure, and a regex cannot tell
a value from the next word. The forms kept all follow a delimiter, which is what
makes them carry a value.

---

## Both copies of the safety layer are fixed, and pinned to agree

This repository carries the safety layer twice: `sentinel/` and a 1,454-line
duplicate inside `custom_components/jev_sentinel/runtime.py`. The second is what
Home Assistant actually loads.

The verifier and the redactor defects were fixed in the first copy, and a test
asserting the shipped copy agrees immediately failed — because it had its own
copies of both, still broken. That is precisely the failure mode
`tests/test_safety_boundaries.py:66` exists to guard, and it is why all three
regression tests run against both implementations rather than one.

**What changed.** All three fixes land in `sentinel/` and in
`custom_components/jev_sentinel/runtime.py`, and the new tests assert the two
agree on each.

---

## Verification

```
251 passed, 3 skipped        (was 218, 3 skipped)
black --check sentinel custom_components tests   clean
isort --check-only ...                            clean
compileall, json.tool, git diff --check          clean
```

The 33 new tests are in `tests/test_fail_closed_regressions.py`: the verifier's
four states plus the shipped copy, the async refusal with the coroutine actually
closed, the synchronous and raising dispatchers still behaving, twelve credential
spellings redacted, four credential string forms redacted, six household keys and
one prose sentence left alone, and the shipped copy agreeing on all of it.

Not fixed, and stated: `runtime.py` has no `execute` at all and its own `Policy`
carries no `approval_required` set, so `lock.unlock` is reported as
"not allowlisted" rather than "approval required" there — a different reason code
for the same refusal. That is a further divergence between the two copies of the
layer and is the next thing to close.
