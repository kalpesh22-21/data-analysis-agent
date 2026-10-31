"""Approved presentation, source selection and repair integration regressions."""

import pytest

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
    from tests.runtime.test_harness_improvements import SQL
    assert out.answer_document["sources"]["q"]["sql"] == SQL
    assert "table_ref" not in out.answer_document["sources"]["q"]
    assert len(mcp.calls) == 1


async def test_figure_exact_values_without_visible_table():
    loop, _, _, _, _ = build(
        [discovery(), query(), final(section("figure", column="n", label="Employees"))], Judge()
    )
    out = await run(loop)
    assert out.review["status"] == "approved"
    presentation = out.answer_document["sections"][0]["presentation"]
    assert presentation["kind"] == "figure"
    assert presentation["column"] == "n"
    assert "value" not in presentation


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
    assert result["ranking"] == {"direction": "descending", "ties": ties}
    assert "items" not in result


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
    assert rendered["x_column"] == "Date"
    assert "points" not in rendered
    assert _visual(plan, ["Date", "n"], rows[::-1]) == rendered


async def test_partial_preview_does_not_limit_ui_chart():
    choice = section("bars", category_column="Department", measure_column="n", label="Employees")
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
    assert out.answer_document["sections"][0]["presentation"]["kind"] == "bars"
    assert judge.briefs[0].answer_document["sources"]["q"]["completeness"] == "partial"
    assert not judge.briefs[0].answer_document["sections"][0]["caveats"]
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


async def test_current_scope_hides_previously_approved_sql():
    from data_agent.runtime.session_history import project_history
    loop, store, _, _, _ = build([discovery(), query(), final(section())], Judge())
    await run(loop)
    doc = await store.get_or_create_session(CREDS.session_id)
    history = project_history(doc.messages, doc.tool_trail, frozenset({"hr.employee.Salary"}), None)
    assert "answer_document" not in history["turns"][0]



def test_excluded_result_drops_dependent_section_and_fallback():
    from data_agent.runtime.loop.proposal import filter_excluded_components

    proposed = proposal_to_internal(
        {"takeaway": "Counts", "sections": [section()]}
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
    assert document["sources"]["total"]["sql"] == total_sql
    assert document["sources"]["q"]["sql"] == SQL



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
    assert visual["x_column"] == "month" and visual["y_column"] == "amount"
    assert not {"points", "items", "value", "formatted_value"} & visual.keys()



@pytest.mark.parametrize("value", ["NaN", "Infinity", "1,200.00", "9007199254740993", "0.1234567890123456789", True])
def test_chart_does_not_coerce_cells_before_ui_fetch(value):
    from data_agent.runtime.composer import _visual

    result = _visual({"kind": "bars", "result_id": "q", "category_column": "category",
                 "measure_column": "amount", "label": "Amount"},
                ["category", "amount"], [["Sales", value]])
    assert result["y_column"] == "amount" and "items" not in result


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
    assert approved_sections[0]["presentation"]["kind"] == "bars"
    assert not approved_sections[0]["caveats"]
    assert out.answer_document["sections"] == approved_sections
    assert out.answer_document["sources"]["q"]["sql"]
    assert len(mcp.calls) == 1


async def test_missing_primary_figure_can_be_repaired_without_querying_again():
    judge = Judge([
        JudgeVerdict(False, "unexplained_gap", "Retain the primary employee-count figure using the existing result.",
                     True, repair_type="presentation"),
        JudgeVerdict(True, reviewed=True),
    ])
    loop, _, _, mcp, _ = build([
        discovery(), query(), final(takeaway="There are 120 employees."),
        final(section("figure", column="n", label="Employees"),
              takeaway="There are 120 employees.", ident="fixed"),
    ], judge)
    out = await run(loop)
    assert judge.briefs[0].answer_document["sections"] == []
    sections = out.answer_document["sections"]
    assert len(sections) == 1
    assert sections[0]["presentation"]["kind"] == "figure"
    assert sections[0]["presentation"]["column"] == "n"
    assert out.review["status"] == "approved"
    assert len(mcp.calls) == 1


async def test_trend_delivers_one_primary_chart_without_extra_visible_table():
    choice = section("line", category_column="Month", measure_column="n", label="Employees")
    loop, _, _, mcp, _ = build(
        [discovery(), query(), final(choice)], Judge(),
        rows=[{"columns": ["Month", "n"], "rows": [["2026-01", 100], ["2026-02", 120]],
               "row_count": 2, "truncated": False}],
    )
    out = await run(loop)
    assert out.review["status"] == "approved"
    assert len(out.answer_document["sections"]) == 1
    assert out.answer_document["sections"][0]["presentation"]["kind"] == "line"
    assert len(mcp.calls) == 1


@pytest.mark.parametrize('replacement', [[], ['Salary means recorded annual salary.']])
async def test_assumption_replacement_repairs_freshness_and_replays(replacement):
    from data_agent.runtime.session_history import project_history
    judge = Judge()
    loop, store, _, mcp, _ = build([
        discovery(), query(),
        batch(call('recordAssumptions', 'old-assumptions', assumptions=['Data is up to date as of today.'])),
        final(section()),
        batch(call('recordAssumptions', 'new-assumptions', assumptions=replacement)),
        final(section(), ident='fixed'),
    ], judge)
    out = await run(loop)
    assert out.review['status'] == 'approved'
    assert len(judge.briefs) == 1  # Invalid freshness is caught before review.
    assert out.answer_document['sections'][0]['caveats'] == replacement
    assert len(mcp.calls) == 1
    assert await loop._compute_turn_assumptions(CREDS.session_id, 0) == replacement
    doc = await store.get_or_create_session(CREDS.session_id)
    history = project_history(doc.messages, doc.tool_trail, CREDS.column_scope, None)['turns'][0]
    assert history['assumptions'] == (replacement or None)
    assert history['answer_document'] == out.answer_document


async def test_recorded_assumptions_are_not_redisplayed_across_sections():
    one = section()
    one['caveats'] = ['Only accessible records are included.', 'Only  accessible records are included']
    two = section()
    two['presentation'] = {'kind': 'text', 'markdown': 'The selected records are shown.'}
    two['id'] = 'notes'
    two['caveats'] = ['ONLY ACCESSIBLE RECORDS ARE INCLUDED.']
    loop, _, _, _, _ = build([
        discovery(), query(),
        batch(call('recordAssumptions', 'assumptions', assumptions=['Only accessible records are included.'])),
        final(one, two),
    ], Judge())
    out = await run(loop)
    assert [c for s in out.answer_document['sections'] for c in s['caveats']] == ['Only accessible records are included.']


async def test_caveat_deduplication_preserves_distinct_long_qualifications():
    caveat = 'A material qualification. ' * 100 + 'Only the listed records are covered.'
    selected = section()
    selected['caveats'] = [caveat, caveat]
    loop, _, _, _, _ = build([discovery(), query(), final(selected)], Judge())
    out = await run(loop)
    assert out.answer_document['sections'][0]['caveats'] == [caveat]


@pytest.mark.parametrize("kind,selectors,rows", [
    ("figure", {"column": "n", "label": "Employees"}, [["Sales", 120]]),
    ("bars", {"category_column": "Department", "measure_column": "n", "label": "Employees"}, [["Sales", 120], ["Engineering", 40]]),
    ("line", {"category_column": "Department", "measure_column": "n", "label": "Employees"}, [["2026-01-01", 10], ["2026-02-01", 20]]),
])
async def test_ui_can_access_visual_source_without_agent_opt_in(kind, selectors, rows):

    loop, store, _, mcp, _ = build(
        [discovery(), query(), final(section(kind, **selectors))], Judge(),
        rows=[{"columns": ["Department", "n"], "rows": rows, "row_count": len(rows), "truncated": False}],
    )
    out = await run(loop)
    assert out.review["status"] == "approved"
    assert len(out.answer_document["sections"]) == 1
    rendered = out.answer_document["sections"][0]
    assert rendered["presentation"]["kind"] == kind
    assert "supporting_table" not in rendered
    assert out.answer_document["sources"]["q"]["sql"]
    assert "table_ref" not in out.answer_document["sources"]["q"]
    assert len(mcp.calls) == 1



def test_supporting_data_is_not_an_agent_decision():
    with pytest.raises(PresentationError, match="supporting_data|declared fields"):
        proposal_to_internal({
            "takeaway": "Employees by department.", "evidence": ["q"],
            "sections": [{**section(), "supporting_data": True}],
        })


async def test_caveats_deduplicate_across_sections_without_erasing_distinct_scope():
    one = section()
    one['caveats'] = ['Only 2026 records are included.', 'Only active employees are included.']
    two = {**section(), 'id':'notes', 'presentation':{'kind':'text','markdown':'Additional coverage.'},
           'caveats':['ONLY  2026 records are included', 'Only 2025 records are included.', 'On Leave employees are not included.']}
    loop, _, _, _, _ = build([discovery(), query(), final(one,two)], Judge())
    out = await run(loop)
    assert [c for s in out.answer_document['sections'] for c in s['caveats']] == [
        'Only 2026 records are included.', 'Only active employees are included.',
        'Only 2025 records are included.', 'On Leave employees are not included.']


async def test_judge_corrects_document_without_agent_round_or_query():
    corrected = section()
    corrected['title'] = 'Active employees by department'
    corrected['caveats'] = ['Only accessible employees are included.']
    replacement = {'takeaway': 'Active employees grouped by department.', 'sections': [corrected], 'evidence': ['q'], 'assumptions': []}
    judge = Judge([JudgeVerdict(True, reviewed=True, repair_type='presentation', corrected_document=replacement)])
    draft = {**section(), 'title': 'Uncorrected draft heading'}
    followup = batch(call('finalizeAnswer', 'followup', takeaway='The active employee breakdown.', sections=[]))
    loop, store, model, mcp, _ = build([discovery(), query(), final(draft), followup], judge)
    out = await run(loop)
    assert out.review['status'] == 'approved'
    assert out.assistant_text == replacement['takeaway']
    assert out.answer_document['sections'][0]['title'] == corrected['title']
    assert out.answer_document['sections'][0]['caveats'] == corrected['caveats']
    assert len(judge.briefs) == 1
    assert len(mcp.calls) == 1
    saved = await store.get_or_create_session(CREDS.session_id)
    assert saved.messages[-1].answer_document == out.answer_document

    await run(loop, 'What did you just show?')
    previous = [m['content'] for m in model.calls[3].messages if m.get('role') == 'assistant' and isinstance(m.get('content'), str)]
    assert any(replacement['takeaway'] in text for text in previous)
    assert any(corrected['title'] in text and corrected['caveats'][0] in text for text in previous)
    assert not any('Uncorrected draft heading' in text for text in previous)


async def test_judge_correction_cannot_introduce_unexecuted_source():
    changed = section()
    changed['presentation']['result_id'] = 'invented'
    changed['evidence_refs'] = ['invented']
    judge = Judge([
        JudgeVerdict(True, reviewed=True, repair_type='presentation', corrected_document={
            'takeaway': 'Employee counts.', 'sections': [changed], 'evidence': ['invented']}),
        JudgeVerdict(True, reviewed=True),
    ])
    loop, _, _, mcp, _ = build([discovery(), query(), final(section()), final(section(), ident='repaired')], judge)
    out = await run(loop)
    assert 'invented' not in out.answer_document['sources']
    assert len(mcp.calls) == 1
    assert len(judge.briefs) == 2
