"""Independent recovery contract and real non-reasoning transport wiring."""

import pytest
from jsonschema import ValidationError

from data_agent.runtime.loop.answer_judge import AnswerJudge, JudgeBrief, JudgeVerdict
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from tests.runtime.final_answer import answer_arguments, recovery_sections
from tests.runtime.loop.test_partial_answer import PART, TEXT, build, final, reject
from tests.runtime.test_harness_improvements import Judge, batch, call, discovery, query, run


class RecoveryClient(ScriptedModelClient):
    def without_thinking(self, options):
        assert options == {"enable_thinking": False, "thinking": False}
        self.non_reasoning = True
        return self


@pytest.mark.parametrize("outcome", ["full", "partial", "none"])
async def test_salvage_has_explicit_outcome_and_non_reasoning_transport(outcome):
    response = {**PART, 'outcome': outcome, 'unfinished': PART['unfinished'] if outcome == 'partial' else [], 'evidence': [] if outcome == 'none' else PART['evidence']}
    client = RecoveryClient([batch(call("salvageAnswer", "s", **response))])
    judge = AnswerJudge(client, token_budget=10000)
    assert await judge.salvage(JudgeBrief("exit_prose", "Headcount?")) == response
    assert client.non_reasoning
    assert client.calls[0].tools[0]["name"] == "salvageAnswer"
    assert "SALVAGE MODE" in client.calls[0].messages[0]["content"]


async def test_invalid_salvage_response_is_not_approval():
    events = []
    judge = AnswerJudge(RecoveryClient([batch(call("salvageAnswer", "s", outcome="full"))]), 10000,
                        observer=lambda name, payload: events.append((name, payload)))
    with pytest.raises(ValidationError):
        await judge.salvage(JudgeBrief("exit_prose", "Headcount?"))
    failure = next(payload for name, payload in events if name == "loop_recovery_call_failed")
    assert failure["reason"] == "schema_validation"
    assert failure["exception_type"] == "ValidationError"
    assert failure["schema_keyword"] == "required"
    assert failure["schema_path"] == "/required"
    assert "takeaway" in failure["missing_fields"]


async def test_missing_salvage_tool_call_reports_response_contract_failure():
    events = []
    judge = AnswerJudge(RecoveryClient([batch()]), 10000,
                        observer=lambda name, payload: events.append((name, payload)))
    with pytest.raises(ValueError, match="invalid_recovery_response"):
        await judge.salvage(JudgeBrief("exit_prose", "Headcount?"))
    assert ("loop_recovery_call_failed", {"reason": "response_contract", "exception_type": "ValueError"}) in events


async def test_salvage_preserves_full_provider_result_id_through_caption_and_delivery():
    ref = "chatcmpl-tool-9050b364e4783be2"
    caption = "Employee count by department, all accessible records"

    class SalvageJudge(Judge):
        async def salvage(self, brief):
            assert ref in {r["tool_call_id"] for r in brief.results}
            assert ref in {c["result_id"] for c in brief.selected_components}
            return {**PART, 'outcome': 'full', 'unfinished': [], 'evidence': [ref], 'sections': recovery_sections([ref], titles=[{'result_id': ref, 'caption': caption}], evidence=[ref])}

    execution = batch(call("runQuery", ref, **query().tool_calls[0].arguments))
    proposal = final()
    proposal.tool_calls[0].arguments["evidence"] = [ref]
    proposal.tool_calls[0].arguments["sections"][0]["presentation"]["result_id"] = ref
    proposal.tool_calls[0].arguments["sections"][0]["evidence_refs"] = [ref]
    judge = SalvageJudge([JudgeVerdict(
        False, "unsupported_by_evidence", "Repair prose.",
        reviewed=True, repair_type="prose",
    )])
    loop, _, _, _, events = build([discovery(), execution, proposal], judge)
    out = await run(loop)
    assert out.review["status"] == "approved"
    assert out.answer_tables[0]["caption"] == caption
    assert not any(name == "loop_recovery_validation_failed" for name, _ in events)


async def test_markdown_presentation_does_not_discard_approved_salvage():
    class SalvageJudge(Judge):
        async def salvage(self, brief):
            return {**PART, 'takeaway': 'Sales has 120 employees.\n\n| Department | Employees |\n|---|---|\n| Sales | 120 |', 'outcome': 'full', 'unfinished': []}

    judge = SalvageJudge([JudgeVerdict(
        False, "unsupported_by_evidence", "Repair prose.",
        reviewed=True, repair_type="prose",
    )])
    loop, _, _, _, events = build([discovery(), query(), final()], judge)
    out = await run(loop)
    assert out.review["status"] == "approved"
    assert out.assistant_text == "Sales has 120 employees."
    assert any(name == "loop_salvage_approved" for name, _ in events)
    assert not any(name == "loop_recovery_validation_failed" for name, _ in events)


async def test_salvage_delivers_full_answer_and_repairs_caption_after_zero_budget():
    class SalvageJudge(Judge):
        async def review(self, brief):
            from data_agent.runtime.loop.delivery import CURRENT_DELIVERY

            CURRENT_DELIVERY.get().review_seconds = 0
            return await super().review(brief)

        async def salvage(self, brief):
            self.salvage_brief = brief
            return {**PART, 'outcome': 'full', 'unfinished': [], 'sections': recovery_sections(['q'], titles=[{'result_id': 'q', 'caption': 'Employees by department'}])}

    judge = SalvageJudge([reject()])
    loop, store, model, _, _ = build([discovery(), query(), final(), final()], judge)
    out = await run(loop)
    assert out.review["status"] == "approved" and out.review["completion"] == "full"
    assert out.answer_tables[0]["caption"] == "Employees by department"
    assert out.assistant_text == PART["takeaway"]
    assert judge.salvage_brief.draft == "There are 120 employees. The leave ranking is shown."


@pytest.mark.parametrize(
    "impact,checks", [("unrelated", 0), ("potentially_relevant", 1), ("unclear", 1), ("failure", 1)]
)
async def test_new_evidence_only_revalidates_when_relevant_or_unknown(impact, checks):
    class RelevanceJudge(Judge):
        salvages = 0

        async def partial_relevance(self, snapshot, current, questions):
            if impact == "failure":
                raise TimeoutError()
            return impact

        async def salvage(self, brief):
            self.salvages += 1
            return {**PART, 'outcome': 'partial'}

    judge = RelevanceJudge([reject(PART), reject()])
    extra = batch(call("runQuery", "q2", sql="SELECT COUNT(*) AS n FROM hr.employee"))
    loop, _, _, _, _ = build(
        [discovery(), query(), final(), extra, final()],
        judge,
        rows=[{"columns": ["n"], "rows": [[120]], "row_count": 1}] * 2,
    )
    out = await run(loop)
    assert out.assistant_text == TEXT
    assert judge.salvages == checks


async def test_verbose_guard_goes_to_judge_without_agent_rewrite():
    long = "The available employee count is 120. " * 80
    judge = Judge(
        [
            JudgeVerdict(
                True,
                reviewed=True,
                repair_type="prose",
                corrected_answer="The employee count is 120.",
            )
        ]
    )
    proposal = batch(
        call("finalizeAnswer", "f", **answer_arguments(takeaway=long, tables=[{"result_id": "q", "caption": "Headcount"}], evidence=["q"]))
    )
    loop, _, model, _, _ = build([discovery(), query(), proposal], judge)
    out = await run(loop)
    assert out.assistant_text == "The employee count is 120."
    assert judge.briefs[0].guard_findings == ("verbose_answer",)
    assert model.calls_made == 3


async def test_salvage_can_select_a_different_executed_result():
    class SelectOther(Judge):
        async def salvage(self, brief):
            assert any(c["result_id"] == "q2" for c in brief.selected_components)
            return {**PART, 'outcome': 'full', 'unfinished': [], 'evidence': ['q2'], 'sections': recovery_sections(['q2'], titles=[], evidence=['q2'])}

    sql = "SELECT COUNT(*) AS n FROM hr.employee"
    judge = SelectOther(
        [
            JudgeVerdict(
                False,
                "measurement_mismatch",
                "Select the correct result.",
                reviewed=True,
                repair_type="prose",
            )
        ]
    )
    loop, _, model, _, _ = build(
        [discovery(), query(), batch(call("runQuery", "q2", sql=sql)), final()],
        judge,
        rows=[{"columns": ["n"], "rows": [[120]], "row_count": 1}] * 2,
    )
    out = await run(loop)
    assert out.answer_tables[0]["sql"] == sql
    assert out.review["completion"] == "full"
    assert model.calls_made == 4


async def test_verbose_approved_partial_is_retained_without_another_salvage():
    class Shorten(Judge):
        async def salvage(self, brief):
            return {**PART, 'outcome': 'partial', 'sections': recovery_sections(['q'], titles=[])}

    part = {**PART, 'takeaway': (PART['takeaway'] + ' ') * 200, 'sections': recovery_sections(['q'])}
    judge = Shorten(
        [
            JudgeVerdict(
                False,
                "unsupported_by_evidence",
                "Shorten the supported subset.",
                reviewed=True,
                repair_type="prose",
                partial_answer=part,
            )
        ]
    )
    loop, _, model, _, events = build([discovery(), query(), final()], judge)
    out = await run(loop)
    assert out.review["status"] == "approved" and out.answer_tables
    assert out.assistant_text.count(PART["takeaway"]) == 200
    assert model.calls_made == 3
    assert not any(e == "loop_recovery_validation_failed" for e, _ in events)


async def test_approved_salvage_is_not_dropped_by_advisory_word_limit():
    text = "Sales has 120 employees in the available records. " + "These are the records available within the requested scope. " * 12
    class SalvageJudge(Judge):
        async def salvage(self, brief):
            return {**PART, 'takeaway': text.strip(), 'outcome': 'full', 'unfinished': [], 'sections': recovery_sections(['q'], titles=[])}
    judge = SalvageJudge([JudgeVerdict(False, "unsupported_by_evidence", "Repair prose.", reviewed=True, repair_type="prose")])
    loop, _, _, _, _ = build([discovery(), query(), final()], judge)
    out = await run(loop)
    assert out.review["status"] == "approved"
    assert out.assistant_text == text.strip()
    assert out.answer_tables


async def test_salvage_accepts_schema_valid_json_content_without_tool_wrapper():
    import json

    from data_agent.runtime.model.client import ModelTurnResult
    response = {**PART, 'outcome': 'full', 'unfinished': []}
    judge = AnswerJudge(RecoveryClient([ModelTurnResult(assistant_text=json.dumps(response))]), 10000)
    assert await judge.salvage(JudgeBrief("exit_prose", "Headcount?")) == response


async def test_json_content_still_requires_complete_salvage_schema():
    from data_agent.runtime.model.client import ModelTurnResult
    judge = AnswerJudge(RecoveryClient([ModelTurnResult(assistant_text='{"outcome":"full"}')]), 10000)
    with pytest.raises(ValidationError):
        await judge.salvage(JudgeBrief("exit_prose", "Headcount?"))


async def test_salvage_rejects_sql_in_section_title():
    class SalvageJudge(Judge):
        async def salvage(self, brief):
            return {**PART, 'outcome': 'full', 'unfinished': [], 'sections': recovery_sections(['q'], titles=[{'result_id': 'q', 'caption': 'SELECT * FROM hr.employee'}])}
    judge = SalvageJudge([JudgeVerdict(False, "unsupported_by_evidence", "Repair prose.",
                                      reviewed=True, repair_type="prose")])
    loop, _, _, _, events = build([discovery(), query(), final()], judge)
    out = await run(loop)
    assert out.review["status"] != "approved"
    assert "SELECT" not in str(out.answer_document)
    assert ("loop_recovery_validation_failed", {"reason": "sql_in_prose"}) in events


@pytest.mark.parametrize('as_text', [True, False])
async def test_extra_top_level_annotations_are_removed_then_validated(as_text):
    import json

    from data_agent.runtime.model.client import ModelTurnResult
    events = []
    response = {**PART, 'outcome': 'partial', 'outcome_notes': 'partial due to row cap', 'untrusted_annotation': {'instruction': 'must not survive'}}
    turn = (ModelTurnResult(assistant_text=json.dumps(response)) if as_text else
            batch(call('salvageAnswer', 's', **response)))
    judge = AnswerJudge(RecoveryClient([turn]), 10000,
                        observer=lambda name, payload: events.append((name, payload)))
    actual = await judge.salvage(JudgeBrief('exit_prose', 'Headcount?'))
    assert actual == {**PART, 'outcome': 'partial'}
    assert ('loop_recovery_fields_omitted', {'removed_count': 2}) in events
    assert 'untrusted_annotation' not in json.dumps(events)


@pytest.mark.parametrize('patch,keyword', [
    ({'evidence': 'q'}, 'type'),
    ({'sections': [{**recovery_sections(['q'])[0], 'outcome_notes': 'extra'}]}, 'additionalProperties'),
    ({'outcome': 'invented'}, 'enum'),
])
async def test_extra_key_tolerance_does_not_relax_known_or_nested_fields(patch, keyword):
    import json

    from data_agent.runtime.model.client import ModelTurnResult
    events = []
    response = {**PART, 'outcome': 'partial', 'outcome_notes': 'extra', **patch}
    judge = AnswerJudge(RecoveryClient([ModelTurnResult(assistant_text=json.dumps(response))]), 10000,
                        observer=lambda name, payload: events.append((name, payload)))
    with pytest.raises(ValidationError):
        await judge.salvage(JudgeBrief('exit_prose', 'Headcount?'))
    failure = next(payload for name, payload in events if name == 'loop_recovery_call_failed')
    assert failure['schema_keyword'] == keyword
    assert PART['takeaway'] not in json.dumps(failure)
    assert 'outcome_notes' not in json.dumps(failure)


@pytest.mark.parametrize('valid_receipt', [True, False])
async def test_json_extra_key_salvage_reaches_ui_with_existing_receipt(valid_receipt):
    import json

    from data_agent.runtime.model.client import ModelTurnResult
    ref = 'q' if valid_receipt else 'never-executed'
    response = {**PART, 'evidence': [ref], 'outcome': 'full', 'unfinished': [], 'outcome_notes': 'complete supported result', 'sections': recovery_sections([ref], titles=[{'result_id': ref, 'caption': 'Employees by department'}], evidence=[ref])}
    recovery = AnswerJudge(RecoveryClient([ModelTurnResult(assistant_text=json.dumps(response))]), 10000)

    class JsonSalvageJudge(Judge):
        async def review(self, brief):
            from data_agent.runtime.loop.delivery import CURRENT_DELIVERY
            CURRENT_DELIVERY.get().review_seconds = 0
            return await super().review(brief)

        async def salvage(self, brief):
            return await recovery.salvage(brief)

    judge = JsonSalvageJudge([reject()])
    loop, store, _, mcp, events = build([discovery(), query(), final(), final()], judge)
    out = await run(loop)
    if not valid_receipt:
        assert out.review['status'] == 'rejected'
        assert not out.answer_tables
        assert not any(name == 'loop_partial_answer_recovered' for name, _ in events)
        return
    assert out.review['status'] == 'approved' and out.review['completion'] == 'full'
    assert out.assistant_text == PART['takeaway']
    assert out.answer_tables[0]['caption'] == 'Employees by department'
    assert len(mcp.calls) == 1
    assert any(name == 'loop_partial_answer_recovered' for name, _ in events)


@pytest.mark.parametrize("kind,selectors,rows", [
    ("figure", {"column": "n", "label": "Employees"}, [["Sales", 120]]),
    ("bars", {"category_column": "Department", "measure_column": "n", "label": "Employees"}, [["Sales", 120], ["Engineering", 40]]),
    ("line", {"category_column": "Department", "measure_column": "n", "label": "Employees"}, [["2026-01-01", 10], ["2026-02-01", 20], ["2026-03-01", 30]]),
])
async def test_salvage_materializes_selected_visual_and_persists_it(kind, selectors, rows):
    from tests.runtime.test_harness_improvements import CREDS

    section = recovery_sections(['q'])[0]
    section['presentation'] = {'kind': kind, 'result_id': 'q', **selectors}
    section['supporting_data'] = True

    class VisualJudge(Judge):
        async def salvage(self, brief):
            return {**PART, 'outcome': 'full', 'unfinished': [], 'sections': [section]}

    loop, store, _, mcp, _ = build(
        [discovery(), query(), final()], VisualJudge([JudgeVerdict(False, "unsupported_by_evidence", "Repair prose.", reviewed=True, repair_type="prose")]),
        rows=[{'columns': ['Department', 'n'], 'rows': rows, 'row_count': len(rows), 'truncated': False}],
    )
    out = await run(loop)
    assert out.review['status'] == 'approved'
    assert out.review['completion'] == 'full'
    rendered = out.answer_document['sections'][0]
    assert rendered['presentation']['kind'] == kind
    assert rendered['supporting_table'] == {'result_id': 'q'}
    assert out.answer_document['sources']['q']['table_ref'].startswith("tbl_")
    assert len(mcp.calls) == 1
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.messages[-1].answer_document == out.answer_document


@pytest.mark.parametrize('patch', [
    {'outcome': 'partial', 'unfinished': []},
    {'outcome': 'full', 'unfinished': ['Missing requested chart']},
    {'outcome': 'none', 'evidence': ['q']},
])
async def test_salvage_schema_rejects_contradictory_completion(patch):
    response = {**PART, 'outcome': 'partial', **patch}
    judge = AnswerJudge(RecoveryClient([batch(call('salvageAnswer', 's', **response))]), 10000)
    with pytest.raises(ValidationError):
        await judge.salvage(JudgeBrief('exit_table', 'Show a chart'))


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("approved", [False, True])
async def test_salvage_falls_back_to_table_and_reviews_exact_document(fallback, approved):
    section = recovery_sections(['q'])[0]
    section['fallback'] = fallback
    section['presentation'] = {'kind': 'bars', 'result_id': 'q', 'category_column': 'Department',
                               'measure_column': 'n', 'label': 'Employees'}

    class VisualJudge(Judge):
        async def approve_presentation_fallback(self, brief):
            self.briefs.append(brief)
            return approved

        async def salvage(self, brief):
            return {**PART, 'outcome': 'full', 'unfinished': [], 'sections': [section]}

    judge = VisualJudge([JudgeVerdict(False, "unsupported_by_evidence", "Repair prose.", reviewed=True, repair_type="prose")])
    loop, _, _, mcp, events = build([discovery(), query(), final()], judge,
        rows=[{'columns': ['Department', 'n'], 'rows': [['Sales', 120]], 'row_count': 50, 'truncated': True}])
    out = await run(loop)
    candidate = judge.briefs[-1].answer_document
    assert candidate['sections'][0]['presentation']['kind'] == 'table'
    assert any('shown as a table' in c and 'complete result' in c for c in candidate['sections'][0]['caveats'])
    assert len(mcp.calls) == 1
    assert ('loop_recovery_fallback_reviewed', {'status': 'approved' if approved else 'rejected'}) in events
    if approved:
        assert out.review['status'] == 'approved'
        assert out.answer_document['sections'] == candidate['sections']
    else:
        assert out.review['status'] != 'approved'
        assert not out.answer_document['sections']


async def test_fallback_approval_uses_non_reasoning_model_and_exact_document():
    client = RecoveryClient([batch(call("approvePresentationFallback", "r", approved=True))])
    judge = AnswerJudge(client, 10000)
    candidate = {"takeaway": "Counts", "sections": [{"presentation": {"kind": "table", "result_id": "q"},
                 "caveats": ["The requested bars presentation is shown as a table: missing is not zero."]}], "sources": {}}
    assert await judge.approve_presentation_fallback(JudgeBrief("exit_table", "Counts as bars", answer_document=candidate))
    assert client.non_reasoning
    assert "missing is not zero" in str(client.calls[0].messages)
