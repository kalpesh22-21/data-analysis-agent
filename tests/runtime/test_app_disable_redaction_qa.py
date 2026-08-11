"""End-to-end app wiring for the `otlp_disable_redaction` master TELEMETRY DEBUG
switch, driven through the real `create_app` + a turn over Layer-1 fakes and the
`/_test/spans` dump endpoint (no OpenAI spend, no Phoenix collector).

**The default flipped on 2026-08-10** (deliberate operator posture choice): the flag now
defaults TRUE, so an all-defaults runtime ships UN-redacted spans and the collector is as
sensitive as the session store. This file proves BOTH directions with the same harness,
because a flipped default is only defensible if the opt-out is tested as hard as the
default:

  * the DEFAULT — a `RuntimeSettings` nobody configured puts the real SQL literal and the
    result preview on the span. Asserted through the app rather than off the field,
    because `extra="ignore"` means a field's value and its EFFECT are two claims.
  * the OPT-OUT (`otlp_disable_redaction=False`) — byte-identical to the D25 posture
    proven in the companion `test_app_span_exporter_qa.py`.

Every builder here pins `_env_file=None`. Without it these tests read the repo's `.env`,
which carries an explicit `OTLP_DISABLE_REDACTION` — a default test that a developer's
local file can flip is not a test of the default.

Tags:
  - otlp-unredacted-by-default
  - otlp-disable-redaction-shows-real-tool-calls
  - otlp-disable-redaction-is-telemetry-only
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime import app as app_module
from data_agent.runtime.app import create_app
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.mcp.client import MCPToolSpec
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

SESSION_ID = "sess-disable-redaction-app"
HEADERS = {"Authorization": "Bearer test-jwt", "X-Session-Id": SESSION_ID}

# Entity-bearing tokens: one rides in a SQL string literal, one is a result cell.
PII_SQL_LITERAL = "PII_SQL_LITERAL_QARED_551155"
PII_RESULT_CELL = "PII_RESULT_CELL_QARED_662266"


def _build_app(monkeypatch, *, disable_redaction: bool | None, span_exporter: Any) -> Any:
    """*disable_redaction* `None` means "leave the field at its SHIPPED DEFAULT" — the
    only way to test a default is to not set it."""
    monkeypatch.setattr(app_module, "verify_jwt", lambda *a, **k: frozenset())
    mcp_client = FakeMCPClient(
        tools=[
            MCPToolSpec(
                name="runQuery",
                description="",
                input_schema={"type": "object", "properties": {"sql": {"type": "string"}}},
            )
        ],
        scripted={
            "runQuery": [
                {
                    "columns": ["national_id"],
                    "rows": [[PII_RESULT_CELL]],
                    "row_count": 1,
                    "truncated": False,
                }
            ]
        },
    )
    settings = RuntimeSettings(
        _env_file=None,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        **({} if disable_redaction is None else {"otlp_disable_redaction": disable_redaction}),
    )
    return create_app(
        settings=settings,
        session_store=InMemorySessionStore(),
        mcp_client=mcp_client,
        model_client=ScriptedModelClient(
            [
                ModelTurnResult(
                    tool_calls=[
                        ToolCallRequest(
                            id="q1",
                            name="runQuery",
                            arguments={
                                "sql": f"SELECT national_id FROM t WHERE ssn = '{PII_SQL_LITERAL}'"
                            },
                        )
                    ]
                ),
                ModelTurnResult(assistant_text="Looked it up."),
            ]
        ),
        catalog=CatalogHandle({}),
        span_exporter=span_exporter,
    )


def _drive_and_dump(app: Any) -> dict[str, Any]:
    client = TestClient(app)
    resp = client.post("/turn", json={"message": "look it up"}, headers=HEADERS)
    assert resp.status_code == 200
    dump = client.get("/_test/spans").json()
    tool_spans = [s for s in dump["spans"] if s["name"] == "tool.runQuery"]
    assert len(tool_spans) == 1, "expected exactly one runQuery TOOL span"
    return tool_spans[0]


def test_the_shipped_default_puts_the_real_tool_call_on_the_span(monkeypatch) -> None:
    """otlp-unredacted-by-default: an all-defaults runtime — nobody set the flag, nobody
    set an env var — ships the REAL SQL literal and the result preview to the collector.

    This is the whole 2026-08-10 posture flip, asserted at the only place that can prove
    it. `RuntimeSettings.otlp_disable_redaction is True` proves the FIELD; this proves the
    EFFECT, and the two are separate claims: the value has to survive `create_app`'s
    threading into the dispatcher and the tool-span writer to change a single attribute.
    """
    app = _build_app(monkeypatch, disable_redaction=None, span_exporter=InMemorySpanExporter())
    attrs = _drive_and_dump(app)["attributes"]
    assert PII_SQL_LITERAL in attrs["tool.args.sql"]
    assert PII_RESULT_CELL in attrs["tool.result.preview_rows"]


def test_flag_off_app_keeps_d25_shape_only_span(monkeypatch) -> None:
    """otlp-unredacted-by-default (the OPT-OUT): `otlp_disable_redaction=False` masks the
    SQL literal and puts NO result on the runQuery TOOL span — the D25 posture, intact.

    Named "flag off" and no longer "the default": it is now the thing an operator must
    ASK for. The reveal is only a defensible default while this stays exact."""
    app = _build_app(monkeypatch, disable_redaction=False, span_exporter=InMemorySpanExporter())
    tool_span = _drive_and_dump(app)
    attrs = tool_span["attributes"]
    assert PII_SQL_LITERAL not in attrs["tool.args.sql"]
    assert not any(k.startswith("tool.result") for k in attrs)
    assert PII_RESULT_CELL not in str(attrs)


def test_flag_on_app_ships_real_sql_and_result_on_span(monkeypatch) -> None:
    """otlp-disable-redaction-shows-real-tool-calls: through the full app, the
    debug flag makes the shipped runQuery TOOL span carry the REAL SQL (literal
    present) AND the result preview (columns + the result cell value)."""
    app = _build_app(monkeypatch, disable_redaction=True, span_exporter=InMemorySpanExporter())
    tool_span = _drive_and_dump(app)
    attrs = tool_span["attributes"]

    # Real args — the literal is present, not masked.
    assert PII_SQL_LITERAL in attrs["tool.args.sql"]
    # The result preview is attached, carrying columns AND the entity-bearing cell.
    assert "national_id" in attrs["tool.result.columns"]
    assert attrs["tool.result.row_count"] == "1"  # dump stringifies attribute values
    assert PII_RESULT_CELL in attrs["tool.result.preview_rows"]


def _read_tools_app(monkeypatch, *, disable_redaction: bool, pii_query: str) -> Any:
    """A `create_app` with an injected retrieval pipeline (so the read tools wire)
    + a span exporter, proving app.py threads `otlp_disable_redaction` all the way
    into the read-tool constructors (not only the dispatcher)."""
    from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
    from data_agent.runtime.retrieval.models import Candidate
    from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
    from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
    from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

    monkeypatch.setattr(app_module, "verify_jwt", lambda *a, **k: frozenset())
    index = FakeVectorIndex(
        [
            (
                Candidate(
                    id="bp-x",
                    kind="blueprint",
                    text="overtime rollup",
                    uses=frozenset(),
                    payload={"intent": "overtime rollup", "slots_summary": "dept"},
                ),
                [1.0, 0.0],
            )
        ]
    )
    retrieval = RetrievalPipeline(
        embedding_client=FakeEmbeddingClient({pii_query: [1.0, 0.0]}),
        reranker=None,
        vector_index=index,
        user_memory=NullUserMemoryProvider(),
        recall_k=30,
        top_k_blueprints=3,
        top_k_knowledge=3,
    )
    return create_app(
        settings=RuntimeSettings(_env_file=None, otlp_disable_redaction=disable_redaction),
        session_store=InMemorySessionStore(),
        mcp_client=FakeMCPClient(scripted={}),
        model_client=ScriptedModelClient(
            [
                ModelTurnResult(
                    tool_calls=[
                        ToolCallRequest(id="sb1", name="searchBlueprints", arguments={"query": pii_query})
                    ]
                ),
                ModelTurnResult(assistant_text="Found it."),
            ]
        ),
        catalog=CatalogHandle({}),
        retrieval=retrieval,
        span_exporter=InMemorySpanExporter(),
    )


def test_app_wires_flag_into_read_tools(monkeypatch) -> None:
    """otlp-disable-redaction-shows-real-tool-calls: app.py threads the flag into
    the read-tool registry — a searchBlueprints turn's span redacts `query` by
    default and reveals it when the flag is on."""
    pii_query = "overtime paid to Jane Doe"
    for disable_redaction, expected in ((False, "<redacted>"), (True, pii_query)):
        app = _read_tools_app(
            monkeypatch, disable_redaction=disable_redaction, pii_query=pii_query
        )
        client = TestClient(app)
        resp = client.post("/turn", json={"message": "overtime?"}, headers=HEADERS)
        assert resp.status_code == 200
        dump = client.get("/_test/spans").json()
        sb = [s for s in dump["spans"] if s["name"] == "tool.searchBlueprints"]
        assert len(sb) == 1
        assert sb[0]["attributes"]["tool.args.query"] == expected


def test_flag_is_telemetry_only_same_answer_both_ways(monkeypatch) -> None:
    """otlp-disable-redaction-is-telemetry-only: the turn's RESULT (what the user
    gets, what the tool did) is identical with the flag on or off — only the span
    differs. The MCP saw the same un-redacted call + injected credentials either
    way."""
    outcomes = []
    for disable_redaction in (False, True):
        app = _build_app(
            monkeypatch, disable_redaction=disable_redaction, span_exporter=None
        )
        client = TestClient(app)
        resp = client.post("/turn", json={"message": "look it up"}, headers=HEADERS)
        assert resp.status_code == 200
        outcomes.append(resp.text)
    assert outcomes[0] == outcomes[1]
