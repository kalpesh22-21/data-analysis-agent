"""Opt-in I7 audit: real MCP HTTP, API validation/conversion, and local Trino.

Set CLICKHOUSE_API_REPO and RUN_TRINO_AGGREGATION=1. Requires the seeded
battery-trino/MySQL containers on localhost:18081. Identity and shard inventory
are local fixtures; database results are never mocked. No warehouse writes.
"""

import asyncio
import os
import socket
from decimal import Decimal
from pathlib import Path

import pytest
import sqlglot
import uvicorn

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_TRINO_AGGREGATION") != "1" or not os.environ.get("CLICKHOUSE_API_REPO"),
    reason="Requires local seeded Trino and CLICKHOUSE_API_REPO",
)

GOOD = "SELECT sum(e.annual_salary) AS total FROM dbpcm_warehouse.employee e JOIN dbpcm_warehouse.department d ON e.department_code=d.department_code"
BAD = "SELECT sum(e.annual_salary) AS total FROM dbpcm_warehouse.employee e JOIN dbpcm_warehouse.employee d ON e.department_code=d.department_code"


@pytest.fixture
async def trino_rig(monkeypatch, tmp_path):
    monkeypatch.setenv("CLICKHOUSE_HOST", "localhost")
    monkeypatch.setenv("CLICKHOUSE_USER", "default")
    monkeypatch.setenv("EMPLOYEE_ACCESS_ENABLED", "false")
    api = Path(os.environ["CLICKHOUSE_API_REPO"])
    monkeypatch.syspath_prepend(str(api))
    from app import trino_client
    from app.catalog_scope import CatalogScope
    from app.config import Settings
    from app.errors import ClickHouseAPIError
    from app.principal import Principal, current_principal
    from app.semantic_catalog import get_semantic_catalog
    from app.service import run_query
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

    from data_agent.runtime.auth.credentials import RuntimeCredentials
    from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
    from data_agent.runtime.mcp.real_client import RealMCPClient
    from data_agent.runtime.provenance.catalog_handle import CatalogHandle

    settings = Settings(
        _env_file=None,
        clickhouse_host="localhost",
        clickhouse_user="default",
        employee_access_enabled=False,
        trino_enabled=True,
        trino_security_ready=True,
        trino_host="localhost",
        trino_port=18081,
        trino_http_scheme="http",
        trino_request_timeout=2,
        trino_mysql_sources_dir=str(api / "app/semantic_catalog/mysql_views"),
        trino_diagnostics_dir=str(tmp_path),
    )
    monkeypatch.setattr(
        trino_client, "resolve_mysql_shard", lambda *a: "online__local__01__dbonline"
    )
    catalog = get_semantic_catalog()
    tables = {
        k: v
        for k, v in catalog.items()
        if k in {"dbpcm_warehouse.employee", "dbpcm_warehouse.department"}
    }
    columns = frozenset(f"{t}.{c}" for t, entry in tables.items() for c in entry["columns"])
    principal = Principal(
        "i7-fixture",
        {
            "sub": "i7-fixture",
            "clientcode": "CLIENT_A",
            "proc_center": "PC01",
            "user_name": "alice",
            "jti": "TESTJTI001",
        },
        catalog_scope=CatalogScope(columns=columns),
    )
    state = {"sql": [], "results": [], "query_ids": [], "unavailable": False}
    original_id = trino_client.record_query_id

    def record_id(value):
        if value:
            state["query_ids"].append(value)
        original_id(value)

    monkeypatch.setattr(trino_client, "record_query_id", record_id)
    mcp = FastMCP("i7-live", stateless_http=True, json_response=True)

    @mcp.tool(name="runQuery")
    def execute(sql: str, limit: int | None = None) -> dict:
        state["sql"].append(sql)
        token = current_principal.set(principal)
        try:
            active = (
                settings.model_copy(update={"trino_port": 1}) if state["unavailable"] else settings
            )
            result = run_query(sql, limit, active, include_provenance=True)
            state["results"].append(result)
            return result
        except ClickHouseAPIError as exc:
            raise ToolError(f"[{exc.code}] {exc.message}") from None
        finally:
            current_principal.reset(token)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), log_level="critical"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        assert server.started
        dispatcher = ToolDispatcher(
            RealMCPClient(f"http://127.0.0.1:{port}/mcp"),
            CatalogHandle(
                {
                    t: {c: v["type"] for c, v in entry["columns"].items()}
                    for t, entry in tables.items()
                }
            ),
        )
        creds = RuntimeCredentials("i7-test", "fixture-bearer", columns)
        yield dispatcher, creds, state
    finally:
        server.should_exit = True
        await task
        sock.close()


@pytest.mark.parametrize("mode", ["valid", "duplicate", "unavailable"])
async def test_trino_join_verification(trino_rig, mode):
    from data_agent.runtime.loop.measurement import validate_join_cardinality

    dispatcher, creds, state = trino_rig
    sql = BAD if mode == "duplicate" else GOOD
    state["unavailable"] = mode == "unavailable"
    issue = await validate_join_cardinality(sql, dispatcher, creds)
    assert state["sql"] and all("system." not in q.lower() for q in state["sql"])
    assert sql not in state["sql"]  # Verification has not executed the aggregate.
    if mode == "valid":
        assert issue is None, issue.denial_detail if issue else None
        assert state["query_ids"] and state["results"][0]["rows"] == [[4, 4]]
        result = await dispatcher.dispatch("runQuery", {"sql": sql}, creds)
        assert result.status == "ok", result
        assert Decimal(str(result.result_full["rows"][0][0])) > 0
    else:
        expected = (
            "AGGREGATION_DUPLICATE_KEYS" if mode == "duplicate" else "AGGREGATION_CHECK_UNAVAILABLE"
        )
        assert issue is not None and issue.code == expected, issue
        if mode == "duplicate":
            assert state["query_ids"] and state["results"][0]["rows"] == [[24, 3]]
        else:
            assert not issue.retryable and not state["results"]
    print(
        mode, "verdict", issue.code if issue else "verified", "Trino query IDs", state["query_ids"]
    )


@pytest.mark.parametrize("mode", ["valid", "duplicate", "unavailable"])
async def test_blueprint_blocks_unverified_aggregate_on_trino(trino_rig, mode):
    from data_agent.runtime.blueprint.executor import BlueprintExecutor, ExecCompleted, ExecFailed
    from data_agent.runtime.retrieval.models import BlueprintDetail
    from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

    dispatcher, creds, state = trino_rig
    sql = BAD if mode == "duplicate" else GOOD
    state["unavailable"] = mode == "unavailable"
    detail = BlueprintDetail(
        id="bp-i7-test",
        intent="salary total",
        slots_summary="",
        uses=creds.column_scope,
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        slots=[],
        sql_template=sql,
        result_grain=[],
    )
    index = FakeVectorIndex()
    index.add_detail(detail)
    outcome = await BlueprintExecutor(tool_dispatcher=dispatcher, vector_index=index).execute(
        blueprint_id=detail.id, slot_bindings={}, credentials=creds
    )
    if mode == "valid":
        assert isinstance(outcome, ExecCompleted), outcome
        assert (
            any(
                sqlglot.parse_one(q, read="clickhouse") == sqlglot.parse_one(sql, read="clickhouse")
                for q in state["sql"]
            )
            and len(state["query_ids"]) >= 2
        )
    else:
        assert isinstance(outcome, ExecFailed), outcome
        assert outcome.error_code == (
            "AGGREGATION_DUPLICATE_KEYS" if mode == "duplicate" else "AGGREGATION_CHECK_UNAVAILABLE"
        )
        assert sql not in state["sql"]
    print("blueprint", mode, "Trino query IDs", state["query_ids"])
