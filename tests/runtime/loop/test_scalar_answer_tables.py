"""Evidence-only results stay separate from explicitly designated tables."""

from dataclasses import replace

import pytest

from data_agent.runtime.loop.proposal import extra_evidence
from data_agent.runtime.session.models import ResultPreview
from data_agent.runtime.session_history import project_history
from tests.runtime.loop.test_repository_regressions import entry
from tests.runtime.test_harness_improvements import (
    CREDS,
    batch,
    build,
    call,
    discovery,
    finish,
    run,
)


@pytest.mark.parametrize(
    "predicate,count", [("", 120), (" WHERE Salary > 10000", 23), (" WHERE Salary > 10000", 0)]
)
async def test_employee_count_returns_prose_and_original_query_with_no_extra_read(predicate, count):
    sql = 'SELECT count() AS "Employee Count" FROM hr.employee' + predicate
    text = f"There are {count} matching employees."
    loop, store, model, mcp, _ = build(
        [discovery(), batch(call("runQuery", "q", sql=sql)), batch(finish(text))],
        rows=[{"columns": ["Employee Count"], "rows": [[count]], "row_count": 1}],
    )
    out = await run(
        loop, "How many employees make more than 10k?" if predicate else "How many employees?"
    )
    assert out.assistant_text == text
    assert out.answer_tables is None
    assert out.extra_evidence[0]["sql"] == sql
    assert len(mcp.calls) == 1 and model.calls_made == 3
    doc = await store.get_or_create_session(CREDS.session_id)
    assert project_history(doc.messages, doc.tool_trail, CREDS.column_scope, None)["turns"][0]["extra_evidence"] == out.extra_evidence
    assert (
        project_history(doc.messages, doc.tool_trail, CREDS.column_scope, None)["turns"][0][
            "answer_tables"
        ]
        == out.answer_tables
    )


def test_only_cited_eligible_evidence_is_projected():
    preview = ResultPreview(columns=["Count"], preview_rows=[[120]], row_count=1, truncated=False)
    query = replace(entry("q", "runQuery"), result_preview=preview)
    blueprint = replace(entry("bp", "runBlueprint"), authoritative=True, result_preview=preview)
    unverified = replace(blueprint, tool_call_id="bad", authoritative=False)
    sample = replace(query, tool_call_id="sample", tool_name="sampleRows")
    result = extra_evidence(["q", "bp", "bp", "bad", "sample", "missing"],
                            [query, blueprint, unverified, sample], selected=["q"])
    assert [r["result_id"] for r in result] == ["bp"]
    assert result[0]["result_preview"] == preview.to_doc()
    assert extra_evidence(["bp"], [blueprint], excluded=["bp"]) is None
    assert extra_evidence(["q"], [replace(query, provenance=None)]) is None


async def test_verified_count_blueprint_is_returned_as_extra_evidence():
    from data_agent.runtime.dispatch.tool_dispatcher import ToolResult
    from tests._blueprint_gate import expand_blueprint

    sql = 'SELECT count() AS "Employee Count" FROM hr.employee'
    preview = ResultPreview(
        columns=["Employee Count"], preview_rows=[[120]], row_count=1, truncated=False
    )

    class CountBlueprint:
        tool_name = "runBlueprint"

        async def run(self, arguments, credentials, **kwargs):
            return ToolResult(
                status="ok",
                tool_name="runBlueprint",
                error_code=None,
                retryable=None,
                user_message=None,
                provenance=frozenset({("hr.employee", "Id")}),
                result_preview=preview,
                authoritative=True,
                result_full={
                    "blueprint_id": "count-employees",
                    "status": "verified",
                    "terminal_sql": sql,
                    "sql": [sql],
                    "columns": ["Employee Count"],
                    "row_count": 1,
                    "preview_rows": [[120]],
                    "verify": {"grain_checked": True},
                },
            )

    loop, store, model, _, _ = build(
        [
            batch(call("runBlueprint", "bp", id="count-employees", slot_bindings={})),
            batch(
                call(
                    "finalizeAnswer",
                    "answer",
                    answer="There are 120 employees.",
                    tables=[],
                    capability_refs=[],
                    evidence=["bp"],
                )
            ),
        ],
        extra={"runBlueprint": CountBlueprint()},
    )
    await expand_blueprint(store, CREDS.session_id, "count-employees")
    out = await run(loop, "Count employees.")
    assert out.answer_tables is None
    assert out.extra_evidence[0]["sql"] == sql
    assert out.extra_evidence[0]["blueprint_id"] == "count-employees"
    assert model.calls_made == 2


async def test_three_citations_only_two_explicit_tables_reach_result_and_history():
    from data_agent.runtime.app import _outcome_to_dict
    from data_agent.runtime.session.models import TurnMessage

    queries = [f'SELECT count(Id) AS "Count {n}" FROM hr.employee' for n in range(3)]
    loop, store, _, _, _ = build(
        [
            discovery(),
            *[batch(call("runQuery", f"q{n}", sql=sql)) for n, sql in enumerate(queries)],
            batch(call(
                "finalizeAnswer", "answer", answer="There are 120 employees.",
                tables=[{"result_id": "q0", "caption": "Count"},
                        {"result_id": "q1", "caption": "Check"}],
                evidence=["q0", "q1", "q2"], capability_refs=[],
            )),
        ],
        rows=[{"columns": [f"Count {n}"], "rows": [[120]], "row_count": 1} for n in range(3)],
    )
    out = await run(loop, "Count employees and show the first two results.")
    wire = _outcome_to_dict(out)
    assert [t["caption"] for t in wire["answer_tables"]] == ["Count", "Check"]
    assert [e["result_id"] for e in wire["extra_evidence"]] == ["q2"]
    assert wire["extra_evidence"][0]["sql"] == queries[2]
    doc = await store.get_or_create_session(CREDS.session_id)
    messages = [TurnMessage.from_doc(m.to_doc()) for m in doc.messages]
    history = project_history(messages, doc.tool_trail, CREDS.column_scope, None)["turns"][0]
    assert history["extra_evidence"] == wire["extra_evidence"]
    assert history["answer_tables"] == wire["answer_tables"]
    narrowed = project_history(messages, doc.tool_trail, frozenset({"hr.employee.Salary"}), None)["turns"][0]
    assert narrowed["extra_evidence"] is None


def test_withheld_answer_clears_extra_evidence():
    from data_agent.runtime.loop.turn_accumulators import TurnAccumulators

    accum = TurnAccumulators()
    accum.extra_evidence = [{"result_id": "q"}]
    accum.apply_ship_disposition("decline_only", ())
    assert accum.extra_evidence is None
