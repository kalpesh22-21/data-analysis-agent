"""Omitted redundant selections must not discard an otherwise complete proposal."""

import pytest

from data_agent.runtime.loop.answer_judge import JudgeVerdict
from data_agent.runtime.loop.proposal import (
    assess_deliverable_evidence,
    normalize_proposal_args,
    validate_proposal_args,
)
from tests.runtime.loop.test_repository_regressions import entry
from tests.runtime.test_harness_improvements import Judge, batch, build, call, discovery, query, run


@pytest.mark.parametrize("reject", [False, True])
async def test_answer_and_table_only_reaches_judge_with_original_draft(reject):
    draft = "Sales has 120 employees."
    verdict = JudgeVerdict(False, "contradicts_result", "Incorrect interpretation.", True)
    judge = Judge([verdict, verdict] if reject else [])
    final = batch(call("finalizeAnswer", "answer", answer=draft, tables=[{"result_id": "q"}]))
    loop, _, _, _, events = build([discovery(), query(), final, final], judge)
    out = await run(loop)
    assert judge.briefs
    proposal_briefs = [b for b in judge.briefs if not b.terminal_partial_review]
    assert all(brief.draft == draft for brief in proposal_briefs)
    assert all(brief.referenced_result_ids == ("q",) for brief in proposal_briefs)
    assert judge.briefs[0].selected_components[0]["result_id"] == "q"
    if reject:
        assert out.review["status"] == "rejected"
        assert not out.answer_tables
    else:
        assert out.assistant_text == draft
        assert len(out.answer_tables) == 1
        assert out.review["status"] == "approved"
        assert not any(name == "loop_finalization_block_spent" for name, _ in events)


@pytest.mark.parametrize("field", ["capability_refs", "evidence"])
@pytest.mark.parametrize("value", [None, "q", [123]])
def test_explicit_malformed_selections_are_not_defaulted(field, value):
    args = normalize_proposal_args({"answer": "Answer", "tables": [], field: value})
    assert validate_proposal_args(args)


def test_explicit_empty_evidence_is_preserved_and_input_is_unchanged():
    original = {"answer": "Answer", "tables": [{"result_id": "q"}], "evidence": []}
    normalized = normalize_proposal_args(original)
    assert normalized["evidence"] == []
    assert normalized["capability_refs"] == []
    assert "capability_refs" not in original


@pytest.mark.parametrize(
    "trail", [[], [entry("q", "runQuery", status="error")], [entry("q", "sampleRows")]]
)
def test_inferred_evidence_drops_known_ineligible_results_but_blocks_missing_ids(trail):
    args = normalize_proposal_args({"answer": "Answer", "tables": [{"result_id": "q"}]})
    assert args["evidence"] == ["q"]
    assessed = assess_deliverable_evidence(None, args, trail)
    assert bool(assessed.feedback) == (not trail)
    assert assessed.references == ()


def test_required_content_is_still_validated():
    assert validate_proposal_args(normalize_proposal_args({"tables": []}))
    assert validate_proposal_args(normalize_proposal_args({"answer": "Answer"}))
    assert validate_proposal_args(normalize_proposal_args({"answer": "Answer", "tables": [None]}))
