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
    assert events[-1]["data"]["status"] == "done"
    assert events[-1]["data"]["assistant_text"] == "Here is your answer."


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
    assert first_events[-1]["data"]["status"] == "paused_ask_user"
    assert first_events[-1]["data"]["pending_question"]["question"] == "Which dept?"

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
