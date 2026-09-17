# Capability preparation safeguards

User decision, 2026-09-17: prevent identical preparation calls within a turn,
including after failure. Changed arguments may execute; an unchanged failed call
does not retry the provider.

- Only explicitly opted-in preparation handlers are guarded. The shipped
  `PresentCapabilityCardTool` prepares a UI option; it does not execute the
  represented action. Actual actions and unrelated tools are not cached.
- Identity is the exact tool name plus canonical JSON arguments, excluding intent
  attribution tags. Object-key order does not matter; array order, case, omitted
  fields and explicit nulls remain distinct. Arguments must not be changed merely
  to evade the guard.
- A successful repeat returns a copy of the original prepared result with its
  source result ID and a reuse notice. It does not change selection or review
  exclusions. A failed repeat remains a failure and is marked non-retryable.
- The cache is local to the invocation and seeds from this session's persisted
  attempts for the same turn and exact column scope. Resume does not grant a new
  identical attempt. A new turn or different scope does. If the original payload
  has expired or cannot be read, return unavailable rather than fabricate success
  or silently retry the provider.
- Equivalent presented cards retain their first occurrence. Identity includes
  tool name, arguments (including unresolved entities), resolved entities,
  additional arguments and execution-bearing presentation metadata. Different
  employees, dates, destinations and data definitions remain distinct. Display
  annotations do not justify duplicate cards. This is conservative structural
  comparison, not fuzzy matching of names or identities.
- `loop_repeated_capability_call_guarded` records the tool/call ID and dedup flag;
  `loop_capability_card_deduped` records a dropped count. Neither logs bindings.

Regression coverage: `tests/runtime/loop/test_capability_preparation_guard.py`.
The existing UI-owned unresolved-employee handoff and blueprint-first warehouse
fallback on availability failures remain in force. Access denials are never a
reason to bypass authorization through the fallback.
