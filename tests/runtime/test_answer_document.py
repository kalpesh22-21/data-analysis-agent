"""Approved presentation, source selection and repair integration regressions."""

import pytest

from data_agent.runtime.answer_access import resolve_table
from data_agent.runtime.composer import PresentationError, proposal_to_internal
from data_agent.runtime.loop.answer_judge import JudgeVerdict
from tests.runtime.test_harness_improvements import (
    CREDS,
    Judge,
    batch,
    build,
    call,
    discovery,
    query,
    run,
)


def section(kind="table", **selectors):
    return {
        "id": "result",
        "title": "Employees by department",
        "evidence_refs": ["q"],
        "presentation": {"kind": kind, "result_id": "q", **selectors},
        "caveats": [],
        "actions": [],
    }


def final(*sections, takeaway="Employee counts by department.", ident="answer"):
    return batch(
        call("finalizeAnswer", ident, takeaway=takeaway, sections=list(sections), evidence=["q"])
    )


async def test_approved_document_persisted_and_table_authorized():
    judge = Judge()
    loop, store, _, mcp, _ = build([discovery(), query(), final(section())], judge)
    out = await run(loop)
    assert out.review["status"] == "approved"
    assert out.answer_document["sections"][0]["presentation"] == {"kind": "table", "result_id": "q"}
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.messages[-1].answer_document == out.answer_document
    assert judge.briefs[0].answer_document["sections"] == out.answer_document["sections"]
    ref = out.answer_document["sources"]["q"]["table_ref"]
    assert await resolve_table(doc, ref, CREDS, store)
    with pytest.raises(PermissionError):
        await resolve_table(doc, "0:unselected", CREDS, store)
    assert len(mcp.calls) == 1


async def test_figure_exact_values_without_visible_table():
    loop, _, _, _, _ = build(
        [discovery(), query(), final(section("figure", column="n", label="Employees"))], Judge()
    )
    out = await run(loop)
    assert out.review["status"] == "approved"
    presentation = out.answer_document["sections"][0]["presentation"]
    assert presentation["kind"] == "figure"
    assert presentation["value"] == "120"


async def test_presentation_repair_reuses_result():
    judge = Judge(
        [
            JudgeVerdict(
                False,
                "unsupported_by_evidence",
                "Use a table for the department breakdown.",
                True,
                repair_type="presentation",
            ),
            JudgeVerdict(True, reviewed=True),
        ]
    )
    loop, _, _, mcp, _ = build(
        [
            discovery(),
            query(),
            final(section("figure", column="n", label="Employees")),
            final(section(), ident="fixed"),
        ],
        judge,
    )
    out = await run(loop)
    assert out.review["status"] == "approved"
    assert out.answer_document["sections"][0]["presentation"]["kind"] == "table"
    assert len(judge.briefs) == 2
    assert len(mcp.calls) == 1


def test_rejects_multiple_unjustified_visible_tables_and_old_proposals():
    second = {**section(), "id": "second"}
    with pytest.raises(PresentationError, match="zero or one"):
        proposal_to_internal({"takeaway": "Counts", "sections": [section(), second]})
    with pytest.raises(PresentationError):
        proposal_to_internal({"answer": "Counts", "tables": []})


@pytest.mark.parametrize("ties,expected", [("dense", [1, 1, 2]), ("competition", [1, 1, 3])])
def test_ranks_use_ties_without_reordering_or_aggregation(ties, expected):
    from data_agent.runtime.composer import _visual

    result = _visual(
        {
            "kind": "bars",
            "result_id": "q",
            "category_column": "Department",
            "measure_column": "n",
            "label": "Employees",
            "direction": "descending",
            "ties": ties,
        },
        ["Department", "n"],
        [["Sales", 10], ["Finance", 10], ["Operations", 4]],
    )
    assert [i["rank"] for i in result["items"]] == expected
    assert [i["raw"] for i in result["items"]] == [10, 10, 4]


def test_line_preserves_missing_values_when_normalizing_period_order():
    from data_agent.runtime.composer import _visual

    plan = {
        "kind": "line",
        "result_id": "q",
        "category_column": "Date",
        "measure_column": "n",
        "label": "Employees",
    }
    rows = [["2026-01-01", 1], ["2026-02-01", None], ["2026-03-01", 3]]
    rendered = _visual(plan, ["Date", "n"], rows)
    assert rendered["points"][1]["value"] is None
    assert _visual(plan, ["Date", "n"], rows[::-1]) == rendered


@pytest.mark.parametrize("fallback", [False, True])
async def test_partial_result_automatically_falls_back_before_judge_review(fallback):
    choice = section("bars", category_column="Department", measure_column="n", label="Employees")
    choice["fallback"] = fallback
    judge = Judge()
    loop, _, _, mcp, _ = build(
        [discovery(), query(), final(choice)],
        judge,
        rows=[
            {
                "columns": ["Department", "n"],
                "rows": [["Sales", 120]],
                "row_count": 211,
                "truncated": True,
            }
        ],
    )
    out = await run(loop)
    assert out.answer_document["sections"][0]["presentation"]["kind"] == "table"
    assert "fallback_table" not in out.answer_document["sections"][0]
    assert judge.briefs[0].answer_document["sources"]["q"]["completeness"] == "partial"
    assert any("shown as a table" in c for c in judge.briefs[0].answer_document["sections"][0]["caveats"])
    assert len(mcp.calls) == 1


async def test_prose_correction_keeps_document_and_receipt_consistent():
    judge = Judge(
        [
            JudgeVerdict(
                True,
                reviewed=True,
                repair_type="prose",
                corrected_answer="The department has 120 employees.",
            )
        ]
    )
    loop, store, _, _, _ = build([discovery(), query(), final(section())], judge)
    out = await run(loop)
    assert out.review["status"] == "approved"
    assert out.answer_document["takeaway"] == "The department has 120 employees."
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.messages[-1].answer_document == out.answer_document


async def test_current_scope_cannot_resolve_previously_approved_table():
    from dataclasses import replace

    loop, store, _, _, _ = build([discovery(), query(), final(section())], Judge())
    out = await run(loop)
    doc = await store.get_or_create_session(CREDS.session_id)
    narrowed = replace(CREDS, column_scope=frozenset({"hr.employee.Salary"}))
    with pytest.raises(PermissionError):
        await resolve_table(doc, out.answer_document["sources"]["q"]["table_ref"], narrowed, store)


def test_excluded_result_drops_dependent_section_and_fallback():
    from data_agent.runtime.loop.proposal import filter_excluded_components

    proposed = proposal_to_internal(
        {"takeaway": "Counts", "sections": [{**section(), "fallback": False}]}
    )
    filtered = filter_excluded_components(proposed, [{"result_id": "q", "kind": "warehouse"}])
    assert filtered["sections"] == []
    assert filtered["tables"] == []


def test_post_approval_presentation_change_changes_receipt():
    from data_agent.runtime.loop.approved_answer import answer_version
    from data_agent.runtime.loop.turn_accumulators import TurnAccumulators

    accum = TurnAccumulators()
    accum.answer_document = {"takeaway": "Counts", "sections": [], "sources": {}}
    before = answer_version("Counts", accum, [], 0, "scope")
    accum.answer_document["sections"].append({"id": "unreviewed"})
    assert answer_version("Counts", accum, [], 0, "scope") != before


async def test_selected_prepared_action_survives_persistence_and_cannot_be_swapped():
    from data_agent.runtime.answer_access import resolve_action
    from data_agent.runtime.session.models import TurnMessage
    from tests.runtime.final_answer import final_answer
    from tests.runtime.loop.test_component_omission import CARD
    from tests.runtime.test_harness_improvements import Tool

    loop, store, _, _, _ = build(
        [
            batch(call("identifier", "p")),
            final_answer(
                assistant_text="The prepared view is available.",
                evidence=["p"],
                capability_refs=["identifier"],
            ),
        ],
        Judge(),
        extra={"identifier": Tool("identifier", CARD)},
    )
    out = await run(loop)
    doc = await store.get_or_create_session(CREDS.session_id)
    message = TurnMessage.from_doc(doc.messages[-1].to_doc())
    assert message.prepared_actions
    answer_id = out.answer_document["answer_id"]
    card = resolve_action(doc, answer_id, "identifier", CREDS)
    assert card["name"] == "identifier"
    card["name"] = "changed"
    assert resolve_action(doc, answer_id, "identifier", CREDS)["name"] == "identifier"
    with pytest.raises(PermissionError):
        resolve_action(doc, answer_id, "unselected", CREDS)


async def test_additional_table_reasons_reach_judge_but_not_ui():
    first = {**section(), "table_reason": "Requested detailed departments."}
    second = {**section(), "id": "second", "table_reason": "Explicitly requested a separate view."}
    judge = Judge()
    loop, _, _, _, _ = build([discovery(), query(), final(first, second)], judge)
    out = await run(loop)
    assert len(judge.briefs[0].presentation_reasons) == 2
    assert all("table_reason" not in s for s in out.answer_document["sections"])


async def test_multiple_requested_results_keep_order_scope_and_snapshot_bindings():
    from tests.runtime.final_answer import final_answer
    from tests.runtime.test_harness_improvements import SQL

    total_sql = "SELECT count() AS n FROM hr.employee"
    loop, store, _, _, _ = build(
        [
            discovery(),
            query(),
            batch(call("runQuery", "total", sql=total_sql)),
            final_answer(
                assistant_text="The requested total and departments.",
                tables=[
                    {"result_id": "total", "caption": "Total"},
                    {"result_id": "q", "caption": "Departments"},
                ],
            ),
        ],
        Judge(),
        rows=[
            {"columns": ["Department", "n"], "rows": [["Sales", 120]], "row_count": 1},
            {"columns": ["n"], "rows": [[120]], "row_count": 1},
        ],
    )
    out = await run(loop)
    document = out.answer_document
    assert [s["title"] for s in document["sections"]] == ["Total", "Departments"]
    assert [s["presentation"]["result_id"] for s in document["sections"]] == ["total", "q"]
    doc = await store.get_or_create_session(CREDS.session_id)
    assert (
        await resolve_table(doc, document["sources"]["total"]["table_ref"], CREDS, store)
        == total_sql
    )
    assert await resolve_table(doc, document["sources"]["q"]["table_ref"], CREDS, store) == SQL


@pytest.mark.parametrize("selector", [{"sql": "SELECT 1"}, {"blueprint_id": "unexecuted"}])
async def test_sql_or_blueprint_name_cannot_replace_an_execution_reference(selector):
    bad = section()
    bad["presentation"] = {"kind": "table", **selector}
    loop, store, _, mcp, _ = build(
        [
            final(bad),
            batch(
                call(
                    "finalizeAnswer", "fixed", takeaway="I cannot verify this result.", sections=[]
                )
            ),
        ],
        Judge(),
    )
    out = await run(loop)
    assert out.answer_document["sections"] == []
    assert not mcp.calls
    doc = await store.get_or_create_session(CREDS.session_id)
    assert next(e for e in doc.tool_trail if e.tool_call_id == "answer").status != "ok"


async def test_assumptions_survive_live_document_and_existing_history():
    from data_agent.runtime.session_history import project_history

    loop, store, _, _, _ = build(
        [
            discovery(),
            query(),
            batch(
                call(
                    "recordAssumptions",
                    "assumptions",
                    assumptions=["Only accessible records are included."],
                )
            ),
            final(section()),
        ],
        Judge(),
    )
    out = await run(loop)
    assert "Only accessible records are included." in out.answer_document["sections"][0]["caveats"]
    doc = await store.get_or_create_session(CREDS.session_id)
    history = project_history(doc.messages, doc.tool_trail, CREDS.column_scope, None)
    assert history["turns"][0]["assumptions"] == ["Only accessible records are included."]


@pytest.mark.parametrize("field", ["actions", "caveats", "evidence_refs"])
def test_missing_section_field_feedback_identifies_the_repair(field):
    proposed = section()
    proposed.pop(field)
    with pytest.raises(
        PresentationError, match=f"sections.0: missing required field\\(s\\): {field}"
    ):
        proposal_to_internal({"takeaway": "Employee counts.", "sections": [proposed]})


@pytest.mark.parametrize("kind", ["bars", "line"])
def test_chart_accepts_serialized_decimals_and_preserves_display_precision(kind):
    from data_agent.runtime.composer import _visual

    visual = _visual(
        {"kind": kind, "result_id": "q", "category_column": "month",
         "measure_column": "amount", "label": "Payroll amount"},
        ["month", "amount"],
        [["2026-01-01", "1100.00"], ["2026-02-01", "1200.25"], ["2026-03-01", "0.00"]],
    )
    if kind == "line":
        assert visual["points"][0]["value"] == 1100
        assert visual["points"][0]["formatted_value"] == "1100.00"
    else:
        assert visual["items"][0]["raw"] == 1100
        assert visual["items"][0]["value"] == "1100.00"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1,200.00", "9007199254740993", "0.1234567890123456789", True])
def test_chart_rejects_ambiguous_or_lossy_numbers(value):
    from data_agent.runtime.composer import _visual

    with pytest.raises(PresentationError):
        _visual({"kind": "bars", "result_id": "q", "category_column": "category",
                 "measure_column": "amount", "label": "Amount"},
                ["category", "amount"], [["Sales", value]])


@pytest.mark.parametrize("value", ["12 employees", "NaN", True, None, 2**53 + 1, {"amount": 12}])
async def test_unsupported_chart_cell_delivers_exact_reviewed_table_without_query_repair(value):
    choice = section("bars", category_column="Department", measure_column="n", label="Employees")
    judge = Judge()
    loop, _, _, mcp, _ = build(
        [discovery(), query(), final(choice)], judge,
        rows=[{"columns": ["Department", "n"], "rows": [["Sales", value]], "row_count": 1, "truncated": False}],
    )
    out = await run(loop)
    assert out.review["status"] == "approved"
    approved_sections = judge.briefs[0].answer_document["sections"]
    assert approved_sections[0]["presentation"] == {"kind": "table", "result_id": "q"}
    assert any("shown as a table" in c for c in approved_sections[0]["caveats"])
    assert out.answer_document["sections"] == approved_sections
    assert out.answer_document["sources"]["q"]["table_ref"] .startswith("tbl_")
    assert len(mcp.calls) == 1
