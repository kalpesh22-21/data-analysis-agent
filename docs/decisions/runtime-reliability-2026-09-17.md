# Runtime timeout and cancellation policy

User decision, 2026-09-17: a timed-out model call finishes the turn with a clear
timeout message, preserving eligible completed results rather than asking whether
to continue.

- `MODEL_CALL_TIMEOUT_SECONDS` defaults to 120, must be positive and finite, and
  bounds each main-loop model call. The effective limit is shortened to the
  remaining budget-window wall time. This is cooperative asyncio cancellation,
  not a process-level deadline for code that suppresses cancellation.
- Timeout finishes through the existing ship guard and persistence path. It
  retains eligible selected tables and existing successful SQL metadata, does
  not promote merely prepared capabilities, and does not clear persisted judge
  refusals. The timeout notice survives any guard rewrite. Pending intents become
  blocked with the existing `ENFORCEMENT_EXHAUSTED` runtime reason, never "no data".
- External cancellation on `run` or `resume` is re-raised, never converted into a
  successful answer. Explicit yield points precede rounds and each dispatch.
  Completed writes are not rolled back; consumed resume checkpoints retain their
  existing semantics. Automatic recovery of a cancelled resume is separate work.
- `loop_model_call_timeout` records elapsed seconds, effective limit and iteration.
  `loop_turn_aborted` records phase and iteration once per cancelled invocation.
  Cancellation closes manual tracing spans with an error status and a fixed
  `cancelled` reason. Diagnostics contain no prompt, SQL, provider exception text,
  or employee data.

Regression coverage: `tests/runtime/loop/test_runtime_reliability.py`, exercising
the real loop/store with hanging model/tool doubles. This change does not add a
deadline to tool execution, context assembly or finalization persistence.
