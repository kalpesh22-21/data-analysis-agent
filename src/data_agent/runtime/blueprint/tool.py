"""blueprint/tool.py — `RunBlueprintTool`, the model-facing runtime tool.

A `RuntimeTool`: intercepted in the agent loop, never dispatched to the MCP under its own
name, counting as exactly ONE `tool_calls_made` — the DAG's inner runQuery probes are the
tool's implementation and are invisible to the loop's budget. It is a THIN wrapper over
`BlueprintExecutor`:

  - one `TOOL` span (`slot_bindings` values REDACTED) plus symmetric start/ok/error progress;
  - a crash guard, so a raising executor NEVER aborts the turn or leaks `str(exc)`;
  - the `ExecOutcome` mapping: `Completed` to an ok `ToolResult` (verified result + preview,
    union provenance), `Paused` to a `ToolResult` carrying a `ToolPause` (the loop's pause
    seam), and `Failed` to an error `ToolResult` — a runBlueprint-family code, or an inner
    denial passed through verbatim — which routes the model to the raw loop.

D49 (absolute): NO LLM runs inside this tool; every resolver is pure code plus
scope-enforced probe queries. The D56 LLM review is the loop's NEXT ordinary round-trip,
never a nested call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from data_agent.runtime.dispatch.tool_dispatcher import (
    ToolObserver,
    ToolPause,
    ToolResult,
    _default_observer,
)
from data_agent.runtime.dispatch.tool_envelope import RuntimeToolBase
from data_agent.runtime.observability.redaction import tool_span_args

from .executor import (
    BlueprintExecutor,
    ExecCompleted,
    ExecFailed,
    ExecOutcome,
    ExecPaused,
)
from .models import DATA_ANCHORED_RESULT_NOTE, DATA_WINDOW_ANCHOR

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

    from data_agent.runtime.auth.credentials import RuntimeCredentials
    from data_agent.runtime.loop.agent_loop import TurnContext

TOOL_NAME = "runBlueprint"

INVALID_ARGS_CODE = "RUN_BLUEPRINT_INVALID_ARGS"
INTERNAL_ERROR_CODE = "RUN_BLUEPRINT_INTERNAL_ERROR"
_INVALID_ARGS_MESSAGE = "runBlueprint needs a blueprint 'id' and a 'slot_bindings' object."
_INTERNAL_ERROR_MESSAGE = "The fast path hit an internal error — answer from the raw tools instead."


def _is_verified_blueprint_result(result_full: Any) -> bool:
    """True iff *result_full* is a genuinely D56-verified blueprint result — the
    ONLY shape that earns the `authoritative` marker (§4). Requires the executor's
    `status:"verified"` plus a clean `verify` block (grain_ok AND signature_ok). A
    non-dict, a non-"verified" status, a missing/failed verify block (a poisoned or
    legacy record) fails closed to False, so the marker is never over-claimed.

    DELIBERATE: a blueprint with an empty or `grain_verifiable:false` grain reports
    `grain_ok=True` VACUOUSLY (the §4.2 row-count-teeth skip, `grain_checked:false`),
    so it earns the marker even though the teeth did not run. This intentionally
    mirrors the executor's own `status:"verified"` labeling — the result IS the
    trusted answer for that intent. Do not retighten this to `grain_checked` (it
    would strip the marker from every legitimately-skipped-grain blueprint) nor
    loosen it to accept a failed verify block."""
    if not isinstance(result_full, dict):
        return False
    if result_full.get("status") != "verified":
        return False
    verify = result_full.get("verify")
    if not isinstance(verify, dict):
        return False
    return bool(verify.get("grain_ok")) and bool(verify.get("signature_ok"))


def window_note_for_result(result_full: Any) -> str | None:
    """The model-facing window note for an executed blueprint's result, or `None` (J7).

    Emitted ONLY for a `window_anchor: "data"` result. A `calendar` blueprint needs no
    note: its window is the dates the caller supplied, so the model's own reading of the
    result is already right, and a note there would be a line of prompt on every run
    buying nothing. The narrow trigger is the point — this note exists to stop ONE
    specific failure (a data-anchored window read as a stale calendar window, then
    silently re-derived), not to annotate windows in general.

    Fail-closed on any surprise (a non-dict, a missing/unknown anchor) → `None`, so the
    tool result is unchanged for every blueprint that does not declare `data`."""
    if not isinstance(result_full, dict):
        return None
    if result_full.get("window_anchor") != DATA_WINDOW_ANCHOR:
        return None
    return DATA_ANCHORED_RESULT_NOTE


def blueprint_outcome_to_tool_result(outcome: ExecOutcome) -> ToolResult | None:
    """The SINGLE `ExecOutcome` → `ToolResult` mapping, shared by
    `RunBlueprintTool._execute` (a first call) AND the loop's mid-DAG resume path
    (`agent_loop._blueprint_outcome_to_tool_result`) so a resumed blueprint returns
    byte-identical results — including the verified `authoritative` marker — to a
    non-paused run (D45). Keeping it in ONE place is what stops the two paths from
    drifting (the resume path silently lost the marker before this dedup). Returns
    `None` for a value outside the closed `ExecOutcome` union so each caller applies
    its OWN internal-error fallback (their error codes differ)."""
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
            # The D56-verified result is the trusted answer — carry an explicit
            # "authoritative" marker into the model's tool message so it does not
            # re-derive/re-verify the same intent with ad-hoc runQuerys. Set ONLY
            # when the result is genuinely verified (status verified + grain +
            # signature ok); an ExecCompleted whose verify block is not clean
            # (poisoned/legacy shape) does NOT earn the marker.
            authoritative=_is_verified_blueprint_result(outcome.result_full),
            # J7: derived HERE, in the shared mapper, for the same reason `authoritative`
            # is — a resumed mid-DAG blueprint must return a byte-identical result to a
            # non-paused run, and the note silently going missing on the resume path is
            # precisely the drift this dedup exists to prevent. Independent of
            # `authoritative`: a blueprint whose verify block is not clean still ran a
            # data-anchored window, and describing THAT honestly does not depend on
            # whether the rows earned the trusted marker.
            window_note=window_note_for_result(outcome.result_full),
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
    return None  # outside the closed union → caller supplies its internal-error fallback


class RunBlueprintTool(RuntimeToolBase):
    """The `runBlueprint(id, slot_bindings)` runtime tool (§5.1) — arg validation and the
    `ExecOutcome` mapping over the shared envelope (`dispatch/tool_envelope.py`, which owns
    the TOOL span, the symmetric progress events and the crash guard)."""

    tool_name = TOOL_NAME

    _INTERNAL_ERROR_CODE = INTERNAL_ERROR_CODE
    _INTERNAL_ERROR_MESSAGE = _INTERNAL_ERROR_MESSAGE
    # UNDETERMINED, and it must STAY undetermined (D44). A runBlueprint error is not
    # necessarily footprint-free the way a retrieval error is: `ExecFailed` passes an INNER
    # denial through verbatim, and that denial arose from per-node SQL over columns this
    # layer cannot enumerate. `None` is dropped from replay unconditionally, which is the
    # fail-closed reading; a determined-empty `frozenset()` would claim "this entry names
    # no column" about a result that may well have, and keep it in replay forever.
    _ERROR_PROVENANCE = None

    def __init__(
        self,
        *,
        executor: BlueprintExecutor,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
        disable_redaction: bool = False,
    ) -> None:
        # `disable_redaction` is the access-controlled TELEMETRY DEBUG switch
        # (RuntimeSettings.otlp_disable_redaction): default False keeps the D25 span
        # (slot_bindings values redacted, slot NAMES kept); True puts the REAL slot values
        # on the span. The envelope always hands `_execute` the raw model_args, so the
        # executor's per-node runQuery/enforcement is unaffected either way.
        super().__init__(
            observer=observer, tracer=tracer, disable_redaction=disable_redaction
        )
        self._executor = executor

    def _span_args(self, model_args: dict[str, Any]) -> dict[str, Any]:
        return tool_span_args(
            TOOL_NAME, model_args, disable_redaction=self._disable_redaction
        )

    async def _execute(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None = None,
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

        # The ONE shared ExecOutcome → ToolResult mapper (the resume path reuses it,
        # so the verified `authoritative` marker can never drift between the two).
        mapped = blueprint_outcome_to_tool_result(outcome)
        if mapped is not None:
            return mapped
        # Unreachable for the closed ExecOutcome union — fail-closed.
        return self._error(INTERNAL_ERROR_CODE, _INTERNAL_ERROR_MESSAGE, retryable=False)


__all__ = [
    "INTERNAL_ERROR_CODE",
    "INVALID_ARGS_CODE",
    "TOOL_NAME",
    "RunBlueprintTool",
    "blueprint_outcome_to_tool_result",
    "window_note_for_result",
]
