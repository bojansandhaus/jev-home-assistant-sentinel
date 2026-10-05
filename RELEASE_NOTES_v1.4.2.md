# v1.4.2

Three defects in the privacy-critical path, all with the same shape: the code
claimed a property it did not enforce. Found by review, reproduced, fixed, and
each pinned by a test that fails against the previous release.

## 1. The local-failure breaker never healed

`LOCAL_FALLBACK_FAILURE_LIMIT` stops a dead local server from turning every
household case into remote traffic. Three consecutive local failures that
qualified for the hosted fallback still fall back; the fourth and every one
after it were suppressed and the local error re-raised.

That suppression had no end. The counter was a process global with no clock, so
a local server that came back stayed suppressed until Home Assistant restarted.
Because the breaker is a process global and not per-entry, two config entries
share it: while one entry's local server was dead, the other entry could be
denied its fallback even though its own local server was healthy, and would keep
being denied indefinitely.

The breaker now decays. After `LOCAL_FALLBACK_COOLDOWN_SECONDS` (300 s) with no
new failure the count returns to zero, so a recovered server is retried. A
successful local call resets it immediately, as before. A fresh failure restarts
the cooldown, so a server that is still down does not decay away.

Applied identically to `custom_components/jev_sentinel/runtime.py` and the
`sentinel/` package copy, which is how it already worked for the other fixes in
this repository.

## 2. `auto` gained a local hop nobody asked for

`auto` is documented as resolving to `api_only` unless a local model was
explicitly named. `resolve_auto_mode` implements exactly that, and the entrypoint
passed `local_model` through correctly.

The defect was one layer up, in the config form. It pre-filled the field with
`default=LOCAL_MODEL`, and read it unconditionally:

```python
local_model = user_input.get(LOCAL_MODEL_FIELD)   # always "laya"
```

`local_model_configured` treats any non-`None` value as an explicit choice. A
pre-filled default is indistinguishable from a deliberate one, so **every** `auto`
entry silently resolved to `api_with_local_fallback`, the opposite of what the
mode promises, whether or not the operator ever touched the field.

Two changes, defence in depth:

- The field now defaults to empty, so opting in is an actual action.
- A stored value equal to the default is normalised to `None` on submit, so an
  entry created by v1.4.1 or earlier cannot keep the wrong behaviour. A value the
  operator typed that happens to equal the default is also normalised, which is
  the right trade: `laya` is the default name, so naming it explicitly is not a
  different choice.

## 3. The breaker logged the opposite of what it did

`note_local_failure` logged `"considering the hosted fallback"` **before**
comparing the count against the limit. Every suppressed call therefore logged
that it was about to fall back remotely, and then did not. Anyone debugging an
outage from the log was told the opposite of the truth, on exactly the calls that
matter.

The message now states which of the two happened, and when the breaker resets:

```
local laya decision failed (RuntimeError); suppressing the hosted fallback,
local failure 4 exceeds the limit of 3. The local error is raised instead.
The breaker resets after 300 s without a new failure, or as soon as a local
call succeeds.
```

Still only the exception class name is logged, never the case, the entity state,
the answer, or the exception message. A test pins that a secret inside an
exception message never reaches a log record.

## Verification

```
pytest                    218 passed, 3 skipped
  new file                23 passed  tests/test_breaker_and_auto_mode.py
black --check             20 files unchanged
isort --check             clean
translations              byte-identical, untouched
secret scan of diff       0
```

The ten tests covering the cooldown, the two honest log messages and the form
defaults were run against the previous release and fail there; all pass here. The
rest of the suite was green before and after.

## Notes for upgrading

Existing `auto` entries need no action. Their stored `local_model` of `laya` is
normalised on the next submit, and any entry that already has a working local
server keeps working: `local_with_api_fallback` is unaffected, since it names the
local hop itself.

If you are relying on `auto` having put Laya in the chain on v1.4.1 without
naming a model, that was the bug. Set the local model field explicitly, or select
`api_with_local_fallback`, and the behaviour is explicit rather than accidental.
