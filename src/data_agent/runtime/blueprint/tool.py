"""blueprint/tool.py — `RunBlueprintTool`, the model-facing runtime tool (§5, Slice B).

A `RuntimeTool` (the `resolveValues`/read-tool shape): intercepted in the agent
loop, never dispatched to the MCP under its own name, counts as exactly ONE
`tool_calls_made` (the DAG's inner runQuery probes are the tool's implementation,
invisible to the loop's budget, §2.7). It is a THIN wrapper over
`BlueprintExecutor`:

  - one `TOOL` span (`slot_bindings` values REDACTED, §5.5) + symmetric
    start/ok/error progress;
  - a B4-parity crash guard so a raising executor NEVER aborts the turn or leaks
    `str(exc)` (§5.4 `RUN_BLUEPRINT_INTERNAL_ERROR`);
  - maps the executor's `ExecOutcome`:
      `Completed` → ok `ToolResult` (verified result + preview, union provenance);
      `Paused`    → a `ToolResult` carrying a `ToolPause` (the §2.5 loop seam —
                    a slot-resolution `askUser`);
      `Failed`    → error `ToolResult` (runBlueprint-family code OR an inner
                    denial passed through verbatim, §5.4) → the raw-loop fallback.

D49 (absolute): NO LLM runs inside this tool — every resolver is pure code +
scope-enforced probe queries. The D56 LLM *review* is the loop's NEXT ordinary
round-trip (the tool returns the verified result + assertion outcomes), never a
nested call.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from data_agent.runtime.dispatch.tool_dispatcher import (
    ToolObserver,
    ToolPause,
    ToolResult,
    _default_observer,
)
from data_agent.runtime.observability import tracing
from data_agent.runtime.observability.redaction import redact_tool_args

from .executor import (
    BlueprintExecutor,
    ExecCompleted,
    ExecFailed,
    ExecPaused,
)

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

    from data_agent.runtime.auth.credentials import RuntimeCredentials

TOOL_NAME = "runBlueprint"

INVALID_ARGS_CODE = "RUN_BLUEPRINT_INVALID_ARGS"
INTERNAL_ERROR_CODE = "RUN_BLUEPRINT_INTERNAL_ERROR"
_INVALID_ARGS_MESSAGE = "runBlueprint needs a blueprint 'id' and a 'slot_bindings' object."
_INTERNAL_ERROR_MESSAGE = "The fast path hit an internal error — answer from the raw tools instead."

_logger = logging.getLogger(__name__)


class RunBlueprintTool:
    """The `runBlueprint(id, slot_bindings)` runtime tool (§5.1)."""

    tool_name = TOOL_NAME

    def __init__(
        self,
        *,
        executor: BlueprintExecutor,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
    ) -> None:
        self._executor = executor
        self._observer = observer
        self._tracer = tracer

    async def run(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ToolResult:
        self._observer("tool_dispatch_start", {"tool_name": TOOL_NAME})
        if self._tracer is None:
            result = await self._guarded(model_args, credentials)
        else:
            with tracing.tool_span(
                self._tracer,
                tool_name=TOOL_NAME,
                args=redact_tool_args(TOOL_NAME, model_args),
                status="ok",
                error_code=None,
            ) as span:
                result = await self._guarded(model_args, credentials)
                span.set_attribute("tool.status", result.status)
                if result.error_code is not None:
                    span.set_attribute("tool.error_code", result.error_code)
        if result.status == "ok":
            self._observer("tool_dispatch_ok", {"tool_name": TOOL_NAME})
        else:
            self._observer(
                "tool_dispatch_error",
                {"tool_name": TOOL_NAME, "error_code": result.error_code},
            )
        return result

    async def _guarded(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ToolResult:
        try:
            return await self._execute(model_args, credentials)
        except Exception:  # noqa: BLE001 - B4-parity: never abort the turn / leak str(exc)
            _logger.exception(
                "runBlueprint internal error (session=%s)", credentials.session_id
            )
            return self._error(INTERNAL_ERROR_CODE, _INTERNAL_ERROR_MESSAGE, retryable=False)

    async def _execute(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ToolResult:
        blueprint_id = model_args.get("id")
        slot_bindings = model_args.get("slot_bindings", {})
        if not isinstance(blueprint_id, str) or not blueprint_id.strip():
            return self._error(INVALID_ARGS_CODE, _INVALID_ARGS_MESSAGE, retryable=True)
        if slot_bindings is None:
            slot_bindings = {}
        if not isinstance(slot_bindings, dict):
            return self._error(INVALID_ARGS_CODE, _INVALID_ARGS_MESSAGE, retryable=True)

        outcome = await self._executor.execute(
            blueprint_id=blueprint_id.strip(),
            slot_bindings=slot_bindings,
            credentials=credentials,
        )

        if isinstance(outcome, ExecCompleted):
            return ToolResult(
                status="ok",
                tool_name=TOOL_NAME,
                error_code=None,
                retryable=None,
                user_message=None,
                provenance=outcome.provenance,
                result_preview=outcome.preview,
                result_full=outcome.result_full,
            )
        if isinstance(outcome, ExecPaused):
            # §2.5 pause seam: surface a `ToolPause` the loop honors (writes the
            # checkpoint + returns paused_ask_user). No result rows are returned.
            return ToolResult(
                status="ok",
                tool_name=TOOL_NAME,
                error_code=None,
                retryable=None,
                user_message=None,
                provenance=frozenset(),
                result_preview=None,
                result_full=None,
                pause=ToolPause(
                    reason=outcome.reason,
                    pending_question=outcome.pending_question,
                    blueprint_id=outcome.blueprint_id,
                    slot_bindings_json=outcome.slot_bindings_json,
                    completed_nodes_json=outcome.completed_nodes_json,
                    awaiting_node=outcome.awaiting_node,
                ),
            )
        if isinstance(outcome, ExecFailed):
            # A runBlueprint-family code OR an inner denial passed through verbatim
            # (relabeled tool_name="runBlueprint", §5.4) → the raw-loop fallback.
            return ToolResult(
                status="error",
                tool_name=TOOL_NAME,
                error_code=outcome.error_code,
                retryable=outcome.retryable,
                user_message=outcome.user_message,
                provenance=outcome.provenance,
                result_preview=None,
                result_full=None,
            )
        # Unreachable for the closed ExecOutcome union — fail-closed.
        return self._error(INTERNAL_ERROR_CODE, _INTERNAL_ERROR_MESSAGE, retryable=False)

    def _error(self, code: str, message: str, *, retryable: bool) -> ToolResult:
        return ToolResult(
            status="error",
            tool_name=TOOL_NAME,
            error_code=code,
            retryable=retryable,
            user_message=message,
            provenance=None,
            result_preview=None,
            result_full=None,
        )


__all__ = ["INTERNAL_ERROR_CODE", "INVALID_ARGS_CODE", "TOOL_NAME", "RunBlueprintTool"]
