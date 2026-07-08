"""`otlp_disable_redaction` — the access-controlled master TELEMETRY DEBUG switch.

Default OFF preserves the D25 shape-only TOOL span byte-for-byte (SQL literals
masked, NO result on the span). ON reveals the REAL tool call in Phoenix (real
SQL WITH literals + the result preview) — and, critically, does so as a
TELEMETRY-ONLY change: the dispatched call and the enforced scope are identical
either way (flipping the flag changes only what a span records, never what the
tool does or what the MCP denies).

Layer-1: real OTel SDK + InMemorySpanExporter, a FakeMCPClient — no OpenAI spend,
no live Phoenix collector.

Tags:
  - otlp-redaction-on-by-default
  - otlp-disable-redaction-shows-real-tool-calls
  - otlp-disable-redaction-is-telemetry-only
"""

from __future__ import annotations

import json

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.observability import tracing
from data_agent.runtime.provenance.catalog_handle import CatalogHandle

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Salary": "Decimal(18,2)"}})

SESSION_ID = "sess-disable-redaction"
JWT = "jwt-secret-should-never-leak"
PII_NAME = "Jane Doe"
PII_SALARY = "128000"
PII_SQL = (
    f"SELECT EmployeeCode FROM employee WHERE Name = '{PII_NAME}' AND Salary > {PII_SALARY}"
)
# A result whose CELL values are entity-bearing (never on a span in the default posture).
RESULT = {
    "columns": ["EmployeeCode", "Name"],
    "rows": [["E1", PII_NAME], ["E2", "John Roe"]],
    "row_count": 2,
    "truncated": False,
}


def _tracer_with_memory_exporter() -> tuple[tracing.Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return tracing.get_tracer(provider), exporter


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=JWT, column_scope=frozenset())


def _tool_spans(exporter: InMemorySpanExporter) -> list:
    return [
        s
        for s in exporter.get_finished_spans()
        if s.attributes.get(SpanAttributes.OPENINFERENCE_SPAN_KIND)
        == OpenInferenceSpanKindValues.TOOL.value
    ]


# ---------------------------------------------------------------------------
# (a) flag OFF (default) — D25 unchanged: args masked + NO result on the span
# ---------------------------------------------------------------------------


async def test_flag_off_masks_sql_and_puts_no_result_on_span() -> None:
    """otlp-redaction-on-by-default: default dispatcher masks SQL literals and
    attaches NO tool result to the span (the D25 shape-only posture)."""
    tracer, exporter = _tracer_with_memory_exporter()
    mcp = FakeMCPClient(scripted={"runQuery": [RESULT]})
    dispatcher = ToolDispatcher(mcp, CATALOG, tracer=tracer)  # disable_redaction defaults False

    result = await dispatcher.dispatch("runQuery", {"sql": PII_SQL}, _credentials())
    assert result.status == "ok"

    (span,) = _tool_spans(exporter)
    attrs = span.attributes
    masked_sql = attrs["tool.args.sql"]
    assert PII_NAME not in masked_sql
    assert PII_SALARY not in masked_sql
    assert "SELECT EmployeeCode FROM employee" in masked_sql  # shape preserved

    # NO result channel on the span at all in the default posture.
    assert not any(k.startswith("tool.result") for k in attrs)
    blob = str(dict(attrs))
    assert "John Roe" not in blob  # a result cell value never reaches the span


# ---------------------------------------------------------------------------
# (b) flag ON — the REAL SQL (literals present) AND the result preview on the span
# ---------------------------------------------------------------------------


async def test_flag_on_shows_real_sql_and_result_preview_on_span() -> None:
    """otlp-disable-redaction-shows-real-tool-calls: with the flag on, the TOOL
    span carries the REAL SQL (literals present) AND the result preview (columns
    + preview rows + shape) so the whole tool call is visible."""
    tracer, exporter = _tracer_with_memory_exporter()
    mcp = FakeMCPClient(scripted={"runQuery": [RESULT]})
    dispatcher = ToolDispatcher(mcp, CATALOG, tracer=tracer, disable_redaction=True)

    result = await dispatcher.dispatch("runQuery", {"sql": PII_SQL}, _credentials())
    assert result.status == "ok"

    (span,) = _tool_spans(exporter)
    attrs = span.attributes

    # (1) REAL args — the actual SQL, literals intact (NOT masked).
    assert attrs["tool.args.sql"] == PII_SQL
    assert PII_NAME in attrs["tool.args.sql"]
    assert PII_SALARY in attrs["tool.args.sql"]

    # (2) the RESULT preview is attached (never present in the default posture).
    assert list(attrs["tool.result.columns"]) == ["EmployeeCode", "Name"]
    assert attrs["tool.result.row_count"] == 2
    assert attrs["tool.result.truncated"] is False
    preview_rows = json.loads(attrs["tool.result.preview_rows"])
    assert preview_rows == [["E1", PII_NAME], ["E2", "John Roe"]]


# ---------------------------------------------------------------------------
# (c) TELEMETRY-ONLY — the enforcement path is unaffected by the flag
# ---------------------------------------------------------------------------


async def test_flag_on_is_telemetry_only_denial_still_denies() -> None:
    """otlp-disable-redaction-is-telemetry-only: the flag changes ONLY what the
    span records, NEVER enforcement. A scope denial (MCPToolError from the MCP)
    still denies identically with the flag ON — and identically to the flag OFF —
    because enforcement lives in the MCP + injected credentials, not the span
    redactor."""
    creds = _credentials()

    async def _run(disable_redaction: bool) -> tuple[str, str | None]:
        tracer, _ = _tracer_with_memory_exporter()
        mcp = FakeMCPClient(
            scripted={"runQuery": [MCPToolError("COLUMN_SCOPE_VIOLATION", "denied")]}
        )
        dispatcher = ToolDispatcher(
            mcp, CATALOG, tracer=tracer, disable_redaction=disable_redaction
        )
        result = await dispatcher.dispatch("runQuery", {"sql": PII_SQL}, creds)
        return result.status, result.error_code

    off = await _run(False)
    on = await _run(True)

    # Denied both ways, with the identical error code — the flag never weakened
    # the MCP-enforced column-scope check.
    assert off == ("denied", "COLUMN_SCOPE_VIOLATION")
    assert on == off


async def test_flag_on_does_not_change_dispatched_call_args() -> None:
    """The dispatched `call_tool` always receives the RAW model_args regardless of
    the flag — the redactor never touched the dispatch path, so telemetry posture
    cannot alter what the tool actually executes (or the JWT/session_id it injects)."""
    for disable_redaction in (False, True):
        mcp = FakeMCPClient(scripted={"runQuery": [RESULT]})
        tracer, _ = _tracer_with_memory_exporter()
        dispatcher = ToolDispatcher(
            mcp, CATALOG, tracer=tracer, disable_redaction=disable_redaction
        )
        await dispatcher.dispatch("runQuery", {"sql": PII_SQL}, _credentials())
        # The MCP saw the UN-redacted, literal-bearing SQL AND the injected
        # credentials (D5), identically with the flag on or off.
        assert len(mcp.calls) == 1
        assert mcp.calls[0].args == {"sql": PII_SQL}
        assert mcp.calls[0].jwt == JWT
        assert mcp.calls[0].session_id == SESSION_ID
