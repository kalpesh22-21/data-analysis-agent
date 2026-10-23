"""Independent recovery contract and real non-reasoning transport wiring."""

import pytest
from jsonschema import ValidationError

from data_agent.runtime.loop.answer_judge import AnswerJudge, JudgeBrief, JudgeVerdict
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from tests.runtime.loop.test_partial_answer import PART, TEXT, build, final, reject
from tests.runtime.test_harness_improvements import Judge, batch, call, discovery, query, run


class RecoveryClient(ScriptedModelClient):
    def without_thinking(self, options):
        assert options == {"enable_thinking": False, "thinking": False}
        self.non_reasoning = True
        return self


@pytest.mark.parametrize("outcome", ["full", "partial", "none"])
async def test_salvage_has_explicit_outcome_and_non_reasoning_transport(outcome):
    response = {**PART, "outcome": outcome, "captions": []}
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
    assert ("loop_recovery_call_failed", {"reason": "schema_validation"}) in events


async def test_missing_salvage_tool_call_reports_response_contract_failure():
    events = []
    judge = AnswerJudge(RecoveryClient([batch()]), 10000,
                        observer=lambda name, payload: events.append((name, payload)))
    with pytest.raises(ValueError, match="invalid_recovery_response"):
        await judge.salvage(JudgeBrief("exit_prose", "Headcount?"))
    assert ("loop_recovery_call_failed", {"reason": "response_contract"}) in events


async def test_salvage_preserves_full_provider_result_id_through_caption_and_delivery():
    ref = "chatcmpl-tool-9050b364e4783be2"
    caption = "Employee count by department, all accessible records"

    class SalvageJudge(Judge):
        async def salvage(self, brief):
            assert ref in {r["tool_call_id"] for r in brief.results}
            assert ref in {c["result_id"] for c in brief.selected_components}
            return {
                **PART, "outcome": "full", "unfinished": [],
                "evidence": [ref], "table_result_ids": [ref],
                "captions": [{"result_id": ref, "caption": caption}],
            }

    execution = batch(call("runQuery", ref, **query().tool_calls[0].arguments))
    proposal = final()
    proposal.tool_calls[0].arguments["evidence"] = [ref]
    proposal.tool_calls[0].arguments["tables"][0]["result_id"] = ref
    judge = SalvageJudge([JudgeVerdict(
        False, "unsupported_by_evidence", "Repair prose.",
        reviewed=True, repair_type="prose",
    )])
    loop, _, _, _, events = build([discovery(), execution, proposal], judge)
    out = await run(loop)
    assert out.review["status"] == "approved"
    assert out.answer_tables[0]["caption"] == caption
    assert not any(name == "loop_recovery_validation_failed" for name, _ in events)


async def test_guard_rejected_salvage_is_not_logged_as_approved():
    class SalvageJudge(Judge):
        async def salvage(self, brief):
            return {
                **PART,
                "answer": "| Department | Employees |\n|---|---|\n| Sales | 120 |",
                "outcome": "full",
                "unfinished": [],
                "captions": [],
            }

    judge = SalvageJudge([JudgeVerdict(
        False, "unsupported_by_evidence", "Repair prose.",
        reviewed=True, repair_type="prose",
    )])
    loop, _, _, _, events = build([discovery(), query(), final()], judge)
    out = await run(loop)
    assert out.review["status"] != "approved"
    assert not any(name == "loop_salvage_approved" for name, _ in events)
    assert ("loop_recovery_validation_failed", {
        "reason": "answer_guard", "rule": "markdown_table",
    }) in events


async def test_salvage_delivers_full_answer_and_repairs_caption_after_zero_budget():
    class SalvageJudge(Judge):
        async def review(self, brief):
            from data_agent.runtime.loop.delivery import CURRENT_DELIVERY

            CURRENT_DELIVERY.get().review_seconds = 0
            return await super().review(brief)

        async def salvage(self, brief):
            self.salvage_brief = brief
            return {
                **PART,
                "outcome": "full",
                "unfinished": [],
                "table_result_ids": ["q"],
                "captions": [{"result_id": "q", "caption": "Employees by department"}],
            }

    judge = SalvageJudge([reject()])
    loop, store, model, _, _ = build([discovery(), query(), final(), final()], judge)
    out = await run(loop)
    assert out.review["status"] == "approved" and out.review["completion"] == "full"
    assert out.answer_tables[0]["caption"] == "Employees by department"
    assert out.assistant_text == PART["answer"]
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
            return {**PART, "outcome": "partial", "captions": []}

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
        call(
            "finalizeAnswer",
            "f",
            answer=long,
            tables=[{"result_id": "q", "caption": "Headcount"}],
            evidence=["q"],
        )
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
            return {
                **PART,
                "outcome": "full",
                "unfinished": [],
                "evidence": ["q2"],
                "table_result_ids": ["q2"],
                "captions": [],
            }

    sql = "SELECT COUNT(*) AS n FROM hr.employee"
    judge = SelectOther(
        [
            JudgeVerdict(
                False,
                "measurement_mismatch",
                "Select the correct result.",
                reviewed=True,
                repair_type="presentation",
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


async def test_oversized_partial_is_shortened_by_salvage_without_agent_prose_repair():
    class Shorten(Judge):
        async def salvage(self, brief):
            return {**PART, "outcome": "partial", "table_result_ids": ["q"], "captions": []}

    part = {**PART, "answer": PART["answer"] * 200, "table_result_ids": ["q"]}
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
    assert out.assistant_text == TEXT and out.answer_tables
    assert model.calls_made == 3
    assert any(e == "loop_recovery_validation_failed" for e, _ in events)


async def test_approved_salvage_is_not_dropped_by_advisory_word_limit():
    text = "Sales has 120 employees in the available records. " + "These are the records available within the requested scope. " * 12
    class SalvageJudge(Judge):
        async def salvage(self, brief):
            return {**PART, "answer": text.strip(), "outcome": "full", "unfinished": [],
                    "table_result_ids": ["q"], "captions": []}
    judge = SalvageJudge([JudgeVerdict(False, "unsupported_by_evidence", "Repair prose.", reviewed=True, repair_type="prose")])
    loop, _, _, _, _ = build([discovery(), query(), final()], judge)
    out = await run(loop)
    assert out.review["status"] == "approved"
    assert out.assistant_text == text.strip()
    assert out.answer_tables


async def test_salvage_accepts_schema_valid_json_content_without_tool_wrapper():
    import json
    from data_agent.runtime.model.client import ModelTurnResult
    response = {**PART, "outcome": "full", "unfinished": [], "captions": []}
    judge = AnswerJudge(RecoveryClient([ModelTurnResult(assistant_text=json.dumps(response))]), 10000)
    assert await judge.salvage(JudgeBrief("exit_prose", "Headcount?")) == response


async def test_json_content_still_requires_complete_salvage_schema():
    from data_agent.runtime.model.client import ModelTurnResult
    judge = AnswerJudge(RecoveryClient([ModelTurnResult(assistant_text='{"outcome":"full"}')]), 10000)
    with pytest.raises(ValidationError):
        await judge.salvage(JudgeBrief("exit_prose", "Headcount?"))


async def test_invalid_caption_does_not_discard_supported_salvage_table():
    class SalvageJudge(Judge):
        async def salvage(self, brief):
            return {
                **PART, "outcome": "full", "unfinished": [], "table_result_ids": ["q"],
                "captions": [{"result_id": "q", "caption": "SELECT * FROM hr.employee"}],
            }
    judge = SalvageJudge([JudgeVerdict(False, "unsupported_by_evidence", "Repair prose.",
                                      reviewed=True, repair_type="prose")])
    loop, _, _, _, events = build([discovery(), query(), final()], judge)
    out = await run(loop)
    assert out.review["status"] == "approved"
    assert out.assistant_text == PART["answer"]
    assert out.answer_tables and not out.answer_tables[0].get("caption")
    assert ("loop_salvage_caption_omitted", {"reason": "invalid_caption"}) in events
