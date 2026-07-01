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
from data_agent.runtime.observability.redaction import redact_tool_args
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
class ToolResult:
    """The outcome of one dispatched tool call — never carries credentials."""

    status: Literal["ok", "denied", "error"]
    tool_name: str
    error_code: str | None
    retryable: bool | None
    user_message: str | None
    provenance: frozenset[tuple[str, str]] | None
    result_preview: ResultPreview | None
    result_full: dict[str, Any] | list[Any] | None


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

    def _emit_tool_span(
        self, tool_name: str, model_args: dict[str, Any], *, status: str, error_code: str | None
    ) -> None:
        if self._tracer is None:
            return
        with tracing.tool_span(
            self._tracer,
            tool_name=tool_name,
            args=redact_tool_args(tool_name, model_args),
            status=status,
            error_code=error_code,
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
                user_message=denial.user_message,
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
        self._emit_tool_span(tool_name, model_args, status="ok", error_code=None)
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
