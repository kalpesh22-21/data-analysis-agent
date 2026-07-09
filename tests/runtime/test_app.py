"""Smoke tests for app.py — the composition root, wired to Layer-1 fakes only.

Proves `create_app(...)` builds a working FastAPI app (routing, auth
extraction, SSE streaming, `AgentLoop` wiring) with zero live infra: no real
MCP/Couchbase/OpenAI/JWKS/Phoenix — just `InMemorySessionStore`,
`FakeMCPClient`, `ScriptedModelClient`, and a monkeypatched `verify_jwt`
(HTTP-layer auth extraction is exercised separately in
`tests/runtime/auth/test_jwt_verify.py`; here we only need *a* scope).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from data_agent.runtime import app as app_module
from data_agent.runtime.app import create_app
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.mcp.client import MCPToolSpec
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

SESSION_ID = "sess-app-smoke"
HEADERS = {"Authorization": "Bearer test-jwt", "X-Session-Id": SESSION_ID}


def _parse_sse(body: str) -> list[dict]:
    events = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        event_line = next(line for line in lines if line.startswith("event:"))
        data_line = next(line for line in lines if line.startswith("data:"))
        events.append(
            {
                "event": event_line.split(":", 1)[1].strip(),
                "data": json.loads(data_line.split(":", 1)[1].strip()),
            }
        )
    return events


def _build_client(monkeypatch, model_client: ScriptedModelClient) -> TestClient:
    # Bypass real JWKS verification (Layer 1 — no live IdP); the auth boundary
    # itself is covered end-to-end by tests/runtime/auth/test_jwt_verify.py.
    monkeypatch.setattr(app_module, "verify_jwt", lambda *args, **kwargs: frozenset())

    mcp_client = FakeMCPClient(
        tools=[
            MCPToolSpec(
                name="listDatabases", description="", input_schema={"type": "object", "properties": {}}
            )
        ],
        scripted={},
    )
    settings = RuntimeSettings(max_loop_iterations=15, max_wall_clock_seconds=60, max_budget_windows=3)
    app = create_app(
        settings=settings,
        session_store=InMemorySessionStore(),
        mcp_client=mcp_client,
        model_client=model_client,
        catalog=CatalogHandle({}),
    )
    return TestClient(app)


def test_create_app_builds_without_live_infra(monkeypatch) -> None:
    client = _build_client(monkeypatch, ScriptedModelClient([ModelTurnResult(assistant_text="hi")]))
    assert client.app is not None


def test_turn_endpoint_streams_progress_and_result(monkeypatch) -> None:
    model_client = ScriptedModelClient([ModelTurnResult(assistant_text="Here is your answer.")])
    client = _build_client(monkeypatch, model_client)

    response = client.post(
        "/turn", json={"message": "How many employees?"}, headers=HEADERS
    )

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert events[-1]["event"] == "result"
    data = events[-1]["data"]
    # (a) the 4 original keys are unchanged (backward compat).
    assert data["status"] == "done"
    assert data["assistant_text"] == "Here is your answer."
    assert data["pending_question"] is None
    assert data["tool_calls_made"] == 0
    # (b) the 5 UI Slice 1 keys are present.
    for key in ("sql", "result_table", "blueprint_use", "verification", "provenance"):
        assert key in data, f"missing enriched-result key {key!r}"
    # Pure-chat turn (no tools): no SQL/table/blueprint, and a determined-empty
    # provenance union (frozenset() -> []).
    assert data["sql"] is None
    assert data["result_table"] is None
    assert data["blueprint_use"] is None
    assert data["verification"] is None
    assert data["provenance"] == []


def test_turn_endpoint_missing_auth_header_returns_401(monkeypatch) -> None:
    client = _build_client(monkeypatch, ScriptedModelClient([]))
    response = client.post("/turn", json={"message": "hi"}, headers={"X-Session-Id": SESSION_ID})
    assert response.status_code == 401


def test_turn_endpoint_missing_session_id_header_returns_400(monkeypatch) -> None:
    client = _build_client(monkeypatch, ScriptedModelClient([]))
    response = client.post("/turn", json={"message": "hi"}, headers={"Authorization": "Bearer x"})
    assert response.status_code == 400


def test_turn_endpoint_malformed_session_id_header_returns_400(monkeypatch) -> None:
    """S5: X-Session-Id is used verbatim to build Couchbase document keys —
    bounded-length/charset validation, same as the Authorization boundary."""
    client = _build_client(monkeypatch, ScriptedModelClient([]))
    bad_ids = [
        "sess with spaces",
        "sess;DROP TABLE sessions",
        "../../etc/passwd",
        "x" * 200,  # too long
        "",
    ]
    for bad_id in bad_ids:
        response = client.post(
            "/turn",
            json={"message": "hi"},
            headers={"Authorization": "Bearer x", "X-Session-Id": bad_id},
        )
        assert response.status_code == 400, f"expected 400 for session_id={bad_id!r}"


def test_resume_endpoint_without_pending_checkpoint_returns_409(monkeypatch) -> None:
    client = _build_client(monkeypatch, ScriptedModelClient([]))
    response = client.post("/turn/resume", json={"answer": "continue"}, headers=HEADERS)
    assert response.status_code == 409


class _ExplodingModelClient:
    """A `ModelClient` double whose `send_turn` raises a raw exception
    carrying sensitive-looking text — used to prove the SSE error path never
    forwards `str(exc)` verbatim to the client (should-fix, 2026-07-01)."""

    def __init__(self, message: str) -> None:
        self._message = message
        self.calls: list[Any] = []

    async def send_turn(self, messages, tools):  # noqa: ANN001 - Layer-1 test double
        raise RuntimeError(self._message)

    def begin_turn(self) -> _ExplodingModelClient:
        return self


def test_turn_endpoint_unexpected_exception_yields_generic_sse_error_not_raw_text(
    monkeypatch,
) -> None:
    """An unexpected (non-`AlreadyConsumedError`/`CASMismatchError`) exception
    raised from within `AgentLoop.run` (e.g. a raw model-client transport
    failure) must yield a GENERIC SSE `error` event — the raw exception text
    must never appear anywhere in the streamed response body."""
    secret_detail = "db-password=hunter2 at postgres://internal-host:5432/prod"
    model_client = _ExplodingModelClient(secret_detail)
    client = _build_client(monkeypatch, model_client)

    response = client.post("/turn", json={"message": "hi"}, headers=HEADERS)

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert events[-1]["event"] == "error"
    assert events[-1]["data"]["code"] == "INTERNAL_ERROR"
    assert secret_detail not in events[-1]["data"]["message"]
    assert secret_detail not in response.text


def test_full_ask_user_pause_then_resume_round_trip_over_http(monkeypatch) -> None:
    model_client = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="c1", name="askUser", arguments={"question": "Which dept?"})
                ]
            ),
            ModelTurnResult(assistant_text="Using Sales."),
        ]
    )
    client = _build_client(monkeypatch, model_client)

    first = client.post("/turn", json={"message": "Show payroll."}, headers=HEADERS)
    assert first.status_code == 200
    first_events = _parse_sse(first.text)
    paused = first_events[-1]["data"]
    assert paused["status"] == "paused_ask_user"
    assert paused["pending_question"]["question"] == "Which dept?"
    # (e) a paused turn tolerates all-null enrichment (paused before any query
    # ran) — the 5 keys are present but null.
    for key in ("sql", "result_table", "blueprint_use", "verification", "provenance"):
        assert key in paused, f"missing enriched-result key {key!r}"
        assert paused[key] is None

    second = client.post("/turn/resume", json={"answer": "Sales"}, headers=HEADERS)
    assert second.status_code == 200
    second_events = _parse_sse(second.text)
    assert second_events[-1]["data"]["status"] == "done"
    assert second_events[-1]["data"]["assistant_text"] == "Using Sales."


# ---------------------------------------------------------------------------
# Read-tools registry wiring (read-tools-design §10): the three read tools are
# wired only when a retrieval pipeline is active; absent it they are advertised
# but return RETRIEVAL_TOOL_UNAVAILABLE (never an MCP unknown-tool denial).
# ---------------------------------------------------------------------------


def _read_tools_app(
    monkeypatch, model_client: ScriptedModelClient, *, with_retrieval: bool
) -> tuple[TestClient, InMemorySessionStore, FakeMCPClient]:
    from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
    from data_agent.runtime.retrieval.models import BlueprintDetail, Candidate
    from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
    from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
    from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

    monkeypatch.setattr(app_module, "verify_jwt", lambda *args, **kwargs: frozenset())
    mcp_client = FakeMCPClient(
        tools=[
            MCPToolSpec(
                name="listDatabases", description="", input_schema={"type": "object", "properties": {}}
            )
        ],
        scripted={},
    )
    store = InMemorySessionStore()
    retrieval = None
    if with_retrieval:
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
            ],
            details={
                "bp-x": BlueprintDetail(
                    id="bp-x",
                    intent="overtime rollup",
                    slots_summary="dept",
                    uses=frozenset(),
                    status="validated",
                    drift_status="clean",
                    hit_count=0,
                    catalog_sha="",
                )
            },
        )
        retrieval = RetrievalPipeline(
            embedding_client=FakeEmbeddingClient({"overtime?": [1.0, 0.0]}),
            reranker=None,
            vector_index=index,
            user_memory=NullUserMemoryProvider(),
            recall_k=30,
            top_k_blueprints=3,
            top_k_knowledge=3,
        )
    app = create_app(
        settings=RuntimeSettings(_env_file=None),
        session_store=store,
        mcp_client=mcp_client,
        model_client=model_client,
        catalog=CatalogHandle({}),
        retrieval=retrieval,
    )
    return TestClient(app), store, mcp_client


def test_read_tool_wired_when_retrieval_active_handled_not_dispatched(monkeypatch) -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[ToolCallRequest(id="gb1", name="getBlueprint", arguments={"id": "bp-x"})]
            ),
            ModelTurnResult(assistant_text="Found bp-x."),
        ]
    )
    client, store, mcp = _read_tools_app(monkeypatch, model, with_retrieval=True)

    resp = client.post("/turn", json={"message": "overtime?"}, headers=HEADERS)
    assert resp.status_code == 200
    assert _parse_sse(resp.text)[-1]["data"]["status"] == "done"

    import anyio

    trail = anyio.run(store.load_trail, SESSION_ID)
    assert trail[0].tool_name == "getBlueprint"
    assert trail[0].status == "ok"
    assert mcp.calls == []  # getBlueprint never dispatched to the MCP


def test_read_tool_unavailable_when_retrieval_absent(monkeypatch) -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="sb1", name="searchBlueprints", arguments={"query": "overtime"})
                ]
            ),
            ModelTurnResult(assistant_text="No search available."),
        ]
    )
    client, store, mcp = _read_tools_app(monkeypatch, model, with_retrieval=False)

    resp = client.post("/turn", json={"message": "overtime?"}, headers=HEADERS)
    assert resp.status_code == 200
    assert _parse_sse(resp.text)[-1]["data"]["status"] == "done"

    import anyio

    trail = anyio.run(store.load_trail, SESSION_ID)
    assert trail[0].tool_name == "searchBlueprints"
    assert trail[0].status == "error"
    assert trail[0].error_code == "RETRIEVAL_TOOL_UNAVAILABLE"
    assert mcp.calls == []  # advertised-but-unwired never hits the MCP


# ---------------------------------------------------------------------------
# runBlueprint registry wiring (runblueprint-design §5, Slice B, reviewer B1):
# runBlueprint is wired ONLY when retrieval is active — it shares the pipeline's
# vector_index (getBlueprint) + the per-request dispatcher (per-node runQuery).
# Absent retrieval it is advertised but returns RUN_BLUEPRINT_UNAVAILABLE, never
# an MCP unknown-tool denial. Mirrors the read-tools wiring smoke tests above.
# ---------------------------------------------------------------------------

_E = "dbpcm_warehouse.employee"
_AVG_TEMPLATE = (
    "SELECT Department AS department, AVG(AnnualSalary) AS avg_salary, "
    "COUNT(DISTINCT EmployeeCode) AS headcount "
    "FROM dbpcm_warehouse.employee WHERE Department = {department} GROUP BY Department"
)


def _run_blueprint_app(
    monkeypatch, model_client: ScriptedModelClient, *, with_retrieval: bool
) -> tuple[TestClient, InMemorySessionStore, FakeMCPClient]:
    from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
    from data_agent.runtime.retrieval.models import BlueprintDetail
    from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
    from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
    from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

    monkeypatch.setattr(app_module, "verify_jwt", lambda *args, **kwargs: frozenset())
    mcp_client = FakeMCPClient(
        tools=[
            MCPToolSpec(
                name="runQuery", description="", input_schema={"type": "object", "properties": {}}
            )
        ],
        scripted={
            "runQuery": [
                {"columns": ["Department"], "rows": [["Sales"]], "row_count": 1, "truncated": False},
                {"columns": ["department", "avg_salary", "headcount"], "rows": [["Sales", 60000.0, 4]], "row_count": 1, "truncated": False},
                {"columns": ["__bp_n", "__bp_d"], "rows": [[1, 1]], "row_count": 1, "truncated": False},
            ]
        },
    )
    store = InMemorySessionStore()
    retrieval = None
    if with_retrieval:
        detail = BlueprintDetail(
            id="bp-avg",
            intent="Average salary by department",
            slots_summary="department",
            uses=frozenset({f"{_E}.Department", f"{_E}.AnnualSalary", f"{_E}.EmployeeCode"}),
            status="validated",
            drift_status="clean",
            hit_count=0,
            catalog_sha="",
            slots=[{"name": "department", "type": "string", "required": True, "binds_to": f"{_E}.Department"}],
            sql_template=_AVG_TEMPLATE,
            result_grain=["Department"],
        )
        index = FakeVectorIndex(details={"bp-avg": detail})
        retrieval = RetrievalPipeline(
            embedding_client=FakeEmbeddingClient({"avg salary?": [1.0, 0.0]}),
            reranker=None,
            vector_index=index,
            user_memory=NullUserMemoryProvider(),
            recall_k=30,
            top_k_blueprints=3,
            top_k_knowledge=3,
        )
    catalog = CatalogHandle(
        {_E: {"EmployeeCode": "String", "Department": "Nullable(String)", "AnnualSalary": "Nullable(Float64)"}}
    )
    app = create_app(
        settings=RuntimeSettings(_env_file=None),
        session_store=store,
        mcp_client=mcp_client,
        model_client=model_client,
        catalog=catalog,
        retrieval=retrieval,
    )
    return TestClient(app), store, mcp_client


def test_run_blueprint_wired_when_retrieval_active_executes_verified(monkeypatch) -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="rb1",
                        name="runBlueprint",
                        arguments={"id": "bp-avg", "slot_bindings": {"department": "Sales"}},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="The average salary in Sales is $60,000."),
        ]
    )
    client, store, mcp = _run_blueprint_app(monkeypatch, model, with_retrieval=True)

    resp = client.post("/turn", json={"message": "avg salary?"}, headers=HEADERS)
    assert resp.status_code == 200
    data = _parse_sse(resp.text)[-1]["data"]
    assert data["status"] == "done"
    # (c) a blueprint scenario yields the enriched fields: non-null blueprint_use
    # (raw model slots) + a passing blueprint-gate verification badge + the
    # per-node SQL + the result table + lineage.
    assert data["blueprint_use"] == {"blueprint_id": "bp-avg", "slots": {"department": "Sales"}}
    assert data["verification"] == {
        "passed": True,
        "method": "blueprint_gate",
        "grain_checked": True,
    }
    assert data["sql"] and all(isinstance(s, str) for s in data["sql"])
    assert data["result_table"] is not None
    assert set(data["result_table"].keys()) == {
        "columns",
        "row_count",
        "truncated",
        "preview_rows",
    }
    assert data["provenance"] is not None

    import anyio

    trail = anyio.run(store.load_trail, SESSION_ID)
    assert trail[0].tool_name == "runBlueprint"
    # HANDLED by the executor through the composition root (NOT the unwired path):
    # a verified result, not RUN_BLUEPRINT_UNAVAILABLE.
    assert trail[0].status == "ok"
    assert trail[0].error_code != "RUN_BLUEPRINT_UNAVAILABLE"
    # The three inner runQuery probes went to the MCP; runBlueprint itself never did.
    assert [c.tool_name for c in mcp.calls] == ["runQuery", "runQuery", "runQuery"]


def test_run_blueprint_unavailable_when_retrieval_absent(monkeypatch) -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="rb1",
                        name="runBlueprint",
                        arguments={"id": "bp-avg", "slot_bindings": {"department": "Sales"}},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="I'll use the raw tools instead."),
        ]
    )
    client, store, mcp = _run_blueprint_app(monkeypatch, model, with_retrieval=False)

    resp = client.post("/turn", json={"message": "avg salary?"}, headers=HEADERS)
    assert resp.status_code == 200
    assert _parse_sse(resp.text)[-1]["data"]["status"] == "done"

    import anyio

    trail = anyio.run(store.load_trail, SESSION_ID)
    assert trail[0].tool_name == "runBlueprint"
    assert trail[0].status == "error"
    assert trail[0].error_code == "RUN_BLUEPRINT_UNAVAILABLE"
    assert mcp.calls == []  # advertised-but-unwired never hits the MCP


# ---------------------------------------------------------------------------
# UI Slice 1 — enriched `/turn` `result` event
# (docs/decisions/ui-slice1-enriched-result-contract.md). The blueprint case is
# covered by test_run_blueprint_wired_when_retrieval_active_executes_verified
# above (non-null blueprint_use + verification.passed); here we cover the
# raw-loop case (a dispatched runQuery answer) and unit-test the serializer.
# ---------------------------------------------------------------------------

_RAW_SQL = "SELECT AVG(AnnualSalary) AS avg_salary FROM dbpcm_warehouse.employee WHERE Department = 'Sales'"


def _raw_loop_app(
    monkeypatch, model_client: ScriptedModelClient
) -> tuple[TestClient, InMemorySessionStore, FakeMCPClient]:
    """A minimal raw-loop app: a runQuery MCP tool + a catalog rich enough for
    the provenance extractor to determine a non-null USES set from `_RAW_SQL`."""
    monkeypatch.setattr(app_module, "verify_jwt", lambda *args, **kwargs: frozenset())
    mcp_client = FakeMCPClient(
        tools=[
            MCPToolSpec(
                name="runQuery", description="", input_schema={"type": "object", "properties": {}}
            )
        ],
        scripted={
            "runQuery": [
                {
                    "columns": ["avg_salary"],
                    "rows": [[60000.0]],
                    "row_count": 1,
                    "truncated": False,
                }
            ]
        },
    )
    store = InMemorySessionStore()
    catalog = CatalogHandle(
        {_E: {"EmployeeCode": "String", "Department": "Nullable(String)", "AnnualSalary": "Nullable(Float64)"}}
    )
    app = create_app(
        settings=RuntimeSettings(_env_file=None),
        session_store=store,
        mcp_client=mcp_client,
        model_client=model_client,
        catalog=catalog,
    )
    return TestClient(app), store, mcp_client


def test_raw_loop_turn_enriched_result_no_blueprint(monkeypatch) -> None:
    """(d) A raw-loop answer (a dispatched runQuery, no blueprint) yields
    blueprint_use==null + verification==null + non-null sql/result_table/
    provenance."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[ToolCallRequest(id="q1", name="runQuery", arguments={"sql": _RAW_SQL})]
            ),
            ModelTurnResult(assistant_text="The average salary in Sales is $60,000."),
        ]
    )
    client, store, mcp = _raw_loop_app(monkeypatch, model)

    resp = client.post("/turn", json={"message": "avg salary?"}, headers=HEADERS)
    assert resp.status_code == 200
    data = _parse_sse(resp.text)[-1]["data"]

    assert data["status"] == "done"
    assert data["blueprint_use"] is None
    assert data["verification"] is None
    assert data["sql"] == [_RAW_SQL]
    assert data["result_table"] == {
        "columns": ["avg_salary"],
        "row_count": 1,
        "truncated": False,
        "preview_rows": [[60000.0]],
    }
    # The scope-enforced extractor determined the USES set (sorted db.table.column).
    assert data["provenance"] == [
        "dbpcm_warehouse.employee.AnnualSalary",
        "dbpcm_warehouse.employee.Department",
    ]


def test_outcome_to_dict_projects_provenance_and_result_preview() -> None:
    """Unit test: `_outcome_to_dict` projects the frozenset provenance to sorted
    `"db.table.column"` strings and a `ResultPreview` via `.to_doc()`, and passes
    the other new fields through as-is."""
    from data_agent.runtime.app import _outcome_to_dict
    from data_agent.runtime.loop.agent_loop import TurnOutcome
    from data_agent.runtime.session.models import ResultPreview

    preview = ResultPreview(
        columns=["department", "headcount"],
        row_count=3,
        truncated=False,
        preview_rows=[["Engineering", 3], ["Sales", 3]],
    )
    outcome = TurnOutcome(
        status="done",
        assistant_text="ans",
        pending_question=None,
        tool_calls_made=1,
        sql=["SELECT 1"],
        result_table=preview,
        blueprint_use={"blueprint_id": "bp", "slots": {"period": "2026-05"}},
        verification={"passed": True, "method": "blueprint_gate", "grain_checked": True},
        # Deliberately UNSORTED input to prove the projection sorts.
        provenance=frozenset({("hr.employees", "id"), ("hr.employees", "department")}),
    )

    doc = _outcome_to_dict(outcome)

    assert doc["status"] == "done"
    assert doc["assistant_text"] == "ans"
    assert doc["pending_question"] is None
    assert doc["tool_calls_made"] == 1
    assert doc["sql"] == ["SELECT 1"]
    assert doc["result_table"] == preview.to_doc()
    assert doc["blueprint_use"] == {"blueprint_id": "bp", "slots": {"period": "2026-05"}}
    assert doc["verification"] == {"passed": True, "method": "blueprint_gate", "grain_checked": True}
    assert doc["provenance"] == ["hr.employees.department", "hr.employees.id"]


_MULTI_SQL_A = (
    "SELECT AVG(AnnualSalary) AS avg_salary FROM dbpcm_warehouse.employee "
    "WHERE Department = 'Sales'"
)
_MULTI_SQL_B = "SELECT COUNT(DISTINCT EmployeeCode) AS headcount FROM dbpcm_warehouse.employee"


def test_raw_loop_multi_query_sql_list_ordered_and_deduped(monkeypatch) -> None:
    """QA gap (contract §1 fork 1): a turn running several successful runQuery
    statements surfaces them as a LIST in execution order, deduped preserving
    first occurrence (a re-run of the identical string shows once). Exercises
    the in-loop `turn_sql` accumulator's dedup branch, which no prior test hit.
    Provenance is the fail-closed UNION across all successful queries."""
    monkeypatch.setattr(app_module, "verify_jwt", lambda *args, **kwargs: frozenset())
    mcp_client = FakeMCPClient(
        tools=[
            MCPToolSpec(
                name="runQuery", description="", input_schema={"type": "object", "properties": {}}
            )
        ],
        scripted={
            "runQuery": [
                {"columns": ["avg_salary"], "rows": [[60000.0]], "row_count": 1, "truncated": False},
                {"columns": ["headcount"], "rows": [[9]], "row_count": 1, "truncated": False},
                # The duplicate A is still dispatched (dedup is on the SQL list,
                # not on dispatch), so it needs its own scripted result.
                {"columns": ["avg_salary"], "rows": [[60000.0]], "row_count": 1, "truncated": False},
            ]
        },
    )
    store = InMemorySessionStore()
    catalog = CatalogHandle(
        {_E: {"EmployeeCode": "String", "Department": "Nullable(String)", "AnnualSalary": "Nullable(Float64)"}}
    )
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="q1", name="runQuery", arguments={"sql": _MULTI_SQL_A}),
                    ToolCallRequest(id="q2", name="runQuery", arguments={"sql": _MULTI_SQL_B}),
                    # Same string as q1 — must NOT appear twice in the list.
                    ToolCallRequest(id="q3", name="runQuery", arguments={"sql": _MULTI_SQL_A}),
                ]
            ),
            ModelTurnResult(assistant_text="Sales avg is $60,000 across 9 people."),
        ]
    )
    app = create_app(
        settings=RuntimeSettings(_env_file=None),
        session_store=store,
        mcp_client=mcp_client,
        model_client=model,
        catalog=catalog,
    )
    client = TestClient(app)

    resp = client.post("/turn", json={"message": "avg + headcount?"}, headers=HEADERS)
    assert resp.status_code == 200
    data = _parse_sse(resp.text)[-1]["data"]

    assert data["status"] == "done"
    # Execution order preserved; the duplicate A is collapsed to one occurrence.
    assert data["sql"] == [_MULTI_SQL_A, _MULTI_SQL_B]
    # result_table is the LAST successful preview (the dup-A re-run here).
    assert data["result_table"]["columns"] == ["avg_salary"]
    assert data["blueprint_use"] is None
    assert data["verification"] is None
    # Fail-closed UNION across BOTH distinct queries' provenance (sorted).
    assert data["provenance"] == [
        "dbpcm_warehouse.employee.AnnualSalary",
        "dbpcm_warehouse.employee.Department",
        "dbpcm_warehouse.employee.EmployeeCode",
    ]


def test_outcome_to_dict_null_enrichment_projects_to_null() -> None:
    """A bare `TurnOutcome` (all 5 new fields defaulting to None) serializes each
    to JSON null — the backward-compatible / partial-turn shape."""
    from data_agent.runtime.app import _outcome_to_dict
    from data_agent.runtime.loop.agent_loop import TurnOutcome

    doc = _outcome_to_dict(
        TurnOutcome(
            status="paused_ask_user",
            assistant_text=None,
            pending_question={"question": "q?", "options": None},
            tool_calls_made=0,
        )
    )
    for key in ("sql", "result_table", "blueprint_use", "verification", "provenance"):
        assert doc[key] is None


# ---------------------------------------------------------------------------
# UI Slice 3 — GET /session/history (read-only provenance transcript endpoint).
# docs/decisions/ui-slice3-history-lineage-contract.md §1/§7.
# ---------------------------------------------------------------------------

import anyio  # noqa: E402

from data_agent.runtime.session.models import (  # noqa: E402
    PauseCheckpoint,
    ResultPreview,
    TrailEntry,
    TurnMessage,
)

_HR_DEPT = ("hr.employees", "department")
_HR_SALARY = ("hr.employees", "salary")


def _history_client(monkeypatch, store: InMemorySessionStore, column_scope: frozenset) -> TestClient:
    """A minimal app wired to a pre-seeded store, with `verify_jwt` returning a
    fixed *column_scope* so the endpoint's D44 read-surface filter is exercised
    end-to-end under a chosen scope."""
    monkeypatch.setattr(app_module, "verify_jwt", lambda *a, **k: column_scope)
    app = create_app(
        settings=RuntimeSettings(_env_file=None),
        session_store=store,
        mcp_client=FakeMCPClient(tools=[], scripted={}),
        model_client=ScriptedModelClient([]),
        catalog=CatalogHandle({}),
    )
    return TestClient(app)


def _seed_two_turn_store() -> InMemorySessionStore:
    """A store with two completed turns: turn 0 (department, in a narrow scope)
    and turn 1 (salary, out of a department-only scope)."""
    store = InMemorySessionStore()

    async def _seed() -> None:
        await store.append_message(SESSION_ID, TurnMessage(0, "user", "headcount by dept?", "t"))
        await store.append_trail_entry(
            SESSION_ID,
            TrailEntry(
                turn_index=0,
                tool_call_id="c0",
                tool_name="runQuery",
                args={"sql": "SELECT department FROM hr.employees"},
                status="ok",
                error_code=None,
                provenance=frozenset({_HR_DEPT}),
                result_preview=ResultPreview(
                    columns=["department", "headcount"],
                    row_count=1,
                    truncated=False,
                    preview_rows=[["Engineering", 3]],
                ),
                result_full_ref=None,
                ts="t",
            ),
        )
        await store.append_message(
            SESSION_ID, TurnMessage(0, "assistant", "Engineering 3.", "t", frozenset({_HR_DEPT}))
        )
        await store.append_message(SESSION_ID, TurnMessage(1, "user", "salaries?", "t"))
        await store.append_trail_entry(
            SESSION_ID,
            TrailEntry(
                turn_index=1,
                tool_call_id="c1",
                tool_name="runQuery",
                args={"sql": "SELECT salary FROM hr.employees"},
                status="ok",
                error_code=None,
                provenance=frozenset({_HR_SALARY}),
                result_preview=ResultPreview(
                    columns=["salary"], row_count=1, truncated=False, preview_rows=[[85000]]
                ),
                result_full_ref=None,
                ts="t",
            ),
        )
        await store.append_message(
            SESSION_ID, TurnMessage(1, "assistant", "Jane earns $85,000.", "t", frozenset({_HR_SALARY}))
        )

    anyio.run(_seed)
    return store


def test_history_missing_auth_header_returns_401(monkeypatch) -> None:
    client = _history_client(monkeypatch, InMemorySessionStore(), frozenset())
    resp = client.get("/session/history", headers={"X-Session-Id": SESSION_ID})
    assert resp.status_code == 401


def test_history_missing_session_id_header_returns_400(monkeypatch) -> None:
    client = _history_client(monkeypatch, InMemorySessionStore(), frozenset())
    resp = client.get("/session/history", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 400


def test_history_unknown_session_returns_200_empty_turns(monkeypatch) -> None:
    client = _history_client(monkeypatch, InMemorySessionStore(), frozenset())
    resp = client.get("/session/history", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == SESSION_ID
    assert body["turns"] == []
    assert body["pending_question"] is None


def test_history_seeded_multi_turn_shape_allow_all(monkeypatch) -> None:
    store = _seed_two_turn_store()
    client = _history_client(monkeypatch, store, frozenset())  # allow-all
    resp = client.get("/session/history", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert [t["turn_index"] for t in body["turns"]] == [0, 1]

    t0 = body["turns"][0]
    assert t0["question"] == "headcount by dept?"
    assert t0["answer"] == "Engineering 3."
    assert t0["provenance_union"] == ["hr.employees.department"]
    assert len(t0["tool_calls"]) == 1
    assert t0["tool_calls"][0]["sql"] == "SELECT department FROM hr.employees"
    assert t0["tool_calls"][0]["provenance"] == ["hr.employees.department"]
    assert t0["tool_calls"][0]["result_table"]["columns"] == ["department", "headcount"]

    t1 = body["turns"][1]
    assert t1["answer"] == "Jane earns $85,000."
    assert t1["provenance_union"] == ["hr.employees.salary"]


def test_history_narrowed_scope_withholds_out_of_scope_turn(monkeypatch) -> None:
    """The end-to-end D44 read-surface assertion: reading the SAME seeded session
    under a department-only scope withholds turn 1's salary answer + tool-call,
    while turn 0 (department) survives — and turn 1's question still renders."""
    store = _seed_two_turn_store()
    client = _history_client(monkeypatch, store, frozenset({"hr.employees.department"}))
    resp = client.get("/session/history", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()

    # Both turns still render their question (user msg always survives).
    assert [t["turn_index"] for t in body["turns"]] == [0, 1]
    t0, t1 = body["turns"]
    assert t0["answer"] == "Engineering 3."  # in scope
    assert len(t0["tool_calls"]) == 1

    assert t1["question"] == "salaries?"  # question survives
    assert t1["answer"] is None  # salary answer withheld
    assert t1["provenance_union"] is None
    assert t1["tool_calls"] == []  # salary tool-call omitted


def test_history_paused_session_pending_question(monkeypatch) -> None:
    store = InMemorySessionStore()

    async def _seed() -> None:
        await store.append_message(SESSION_ID, TurnMessage(0, "user", "payroll?", "t"))
        await store.write_pause_checkpoint(
            SESSION_ID,
            PauseCheckpoint(
                reason="askUser",
                pending_question={"question": "Which department?", "options": None},
                awaiting="user_answer",
                consumed=False,
            ),
        )

    anyio.run(_seed)
    client = _history_client(monkeypatch, store, frozenset())
    resp = client.get("/session/history", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    # Paused turn: question present, answer null (no assistant message yet).
    assert body["turns"][0]["question"] == "payroll?"
    assert body["turns"][0]["answer"] is None
    assert body["pending_question"] == {"question": "Which department?", "options": None}
