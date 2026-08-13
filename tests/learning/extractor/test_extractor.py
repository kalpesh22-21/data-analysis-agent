"""LearningExtractor — forced structured output + retry-on-mismatch (D31).
Matrix rows 1/8/9; task items 1, 7 (extractor level).
"""

from __future__ import annotations

import json

import pytest

from data_agent.learning.extractor import ExtractedCandidate
from data_agent.learning.extractor.schema import EXTRACTOR_TOOL_NAME, SchemaMismatchError

from .helpers import (
    KEEP_VERDICT,
    PAYROLL_SQL,
    blueprint_raw,
    emit_extractor,
    make_answer_sql,
    make_extractor,
    make_summary,
    make_tool_call,
    malformed_turn,
    scripted_turn,
)


def _payload(extractor) -> dict:
    """The session JSON the extractor handed the model on its FIRST turn — always
    the last message of that turn (the prior-art block, when present, precedes it)."""
    return json.loads(extractor._model_client.calls[0].messages[-1]["content"])


async def test_valid_scripted_emit_yields_one_candidate():
    extractor = emit_extractor([blueprint_raw()])
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert len(result.candidates) == 1
    assert result.declines == ()
    assert isinstance(result.candidates[0], ExtractedCandidate)
    assert result.candidates[0].header.type == "blueprint"


async def test_declined_candidate_surfaces_in_declines_not_candidates():
    # A no-evidence candidate is emitted by the model but rejected at validation.
    extractor = emit_extractor([blueprint_raw(evidence=[])])
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert result.candidates == ()
    assert len(result.declines) == 1
    assert result.declines[0].reason == "no_evidence"


async def test_empty_candidates_array_is_valid_zero_output():
    extractor = emit_extractor([])
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert result.candidates == ()
    assert result.declines == ()


async def test_forced_tool_is_offered_to_the_model():
    extractor = emit_extractor([blueprint_raw()])
    await extractor.extract(make_summary(), KEEP_VERDICT)
    # The extractor offers exactly the emit_candidates tool.
    client = extractor._model_client
    tool_names = {t.get("name") for t in client.calls[0].tools}
    assert tool_names == {EXTRACTOR_TOOL_NAME}


# --- item 7: retry-on-mismatch → raises (drives dead-letter in the consumer) -


async def test_persistent_malformed_response_raises_after_retries():
    # max_retries=2 ⇒ 1 initial + 2 retries = 3 malformed attempts, then raise.
    extractor = make_extractor(
        [malformed_turn(), malformed_turn(), malformed_turn()], max_retries=2
    )
    with pytest.raises(SchemaMismatchError):
        await extractor.extract(make_summary(), KEEP_VERDICT)


async def test_retry_recovers_when_a_later_attempt_is_well_formed():
    # malformed first, then a valid emit within the retry budget → succeeds.
    extractor = make_extractor(
        [malformed_turn(), scripted_turn([blueprint_raw()])], max_retries=2
    )
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert len(result.candidates) == 1


async def test_exactly_max_retries_plus_one_attempts_are_made():
    client_turns = [malformed_turn(), malformed_turn()]  # 1 initial + 1 retry
    extractor = make_extractor(client_turns, max_retries=1)
    with pytest.raises(SchemaMismatchError):
        await extractor.extract(make_summary(), KEEP_VERDICT)
    assert extractor._model_client.calls_made == 2


# --- Release 1: the payload seam ---------------------------------------------


@pytest.mark.parametrize("tool_name", ["updateAnalysisState", "recordAssumptions"])
async def test_state_bookkeeping_calls_are_dropped_from_the_payload(tool_name):
    """A Release-1 session carries many of these per turn (the model re-sends the
    whole intent list every round, and rejected attempts are persisted too). They
    carry no SQL and no tool outcome the extractor can use — only tokens, and a
    `denied` count that reads as friction that never happened."""
    extractor = emit_extractor([blueprint_raw()])
    summary = make_summary(
        tool_calls=(
            make_tool_call(ref="tc1", sql=PAYROLL_SQL),
            make_tool_call(ref="st1", sql=None, tool_name=tool_name, status="denied"),
            make_tool_call(ref="st2", sql=None, tool_name=tool_name, status="ok"),
        ),
    )
    await extractor.extract(summary, KEEP_VERDICT)
    payload = _payload(extractor)
    assert [tc["tool_call_ref"] for tc in payload["tool_calls"]] == ["tc1"]
    # The SUMMARY is untouched — the filter lives at the payload seam only, because
    # the summary is a faithful projection other stages read.
    assert [tc.tool_call_ref for tc in summary.tool_calls] == ["tc1", "st1", "st2"]


async def test_the_answer_sql_is_its_own_payload_section():
    """The final answer's SQL may never have been dispatched as a runQuery, so
    `tool_calls` can be missing it entirely — it gets its own key."""
    extractor = emit_extractor([blueprint_raw()])
    summary = make_summary(
        tool_calls=(make_tool_call(ref="tc1", sql=None, tool_name="answerWithTable"),),
        answer_sqls=(
            make_answer_sql(PAYROLL_SQL, ref="ans1", blueprint_id="bp-earnings"),
            make_answer_sql("SELECT headcount FROM hr.emp", ref="ans1"),
        ),
    )
    await extractor.extract(summary, KEEP_VERDICT)
    payload = _payload(extractor)
    assert payload["answer_sql"] == [
        {"tool_call_ref": "ans1", "sql": PAYROLL_SQL, "blueprint_id": "bp-earnings"},
        {"tool_call_ref": "ans1", "sql": "SELECT headcount FROM hr.emp",
         "blueprint_id": None},
    ]


async def test_a_session_with_no_answer_table_sends_an_empty_section():
    extractor = emit_extractor([blueprint_raw()])
    await extractor.extract(make_summary(), KEEP_VERDICT)
    assert _payload(extractor)["answer_sql"] == []
