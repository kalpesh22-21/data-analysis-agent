"""QA adversarial companion to `test_run_ui_runtime_blueprints.py` (Layer-3
Slice 1 / D89). Tests ONLY — new file, additive (never modifies the dev's
module or `scripts/run_ui_runtime.py`).

Where the dev's module proves the happy path of the Slice-1 demo wiring, this
module attacks the seams that a browser-less CI run can actually reach:

  * `DemoMCPClient` content-routing ROBUSTNESS — SQL that overlaps two routes
    (grain marker vs table vs domain probe), the `_bad`/good superstring hazard,
    and empty/unknown SQL fail-closing to "no fabricated data".
  * The four scenarios through the REAL `BlueprintExecutor` over the demo wiring,
    adversarially: withheld grain-violating result carries NO rows/leak, the
    gated approval node is NOT dispatched during the pause, provenance is
    DETERMINED (non-None) on every completed path.
  * Provenance determinism — the demo-catalogue fix is load-bearing: an
    uncatalogued table completes VERIFIED but with `provenance=None` (the
    executor does NOT fail-closed; the downstream D44 replay filter does).
  * Regression / structural disjointness — the seeded `FakeVectorIndex` recalls
    0 cards for the 5 pre-existing green trigger phrases (and even for the
    blueprints' OWN intent text), driven through the REAL embed→recall pipeline.
  * `DemoModelClient` trigger routing — the 4 phrases, a phrase that overlaps two
    triggers, and a near-miss non-trigger that must fall to the raw loop.
"""

from __future__ import annotations

import pytest
import scripts.run_ui_runtime as demo

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
    return RuntimeCredentials(session_id="sess-qa-bp", jwt="qa-jwt", column_scope=frozenset())


def _executor_and_mcp(
    catalog_schema: dict[str, dict[str, str]] | None = None,
) -> tuple[BlueprintExecutor, demo.DemoMCPClient]:
    """A BlueprintExecutor over the demo launcher's DemoMCPClient + seeded index,
    returning the mcp so a test can inspect the recorded inner-query calls
    (`mcp.calls`). Defaults to the demo blueprint-table catalogue (determined
    provenance); pass a narrower/empty schema to exercise the uncatalogued path."""
    schema = dict(demo._BP_TABLE_SCHEMAS) if catalog_schema is None else catalog_schema
    mcp = demo.DemoMCPClient(tools=[], scripted={})
    dispatcher = ToolDispatcher(mcp, CatalogHandle(schema))
    index = demo.FakeVectorIndex(entries=[], details=demo.build_blueprint_details())
    return BlueprintExecutor(tool_dispatcher=dispatcher, vector_index=index), mcp


def _recorded_sql(mcp: demo.DemoMCPClient) -> list[str]:
    """Every SQL string the executor dispatched through the demo MCP (either arg key)."""
    out: list[str] = []
    for call in mcp.calls:
        if call.tool_name == "runQuery":
            out.append(str(call.args.get("sql") or call.args.get("query") or ""))
    return out


# ==========================================================================
# 1. DemoMCPClient content-routing robustness (D-L3-2) — the mis-route surface
# ==========================================================================


class TestDemoMCPRoutingRobustness:
    async def _run(self, sql: str, *, key: str = "sql") -> object:
        mcp = demo.DemoMCPClient(tools=[], scripted={})
        return await mcp.call_tool("runQuery", {key: sql}, jwt="j", session_id="s")

    async def test_grain_marker_dominates_when_probe_wraps_the_domain_table(self) -> None:
        # ADVERSARIAL two-route overlap: a grain probe whose INNER query references
        # the dept_dim table carries BOTH `__bp_` AND `demo.dept_dim`. The `__bp_`
        # marker is checked first, so it MUST route to a grain result ([__bp_n,
        # __bp_d]) — never the domain (department) list. If ordering regressed, the
        # verify gate would receive a 3-row department list and mis-compute.
        sql = (
            "SELECT COUNT(*) AS __bp_n, COUNT(DISTINCT department) AS __bp_d "
            f"FROM (SELECT DISTINCT department FROM {demo._BP_DEPT_DIM_TABLE}) AS __bp_sub"
        )
        result = await self._run(sql)
        assert result["columns"] == ["__bp_n", "__bp_d"]
        assert result["rows"] == [[3, 3]]  # grain, not the ["Sales", ...] domain

    async def test_grain_marker_over_bad_table_wins_even_though_good_is_a_substring(
        self,
    ) -> None:
        # The bad table name is a SUPERSTRING of the good one; a grain probe over
        # the bad node contains both `demo.headcount_by_dept` and
        # `demo.headcount_by_dept_bad`. The `_bad` check must win → fan-out (12,3),
        # NOT the passing (3,3). A regressed order silently converts a D56 FAIL
        # into a PASS — the exact no-silent-verification hole.
        sql = (
            "SELECT COUNT(*) AS __bp_n, COUNT(DISTINCT department) AS __bp_d "
            f"FROM (SELECT department FROM {demo._BP_BAD_TABLE}) AS __bp_sub"
        )
        result = await self._run(sql)
        assert result["rows"] == [[12, 3]]  # bad grain — verify FAILS

    async def test_plain_node_query_over_bad_table_returns_the_fanout_not_the_good_rows(
        self,
    ) -> None:
        # A plain node query over the bad table (no `__bp_`) must return the
        # withheld fan-out sentinel row + row_count 12, never the good 3-row shape.
        result = await self._run(
            f"SELECT department, headcount FROM {demo._BP_BAD_TABLE} GROUP BY department"
        )
        assert result["row_count"] == 12
        assert result["rows"] == [[demo._FANOUT_LEAK_ROW, demo._FANOUT_LEAK_FIGURE]]

    async def test_good_table_query_never_matches_the_bad_route(self) -> None:
        # The good table string cannot contain `_bad`, so a good node query returns
        # the clean 3-row result — proving the superstring hazard is one-directional.
        result = await self._run(
            f"SELECT department, n FROM {demo._BP_GOOD_TABLE} GROUP BY department"
        )
        assert result["row_count"] == 3
        assert demo._FANOUT_LEAK_ROW not in {row[0] for row in result["rows"]}

    async def test_approval_upstream_and_terminal_route_distinctly(self) -> None:
        # The upstream scalar table and the terminal table share no substring, so a
        # scalar query returns the single [[42]] cell and the terminal returns rows.
        upstream = await self._run(
            f"SELECT COUNT(DISTINCT emp_id) AS total FROM {demo._BP_APPROVAL_UPSTREAM_TABLE}"
        )
        assert upstream["rows"] == [[42]]
        terminal = await self._run(
            "SELECT department, COUNT(DISTINCT emp_id) AS n "
            f"FROM {demo._BP_APPROVAL_TERMINAL_TABLE} GROUP BY department"
        )
        assert terminal["columns"] == ["department", "n"]

    async def test_domain_probe_matches_via_model_query_key_too(self) -> None:
        # The executor dispatches with `sql`; a model-emitted runQuery uses `query`.
        # A domain probe must route identically off either key (defensive D-L3-2).
        result = await self._run(
            f"SELECT DISTINCT department FROM {demo._BP_DEPT_DIM_TABLE} LIMIT 500", key="query"
        )
        assert [row[0] for row in result["rows"]] == ["Sales", "Engineering", "Support"]

    @pytest.mark.parametrize("sql", ["", "   ", "\n\t ", "SELECT 1", "SELECT 1 FROM other.table"])
    async def test_unknown_or_empty_sql_fabricates_no_data(self, sql: str) -> None:
        # The router must return None (no canned blueprint data) for empty/unknown
        # SQL — never silently fabricate one of the fixtures. `call_tool` then
        # defers to the base FakeMCPClient (unscripted runQuery → AssertionError,
        # which the real ToolDispatcher's B4 guard contains as an internal-error
        # ToolResult upstream — never wrong data).
        assert demo._blueprint_run_query(sql) is None
        mcp = demo.DemoMCPClient(tools=[], scripted={})
        with pytest.raises(AssertionError):
            await mcp.call_tool("runQuery", {"sql": sql}, jwt="j", session_id="s")

    async def test_sentinel_denials_take_priority_over_blueprint_routing(self) -> None:
        # Both existing denials must still raise even though the blueprint router
        # sits in the same call_tool override (purely additive, non-shadowing).
        mcp = demo.DemoMCPClient(tools=[], scripted={})
        with pytest.raises(MCPToolError) as exc:
            await mcp.call_tool(
                "runQuery", {"query": demo._SCOPE_DENIAL_QUERY}, jwt="j", session_id="s"
            )
        assert exc.value.code == "COLUMN_SCOPE_VIOLATION"

    async def test_non_runquery_tool_is_never_blueprint_routed(self) -> None:
        # getTableSchema must fall straight through to the base scripted queue —
        # the blueprint router only intercepts runQuery.
        mcp = demo.DemoMCPClient(
            tools=[], scripted={"getTableSchema": [{"database": "demo", "table": "t"}]}
        )
        result = await mcp.call_tool(
            "getTableSchema", {"database": "demo", "table": "t"}, jwt="j", session_id="s"
        )
        assert result == {"database": "demo", "table": "t"}


# ==========================================================================
# 2. The four scenarios through the REAL BlueprintExecutor (adversarial)
# ==========================================================================


class TestScenariosOverExecutorAdversarial:
    async def test_fast_path_verified_answer_has_determined_provenance(self) -> None:
        # The dev's fix: the verified fast-path result must carry DETERMINED
        # (non-None) provenance so it survives the D44 replay filter within its own
        # turn (else the model re-emits forever). Assert the exact USES tuples.
        executor, _ = _executor_and_mcp()
        outcome = await executor.execute(
            blueprint_id=demo._BP_GOOD_ID,
            slot_bindings={"department": "Sales"},
            credentials=_creds(),
        )
        assert isinstance(outcome, ExecCompleted)
        assert outcome.result_full["status"] == "verified"
        assert outcome.provenance is not None
        assert (demo._BP_GOOD_TABLE, "department") in outcome.provenance
        assert (demo._BP_GOOD_TABLE, "emp_id") in outcome.provenance

    async def test_no_silent_verification_withholds_rows_and_leaks_no_figure(self) -> None:
        # D56: the grain-violating result MUST be withheld — ExecFailed(VERIFY_FAILED)
        # carrying NO rows, and the fan-out sentinel row/figure must appear NOWHERE
        # on the outcome (not on result_full, not in the user_message).
        executor, _ = _executor_and_mcp()
        outcome = await executor.execute(
            blueprint_id=demo._BP_BAD_ID,
            slot_bindings={"department": "Sales"},
            credentials=_creds(),
        )
        assert isinstance(outcome, ExecFailed)
        assert outcome.error_code == VERIFY_FAILED_CODE
        assert getattr(outcome, "result_full", None) is None
        assert demo._FANOUT_LEAK_ROW not in outcome.user_message
        assert str(demo._FANOUT_LEAK_FIGURE) not in outcome.user_message

    async def test_no_silent_verification_never_dispatches_a_return_of_the_bad_rows(
        self,
    ) -> None:
        # Defense-in-depth: even though the bad node query WAS dispatched (the
        # executor must run it to grain-check it), the withheld rows never escape as
        # a completed outcome. Confirm the executor did reach the grain probe
        # (the fan-out canary) and still refused to complete.
        executor, mcp = _executor_and_mcp()
        outcome = await executor.execute(
            blueprint_id=demo._BP_BAD_ID,
            slot_bindings={"department": "Sales"},
            credentials=_creds(),
        )
        assert isinstance(outcome, ExecFailed)
        assert any("__bp_" in sql for sql in _recorded_sql(mcp)), "grain probe must have run"

    async def test_ask_clarify_pauses_before_any_warehouse_query(self) -> None:
        # D49: an empty slot_bindings pauses on the missing required slot BEFORE any
        # node/probe query — a missing slot must not waste a warehouse call (n3).
        executor, mcp = _executor_and_mcp()
        outcome = await executor.execute(
            blueprint_id=demo._BP_TENURE_ID, slot_bindings={}, credentials=_creds()
        )
        assert isinstance(outcome, ExecPaused)
        assert outcome.reason == "blueprint_slot"
        assert outcome.awaiting_node is None
        assert _recorded_sql(mcp) == []  # zero queries fired before the pause

    async def test_ask_clarify_resume_verifies_with_determined_provenance(self) -> None:
        # On resume the filled slot fires the domain probe → node → grain → verified.
        # The completed result must ALSO carry determined provenance (the domain-dim
        # + tenure tables are catalogued), else it strands the model post-clarify.
        executor, _ = _executor_and_mcp()
        outcome = await executor.execute(
            blueprint_id=demo._BP_TENURE_ID,
            slot_bindings={"department": "Sales"},
            credentials=_creds(),
        )
        assert isinstance(outcome, ExecCompleted)
        assert outcome.result_full["status"] == "verified"
        assert outcome.provenance is not None

    async def test_approval_pause_does_not_dispatch_the_gated_terminal_node(self) -> None:
        # D45: at the approval pause, the UPSTREAM scalar node has run but the GATED
        # terminal query must NOT have been dispatched yet — the whole point of the
        # gate is to withhold the downstream work until approval.
        executor, mcp = _executor_and_mcp()
        paused = await executor.execute(
            blueprint_id=demo._BP_APPROVAL_ID,
            slot_bindings={"department": "Sales"},
            credentials=_creds(),
        )
        assert isinstance(paused, ExecPaused)
        assert paused.reason == "blueprint_approval"
        assert paused.awaiting_node == 1
        dispatched = _recorded_sql(mcp)
        assert any(demo._BP_APPROVAL_UPSTREAM_TABLE in sql for sql in dispatched)
        assert not any(demo._BP_APPROVAL_TERMINAL_TABLE in sql for sql in dispatched), (
            "the gated terminal node must NOT run before approval"
        )

    async def test_approval_resume_verifies_with_determined_provenance(self) -> None:
        executor, _ = _executor_and_mcp()
        paused = await executor.execute(
            blueprint_id=demo._BP_APPROVAL_ID,
            slot_bindings={"department": "Sales"},
            credentials=_creds(),
        )
        assert isinstance(paused, ExecPaused)
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
        assert resumed.provenance is not None

    async def test_approval_deny_never_returns_a_result(self) -> None:
        executor, _ = _executor_and_mcp()
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
        assert isinstance(denied, ExecFailed)
        assert getattr(denied, "result_full", None) is None


# ==========================================================================
# 3. Provenance determinism — the demo-catalogue fix is load-bearing
# ==========================================================================


class TestProvenanceDeterminism:
    async def test_every_completed_scenario_has_non_none_provenance(self) -> None:
        # The re-emit-loop guard: EVERY completed runBlueprint path must carry
        # determined provenance (fast path + resolved slot + approved DAG).
        executor, _ = _executor_and_mcp()
        creds = _creds()
        fast = await executor.execute(
            blueprint_id=demo._BP_GOOD_ID, slot_bindings={"department": "Sales"}, credentials=creds
        )
        tenure = await executor.execute(
            blueprint_id=demo._BP_TENURE_ID,
            slot_bindings={"department": "Sales"},
            credentials=creds,
        )
        assert isinstance(fast, ExecCompleted) and fast.provenance is not None
        assert isinstance(tenure, ExecCompleted) and tenure.provenance is not None

    async def test_uncatalogued_table_completes_but_provenance_is_none(self) -> None:
        # PIN: a blueprint touching a table NOT in the demo catalogue does NOT
        # fail-closed at the executor — it still returns ExecCompleted/verified with
        # `provenance=None`. This is exactly why the launcher folds the blueprint
        # tables into its CatalogHandle: without that fix the verified result carries
        # undetermined provenance, the D44 replay filter drops it from the very turn
        # that produced it, and the model is stranded in a re-emit loop. This test
        # is the load-bearing proof that the catalogue fix is NECESSARY, and pins
        # that the fail-closed happens downstream (replay filter), not here.
        executor, _ = _executor_and_mcp(catalog_schema={})  # nothing catalogued
        outcome = await executor.execute(
            blueprint_id=demo._BP_GOOD_ID,
            slot_bindings={"department": "Sales"},
            credentials=_creds(),
        )
        assert isinstance(outcome, ExecCompleted)
        assert outcome.result_full["status"] == "verified"
        assert outcome.provenance is None  # undetermined → dropped downstream (D44)


# ==========================================================================
# 4. Regression — structural disjointness of the seeded recall corpus (§4)
# ==========================================================================


_GREEN_TRIGGER_PHRASES = [
    "show me the columns",
    "ask me a question",
    "show me salaries please",
    "run raw sql for me",
    "keep going forever",
]


class TestSeededCorpusDisjointness:
    async def test_recall_returns_zero_cards_for_the_five_green_triggers(self) -> None:
        # §4 regression floor, driven through the REAL embed→recall pipeline (the
        # dev's test uses a fixed vector; this uses the actual FakeEmbeddingClient
        # the launcher wires). A future corpus change that seeds recall `entries`
        # and breaks disjointness would surface a blueprint/knowledge card here and
        # fail — catching an accidental behaviour shift for the 5 green scenarios.
        pipeline = demo.build_retrieval_pipeline(RuntimeSettings())
        for phrase in _GREEN_TRIGGER_PHRASES:
            ctx = await pipeline.retrieve(
                question=phrase, column_scope=frozenset(), user_id=None
            )
            assert ctx.thin_cards == [], f"blueprint recall leaked for {phrase!r}"
            assert ctx.knowledge_hits == [], f"knowledge recall leaked for {phrase!r}"

    async def test_recall_is_empty_even_for_the_blueprints_own_intent_text(self) -> None:
        # The strongest disjointness proof: recall is structurally DISABLED (empty
        # `entries`), so even a blueprint's OWN intent text recalls nothing — the
        # scenarios reach their blueprints ONLY via keyed get_blueprint, never recall.
        pipeline = demo.build_retrieval_pipeline(RuntimeSettings())
        for detail in demo.build_blueprint_details().values():
            ctx = await pipeline.retrieve(
                question=detail.intent, column_scope=frozenset(), user_id=None
            )
            assert ctx.thin_cards == [], f"own-intent recall leaked for {detail.id!r}"

    async def test_get_blueprint_still_resolves_each_seeded_id(self) -> None:
        # The other half of the disjointness contract: keyed fetch DOES resolve
        # (recall empty, get_blueprint full) — the path the scenarios actually use.
        index = demo.FakeVectorIndex(entries=[], details=demo.build_blueprint_details())
        for bid in (demo._BP_GOOD_ID, demo._BP_BAD_ID, demo._BP_TENURE_ID, demo._BP_APPROVAL_ID):
            assert (await index.get_blueprint(bid)) is not None


# ==========================================================================
# 5. DemoModelClient trigger routing (D-L3-3) — overlap + near-miss
# ==========================================================================


def _u(content: str) -> dict[str, object]:
    return {"role": "user", "content": content}


_TOOL = {"role": "tool", "tool_call_id": "x", "content": "{}"}


class TestDemoModelTriggerRouting:
    async def test_each_scenario_phrase_routes_to_its_blueprint(self) -> None:
        model = demo.DemoModelClient()
        cases = [
            ("run the headcount by department blueprint", demo._BP_GOOD_ID),
            ("run the bad headcount blueprint", demo._BP_BAD_ID),
            ("show average tenure by department", demo._BP_TENURE_ID),
            ("approve headcount for sales", demo._BP_APPROVAL_ID),
        ]
        for phrase, expected_id in cases:
            first = await model.send_turn([_u(phrase)], [])
            assert first.tool_calls[0].name == "runBlueprint"
            assert first.tool_calls[0].arguments["id"] == expected_id

    async def test_bad_headcount_phrase_is_not_shadowed_by_the_good_trigger(self) -> None:
        # The scenario phrase "bad headcount blueprint" reaches the BAD blueprint:
        # the more-specific "bad headcount" trigger is checked BEFORE "headcount by
        # department" (the ordering fix), so it wins regardless of any shared tokens.
        model = demo.DemoModelClient()
        first = await model.send_turn([_u("run the bad headcount blueprint")], [])
        assert first.tool_calls[0].arguments["id"] == demo._BP_BAD_ID

    async def test_overlapping_phrase_routes_to_the_more_specific_bad_trigger(self) -> None:
        # ADVERSARIAL near-two-triggers: a phrase containing BOTH "bad headcount"
        # AND "headcount by department" resolves to the BAD blueprint, because the
        # more-specific "bad headcount" branch is checked FIRST (the ordering fix —
        # substring triggers are order-sensitive, and the fragile good-before-bad
        # ordering was corrected). Pinned so the ordering stays a conscious choice.
        model = demo.DemoModelClient()
        first = await model.send_turn([_u("show the bad headcount by department")], [])
        assert first.tool_calls[0].arguments["id"] == demo._BP_BAD_ID

    async def test_bare_headcount_is_a_non_trigger_and_falls_to_raw_loop(self) -> None:
        # "headcount" alone (no "by department" / "bad" / "approve") must NOT trigger
        # any blueprint — it falls to the default raw-loop getTableSchema branch.
        model = demo.DemoModelClient()
        first = await model.send_turn([_u("what is the total headcount")], [])
        assert first.tool_calls[0].name == "getTableSchema"

    async def test_fast_path_second_call_omits_tool_calls_and_answers(self) -> None:
        model = demo.DemoModelClient()
        second = await model.send_turn(
            [_u("run the headcount by department blueprint"), _TOOL], []
        )
        assert not second.tool_calls
        assert second.assistant_text

    async def test_tenure_resume_re_emits_run_blueprint_with_filled_slot(self) -> None:
        model = demo.DemoModelClient()
        resumed = await model.send_turn(
            [_u("show average tenure by department"), _u("Engineering")], []
        )
        assert resumed.tool_calls[0].arguments == {
            "id": demo._BP_TENURE_ID,
            "slot_bindings": {"department": "Engineering"},
        }

    async def test_bad_headcount_final_answer_omits_the_withheld_figures(self) -> None:
        # The raw-loop fallback answer must never echo the withheld fan-out numbers.
        model = demo.DemoModelClient()
        second = await model.send_turn(
            [_u("run the bad headcount blueprint"), _TOOL], []
        )
        assert second.assistant_text
        assert str(demo._FANOUT_LEAK_FIGURE) not in second.assistant_text
        assert demo._FANOUT_LEAK_ROW not in second.assistant_text

    async def test_pre_existing_triggers_unaffected_by_blueprint_routes(self) -> None:
        model = demo.DemoModelClient()
        assert (await model.send_turn([_u("show me salaries please")], [])).tool_calls[
            0
        ].arguments["query"] == demo._SCOPE_DENIAL_QUERY
        assert (await model.send_turn([_u("run raw sql for me")], [])).tool_calls[
            0
        ].arguments["query"] == demo._PARSE_FAIL_QUERY
