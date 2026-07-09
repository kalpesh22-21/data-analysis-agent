"""ToolDispatcher — the single choke point for every model-requested tool call (design §3.3).

    1. Credential injection (jwt/session_id attached at the MCP-call boundary
       only — `MCPClient.call_tool(..., jwt=..., session_id=...)`).
    2. MCP call (`askUser` is intercepted upstream in Pass B's `AgentLoop`;
       it never reaches this dispatcher).
    3. Graceful-denial mapping on `MCPToolError` (`denial_mapping.py`).
    4. Provenance capture for every tool (`provenance/capture.py`).
    5. Result-preview construction (preview-only, D46 — the full result is
       returned on `ToolResult.result_full` for the caller to persist via
       `SessionStore.write_full_result`; the dispatcher itself does no
       session I/O).

B4 (2026-07-01, HIGH fix): a RAW (non-`MCPToolError`) transport exception from
`MCPClient.call_tool` — a connection refusal, timeout, or malformed response,
none of which carry the `[{CODE}]` prefix `MCPToolError` expects — used to
propagate straight out of `dispatch()` uncaught, crashing the whole turn and
(via `app.py`'s last-resort SSE handler) leaking `str(exc)` verbatim to the
client. `dispatch()` now wraps the `call_tool` invocation in a broad
`except Exception` fallback (below the specific `MCPToolError` handler) that
degrades to a clean `ToolResult(status="error", ...)` with a generic,
PII-safe `user_message` — never the raw exception text — and logs the real
exception server-side only (`logging`, never forwarded to the client/model).
This is what makes `ToolResult.status == "error"` (declared but previously
dead code) actually reachable.

Pass-B observability seam: `observer` is an optional callback invoked at each
stage boundary (`observer(event_name, payload)`), defaulting to a no-op.
Pass B's `observability/tracing.py` + `observability/progress.py` wire a real
observer in here without any change to this class's public interface — see
`_default_observer` below.

D5 model-invisibility (load-bearing): `ToolResult` NEVER carries the JWT or
session_id in any field — `credentials` is consumed only to build the
`call_tool(..., jwt=credentials.jwt, session_id=credentials.session_id)` call
and is not otherwise referenced. `tests/runtime/dispatch/test_tool_dispatcher.py`
scans every `ToolResult` field for the JWT/session_id substrings.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.mcp.client import MCPClient, MCPToolError
from data_agent.runtime.observability import tracing
from data_agent.runtime.observability.redaction import tool_span_args
from data_agent.runtime.provenance.capture import capture_provenance
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.models import ResultPreview

from .denial_mapping import DenialInfo, classify_denial

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

ToolObserver = Callable[[str, dict[str, Any]], None]

_logger = logging.getLogger(__name__)

# B4: a raw transport exception (connection refusal, timeout, malformed
# response) never carries a `[{CODE}]` prefix an MCPToolError would — it is
# NOT a tool-level denial the MCP itself classified, so it is not looked up
# in `denial_mapping.py`'s table at all. Generic, PII-safe, never the raw
# exception text.
INTERNAL_TRANSPORT_ERROR_CODE = "INTERNAL_TRANSPORT_ERROR"
_INTERNAL_TRANSPORT_ERROR_MESSAGE = (
    "Something went wrong reaching the data warehouse. Please try again."
)


def _default_observer(event: str, payload: dict[str, Any]) -> None:
    """No-op observer — Pass B replaces this with real spans/progress events."""
    return None


@dataclass(frozen=True)
class ToolPause:
    """A runtime tool's request to PAUSE the turn — the generalized `askUser`
    terminal-pause seam (runblueprint-design §2.5). Carried on `ToolResult.pause`;
    the agent loop honors it by writing a `PauseCheckpoint` and returning
    `paused_ask_user`, exactly as for `askUser`. Only RUNTIME tools set it
    (dispatched MCP tools never pause); the loop ignores it on any dispatched
    result. Slice B uses it for a slot-resolution `askUser`; the `blueprint_*`
    fields carry the D45 mid-DAG resume state Slice C populates for approvals."""

    reason: str  # "blueprint_slot" (Slice C: "blueprint_approval" | "blueprint_when_ask")
    pending_question: dict[str, Any]
    blueprint_id: str | None = None
    slot_bindings_json: str | None = None
    completed_nodes_json: str | None = None
    awaiting_node: int | None = None


@dataclass(frozen=True)
class ToolResult:
    """The outcome of one dispatched tool call — never carries credentials.

    `pause` (additive, runblueprint-design §2.5) is set ONLY by a runtime tool
    that needs to pause the turn (`runBlueprint` on a slot-resolution `askUser`);
    it is `None` for every dispatched MCP tool and every non-pausing runtime tool,
    so the existing trail/budget path is unchanged."""

    status: Literal["ok", "denied", "error"]
    tool_name: str
    error_code: str | None
    retryable: bool | None
    user_message: str | None
    provenance: frozenset[tuple[str, str]] | None
    result_preview: ResultPreview | None
    result_full: dict[str, Any] | list[Any] | None
    pause: ToolPause | None = None


def _build_preview(raw_result: Any, preview_row_count: int) -> ResultPreview:
    """Build the `{columns, row_count, truncated, preview_rows}` preview object.

    Handles the three MCP result shapes actually returned by the six tools
    (design §0):
      - `{columns, rows, row_count, truncated}` (runQuery/sampleRows/explainQuery)
      - a bare list of dicts (listDatabases/listTables)
      - a small non-tabular dict (getTableSchema: `{database, table, columns}`)
    The last two never grow unbounded (no PII-row exposure risk), so they are
    previewed whole rather than truncated to N; only the first (row-oriented)
    shape enforces the N-row preview cap.
    """
    if isinstance(raw_result, dict) and "rows" in raw_result and "columns" in raw_result:
        rows = raw_result["rows"]
        preview_rows = rows[:preview_row_count]
        truncated = bool(raw_result.get("truncated", False)) or len(rows) > preview_row_count
        return ResultPreview(
            columns=list(raw_result["columns"]),
            row_count=int(raw_result.get("row_count", len(rows))),
            truncated=truncated,
            preview_rows=[list(row) for row in preview_rows],
        )
    if isinstance(raw_result, list):
        preview_rows = raw_result[:preview_row_count]
        return ResultPreview(
            columns=[],
            row_count=len(raw_result),
            truncated=len(raw_result) > preview_row_count,
            preview_rows=[[item] for item in preview_rows],
        )
    # Small non-tabular dict (e.g. getTableSchema) — no row-level truncation.
    return ResultPreview(columns=[], row_count=1, truncated=False, preview_rows=[[raw_result]])


class ToolDispatcher:
    """Executes one model-requested tool call end-to-end (design §3.3)."""

    def __init__(
        self,
        mcp_client: MCPClient,
        catalog: CatalogHandle,
        *,
        preview_row_count: int = 20,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
        disable_redaction: bool = False,
    ) -> None:
        self._mcp_client = mcp_client
        self._catalog = catalog
        self._preview_row_count = preview_row_count
        self._observer = observer
        # B5: optional — when a real tracer is wired (app.py's composition
        # root), dispatch() emits one TOOL span per call, SQL-literal-masked
        # via observability/redaction.py::redact_tool_args (D25). `None` here
        # (Layer-1 tests, no Phoenix) means spans are simply never created —
        # no behavioral difference otherwise.
        self._tracer = tracer
        # Access-controlled TELEMETRY DEBUG switch (RuntimeSettings.
        # otlp_disable_redaction, wired by app.py). Default False keeps the D25
        # shape-only span byte-identical. When True the TOOL span carries the REAL
        # args (SQL WITH literals) AND the result preview — see `_emit_tool_span`.
        # TELEMETRY-ONLY: this ONLY changes what the span records; the dispatched
        # `call_tool` below always receives the raw `model_args` regardless (the
        # redactor never touched the dispatch path), and no scope/PII ENFORCEMENT
        # (D5/D57, enforced in the MCP + injected credentials) depends on it.
        self._disable_redaction = disable_redaction

    def _emit_tool_span(
        self,
        tool_name: str,
        model_args: dict[str, Any],
        *,
        status: str,
        error_code: str | None,
        result_preview: ResultPreview | None = None,
    ) -> None:
        if self._tracer is None:
            return
        # DEFAULT (D25): SQL literals masked, NO result on the span. DEBUG
        # (self._disable_redaction): the REAL args + the result preview, so the
        # whole tool call is visible in Phoenix. Purely a telemetry choice — the
        # dispatched call and enforced scope are identical either way.
        span_result = result_preview if self._disable_redaction else None
        with tracing.tool_span(
            self._tracer,
            tool_name=tool_name,
            args=tool_span_args(
                tool_name, model_args, disable_redaction=self._disable_redaction
            ),
            status=status,
            error_code=error_code,
            result_preview=span_result,
            reveal_complex_args=self._disable_redaction,
        ):
            pass

    async def dispatch(
        self,
        tool_name: str,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
    ) -> ToolResult:
        self._observer("tool_dispatch_start", {"tool_name": tool_name})

        try:
            raw_result = await self._mcp_client.call_tool(
                tool_name,
                model_args,
                jwt=credentials.jwt,
                session_id=credentials.session_id,
            )
        except MCPToolError as exc:
            denial: DenialInfo = classify_denial(exc.code)
            # B4/D25 posture: the model-facing `user_message` is normally the
            # GENERIC canned string from `denial_mapping.py` — raw backend /
            # transport text is NEVER surfaced. The single narrow exception is
            # COLUMN_SCOPE_VIOLATION: its `exc.message` is an author-CONTROLLED
            # `ColumnScopeError` string that NAMES the out-of-scope column(s)
            # (catalog metadata only — not PII / cell values), so surfacing it
            # lets the model self-correct on the LIVE turn by seeing WHICH
            # columns it lacks. All other codes (and the transport path below)
            # stay canned. NOTE: `user_message` is not persisted on TrailEntry;
            # on replay `context/budget.py::_render_entry` re-derives the
            # GENERIC message from `error_code` via `denial_mapping.py` (it
            # cannot know the column) — acceptable, since the column-specific
            # message already reached the model on the live turn.
            if denial.code == "COLUMN_SCOPE_VIOLATION" and exc.message:
                user_message = exc.message
            else:
                user_message = denial.user_message
            self._observer(
                "tool_dispatch_denied", {"tool_name": tool_name, "error_code": denial.code}
            )
            self._emit_tool_span(
                tool_name, model_args, status="denied", error_code=denial.code
            )
            return ToolResult(
                status="denied",
                tool_name=tool_name,
                error_code=denial.code,
                retryable=denial.retryable,
                user_message=user_message,
                provenance=None,
                result_preview=None,
                result_full=None,
            )
        except Exception:
            # B4: a RAW (non-MCPToolError) transport exception — connection
            # refusal, timeout, malformed response — must never crash the
            # turn or leak str(exc) to the client. Log the real exception
            # server-side ONLY; the client/model only ever sees the generic,
            # canned user_message below (D5/D25 — never raw backend detail).
            _logger.exception(
                "Transport exception dispatching tool %r (session=%s)",
                tool_name,
                credentials.session_id,
            )
            self._observer(
                "tool_dispatch_error",
                {"tool_name": tool_name, "error_code": INTERNAL_TRANSPORT_ERROR_CODE},
            )
            self._emit_tool_span(
                tool_name, model_args, status="error", error_code=INTERNAL_TRANSPORT_ERROR_CODE
            )
            return ToolResult(
                status="error",
                tool_name=tool_name,
                error_code=INTERNAL_TRANSPORT_ERROR_CODE,
                retryable=False,
                user_message=_INTERNAL_TRANSPORT_ERROR_MESSAGE,
                provenance=None,
                result_preview=None,
                result_full=None,
            )

        provenance = await capture_provenance(
            tool_name, model_args, self._catalog, session_id=credentials.session_id
        )
        preview = _build_preview(raw_result, self._preview_row_count)

        self._observer("tool_dispatch_ok", {"tool_name": tool_name})
        self._emit_tool_span(
            tool_name, model_args, status="ok", error_code=None, result_preview=preview
        )
        return ToolResult(
            status="ok",
            tool_name=tool_name,
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=provenance,
            result_preview=preview,
            result_full=raw_result,
        )
