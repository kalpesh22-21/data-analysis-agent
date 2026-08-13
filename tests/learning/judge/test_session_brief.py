"""`judge/prompt.py::session_brief` — what the PRE-extraction judge is shown.

The judge is asked one question ("does the corpus already cover this session?") and it
is the only stage in the loop allowed to discard an analyst's work on the answer. So
what reaches the brief is a correctness concern, not a formatting one: a query the
brief omits is a query the drop decision was made without.

These cover the Release-1 addition only — the `answerWithTable` designation, which is
absent from `tool_calls` whenever the designated query was never dispatched as a
runQuery. Everything else about the brief is exercised through the judge's own suite.
"""

from __future__ import annotations

import json

from data_agent.learning.judge.prompt import session_brief
from data_agent.learning.summary.models import AnswerSql

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
