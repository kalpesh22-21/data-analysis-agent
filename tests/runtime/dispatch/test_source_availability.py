import asyncio
import json
from types import SimpleNamespace

from data_agent.runtime.context.scope_filter import compute_scope_hash
from data_agent.runtime.dispatch.source_availability import (
    source_availability_boundary, restore_sources, source_receipt,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.measurement import validate_join_cardinality
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from tests.runtime.dispatch.test_tool_dispatcher import CATALOG, _credentials

SOURCE = "dbpcm_warehouse.employee"
DETAIL = json.dumps({
    "version": 1, "code": "SOURCE_UNAVAILABLE", "source": SOURCE,
    "input_dialect": "clickhouse", "retry": "after_source_recovery",
    "message": "Source unavailable", "guidance": "Use independent sources.",
})
OK = {"columns": ["n"], "rows": [[2]], "row_count": 1, "provenance": {"version": 1, "columns": []}}


async def test_rewrites_and_blueprint_are_blocked_but_independent_query_and_next_turn_work():
    from tests.runtime.blueprint.test_executor import _real_executor, _detail, ExecFailed
    mcp = FakeMCPClient(scripted={"runQuery": [
        MCPToolError("SOURCE_UNAVAILABLE", DETAIL), OK, OK,
    ]})
    dispatcher = ToolDispatcher(mcp, CATALOG)

    @source_availability_boundary
    async def turn():
        first = await dispatcher.dispatch("runQuery", {"sql": f"SELECT * FROM {SOURCE}"}, _credentials())
        assert first.error_code == "SOURCE_UNAVAILABLE" and first.retryable is False
        rewrite = await dispatcher.dispatch("runQuery", {"sql": f"SELECT count(*) FROM {SOURCE} e"}, _credentials())
        assert rewrite.error_code == "SOURCE_UNAVAILABLE"
        assert source_receipt(rewrite.denial_detail) == SOURCE
        # This real executor uses the same dispatcher path as runtime blueprints.
        executor = _real_executor(mcp, _detail(slots=[],
            sql_template=f"SELECT Department FROM {SOURCE}", result_grain=["Department"], bid="bp-test"))
        result = await executor.execute(blueprint_id="bp-test", slot_bindings={}, credentials=_credentials())
        assert isinstance(result, ExecFailed) and result.error_code == "SOURCE_UNAVAILABLE"
        assert len(mcp.calls) == 1
        other = await dispatcher.dispatch("runQuery", {"sql": "SELECT count(*) FROM dbpcm_warehouse.department"}, _credentials())
        assert other.status == "ok"
    await turn()
    assert len(mcp.calls) == 2

    @source_availability_boundary
    async def next_turn():
        result = await dispatcher.dispatch("runQuery", {"sql": f"SELECT count(*) FROM {SOURCE}"}, _credentials())
        assert result.status == "ok"
    await next_turn()
    assert len(mcp.calls) == 3


async def test_resume_restores_only_same_turn_scope_and_prevents_verification_calls():
    creds = _credentials()
    entry = SimpleNamespace(turn_index=2, error_code="SOURCE_UNAVAILABLE",
        denial_detail="sql_diagnostic:" + DETAIL,
        model_response={"scope_hash": compute_scope_hash(creds.column_scope)})
    mcp = FakeMCPClient(scripted={})
    dispatcher = ToolDispatcher(mcp, CATALOG)

    @source_availability_boundary
    async def resume():
        restore_sources(SimpleNamespace(tool_trail=[entry]), 2, compute_scope_hash(creds.column_scope))
        result = await validate_join_cardinality(f"SELECT count(*) FROM {SOURCE}", dispatcher, creds)
        assert result.code == "SOURCE_UNAVAILABLE"
        assert source_receipt(result.denial_detail) == SOURCE
        assert not mcp.calls
    await resume()

    @source_availability_boundary
    async def unrelated(turn, scope):
        restore_sources(SimpleNamespace(tool_trail=[entry]), turn, scope)
        assert await validate_join_cardinality(f"SELECT count(*) FROM {SOURCE}", dispatcher, creds) is None
    await unrelated(3, compute_scope_hash(creds.column_scope))
    await unrelated(2, "other-scope")


async def test_concurrent_turns_do_not_share_failure_state():
    failed = asyncio.Event()
    mcp = FakeMCPClient(scripted={"runQuery": [MCPToolError("SOURCE_UNAVAILABLE", DETAIL), OK]})
    dispatcher = ToolDispatcher(mcp, CATALOG)

    @source_availability_boundary
    async def failing():
        await dispatcher.dispatch("runQuery", {"sql": f"SELECT * FROM {SOURCE}"}, _credentials())
        failed.set()
        await asyncio.sleep(0)
        result = await dispatcher.dispatch("runQuery", {"sql": f"SELECT * FROM {SOURCE}"}, _credentials())
        assert result.error_code == "SOURCE_UNAVAILABLE"

    @source_availability_boundary
    async def independent():
        await failed.wait()
        result = await dispatcher.dispatch("runQuery", {"sql": f"SELECT * FROM {SOURCE}"}, _credentials())
        assert result.status == "ok"
    await asyncio.gather(failing(), independent())
    assert len(mcp.calls) == 2


def test_blueprint_missing_source_receipt_supports_blocked_intent_and_resume():
    from data_agent.runtime.blueprint.executor import ExecFailed
    from data_agent.runtime.blueprint.tool import blueprint_outcome_to_tool_result
    from data_agent.runtime.composite.analysis_state import classify_block_evidence
    from data_agent.runtime.session.models import TrailEntry

    result = blueprint_outcome_to_tool_result(ExecFailed(
        "SOURCE_UNAVAILABLE", "sql_diagnostic:" + DETAIL, retryable=False))
    assert source_receipt(result.denial_detail) == SOURCE
    entry = TrailEntry(
        turn_index=0, tool_call_id="failed-bp", tool_name="runBlueprint",
        args={"id": "missing"}, status=result.status, error_code=result.error_code,
        provenance=None, result_preview=None, result_full_ref=None, ts="now",
        denial_detail=result.denial_detail,
    )
    assert classify_block_evidence(entry.tool_call_id, [entry], 0) == "EXECUTION_FAILED"
