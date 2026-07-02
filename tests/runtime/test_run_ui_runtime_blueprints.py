"""Lower-layer (non-e2e) proof of the Layer-3 Slice-1 runBlueprint wiring in
`scripts/run_ui_runtime.py` (D89).

The Layer-3 Playwright suite (`tests/e2e/test_conformance.py`) needs a browser +
subprocesses + the `l2-token` container, so it is skip-guarded behind `RUN_E2E`.
This module proves the SAME Slice-1 machinery — the seeded blueprint corpus, the
`DemoMCPClient` content-routing, the `DemoModelClient` trigger phrases, and the
`create_app(retrieval=...)` wiring that registers `runBlueprint` — deterministically
at Layer-1, so `uv run pytest` (no env) exercises it on every run.

It drives the REAL `BlueprintExecutor` over the REAL `ToolDispatcher` over the demo
launcher's `DemoMCPClient` + seeded `FakeVectorIndex` — the identical path the wired
runtime's `runBlueprint` tool takes — and asserts each of the four scenarios'
executor outcome (verified / withheld / paused / resumed).
"""

from __future__ import annotations

import pytest
import scripts.run_ui_runtime as demo
from fastapi import FastAPI

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    VERIFY_FAILED_CODE,
    BlueprintExecutor,
    ExecCompleted,
    ExecFailed,
    ExecPaused,
)
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.provenance.catalog_handle import CatalogHandle


def _creds() -> RuntimeCredentials:
    # Allow-all scope (frozenset()) — exactly what the BFF mints (column_scope=[]).
    return RuntimeCredentials(session_id="sess-demo-bp", jwt="demo-jwt", column_scope=frozenset())


def _executor() -> BlueprintExecutor:
    """A BlueprintExecutor over the demo launcher's DemoMCPClient + seeded index —
    the identical wiring `create_app(retrieval=...)` builds behind `runBlueprint`.
    The catalog carries the demo blueprint tables (as `build_demo_app` wires them)
    so inner runQuery provenance is DETERMINED — a verified result with `None`
    provenance would be dropped by the D44 replay filter within its own turn and
    strand the model in a re-emit loop (the fast-path e2e bug this guards)."""
    mcp = demo.DemoMCPClient(tools=[], scripted={})
    dispatcher = ToolDispatcher(mcp, CatalogHandle(dict(demo._BP_TABLE_SCHEMAS)))
    index = demo.FakeVectorIndex(entries=[], details=demo.build_blueprint_details())
    return BlueprintExecutor(tool_dispatcher=dispatcher, vector_index=index)


# --------------------------------------------------------------------------
# DemoMCPClient content-routing (D-L3-2)
# --------------------------------------------------------------------------


class TestDemoMCPBlueprintRouting:
    async def test_node_query_routes_off_sql_key(self) -> None:
        mcp = demo.DemoMCPClient(tools=[], scripted={})
        result = await mcp.call_tool(
            "runQuery",
            {"sql": f"SELECT department, n FROM {demo._BP_GOOD_TABLE} GROUP BY department"},
            jwt="j",
            session_id="s",
        )
        assert result["row_count"] == 3
        assert result["columns"] == ["department", "n"]

    async def test_node_query_routes_off_query_key_too(self) -> None:
        # A model-emitted runQuery uses the `query` key; routing must match either.
        mcp = demo.DemoMCPClient(tools=[], scripted={})
        result = await mcp.call_tool(
            "runQuery",
            {"query": f"SELECT * FROM {demo._BP_TENURE_TABLE}"},
            jwt="j",
            session_id="s",
        )
        assert result["columns"] == ["department", "avg_tenure"]

    async def test_grain_probe_passes_for_good_and_fails_for_bad(self) -> None:
        mcp = demo.DemoMCPClient(tools=[], scripted={})
        good = await mcp.call_tool(
            "runQuery",
            {"sql": f"SELECT COUNT(*) AS __bp_n, COUNT(DISTINCT department) AS __bp_d "
                    f"FROM (SELECT department FROM {demo._BP_GOOD_TABLE}) AS __bp_sub"},
            jwt="j",
            session_id="s",
        )
        assert good["rows"] == [[3, 3]]  # total == distinct → PASS
        bad = await mcp.call_tool(
            "runQuery",
            {"sql": f"SELECT COUNT(*) AS __bp_n, COUNT(DISTINCT department) AS __bp_d "
                    f"FROM (SELECT department FROM {demo._BP_BAD_TABLE}) AS __bp_sub"},
            jwt="j",
            session_id="s",
        )
        assert bad["rows"] == [[12, 3]]  # fan-out → total != distinct → FAIL

    async def test_domain_probe_returns_departments(self) -> None:
        mcp = demo.DemoMCPClient(tools=[], scripted={})
        result = await mcp.call_tool(
            "runQuery",
            {"sql": f"SELECT DISTINCT department FROM {demo._BP_DEPT_DIM_TABLE} LIMIT 500"},
            jwt="j",
            session_id="s",
        )
        assert [row[0] for row in result["rows"]] == ["Sales", "Engineering", "Support"]

    async def test_bad_node_result_carries_the_leak_sentinel(self) -> None:
        # The withheld fan-out row — the D56 gate blocks it, and the Layer-3
        # scenario asserts neither the sentinel nor the figure ever reach the DOM.
        result = demo._blueprint_run_query(f"SELECT department FROM {demo._BP_BAD_TABLE}")
        assert result is not None
        assert result["rows"] == [[demo._FANOUT_LEAK_ROW, demo._FANOUT_LEAK_FIGURE]]

    async def test_non_blueprint_sql_falls_through_to_base(self) -> None:
        # A runQuery that matches no blueprint route returns None from the router,
        # so call_tool defers to the base FakeMCPClient (here: unscripted → raises).
        assert demo._blueprint_run_query("SELECT 1 FROM analytics.employees") is None

    async def test_existing_denials_still_raise(self) -> None:
        mcp = demo.DemoMCPClient(tools=[], scripted={})
        with pytest.raises(MCPToolError) as scope_exc:
            await mcp.call_tool(
                "runQuery", {"query": demo._SCOPE_DENIAL_QUERY}, jwt="j", session_id="s"
            )
        assert scope_exc.value.code == "COLUMN_SCOPE_VIOLATION"
        with pytest.raises(MCPToolError) as parse_exc:
            await mcp.call_tool(
                "runQuery", {"query": demo._PARSE_FAIL_QUERY}, jwt="j", session_id="s"
            )
        assert parse_exc.value.code == "PARSE_FAILED_CLOSED"


# --------------------------------------------------------------------------
# DemoModelClient blueprint trigger phrases (D-L3-3)
# --------------------------------------------------------------------------


class TestDemoModelBlueprintTriggers:
    def _u(self, content: str) -> dict[str, object]:
        return {"role": "user", "content": content}

    _TOOL = {"role": "tool", "tool_call_id": "x", "content": "{}"}

    async def test_fast_path_emits_run_blueprint_then_answers(self) -> None:
        model = demo.DemoModelClient()
        first = await model.send_turn([self._u("run the headcount by department blueprint")], [])
        assert first.tool_calls[0].name == "runBlueprint"
        assert first.tool_calls[0].arguments == {
            "id": demo._BP_GOOD_ID,
            "slot_bindings": {"department": "Sales"},
        }
        second = await model.send_turn(
            [self._u("run the headcount by department blueprint"), self._TOOL], []
        )
        assert not second.tool_calls
        assert second.assistant_text

    async def test_bad_headcount_answer_omits_the_withheld_figures(self) -> None:
        model = demo.DemoModelClient()
        first = await model.send_turn([self._u("run the bad headcount blueprint")], [])
        assert first.tool_calls[0].arguments["id"] == demo._BP_BAD_ID
        second = await model.send_turn(
            [self._u("run the bad headcount blueprint"), self._TOOL], []
        )
        assert second.assistant_text
        assert str(demo._FANOUT_LEAK_FIGURE) not in second.assistant_text
        assert demo._FANOUT_LEAK_ROW not in second.assistant_text

    async def test_average_tenure_missing_slot_then_resume_fills_it(self) -> None:
        model = demo.DemoModelClient()
        first = await model.send_turn([self._u("show average tenure by department")], [])
        assert first.tool_calls[0].arguments == {"id": demo._BP_TENURE_ID, "slot_bindings": {}}
        resumed = await model.send_turn(
            [self._u("show average tenure by department"), self._u("Sales")], []
        )
        assert resumed.tool_calls[0].arguments == {
            "id": demo._BP_TENURE_ID,
            "slot_bindings": {"department": "Sales"},
        }

    async def test_approve_headcount_emits_run_blueprint(self) -> None:
        model = demo.DemoModelClient()
        first = await model.send_turn([self._u("approve headcount for sales")], [])
        assert first.tool_calls[0].arguments["id"] == demo._BP_APPROVAL_ID

    async def test_existing_triggers_untouched(self) -> None:
        model = demo.DemoModelClient()
        assert (
            await model.send_turn([self._u("show me the columns")], [])
        ).tool_calls[0].name == "getTableSchema"
        assert (
            await model.send_turn([self._u("ask me a question")], [])
        ).tool_calls[0].name == "askUser"


# --------------------------------------------------------------------------
# Wiring: create_app(retrieval=...) + the seeded corpus (regression guard)
# --------------------------------------------------------------------------


class TestDemoRetrievalWiring:
    def test_build_demo_app_constructs_with_retrieval(self) -> None:
        # create_app accepts the injected pipeline (active_retrieval non-None →
        # runBlueprint + read tools registered); merely building it must not touch
        # neo4j/OpenAI/Couchbase.
        assert isinstance(demo.build_demo_app(), FastAPI)

    def test_pipeline_seeds_the_four_blueprints(self) -> None:
        pipeline = demo.build_retrieval_pipeline(RuntimeSettings())
        assert pipeline is not None
        assert demo.RuntimeSettings().retrieval_enabled is True  # active_retrieval flips on

    async def test_get_blueprint_returns_each_seeded_detail(self) -> None:
        index = demo.FakeVectorIndex(entries=[], details=demo.build_blueprint_details())
        for bid in (
            demo._BP_GOOD_ID,
            demo._BP_BAD_ID,
            demo._BP_TENURE_ID,
            demo._BP_APPROVAL_ID,
        ):
            assert (await index.get_blueprint(bid)) is not None

    async def test_recall_is_empty_disjoint_from_green_triggers(self) -> None:
        # §4 regression guard: recall entries are empty, so recall returns 0 cards
        # for EVERY question — the retrieval pre-injection is inert for the 5
        # pre-existing green scenarios (no accidental blueprint match).
        index = demo.FakeVectorIndex(entries=[], details=demo.build_blueprint_details())
        for kind in ("blueprint", "knowledge"):
            assert (
                await index.recall(query_vector=[0.1] * 16, kind=kind, k=30)
            ) == []


# --------------------------------------------------------------------------
# End-to-end executor over the demo wiring — the four scenarios' core behavior
# (the "runBlueprint is handled through the demo launcher's app" proof)
# --------------------------------------------------------------------------


class TestBlueprintExecutorOverDemoWiring:
    async def test_fast_path_returns_verified_result(self) -> None:
        outcome = await _executor().execute(
            blueprint_id=demo._BP_GOOD_ID,
            slot_bindings={"department": "Sales"},
            credentials=_creds(),
        )
        assert isinstance(outcome, ExecCompleted)
        assert outcome.result_full["status"] == "verified"
        assert outcome.result_full["verify"]["grain_ok"] is True
        # DETERMINED provenance (not None) — the catalogued demo tables resolve a
        # real USES set, so the verified result survives the D44 replay filter.
        assert outcome.provenance is not None
        assert (demo._BP_GOOD_TABLE, "department") in outcome.provenance

    async def test_bad_grain_is_withheld_never_returned(self) -> None:
        outcome = await _executor().execute(
            blueprint_id=demo._BP_BAD_ID,
            slot_bindings={"department": "Sales"},
            credentials=_creds(),
        )
        assert isinstance(outcome, ExecFailed)
        assert outcome.error_code == VERIFY_FAILED_CODE
        # The withheld fan-out result is NOT carried on the failed outcome.
        assert getattr(outcome, "result_full", None) is None

    async def test_missing_slot_pauses_before_any_node_runs(self) -> None:
        outcome = await _executor().execute(
            blueprint_id=demo._BP_TENURE_ID, slot_bindings={}, credentials=_creds()
        )
        assert isinstance(outcome, ExecPaused)
        assert outcome.reason == "blueprint_slot"
        assert outcome.awaiting_node is None  # slot pause → resume re-runs the model loop

    async def test_filled_slot_resolves_via_domain_probe_and_verifies(self) -> None:
        outcome = await _executor().execute(
            blueprint_id=demo._BP_TENURE_ID,
            slot_bindings={"department": "Sales"},
            credentials=_creds(),
        )
        assert isinstance(outcome, ExecCompleted)
        assert outcome.result_full["status"] == "verified"
        # Prove the DISTINCT-domain slot probe actually FIRED: its provenance is
        # folded into the union, so the dept-dim column being present means the
        # probe ran (had it silently returned (None, []), resolve_slot would have
        # bound the raw value and this column would be absent).
        assert outcome.provenance is not None
        assert (demo._BP_DEPT_DIM_TABLE, "department") in outcome.provenance

    async def test_approval_pauses_then_resumes_to_verified(self) -> None:
        executor = _executor()
        paused = await executor.execute(
            blueprint_id=demo._BP_APPROVAL_ID,
            slot_bindings={"department": "Sales"},
            credentials=_creds(),
        )
        assert isinstance(paused, ExecPaused)
        assert paused.reason == "blueprint_approval"
        assert paused.awaiting_node == 1  # the approval gate
        resumed = await executor.resume(
            blueprint_id=demo._BP_APPROVAL_ID,
            slot_bindings={"department": "Sales"},
            completed_nodes_json=paused.completed_nodes_json,
            awaiting_node=paused.awaiting_node,
            approval_answer="approve",
            credentials=_creds(),
        )
        assert isinstance(resumed, ExecCompleted)
        assert resumed.result_full["status"] == "verified"

    async def test_approval_deny_falls_back_to_raw_loop(self) -> None:
        executor = _executor()
        paused = await executor.execute(
            blueprint_id=demo._BP_APPROVAL_ID,
            slot_bindings={"department": "Sales"},
            credentials=_creds(),
        )
        assert isinstance(paused, ExecPaused)
        denied = await executor.resume(
            blueprint_id=demo._BP_APPROVAL_ID,
            slot_bindings={"department": "Sales"},
            completed_nodes_json=paused.completed_nodes_json,
            awaiting_node=paused.awaiting_node,
            approval_answer="deny",
            credentials=_creds(),
        )
        assert isinstance(denied, ExecFailed)  # never a wrong answer
