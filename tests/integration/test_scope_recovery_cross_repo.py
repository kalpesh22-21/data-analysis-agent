"""Opt-in HTTP integration with the sibling clickhouse-api auth/MCP server.

Run with CLICKHOUSE_API_REPO and both repositories on PYTHONPATH, using an
interpreter with both projects' dependencies. Warehouse rows and model/judge
responses are deterministic fixtures; HTTP, MCP SDK, signed scope mint/verify,
SessionCatalog, dispatcher, agent loop and SSE delivery are real.
"""

import asyncio
import os
import socket
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import uvicorn

API = os.environ.get("CLICKHOUSE_API_REPO")
pytestmark = pytest.mark.skipif(not API, reason="Set CLICKHOUSE_API_REPO for cross-repo HTTP test")


@pytest.fixture
async def rig(monkeypatch):
    sys.path.append(str(Path(API)))
    import app.session_catalog as api_catalog
    from app.catalog_scope import CatalogScope
    from app.config import Settings
    from app.principal import Principal

    from data_agent.runtime.auth.credentials import RuntimeCredentials
    from data_agent.runtime.catalog.session_catalog import SessionCatalog
    from data_agent.runtime.config import RuntimeSettings
    from data_agent.runtime.mcp.real_client import RealMCPClient
    from data_agent.runtime.session.memory_store import InMemorySessionStore

    claims = {
        "sub": "fixture-user",
        "clientcode": "fixture-client",
        "jti": "fixture-jti",
        "user_name": "fixture-user",
        "proc_center": "pc",
    }
    principal = Principal(claims["sub"], claims)
    state = {"sha": "A", "mints": 0, "executions": 0, "deny": None}
    settings = Settings(
        _env_file=None,
        mcp_service_key="local-test-only-signing-key-at-least-32-bytes",
        require_sid_binding=False,
        trino_enabled=False,
        clickhouse_host="localhost",
        clickhouse_user="fixture",
        employee_access_enabled=False,
    )
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    import app.mcp_server as server

    # Identity verification is the fixture boundary; real scope signatures and
    # owner/session binding are checked by production middleware on every call.
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "validate_token", lambda *a, **k: principal)
    monkeypatch.setattr(
        "data_agent.runtime.catalog.session_catalog.verify_jwt_claims", lambda *a, **k: claims
    )
    catalog = {
        "hr.employee": {
            "database": "hr",
            "table": "employee",
            "columns": {
                "Department": {"type": "String"},
                "Id": {"type": "Int64"},
                "Salary": {"type": "Int64"},
            },
        }
    }
    monkeypatch.setattr(api_catalog, "get_semantic_catalog", lambda: catalog)
    monkeypatch.setattr(api_catalog, "get_catalog_sha", lambda: state["sha"])

    def permissions(p):
        state["mints"] += 1
        return replace(
            p,
            catalog_scope=CatalogScope(
                columns=frozenset("hr.employee." + c for c in catalog["hr.employee"]["columns"])
            ),
        )

    monkeypatch.setattr(api_catalog, "resolve_principal_permissions", permissions)
    original_verify = api_catalog.verify_scope

    def verify(*a, **k):
        if state["deny"]:
            raise api_catalog.SessionScopeError(state["deny"])
        return original_verify(*a, **k)

    monkeypatch.setattr(api_catalog, "verify_scope", verify)

    def execute(*a, **k):
        state["executions"] += 1
        return {
            "columns": ["Department", "n"],
            "rows": [["Sales", 120]],
            "row_count": 1,
            "truncated": False,
            "provenance": {"version": 1, "columns": [["hr.employee", "Department"]]},
        }

    monkeypatch.setattr(server, "svc_run_query", execute)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    app = server.JWTAuthMiddleware(server.create_http_app(), settings=settings)
    uv = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(uv.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if uv.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        assert uv.started
        store = InMemorySessionStore()
        url = f"http://127.0.0.1:{port}/mcp"
        manager = SessionCatalog(RuntimeSettings(_env_file=None, mcp_url=url), store)
        creds = RuntimeCredentials("scope-http-test", "fixture-bearer", frozenset())
        snapshot = await manager.ensure(jwt=creds.jwt, session_id=creds.session_id)
        client = RealMCPClient(url)
        client.scope_token_provider = manager.scope_token
        client.scope_token_refresher = manager.refresh_scope_token
        yield state, client, manager, creds, store, snapshot
    finally:
        uv.should_exit = True
        await task
        sock.close()


async def test_catalog_rollover_recovers_real_http_query_and_discovery(rig):
    state, client, manager, creds, _, old = rig
    state["sha"] = "B"
    result = await client.call_tool(
        "runQuery", {"sql": "SELECT 120"}, jwt=creds.jwt, session_id=creds.session_id
    )
    assert result["rows"] == [["Sales", 120]]
    assert state["mints"] == 2 and state["executions"] == 1
    state["sha"] = "C"
    assert await client.list_tools(jwt=creds.jwt, session_id=creds.session_id)
    assert state["mints"] == 3


@pytest.mark.parametrize("code", ["SESSION_SCOPE_INVALID", "SESSION_BINDING_MISMATCH"])
async def test_other_http_denials_do_not_remint(rig, code):
    state, client, _, creds, _, _ = rig
    state["deny"] = code
    with pytest.raises(BaseExceptionGroup):
        await client.call_tool(
            "runQuery", {"sql": "SELECT 120"}, jwt=creds.jwt, session_id=creds.session_id
        )
    assert state["mints"] == 1 and state["executions"] == 0


@pytest.mark.parametrize("resume", [False, True])
async def test_real_loop_delivers_reviewed_document_after_rollover(rig, resume):
    from data_agent.runtime.app import _stream_turn
    from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
    from data_agent.runtime.observability.progress import ProgressEmitter
    from tests.runtime.test_answer_document import final, section
    from tests.runtime.test_harness_improvements import (
        CATALOG,
        Judge,
        batch,
        build,
        call,
        discovery,
        query,
    )

    state, client, manager, creds, store, _ = rig
    creds = await manager.credentials(creds)
    steps = [discovery(), query(), final(section())]
    if resume:
        steps.insert(
            0,
            batch(
                call(
                    "askUser", "ask", question="Which department?", options=["Sales", "Engineering"]
                )
            ),
        )
    loop, *_ = build(steps, Judge(), store=store)
    loop._tool_dispatcher = ToolDispatcher(client, CATALOG)
    if resume:
        paused = await loop.run(
            session_id=creds.session_id,
            credentials=creds,
            user_message="How many employees in my chosen department?",
        )
        assert paused.status == "paused_ask_user"
    state["sha"] = "B"
    emitter = ProgressEmitter()

    async def turn():
        if resume:
            return await loop.resume(session_id=creds.session_id, credentials=creds, answer="Sales")
        return await loop.run(
            session_id=creds.session_id,
            credentials=creds,
            user_message="How many employees by department?",
        )

    async def scope_matches():
        return (await manager.credentials(creds)).column_scope == creds.column_scope

    stream = "".join(
        [frame async for frame in _stream_turn(turn, emitter, delivery_scope_check=scope_matches)]
    )
    assert "event: result" in stream and "answer_document" in stream
    assert state["mints"] == 2 and state["executions"] == 1


async def test_persistent_stale_http_stops_after_one_refresh(rig):
    from data_agent.runtime.mcp.client import MCPToolError

    state, client, _, creds, _, _ = rig
    state["deny"] = "SESSION_SCOPE_STALE"
    with pytest.raises(MCPToolError, match=".") as exc:
        await client.call_tool(
            "runQuery", {"sql": "SELECT 120"}, jwt=creds.jwt, session_id=creds.session_id
        )
    assert exc.value.code == "SESSION_SCOPE_STALE"
    assert state["mints"] == 2 and state["executions"] == 0


async def test_blueprint_inner_grain_probe_recovers(rig, monkeypatch):
    import app.mcp_server as server

    from data_agent.runtime.blueprint.executor import BlueprintExecutor, ExecCompleted
    from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
    from data_agent.runtime.retrieval.models import BlueprintDetail
    from data_agent.runtime.retrieval.vector_index import FakeVectorIndex
    from tests.runtime.test_harness_improvements import CATALOG

    state, client, manager, creds, _, _ = rig
    creds = await manager.credentials(creds)
    statements = []
    logical_calls = []
    original_call = client.call_tool

    async def tracked(name, args, **kwargs):
        logical_calls.append((name, args))
        return await original_call(name, args, **kwargs)

    monkeypatch.setattr(client, "call_tool", tracked)

    def execute(*args, **kwargs):
        statements.append(args[0])
        state["executions"] += 1
        if state["executions"] == 1:
            # Deploy after the data query, before the executor's grain check.
            state["sha"] = "B"
            return {
                "columns": ["Department", "n"],
                "rows": [["Sales", 120]],
                "row_count": 1,
                "truncated": False,
                "provenance": {"version": 1, "columns": [["hr.employee", "Department"]]},
            }
        return {
            "columns": ["__bp_n", "__bp_d"],
            "rows": [[1, 1]],
            "row_count": 1,
            "truncated": False,
            "provenance": {"version": 1, "columns": [["hr.employee", "Department"]]},
        }

    monkeypatch.setattr(server, "svc_run_query", execute)
    detail = BlueprintDetail(
        id="bp-scope",
        intent="headcount",
        slots_summary="",
        uses=frozenset({"hr.employee.Department"}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        slots=[],
        sql_template="SELECT Department, count() AS n FROM hr.employee GROUP BY Department",
        result_grain=["Department"],
    )
    index = FakeVectorIndex()
    index.add_detail(detail)
    executor = BlueprintExecutor(
        tool_dispatcher=ToolDispatcher(client, CATALOG), vector_index=index
    )
    result = await executor.execute(blueprint_id=detail.id, slot_bindings={}, credentials=creds)
    assert isinstance(result, ExecCompleted), result
    assert state["mints"] == 2 and state["executions"] == 2, repr((statements, logical_calls))


@pytest.mark.parametrize("failure", [False, True])
async def test_delivery_never_emits_result_after_scope_change_or_check_failure(failure):
    from data_agent.runtime.app import _stream_turn
    from data_agent.runtime.observability.progress import ProgressEmitter

    async def turn():
        return object()  # Must never be serialized.

    async def changed():
        if failure:
            raise RuntimeError("fixture failure")
        return False

    frames = "".join(
        [f async for f in _stream_turn(turn, ProgressEmitter(), delivery_scope_check=changed)]
    )
    assert "event: result" not in frames
    assert "event: error" in frames
    assert ("INTERNAL_ERROR" if failure else "SESSION_SCOPE_CHANGED") in frames


async def test_http_refresh_with_changed_grants_does_not_execute_query(rig, monkeypatch):
    import app.session_catalog as api_catalog
    from app.catalog_scope import CatalogScope

    from data_agent.runtime.mcp.client import MCPToolError

    state, client, manager, creds, _, _ = rig
    original = api_catalog.resolve_principal_permissions

    def narrowed(principal):
        principal = original(principal)
        return replace(principal, catalog_scope=CatalogScope(columns=frozenset({"hr.employee.Id"})))

    monkeypatch.setattr(api_catalog, "resolve_principal_permissions", narrowed)
    state["sha"] = "B"
    with pytest.raises(MCPToolError) as exc:
        await client.call_tool(
            "runQuery",
            {"sql": "SELECT Department FROM hr.employee"},
            jwt=creds.jwt,
            session_id=creds.session_id,
        )
    assert exc.value.code == "SESSION_SCOPE_CHANGED"
    assert state["mints"] == 2 and state["executions"] == 0
    assert (await manager.credentials(creds)).column_scope == frozenset({"hr.employee.Id"})
