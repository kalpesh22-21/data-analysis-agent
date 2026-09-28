"""Scalar warehouse answers include the exact executed query, without another turn."""

from dataclasses import replace

import pytest

from data_agent.runtime.loop.proposal import include_scalar_result_tables
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
    assert out.answer_tables[0]["sql"] == sql
    assert len(mcp.calls) == 1 and model.calls_made == 3
    doc = await store.get_or_create_session(CREDS.session_id)
    assert (
        project_history(doc.messages, doc.tool_trail, CREDS.column_scope, None)["turns"][0][
            "answer_tables"
        ]
        == out.answer_tables
    )


def test_only_cited_valid_scalar_query_or_verified_blueprint_is_attached():
    preview = ResultPreview(columns=["Count"], preview_rows=[[120]], row_count=1, truncated=False)
    query = replace(entry("q", "runQuery"), result_preview=preview)
    blueprint = replace(entry("bp", "runBlueprint"), authoritative=True, result_preview=preview)
    unverified = replace(blueprint, tool_call_id="bad", authoritative=False)
    sample = replace(query, tool_call_id="sample", tool_name="sampleRows")
    args = {"answer": "120 employees.", "tables": [], "evidence": ["bp", "bad", "sample"]}
    result = include_scalar_result_tables(args, [query, blueprint, unverified, sample])
    assert result["tables"] == [{"result_id": "bp", "caption": "Summary"}]
    assert (
        include_scalar_result_tables(
            args, [query, blueprint, unverified, sample], excluded=[{"result_id": "bp"}]
        )["tables"]
        == []
    )
    assert include_scalar_result_tables({**args, "evidence": ["missing"]}, [query]) == {
        **args,
        "evidence": ["missing"],
    }


def test_existing_table_caption_is_preserved_without_duplicates():
    preview = ResultPreview(columns=["Count"], preview_rows=[[120]], row_count=1, truncated=False)
    query = replace(entry("q", "runQuery"), result_preview=preview)
    args = {
        "answer": "120 employees.",
        "tables": [{"result_id": "q", "caption": "Employee count"}],
        "evidence": ["q"],
    }
    assert include_scalar_result_tables(args, [query]) == args


async def test_verified_count_blueprint_is_returned_as_a_query_backed_table():
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
    assert out.answer_tables[0]["sql"] == sql
    assert out.answer_tables[0]["blueprint_use"] is not None
    assert model.calls_made == 2
