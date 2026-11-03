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

## Tool call timeouts (2026-11-03)

- `TOOL_CALL_TIMEOUT_SECONDS` defaults to 60, must be positive and finite, and
  bounds each MCP call made through `ToolDispatcher`: the loop's own calls, every
  blueprint node, entity probes and measurement checks. It sits above the API's
  30s query limit (`max_execution_time` / Trino `query_max_run_time`), so a slow
  query still arrives as the API's typed `QUERY_RESOURCE_LIMIT`; this limit is the
  backstop for a call the API never answers.
- Every model-requested tool call is also capped by the remaining budget-window
  wall time. A runtime tool (a composed `runBlueprint` makes one MCP call per node)
  is bounded only by that window, because each of its MCP calls already has the
  per-call limit.
- A cancelled call returns `TOOL_CALL_TIMEOUT` (retryable, `INFRA_FAILED`): its
  result is unknown, never empty. A transport's own timeout keeps reporting
  `INTERNAL_TRANSPORT_ERROR`; the dispatcher tells them apart by its timer, not
  by exception type.
- A timed-out `runQuery`/`runBlueprint` receipt supports an `EXECUTION_FAILED`
  block, so the model can deliver verified results with the limitation. A
  timed-out join-cardinality probe reports `AGGREGATION_CHECK_UNAVAILABLE`: the
  runtime wrote the probe, so there is no SQL for the model to correct.
- The agent does not rely on its cancellation to stop a query the API already
  started; the API's own query limit ends it. Context assembly and finalization
  persistence still have no deadline.

Regression coverage: `tests/runtime/dispatch/test_tool_call_timeout.py`, including
a real `RealMCPClient` against a server that accepts the connection and never
answers.
