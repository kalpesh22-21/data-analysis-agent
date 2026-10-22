from dataclasses import replace
from types import SimpleNamespace

import pytest

from data_agent.runtime.answer_scrub import scrub_answer_prose
from data_agent.runtime.dispatch.tool_dispatcher import _build_preview
from data_agent.runtime.loop.answer_judge import JudgeBrief
from data_agent.runtime.loop.answer_rules import first_match
from data_agent.runtime.loop.column_context import business_values, column_context_evidence
from data_agent.runtime.loop.judge_evidence import enrich_brief
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.provenance.capture import capture_provenance
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import TrailEntry

PAYLOAD = {
    "columns": [
        {
            "name": "earn_code",
            "description": "Current active assignments across the whole client; not historical usage.",
            "context": {"active_earnings": [{"earn_code": "VAC_CTX", "description": "Vacation"}]},
        }
    ],
    "context_provenance": {"version": 1, "columns": [["hr.accruals", "earn_code"]]},
}


async def fixture():
    store = InMemorySessionStore()
    ref = await store.write_full_result("s", "schema", PAYLOAD)
    entry = TrailEntry(
        1,
        "schema",
        "getTableSchema",
        {},
        "ok",
        None,
        capture_provenance("getTableSchema", PAYLOAD),
        _build_preview(PAYLOAD, 20),
        ref,
        "2026-10-21T00:00:00Z",
    )
    return store, entry


async def test_context_is_reviewable_lookup_evidence_without_query():
    store, entry = await fixture()
    brief = await enrich_brief(
        JudgeBrief(
            "exit_prose",
            "Available earning codes?",
            results=({"tool_call_id": "schema"},),
            referenced_result_ids=("schema",),
        ),
        trail=[entry],
        turn_index=1,
        session_id="s",
        store=store,
        catalog_provider=None,
        credentials=SimpleNamespace(column_scope=frozenset({"hr.accruals.earn_code"})),
        analysis_state=None,
    )
    receipt = brief.results[0]["result_preview"]["preview_rows"][0][0]
    assert receipt["rows"][0]["earn_code"] == "VAC_CTX"
    assert receipt["result_id"] == "schema"
    assert receipt["retrieved_at"] == entry.ts
    assert "not historical" in receipt["description"]
    assert "leave taken" in receipt["limitations"]


@pytest.mark.parametrize(
    "change,scope",
    [
        ({"status": "error"}, frozenset()),
        ({"turn_index": 0}, frozenset()),
        ({"provenance": None}, frozenset()),
        ({}, frozenset({"hr.accruals.balance"})),
    ],
)
async def test_unavailable_or_out_of_scope_context_is_not_evidence(change, scope):
    store, entry = await fixture()
    receipts = await column_context_evidence(
        [replace(entry, **change)], turn_index=1, session_id="s", store=store, column_scope=scope
    )
    assert receipts == []


async def test_business_codes_survive_but_metadata_does_not():
    store, entry = await fixture()
    receipts = await column_context_evidence(
        [entry], turn_index=1, session_id="s", store=store, column_scope=frozenset()
    )
    text, count = scrub_answer_prose(
        "VAC_CTX uses earn_code and active_earnings.", business_values=business_values(receipts)
    )
    assert text == "VAC_CTX uses [schema detail withheld] and [schema detail withheld]."
    assert count == 2
    assert scrub_answer_prose("OTHER_CTX", business_values=business_values(receipts))[1] == 1


def test_context_without_api_receipt_fails_closed():
    with pytest.raises(MCPToolError):
        capture_provenance("getTableSchema", {"columns": PAYLOAD["columns"]})


def test_concision_guard_preserves_tables_and_respects_requested_detail():
    prose = "Important findings are shown here. " * 30
    rule = first_match(prose, ["SELECT 1"], "Show payroll", has_tables=True)
    assert rule.name == "verbose_answer"
    assert "do not rerun queries" in rule.nudge(prose)
    assert first_match(prose, ["SELECT 1"], "Explain in detail", has_tables=True) is None
    assert first_match("Sales has the highest total.", ["SELECT 1"], has_tables=True) is None


async def test_real_loop_finalizes_context_without_resolving_or_querying():
    from data_agent.runtime.dispatch.tool_dispatcher import ToolResult
    from tests.runtime.test_harness_improvements import Judge, batch, build, call, run

    class Schema:
        async def run(self, *args, **kwargs):
            return ToolResult(
                status="ok",
                tool_name="getTableSchema",
                error_code=None,
                retryable=None,
                user_message=None,
                provenance=capture_provenance("getTableSchema", PAYLOAD),
                result_preview=_build_preview(PAYLOAD, 20),
                result_full=PAYLOAD,
            )

    judge = Judge()
    loop, store, events, *rest = build(
        [
            batch(call("getTableSchema", "schema", database="hr", table="accruals")),
            batch(
                call(
                    "finalizeAnswer",
                    "final",
                    answer="VAC_CTX means Vacation. This is current client configuration.",
                    tables=[],
                    capability_refs=[],
                    evidence=["schema"],
                )
            ),
        ],
        judge=judge,
        extra={"getTableSchema": Schema()},
    )
    result = await run(loop, "Which earning codes are configured?")
    assert result.status == "done"
    assert "VAC_CTX" in result.assistant_text
    assert "[schema detail withheld]" not in result.assistant_text
    receipts = [r for b in judge.briefs for r in b.results if r.get("tool_call_id") == "schema"]
    assert receipts
    assert receipts[0]["result_preview"]["preview_rows"][0][0]["rows"][0]["earn_code"] == "VAC_CTX"


def test_quoted_business_values_are_not_schema_disclosure():
    assert (
        first_match(
            "`VAC_CTX` means Vacation.",
            [],
            has_alternative_evidence=True,
            business_values=frozenset({"VAC_CTX"}),
        )
        is None
    )
    assert (
        first_match(
            "`earn_code` means Vacation.",
            [],
            has_alternative_evidence=True,
            business_values=frozenset({"VAC_CTX"}),
        ).name
        == "schema_in_answer"
    )


def test_behavior_instructions_and_guard_agree():
    from data_agent.runtime.dispatch.denial_mapping import BLUEPRINT_NOT_SEARCHED_MESSAGE
    from data_agent.runtime.loop.answer_judge import _ANSWER_JUDGE_PROMPT
    from data_agent.runtime.prompts import AGENT_SYSTEM_PROMPT

    assert (
        "explicit user scope overrides defaults"
        in AGENT_SYSTEM_PROMPT
    )
    assert (
        "applicable catalog defaults"
        in _ANSWER_JUDGE_PROMPT
    )
    assert "useful part of the request" in AGENT_SYSTEM_PROMPT
    assert "use it for that part" in BLUEPRINT_NOT_SEARCHED_MESSAGE
    assert "do not repeat their rows/totals" in AGENT_SYSTEM_PROMPT


async def test_empty_lookup_is_evidence_of_no_returned_values():
    import copy

    store, entry = await fixture()
    payload = copy.deepcopy(PAYLOAD)
    payload["columns"][0]["context"] = {}
    ref = await store.write_full_result("s", "empty", payload)
    receipts = await column_context_evidence(
        [replace(entry, result_full_ref=ref)],
        turn_index=1,
        session_id="s",
        store=store,
        column_scope=frozenset(),
    )
    assert len(receipts) == 1
    assert receipts[0]["rows"] == []
    assert receipts[0]["context_key"] is None
