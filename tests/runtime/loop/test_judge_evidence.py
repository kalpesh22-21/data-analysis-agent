"""Post-execution review wiring, authorized catalog evidence and honest omissions."""

import json
from types import SimpleNamespace

from data_agent.runtime.loop.answer_judge import AnswerJudge, JudgeBrief
from data_agent.runtime.loop.judge_evidence import catalog_context, coverage_package, enrich_brief
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import load_catalog_handle_from_catalog
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import TrackedIntent, TrailEntry
from tests.runtime.test_harness_improvements import (
    CREDS,
    Judge,
    batch,
    build,
    discovery,
    finish,
    query,
    run,
)


def catalog():
    return load_catalog_handle_from_catalog(
        {
            "hr.employee": {
                "columns": {
                    "id": {"type": "String", "description": "Employee identity"},
                    "status": {"type": "String", "description": "A means active"},
                    "salary": {"type": "Float64", "units": "USD per year"},
                },
                "grain": ["id"],
                "default_filters": ["status = 'A'"],
                "rules": [
                    {
                        "id": "active",
                        "predicate": "status = 'A'",
                        "applies_when": "active headcount",
                    },
                    {"id": "known_salary", "predicate": "salary > 0", "applies_when": "known pay"},
                ],
            }
        }
    )


def test_rules_missing_from_sql_still_reach_judge():
    context = catalog_context(catalog(), ["SELECT count(id) FROM hr.employee"], frozenset())
    table = context["tables"][0]
    assert len(table["rules"]) == 2
    assert table["columns"]["status"]["description"] == "A means active"
    assert table["default_filters"] == ["status = 'A'"]
    assert table["grain"] == ["id"]


def test_restricted_catalog_does_not_disclose_salary_rule_or_column():
    context = catalog_context(
        catalog(),
        ["SELECT count(id) FROM hr.employee"],
        frozenset({"hr.employee.id", "hr.employee.status"}),
    )
    assert "salary" not in json.dumps(context)
    assert context["tables"][0]["omitted_for_scope"] == ["rule"]
    assert context["tables"][0]["rules"][0]["id"] == "active"


def test_catalog_does_not_include_unrelated_tables_or_guess_missing_docs():
    handle = catalog()
    assert catalog_context(handle, ["SELECT 1"], frozenset())["tables"] == []
    assert catalog_context(handle, ["SELECT * FROM other.missing"], frozenset())["limitations"]
    copy = handle.documentation_for("hr.employee")
    copy["rules"].clear()
    assert handle.documentation_for("hr.employee")["rules"]


def test_coverage_uses_explicit_bindings_and_reports_unknown_access():
    state = SimpleNamespace(
        intents=[
            TrackedIntent("i1", "Headcount", "completed", "q"),
            TrackedIntent("i2", "Payroll", "pending"),
        ]
    )
    brief = JudgeBrief(
        "exit_prose",
        "Headcount and payroll",
        deliverables=(
            {
                "intent_id": "i1",
                "proposed_answer": "Two",
                "evidence": [{"result_id": "q", "kind": "warehouse"}],
            },
        ),
    )
    package = coverage_package(
        brief, [{"tool_call_id": "q"}, {"tool_call_id": "other"}], state, frozenset()
    )
    assert package["parts"][0]["result_ids"] == ["q"]
    assert package["parts"][1]["binding_status"] == "unassigned"
    assert package["unassigned_result_ids"] == ["other"]
    assert package["caller_access"]["company_wide_completeness"] == "unknown"


async def test_blueprint_review_reads_actual_execution_without_sending_full_rows():
    store = InMemorySessionStore()
    ref = await store.write_full_result(
        CREDS.session_id,
        "full",
        {
            "sql": ["SELECT count(id) FROM hr.employee WHERE TRUE"],
            "terminal_sql": "SELECT count(id) FROM hr.employee WHERE TRUE",
            "bound_slots": {},
            "omitted_slots": ["department"],
            "uses_rules": ["active"],
            "rows": [["must not enter judge"]],
        },
    )
    entry = TrailEntry(
        turn_index=0,
        tool_call_id="q",
        tool_name="runBlueprint",
        args={"id": "bp", "slot_bindings": {}},
        status="ok",
        error_code=None,
        provenance=frozenset({("hr.employee", "id")}),
        result_preview=None,
        result_full_ref=ref,
        ts="now",
    )
    brief = JudgeBrief("exit_table", "Headcount", results=({"tool_call_id": "q"},))
    enriched = await enrich_brief(
        brief,
        trail=[entry],
        turn_index=0,
        session_id=CREDS.session_id,
        store=store,
        catalog_provider=catalog(),
        credentials=CREDS,
        analysis_state=None,
    )
    execution = enriched.results[0]["execution"]
    assert execution["terminal_sql"].endswith("WHERE TRUE")
    assert execution["omitted_slots"] == ["department"]
    assert "must not enter judge" not in json.dumps(enriched.payload())
    assert (
        enriched.evidence_package["blueprint_rules"][0]["matches"][0]["definition"]["predicate"]
        == "status = 'A'"
    )


async def test_raw_query_executes_before_only_semantic_review():
    judge = Judge()
    loop, store, model, mcp, _ = build([discovery(), query(), batch(finish())], judge)
    loop._judge_catalog = catalog()
    await run(loop)
    assert len(judge.briefs) == 1
    brief = judge.briefs[0]
    execution = next(r["execution"] for r in brief.results if r["tool_call_id"] == "q")
    assert execution["sql"]
    assert "record_measurement_review" not in str(model.calls)
    assert "figures_found_in_results" not in brief.payload()
    assert (await store.get_or_create_session(CREDS.session_id)).tool_trail


async def test_complete_help_document_survives_when_budget_allows():
    store = InMemorySessionStore()
    ref = await store.write_full_result(CREDS.session_id, "article", {"text": "Complete article"})
    entry = TrailEntry(
        turn_index=0,
        tool_call_id="hc",
        tool_name="getHelpCenterDocument",
        args={"id": "doc"},
        status="ok",
        error_code=None,
        provenance=frozenset(),
        result_preview=None,
        result_full_ref=ref,
        ts="now",
    )
    brief = JudgeBrief("exit_prose", "How?", results=({"tool_call_id": "hc"},))
    enriched = await enrich_brief(
        brief,
        trail=[entry],
        turn_index=0,
        session_id=CREDS.session_id,
        store=store,
        catalog_provider=None,
        credentials=CREDS,
        analysis_state=None,
    )
    assert enriched.results[0]["document"]["text"] == "Complete article"
    judge = AnswerJudge(ScriptedModelClient([]), 2000)
    assert "Complete article" in judge.messages_for(enriched)[1]["content"]


def test_large_metadata_is_explicitly_omitted_without_losing_answer():
    brief = JudgeBrief(
        "exit_prose",
        "Question",
        draft="Answer",
        evidence_package={
            "catalog": {
                "tables": [
                    {"table": "hr.employee", "rules": ["x" * 40000], "columns": {"id": "x" * 40000}}
                ]
            },
        },
    )
    judge = AnswerJudge(ScriptedModelClient([]), 2000)
    payload = json.loads(judge.messages_for(brief)[1]["content"])
    assert payload["draft_answer"] == "Answer"
    assert payload["evidence_package"]["catalog"]["tables"][0]["omitted_for_size"]
    assert brief.evidence_package["catalog"]["tables"][0]["columns"]["id"]


def test_paired_accuracy_cases_preserve_their_evidence_in_the_real_prompt():
    from pathlib import Path

    cases = json.loads(
        (Path(__file__).parents[2] / "fixtures/runtime/post_execution_judge_cases.json").read_text()
    )
    assert len(cases) == 14
    assert sum(c["expected_approved"] for c in cases) == 7
    judge = AnswerJudge(ScriptedModelClient([]), 32000)
    for case in cases:
        brief = JudgeBrief(**case["brief"])
        messages = judge.messages_for(brief)
        payload = json.loads(messages[1]["content"])
        assert payload["draft_answer"] == brief.draft
        assert payload.get("results", []) == list(brief.results)
        assert "ACTUAL executed SQL" in messages[0]["content"]
        assert "figures_found_in_results" not in payload


async def test_unavailable_review_cannot_clear_prior_rejection():
    from data_agent.runtime.loop.answer_judge import APPROVED, JudgeVerdict

    judge = Judge(
        [JudgeVerdict(False, "contradicts_result", "Use the returned count.", True), APPROVED]
    )
    loop, store, *_ = build(
        [
            discovery(),
            query(),
            batch(finish("There are 999 employees.")),
            batch(finish("The employee count is 999.")),
        ],
        judge,
    )
    outcome = await run(loop)
    assert "999" not in outcome.assistant_text
    session = await store.get_or_create_session(CREDS.session_id)
    assert session.review_states["0"]["violation"] == "contradicts_result"


async def test_oversized_pinned_brief_is_unavailable_not_a_semantic_approval():
    model = ScriptedModelClient([])
    events = []
    judge = AnswerJudge(model, 10, observer=lambda e, p: events.append((e, p)))
    verdict = await judge.review(JudgeBrief("exit_prose", "question", draft="x" * 1000))
    assert verdict.approved and not verdict.reviewed
    assert model.calls_made == 0
    assert ("loop_answer_judge_failed", {"reason": "evidence_budget"}) in events


def test_single_scalar_evidence_has_priority_over_unreferenced_results():
    wanted = {
        "tool_call_id": "answer",
        "result_preview": {
            "columns": ["count"],
            "preview_rows": [[7]],
            "row_count": 1,
            "truncated": False,
        },
    }
    other = {
        "tool_call_id": "other",
        "result_preview": {
            "columns": ["text"],
            "preview_rows": [["x" * 10000]],
            "row_count": 1,
            "truncated": False,
        },
    }
    brief = JudgeBrief(
        "exit_prose",
        "Count?",
        draft="Seven",
        results=(wanted, other),
        referenced_result_ids=("answer",),
        deliverables=(
            {"intent_id": None, "evidence": [{"result_id": "answer", "kind": "warehouse"}]},
        ),
    )
    payload = json.loads(
        AnswerJudge(ScriptedModelClient([]), 700).messages_for(brief)[1]["content"]
    )
    assert payload["results"][0]["result_preview"]["preview_rows"] == [[7]]
    assert payload["results"][1]["result_preview"]["truncated"]


def test_capability_review_retains_parameter_meaning_and_unresolved_selection():
    from data_agent.runtime.loop.turn_accumulators import TurnAccumulators

    accum = TurnAccumulators()
    definition = {
        "kind": "data_widget",
        "description": "Employee department",
        "parameters": [{"name": "employee", "description": "Select the employee in the UI"}],
        "metadata": {"columns": ["Employee Name", "Department Name"]},
    }
    accum._remember_capability(
        {
            "name": "show_department",
            "arguments": {"has_unresolved_entities": True},
            "unresolved_entities": {"employee": "Jane Doe"},
            "_agent_evidence": definition,
        },
        "show_department",
    )
    accum.select_capabilities(["show_department"])
    context = accum.capability_judge_context[0]
    assert context["definition"] == definition
    assert context["unresolved_entities"] == {"employee": "Jane Doe"}
    assert context["arguments"]["has_unresolved_entities"] is True
