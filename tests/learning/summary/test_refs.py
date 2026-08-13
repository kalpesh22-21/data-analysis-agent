"""`summary/refs.py::sql_by_ref` — the ONE ref→SQL resolution S3 totality and S4
generalize share.

These pin the three properties both readers depend on: an `answerWithTable` ref
resolves (it is the only ref a never-dispatched answer query has), a multi-table ref
resolves to ALL of its queries (so none escapes the D97 totality gate), and a ref
with no usable SQL is ABSENT rather than mapped to something falsy-but-present.
"""

from __future__ import annotations

from data_agent.learning.summary import load_session_summary, sql_by_ref

from .helpers import make_doc, make_job, make_trail_entry

HEADCOUNT_SQL = "SELECT count(*) FROM hr.employee"
PAYROLL_SQL = "SELECT sum(gross_pay) FROM payroll.payroll_fact"


async def _load(store, doc):
    return await load_session_summary(doc, store, job=make_job(doc.session_id))


async def test_a_runquery_ref_resolves_to_the_sql_it_ran(store):
    doc = make_doc(
        tool_trail=[make_trail_entry(turn_index=0, tool_call_id="q1", tool_name="runQuery",
                                     args={"sql": HEADCOUNT_SQL}, status="ok")],
    )
    assert sql_by_ref(await _load(store, doc)) == {"q1": (HEADCOUNT_SQL,)}


async def test_an_answer_ref_resolves_to_the_sql_it_designated(store):
    """The case the whole helper exists for: the query was NEVER dispatched, so the
    answer call is the only ref that can stand for it."""
    doc = make_doc(
        tool_trail=[make_trail_entry(turn_index=0, tool_call_id="ans",
                                     tool_name="answerWithTable",
                                     args={"answer": "1,204.", "sql": HEADCOUNT_SQL},
                                     status="ok")],
    )
    summary = await _load(store, doc)
    assert summary.tool_calls[0].sql is None  # not a _SQL_TOOLS member
    assert sql_by_ref(summary) == {"ans": (HEADCOUNT_SQL,)}


async def test_a_multi_table_ref_resolves_to_every_query_it_designated(store):
    """One ref, two queries. Both must come back: the totality gate checks each, and
    dropping one would let its literal predicates through unexamined."""
    doc = make_doc(
        tool_trail=[
            make_trail_entry(
                turn_index=0, tool_call_id="ans", tool_name="answerWithTable",
                args={"sql": "", "tables": [{"sql": HEADCOUNT_SQL},
                                            {"sql": PAYROLL_SQL}]},
                status="ok",
            ),
        ],
    )
    assert sql_by_ref(await _load(store, doc)) == {"ans": (HEADCOUNT_SQL, PAYROLL_SQL)}


async def test_a_ref_with_no_usable_sql_is_absent_not_empty(store):
    """Absent and empty are the same answer to every caller (`.get(ref, ())` /
    truthiness), so the map holds only the refs that can actually be cited."""
    doc = make_doc(
        tool_trail=[
            make_trail_entry(turn_index=0, tool_call_id="ask", tool_name="askUser",
                             args={"question": "which dept?"}, status="ok"),
            make_trail_entry(turn_index=0, tool_call_id="ans", tool_name="answerWithTable",
                             args={"answer": "No rows matched.", "sql": ""}, status="ok"),
        ],
    )
    assert sql_by_ref(await _load(store, doc)) == {}


async def test_a_failed_query_still_resolves_under_its_own_ref(store):
    """This map answers "what SQL does this ref stand for?", not "was it accepted?".
    `_validate_totality` cites refs the model chose and checks their predicates; a
    status filter here would silently answer a different question than either caller
    asked — and the callers already gate on acceptance in their own terms."""
    doc = make_doc(
        tool_trail=[make_trail_entry(turn_index=0, tool_call_id="bad", tool_name="runQuery",
                                     args={"sql": HEADCOUNT_SQL}, status="error",
                                     error_code="CLICKHOUSE_QUERY_ERROR")],
    )
    assert sql_by_ref(await _load(store, doc)) == {"bad": (HEADCOUNT_SQL,)}
