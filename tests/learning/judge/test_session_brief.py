"""`judge/prompt.py::session_brief` — what the PRE-extraction judge is shown.

The judge is asked one question ("does the corpus already cover this session?") and it
is the only stage in the loop allowed to discard an analyst's work on the answer. So
what reaches the brief is a correctness concern, not a formatting one: a query the
brief omits is a query the drop decision was made without.

These cover the Release-1 additions — the `answerWithTable` designation, which is
absent from `tool_calls` whenever the designated query was never dispatched as a
runQuery, and the bookkeeping exclusion (H2) that stops intent-ledger churn from eating
the tool-call cap. Everything else about the brief is exercised through the judge's own
suite.
"""

from __future__ import annotations

import json

import pytest

from data_agent.learning.judge.prompt import _MAX_TOOL_CALLS, session_brief
from data_agent.learning.summary.models import (
    BOOKKEEPING_TOOLS,
    AnswerSql,
    ToolCallSummary,
)

from .helpers import CANON_SQL, make_summary

ANSWER_SQL = "SELECT sum(AnnualSalary) FROM dbpcm_warehouse.employee WHERE Region = 'NA'"


def _payload(summary) -> dict:
    return json.loads(session_brief(summary).split("\n", 1)[1])


def test_the_answer_designation_reaches_the_judge():
    """The query the user was actually shown, which the judge would otherwise never
    see: `answerWithTable` is not a `_SQL_TOOLS` member, so its `tool_calls` entry
    carries `sql: null`."""
    summary = make_summary(answer_sqls=(AnswerSql("ans", ANSWER_SQL, "bp-pay"),))
    payload = _payload(summary)
    assert payload["answer_sql"] == [
        {"ref": "ans", "sql": ANSWER_SQL, "blueprint_id": "bp-pay"}
    ]
    # The executed SQL is still there — the section is additive, not a replacement.
    assert [tc["sql"] for tc in payload["tool_calls"]] == [CANON_SQL]


def test_a_session_with_no_answer_table_carries_an_empty_section():
    payload = _payload(make_summary())
    assert payload["answer_sql"] == []
    assert payload["truncated"] is False


def _call(ref: str, tool: str, sql: str | None = None) -> ToolCallSummary:
    return ToolCallSummary(
        turn_index=0,
        tool_call_ref=ref,
        tool_name=tool,
        args={"sql": sql} if sql else {},
        sql=sql,
        status="ok",
        error_code=None,
        provenance=frozenset(),
        result_columns=(),
        result_row_count=1,
        result_full_ref=None,
        full_result_loaded=False,
    )


def test_state_churn_does_not_crowd_substantive_calls_out_of_the_brief():
    """H2. Twelve real queries interleaved with fifteen ledger updates: every query
    must reach the judge. Before the exclusion the first 12 entries of the trail won
    the cap, so a session could hand the judge four queries and eight `sql: null`
    bookkeeping entries and ask it whether the corpus covers the work."""
    substantive = [_call(f"q{i}", "runQuery", f"SELECT {i}") for i in range(_MAX_TOOL_CALLS)]
    churn = [_call(f"s{i}", "updateAnalysisState") for i in range(15)]
    # Interleaved churn-first, the live Release-1 shape (the ledger is declared before
    # the work and re-sent after every call).
    mixed = [churn[0]]
    for i, call in enumerate(substantive):
        mixed.extend([call, churn[i + 1]])
    payload = _payload(make_summary(tool_calls=tuple(mixed)))

    assert [tc["ref"] for tc in payload["tool_calls"]] == [f"q{i}" for i in range(12)]
    assert payload["bookkeeping_calls_omitted"] == 13


def test_truncated_stays_false_when_only_state_churn_overflowed():
    """The verdict-poisoning half of H2: `truncated` tells the judge (system prompt
    rule 6) to lower its confidence. Churn overflowing the cap is not evidence the
    judge is missing anything, and flagging it discounts a verdict made on the whole
    session."""
    calls = [_call("q1", "runQuery", "SELECT 1")]
    calls += [_call(f"s{i}", "recordAssumptions") for i in range(_MAX_TOOL_CALLS + 5)]
    payload = _payload(make_summary(tool_calls=tuple(calls)))

    assert payload["truncated"] is False
    assert len(payload["tool_calls"]) == 1
    assert payload["bookkeeping_calls_omitted"] == _MAX_TOOL_CALLS + 5


def test_genuine_overflow_of_substantive_calls_still_reports_truncated():
    """The exclusion must not become a way to hide real truncation."""
    calls = tuple(
        _call(f"q{i}", "runQuery", f"SELECT {i}") for i in range(_MAX_TOOL_CALLS + 1)
    )
    payload = _payload(make_summary(tool_calls=calls))

    assert payload["truncated"] is True
    assert len(payload["tool_calls"]) == _MAX_TOOL_CALLS
    assert payload["bookkeeping_calls_omitted"] == 0


def test_a_session_with_no_bookkeeping_reports_zero_omissions():
    assert _payload(make_summary())["bookkeeping_calls_omitted"] == 0


@pytest.mark.parametrize("tool_name", sorted(BOOKKEEPING_TOOLS))
def test_every_bookkeeping_tool_is_dropped_from_the_brief(tool_name: str):
    """Parity with the extractor's payload seam, by CONSTRUCTION rather than by mirror:
    both stages read `summary/models.py::BOOKKEEPING_TOOLS`, and both suites parametrize
    over it (`tests/learning/extractor/test_extractor.py::test_state_bookkeeping_calls_
    are_dropped_from_the_payload` is the other half). A tool added to the set is covered
    in both places without anyone widening a literal twice."""
    summary = make_summary(
        tool_calls=(_call("q1", "runQuery", "SELECT 1"), _call("bk1", tool_name)),
    )
    payload = _payload(summary)

    assert [tc["ref"] for tc in payload["tool_calls"]] == ["q1"]
    assert payload["bookkeeping_calls_omitted"] == 1
    # The SUMMARY is untouched — the filter lives at the prompt seam only.
    assert [tc.tool_call_ref for tc in summary.tool_calls] == ["q1", "bk1"]
