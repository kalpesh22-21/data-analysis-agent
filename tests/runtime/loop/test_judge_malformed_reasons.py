"""A judgement is read once, and an unusable one says why.

The judge model echoes shapes it was shown. A rendered answer document carries a
runtime-owned `sources`, so a corrected document that copies it must still apply (P01:
the correction was discarded and the turn fell through to salvage). Every other
unusable shape still fails open to an unreviewed approval, now with a closed
`malformed_reason`, and the two rejection shapes that used to read as a REVIEWED
approval are reported as unreadable instead.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.runtime.loop.answer_judge import (
    APPROVED,
    JUDGE_TOOL_NAME,
    AnswerJudge,
    JudgeBrief,
    _read_verdict,
    parse_verdict,
)
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.observability.tracing import _GUARDRAIL_OBSERVER_ATTR_ALLOWLIST

DOCUMENT = {
    "takeaway": "I can't provide verified instructions for updating direct deposit information right now.",
    "sections": [
        {
            "id": "s1",
            "title": "Direct deposit instructions unavailable",
            "evidence_refs": [],
            "presentation": {"kind": "text", "markdown": "The Help Center documentation could not be retrieved."},
            "caveats": ["This is a documentation-access limitation, not a confirmation that the feature is unavailable."],
            "actions": [],
        }
    ],
    "evidence": [],
}
APPROVE_DOCUMENT = {
    "approved": True, "violation": "", "feedback": "", "repair_type": "presentation",
    "corrected_document": DOCUMENT,
}
REJECTION = {
    "approved": False, "violation": "unsupported_by_evidence", "feedback": "Cite the result.",
    "repair_type": "prose", "result_ids": [],
}


def _turn(arguments: Any) -> ModelTurnResult:
    return ModelTurnResult(
        assistant_text=None,
        tool_calls=[ToolCallRequest(id="j1", name=JUDGE_TOOL_NAME, arguments=arguments)],
    )


async def _review(arguments: Any, *, site: str = "exit_prose", allow: bool = True):
    events: list[tuple[str, dict]] = []
    judge = AnswerJudge(
        ScriptedModelClient([_turn(arguments), _turn(arguments)]), token_budget=20000,
        observer=lambda event, payload: events.append((event, payload)),
    )
    verdict = await judge.review(JudgeBrief(site, "How do I update direct deposit?",
                                            draft="Draft.", allow_prose_correction=allow))
    return verdict, [payload for event, payload in events if event == "loop_answer_judge_failed"]


async def test_p01_a_corrected_document_carrying_sources_is_applied() -> None:
    verdict, failures = await _review({**APPROVE_DOCUMENT, "corrected_document": {**DOCUMENT, "sources": {}}})
    assert verdict.approved and verdict.reviewed
    assert verdict.corrected_document == DOCUMENT  # the echoed `sources` is gone
    assert failures == []


def test_a_document_bringing_its_own_sql_is_still_void_and_names_the_schema_rule() -> None:
    reading = _read_verdict(
        _turn({**APPROVE_DOCUMENT, "corrected_document": {**DOCUMENT, "sql": "SELECT 1"}}), "exit_prose"
    )
    assert reading.verdict is APPROVED
    assert reading.malformed_reason == "correction_schema"
    assert reading.schema_path.endswith("additionalProperties")


@pytest.mark.parametrize(
    ("correction", "expected"),
    [({"corrected_document": DOCUMENT}, "presentation"), ({"corrected_answer": "Better takeaway."}, "prose")],
)
def test_a_missing_repair_type_is_fixed_by_the_one_correction_carried(correction, expected) -> None:
    arguments = {"approved": True, "violation": "", "feedback": "", **correction}
    verdict = parse_verdict(_turn(arguments), "exit_prose")
    assert verdict.repair_type == expected
    assert verdict.corrected_document is not None or verdict.corrected_answer is not None


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (ModelTurnResult(assistant_text=None, tool_calls="record_judgement"), "not_a_sequence"),  # type: ignore[arg-type]
        (ModelTurnResult(assistant_text=None, tool_calls=[]), "no_judgement_call"),
        (_turn("approved"), "arguments_not_object"),
        (_turn({"approved": "true"}), "approved_not_boolean"),
        (_turn({"approved": True, "partial_answer": {"takeaway": "x"}}), "approval_with_partial_answer"),
        (_turn({**APPROVE_DOCUMENT, "corrected_answer": "Also this."}), "correction_count"),
        (_turn({**APPROVE_DOCUMENT, "violation": "unsupported_by_evidence", "feedback": "Still unsupported."}), "correction_with_violation"),
        (_turn({**APPROVE_DOCUMENT, "repair_type": "analysis"}), "correction_repair_type"),
        (_turn({"approved": True, "repair_type": "prose", "corrected_question": {"question": "Which?", "options": []}}),
         "correction_site"),
        (_turn({"approved": True, "repair_type": "prose", "corrected_answer": " "}), "correction_text"),
        (_turn({**REJECTION, "violation": ""}), "unknown_violation"),
        (_turn({**REJECTION, "feedback": ""}), "unusable_feedback"),
        (_turn({**REJECTION, "repair_type": "rewrite"}), "unknown_repair_type"),
        (_turn({**REJECTION, "result_ids": "c1"}), "result_ids_invalid"),
    ],
)
def test_every_unusable_shape_approves_and_names_its_reason(result, reason) -> None:
    reading = _read_verdict(result, "exit_prose")
    assert reading.verdict is APPROVED
    assert reading.malformed_reason == reason


@pytest.mark.parametrize("override", [{"repair_type": "rewrite"}, {"result_ids": "c1"}])
async def test_an_unusable_rejection_is_no_longer_a_reviewed_approval(override) -> None:
    """These rejections were dropped as approvals AND recorded as reviewed, so a rejected
    answer shipped with nothing in telemetry to show the review never happened."""
    verdict, failures = await _review({**REJECTION, **override})
    assert verdict.approved and not verdict.reviewed
    assert [f["malformed_reason"] for f in failures] in (["unknown_repair_type"], ["result_ids_invalid"])


async def test_a_correction_where_none_is_allowed_says_so() -> None:
    verdict, failures = await _review(APPROVE_DOCUMENT, allow=False)
    assert verdict is APPROVED and not verdict.reviewed
    assert failures == [{"reason": "malformed", "malformed_reason": "correction_not_allowed"}]


async def test_a_schema_failure_reports_the_schema_path_on_the_failed_event() -> None:
    _, failures = await _review({**APPROVE_DOCUMENT, "corrected_document": {**DOCUMENT, "sql": "SELECT 1"}})
    assert failures[0]["malformed_reason"] == "correction_schema"
    assert failures[0]["schema_path"].startswith("/")
    assert "SELECT" not in failures[0]["schema_path"]  # schema coordinates, never response values


def test_the_reason_and_schema_path_reach_phoenix() -> None:
    assert {"malformed_reason", "schema_path"} <= set(_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST)


def test_a_clean_verdict_has_no_reason() -> None:
    assert _read_verdict(_turn(APPROVE_DOCUMENT), "exit_prose").malformed_reason is None
    assert _read_verdict(_turn(REJECTION), "exit_prose").malformed_reason is None


def test_a_chart_section_copied_from_the_rendered_draft_is_returned_to_proposal_form() -> None:
    rendered = {"kind": "bars", "result_id": "c1", "x_column": "Department", "y_column": "Headcount",
                "label": "Headcount", "ranking": {"direction": "descending", "ties": "dense"}}
    section = {**DOCUMENT["sections"][0], "evidence_refs": ["c1"], "presentation": rendered}
    document = {**DOCUMENT, "sections": [section], "evidence": ["c1"], "sources": {}, "answer_id": "a1"}
    reading = _read_verdict(_turn({**APPROVE_DOCUMENT, "corrected_document": document}), "exit_table")
    assert reading.malformed_reason is None
    assert reading.verdict.corrected_document["sections"][0]["presentation"] == {
        "kind": "bars", "result_id": "c1", "category_column": "Department", "measure_column": "Headcount",
        "label": "Headcount", "direction": "descending", "ties": "dense",
    }
    assert "sources" not in reading.verdict.corrected_document


def test_a_rejection_carrying_a_rendered_partial_answer_is_returned_to_proposal_form() -> None:
    partial = {"takeaway": "Partial.", "evidence": ["c1"], "unfinished": ["The ranking."], "sources": {},
               "sections": [{**DOCUMENT["sections"][0], "evidence_refs": ["c1"],
                             "presentation": {"kind": "bars", "result_id": "c1", "x_column": "Dept",
                                              "y_column": "Headcount", "label": "Headcount"}}]}
    verdict = parse_verdict(_turn({**REJECTION, "partial_answer": partial}), "exit_table")
    assert not verdict.approved
    assert "sources" not in verdict.partial_answer
    assert verdict.partial_answer["sections"][0]["presentation"]["category_column"] == "Dept"


async def test_a_repeated_caveat_complaint_is_a_reviewed_approval() -> None:
    """The prompt calls repeated caveats cosmetic and the runtime removes them, so the
    answer stands; this is not a judge that could not be read."""
    verdict, failures = await _review({**REJECTION, "violation": "repeated_caveat"}, site="exit_table")
    assert verdict.approved and verdict.reviewed
    assert failures == []


def test_a_salvage_outcome_mismatch_is_explained_in_words() -> None:
    from jsonschema import Draft202012Validator

    from data_agent.runtime.loop.salvage import _OUTCOME_LISTS, _retry_reason

    schema = {"allOf": [{"if": {"properties": {"outcome": {"const": o}}}, "then": {"properties": lists}}
                        for o, lists, _ in _OUTCOME_LISTS]}
    error = next(Draft202012Validator(schema).iter_errors(
        {"outcome": "full", "unfinished": ["The ranking."], "evidence": ["c1"], "sections": []}))
    reason = _retry_reason(error)
    assert "outcome=full needs unfinished=[]" in reason
    assert "allOf" not in reason



def test_a_chart_correction_with_json_encoded_sections_is_mapped_back_too():
    """Rendered names were mapped back before encoded arrays were decoded, so the same
    correction applied as an array and was discarded as a JSON-encoded string."""
    import json

    rendered = {"kind": "bars", "result_id": "c1", "x_column": "Department", "y_column": "Headcount",
                "label": "Headcount", "ranking": {"direction": "descending", "ties": "dense"}}
    section = {**DOCUMENT["sections"][0], "evidence_refs": ["c1"], "presentation": rendered}
    for sections in ([section], json.dumps([section])):
        document = {**DOCUMENT, "sections": sections, "evidence": ["c1"]}
        reading = _read_verdict(_turn({**APPROVE_DOCUMENT, "corrected_document": document}), "exit_table")
        assert reading.malformed_reason is None, sections
        assert reading.verdict.corrected_document["sections"][0]["presentation"]["category_column"] == "Department"


def test_a_partial_answer_keeps_its_unfinished_list_through_decoding():
    import json

    partial = {"takeaway": "Partial.", "evidence": ["c1"], "unfinished": ["The ranking."],
               "sections": json.dumps([{**DOCUMENT["sections"][0], "evidence_refs": ["c1"],
                                        "presentation": {"kind": "bars", "result_id": "c1", "x_column": "Dept",
                                                         "y_column": "Headcount", "label": "Headcount"}}])}
    verdict = parse_verdict(_turn({**REJECTION, "partial_answer": partial}), "exit_table")
    assert verdict.partial_answer["unfinished"] == ["The ranking."]
    assert verdict.partial_answer["sections"][0]["presentation"]["category_column"] == "Dept"
