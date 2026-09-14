"""Interaction regressions for the agreed harness design; real loop and store."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import UpdateAnalysisStateTool
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.composite.answer_with_text import AnswerWithTextTool
from data_agent.runtime.composite.record_assumptions import RecordAssumptionsTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import (
    ToolDispatcher,
    ToolPause,
    ToolResult,
    _build_preview,
)
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.loop.answer_judge import (
    JUDGE_TOOL_NAME,
    JudgeVerdict,
    parse_verdict,
)
from data_agent.runtime.loop.clarification import normalize_clarification
from data_agent.runtime.loop.measurement import cardinality_probes, validate_join_cardinality
from data_agent.runtime.loop.proposal import ReviewState
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.conversation import restore_response_batches
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

SQL = "SELECT Department, count() AS n FROM hr.employee GROUP BY Department"
CREDS = RuntimeCredentials(session_id="harness-test", jwt="test", column_scope=frozenset())
CATALOG = CatalogHandle(
    {
        "hr.employee": {"Department": "String", "Salary": "Int64", "Id": "Int64"},
        "hr.payments": {"EmployeeId": "Int64"},
    }
)


def call(name, ident, **args):
    return ToolCallRequest(id=ident, name=name, arguments=args)


def batch(*calls):
    return ModelTurnResult(tool_calls=list(calls))


def finish(answer="There are 120 employees.", **kwargs):
    return call(
        "finalizeAnswer",
        "answer",
        answer=answer,
        tables=[],
        capability_refs=[],
        evidence=["q"],
        **kwargs,
    )


class Tool:
    def __init__(self, name, value=None, pause=None):
        self.name, self.value, self.pause = name, value or {}, pause

    async def run(self, *args, **kwargs):
        return ToolResult(
            "ok",
            self.name,
            None,
            None,
            None,
            frozenset(),
            _build_preview(self.value, 20),
            self.value,
            pause=self.pause,
        )


class Judge:
    enabled = True
    timeout_seconds = 2

    def __init__(self, verdicts=()):
        self.verdicts = list(verdicts)
        self.briefs = []

    async def review(self, brief):
        self.briefs.append(brief)
        return self.verdicts.pop(0) if self.verdicts else JudgeVerdict(True, reviewed=True)


def build(steps, judge=None, extra=None, rows=None, store=None):
    store = store or InMemorySessionStore()
    model = ScriptedModelClient(steps)
    mcp = FakeMCPClient(
        scripted={
            "runQuery": rows
            or [
                {
                    "columns": ["Department", "n"],
                    "rows": [["Sales", 120]],
                    "row_count": 1,
                    "truncated": False,
                }
            ]
        }
    )

    async def schemas(_):
        return [
            {"type": "function", "name": "runQuery", "parameters": {}},
            {"type": "function", "name": "finalizeAnswer", "parameters": {}},
        ]

    runtime = {
        "searchBlueprints": Tool("searchBlueprints", {"blueprints": []}),
        "answerWithText": AnswerWithTextTool(),
        "answerWithTable": AnswerWithTableTool(),
        "recordAssumptions": RecordAssumptionsTool(),
        "updateAnalysisState": UpdateAnalysisStateTool(session_store=store),
        **(extra or {}),
    }
    events = []
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(mcp, CATALOG),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=schemas,
        runtime_tools=runtime,
        answer_judge=judge,
        max_loop_iterations=12,
        max_budget_windows=2,
        max_wall_clock_seconds=180,
        observer=lambda e, p: events.append((e, p)),
    )
    return loop, store, model, mcp, events


def discovery():
    return batch(call("searchBlueprints", "s", query="headcount"))


def query():
    return batch(call("runQuery", "q", sql=SQL))


async def run(loop, question="How many employees by department?"):
    return await loop.run(session_id=CREDS.session_id, credentials=CREDS, user_message=question)


async def test_corrected_answer_gets_final_validation():
    judge = Judge(
        [
            JudgeVerdict(False, "contradicts_result", "Use 120.", True, repair_type="prose"),
            JudgeVerdict(True, reviewed=True),
        ]
    )
    loop, store, *_ = build(
        [
            discovery(),
            query(),
            batch(finish("There are 125 employees.")),
            batch(replace(finish(), id="fixed")),
        ],
        judge,
    )
    out = await run(loop)
    assert out.assistant_text == "There are 120 employees."
    assert len(judge.briefs) == 2
    assert (await store.get_or_create_session(CREDS.session_id)).review_states["0"]["calls"] == 2


async def test_final_rejection_does_not_grant_third_review():
    rejected = JudgeVerdict(False, "contradicts_result", "Correct the count.", True)
    judge = Judge([rejected, rejected])
    loop, *_ = build(
        [
            discovery(),
            query(),
            batch(finish("There are 125 employees.")),
            batch(replace(finish(), id="fixed")),
        ],
        judge,
    )
    out = await run(loop)
    assert "125" not in out.assistant_text
    assert len(judge.briefs) == 2


async def test_assumptions_after_finalizer_are_reviewed():
    judge = Judge()
    loop, *_ = build(
        [
            discovery(),
            query(),
            batch(
                finish(),
                call(
                    "recordAssumptions", "a", assumptions=["Only current employees are included."]
                ),
            ),
        ],
        judge,
    )
    out = await run(loop)
    assert tuple(out.assumptions) == judge.briefs[0].assumptions


async def test_discovery_in_same_batch_cannot_authorize_sql():
    loop, store, model, *_ = build(
        [
            batch(
                call("runQuery", "too-early", sql=SQL),
                call("searchBlueprints", "s", query="headcount"),
            ),
            query(),
            batch(finish()),
        ]
    )
    await run(loop)
    entries = (await store.get_or_create_session(CREDS.session_id)).tool_trail
    assert (
        next(e for e in entries if e.tool_call_id == "too-early").error_code
        == "BLUEPRINT_NOT_SEARCHED"
    )
    assert next(e for e in entries if e.tool_call_id == "q").status == "ok"


async def test_two_runs_of_same_blueprint_preserve_both_queries():
    class Blueprint(Tool):
        async def run(self, args, *a, **kw):
            dept = args["slot_bindings"]["department"]
            value = {
                "status": "verified",
                "blueprint_id": "bp",
                "terminal_sql": f"SELECT Department FROM hr.employee WHERE Department='{dept}'",
                "verify": {"grain_ok": True, "signature_ok": True, "grain_checked": True},
                "row_count": 1,
            }
            return replace(await Tool("runBlueprint", value).run(), authoritative=True)

    loop, *_ = build(
        [
            batch(call("getBlueprint", "g", id="bp")),
            batch(
                call("runBlueprint", "sales", id="bp", slot_bindings={"department": "Sales"}),
                call("runBlueprint", "eng", id="bp", slot_bindings={"department": "Engineering"}),
            ),
            batch(
                call(
                    "finalizeAnswer",
                    "a",
                    answer="Both departments are shown.",
                    tables=[
                        {"result_id": "sales", "caption": "Sales"},
                        {"result_id": "eng", "caption": "Engineering"},
                    ],
                    capability_refs=[],
                    evidence=["sales", "eng"],
                )
            ),
        ],
        extra={
            "getBlueprint": Tool("getBlueprint", {"found": True}),
            "runBlueprint": Blueprint("runBlueprint"),
        },
    )
    out = await run(loop)
    assert len(out.answer_tables) == 2
    assert "'Sales'" in out.answer_tables[0]["sql"]
    assert "'Engineering'" in out.answer_tables[1]["sql"]
    assert out.answer_tables[0]["blueprint_use"]["blueprint_id"] == "bp"


async def test_late_intents_bind_one_result_to_two_deliverables():
    loop, store, *_ = build(
        [
            discovery(),
            query(),
            batch(
                call(
                    "updateAnalysisState",
                    "declare",
                    intents=[{"description": "Headcount"}, {"description": "Department breakdown"}],
                )
            ),
            batch(
                call(
                    "updateAnalysisState",
                    "complete",
                    intents=[
                        {"intent_id": "i1", "status": "completed", "result_id": "q"},
                        {"intent_id": "i2", "status": "completed", "result_id": "q"},
                    ],
                ),
                finish(),
            ),
        ]
    )
    await run(loop)
    state = (await store.get_or_create_session(CREDS.session_id)).analysis_state
    assert [i.evidence_tool_call_id for i in state.intents] == ["q", "q"]
    assert all(i.status == "completed" for i in state.intents)


async def test_runtime_pause_uses_shared_question_checks():
    pause = ToolPause(
        "blueprint_slot", {"question": "Which hr.employee.Id?", "options": ["E01", "E02"]}
    )
    judge = Judge()
    loop, *_ = build(
        [batch(call("pauser", "p"))], judge, extra={"pauser": Tool("pauser", pause=pause)}
    )
    out = await run(loop)
    assert out.status == "paused_ask_user"
    assert out.pending_question["options"] is None
    assert "hr.employee" not in out.pending_question["question"]
    assert judge.briefs[0].site == "ask_user"


def test_excess_choices_request_narrowing_instead_of_silent_truncation():
    result = normalize_clarification(
        "Which department?", ["Sales", "Engineering", "Finance", "HR", "Legal", "Support"]
    )
    assert result["options"] is None
    assert "narrow" in result["question"]


def test_review_state_roundtrip_preserves_actual_violation():
    state = ReviewState(
        calls=1,
        repaired=True,
        scope_hash="scope",
        violation="contradicts_result",
        feedback="Use 120",
        answer_version="v1",
    )
    restored = ReviewState.restore(json.loads(json.dumps(state.to_doc())), "scope")
    assert restored == state
    changed = ReviewState.restore(state.to_doc(), "other")
    assert changed.calls == 1 and changed.repaired and changed.feedback == ""


def test_reasoning_replays_only_for_complete_same_scope_batch():
    calls = [
        {"id": v, "type": "function", "function": {"name": "read", "arguments": "{}"}}
        for v in ["a", "b"]
    ]
    envelope = {
        "id": "batch",
        "scope_hash": "same",
        "tool_calls": calls,
        "reasoning_metadata": {"reasoning_content": "internal"},
    }
    messages = [
        {"role": "assistant", "_model_response": envelope},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
        {"role": "assistant", "_model_response": envelope},
        {"role": "tool", "tool_call_id": "b", "content": "ok"},
    ]
    restored = restore_response_batches(messages, scope_hash="same", use_reasoning_metadata=True)
    assert len(restored) == 3 and len(restored[0]["tool_calls"]) == 2
    assert restored[0]["reasoning_content"] == "internal"
    assert (
        "reasoning_content"
        not in restore_response_batches(messages, scope_hash="narrow", use_reasoning_metadata=True)[
            0
        ]
    )
    assert (
        "reasoning_content"
        not in restore_response_batches(messages, scope_hash="same", use_reasoning_metadata=False)[
            0
        ]
    )


def test_structured_judge_feedback_roundtrip():
    verdict = parse_verdict(
        batch(
            call(
                JUDGE_TOOL_NAME,
                "j",
                approved=False,
                violation="unsupported_by_evidence",
                feedback="Remove steps.",
                intent_id="i2",
                result_ids=[],
                repair_type="obtain_evidence_or_disclose_gap",
            )
        ),
        "exit_prose",
    )
    assert verdict.intent_id == "i2" and verdict.repair_type == "obtain_evidence_or_disclose_gap"


async def test_fanout_probe_refuses_inflated_aggregate():
    sql = "SELECT e.Department, sum(e.Salary) FROM hr.employee e JOIN hr.payments p ON e.Id=p.EmployeeId GROUP BY e.Department"
    statements = cardinality_probes(sql)
    assert len(statements) == 1 and "uniqExact" in statements[0]
    dispatcher = ToolDispatcher(
        FakeMCPClient(
            scripted={
                "runQuery": [
                    {"columns": ["row_count", "distinct_count"], "rows": [[2, 1]], "row_count": 1}
                ]
            }
        ),
        CATALOG,
    )
    assert await validate_join_cardinality(sql, dispatcher, CREDS)
    assert cardinality_probes(SQL) == []


async def test_multiple_intent_tags_close_without_auto_binding():
    loop, store, *_ = build(
        [
            batch(
                call(
                    "updateAnalysisState",
                    "declare",
                    intents=[{"description": "Count"}, {"description": "Breakdown"}],
                )
            ),
            discovery(),
            batch(call("runQuery", "q", sql=SQL, serves_intents=["i1", "i2"])),
            batch(
                call(
                    "updateAnalysisState",
                    "close",
                    intents=[
                        {"intent_id": "i1", "status": "completed"},
                        {"intent_id": "i2", "status": "completed"},
                    ],
                ),
                finish(),
            ),
        ]
    )
    await run(loop)
    doc = await store.get_or_create_session(CREDS.session_id)
    assert {i.evidence_tool_call_id for i in doc.analysis_state.intents} == {"q"}
    assert next(e for e in doc.tool_trail if e.tool_call_id == "q").serves_intents == ("i1", "i2")


async def test_malformed_arguments_are_not_dispatched():
    invalid = replace(
        call("runQuery", "bad"), argument_error="Expected a JSON object.", raw_arguments="[1]"
    )
    loop, store, _, mcp, _ = build([discovery(), batch(invalid), query(), batch(finish())])
    await run(loop)
    doc = await store.get_or_create_session(CREDS.session_id)
    assert (
        next(e for e in doc.tool_trail if e.tool_call_id == "bad").error_code
        == "INVALID_TOOL_ARGUMENTS"
    )
    assert len(mcp.calls) == 1


async def test_runtime_pause_records_unexecuted_calls_and_multi_tags():
    pause = ToolPause(
        reason="blueprint_slot",
        pending_question={"question": "Which group?", "options": ["Sales", "Engineering"]},
        blueprint_id="bp",
    )
    loop, store, *_ = build(
        [
            batch(
                call(
                    "updateAnalysisState",
                    "declare",
                    intents=[{"description": "Count"}, {"description": "Breakdown"}],
                )
            ),
            batch(call("getBlueprint", "g", id="bp")),
            batch(
                call("runBlueprint", "paused", id="bp", serves_intents=["i1", "i2"]),
                call("recordAssumptions", "skipped", assumptions=["Never executed."]),
            ),
        ],
        extra={
            "getBlueprint": Tool("getBlueprint", {"found": True}),
            "runBlueprint": Tool("runBlueprint", pause=pause),
        },
    )
    out = await run(loop)
    doc = await store.get_or_create_session(CREDS.session_id)
    assert out.status == "paused_ask_user"
    assert doc.pause_checkpoint.serves_intents == ("i1", "i2")
    assert (
        next(e for e in doc.tool_trail if e.tool_call_id == "skipped").error_code
        == "TOOL_NOT_EXECUTED"
    )
    assert not out.assumptions


async def test_review_budget_and_feedback_survive_actual_resume():
    judge = Judge(
        [
            JudgeVerdict(False, "contradicts_result", "Use 120.", True),
            JudgeVerdict(True, reviewed=True),
            JudgeVerdict(True, reviewed=True),
        ]
    )
    loop, store, model, *_ = build(
        [
            discovery(),
            query(),
            batch(finish("There are 125 employees.")),
            batch(
                call(
                    "askUser",
                    "clarify",
                    question="Which period should the explanation describe?",
                    options=["Current", "Previous"],
                )
            ),
            batch(replace(finish(), id="fixed")),
        ],
        judge,
    )
    out = await run(loop)
    assert out.status == "paused_ask_user"
    out = await loop.resume(session_id=CREDS.session_id, credentials=CREDS, answer="Current")
    assert out.assistant_text == "There are 120 employees."
    assert (await store.get_or_create_session(CREDS.session_id)).review_states["0"]["calls"] == 2
    assert len([b for b in judge.briefs if b.site != "ask_user"]) == 2


async def test_prepared_capability_is_not_visible_on_pause():
    prepared = {
        "name": "open_page",
        "kind": "navigation",
        "prepared": True,
        "capability_ref": "open_page",
        "metadata": {},
    }
    loop, *_ = build(
        [
            batch(call("open_page", "p")),
            batch(call("askUser", "a", question="Which group?", options=["Sales", "Engineering"])),
        ],
        extra={"open_page": Tool("open_page", prepared)},
    )
    out = await run(loop)
    assert out.status == "paused_ask_user"
    assert not out.capability_cards


async def test_prepared_capability_requires_explicit_selection():
    prepared = {
        "name": "open_page",
        "kind": "navigation",
        "prepared": True,
        "capability_ref": "open_page",
        "metadata": {},
    }
    loop, *_ = build(
        [
            batch(call("open_page", "p")),
            batch(
                call(
                    "finalizeAnswer",
                    "f",
                    answer="Use this option to view the page.",
                    tables=[],
                    capability_refs=["open_page"],
                    evidence=["p"],
                )
            ),
        ],
        extra={"open_page": Tool("open_page", prepared)},
    )
    out = await run(loop, "Take me to the page.")
    assert len(out.capability_cards) == 1
    assert out.capability_cards[0]["name"] == "open_page"
    assert "prepared" not in out.capability_cards[0]


async def test_missing_capability_ref_is_repairable_not_an_exception():
    loop, store, *_ = build(
        [
            batch(
                call(
                    "finalizeAnswer",
                    "bad",
                    answer="Use the option.",
                    tables=[],
                    capability_refs=["absent"],
                    evidence=[],
                )
            ),
            batch(
                call(
                    "finalizeAnswer",
                    "fixed",
                    answer="I could not find an available option.",
                    tables=[],
                    capability_refs=[],
                    evidence=[],
                )
            ),
        ]
    )
    out = await run(loop, "Take me to the page.")
    assert not out.capability_cards


async def test_top_n_result_reference_preserves_query_meaning():
    sql = SQL + " ORDER BY n DESC LIMIT 2"
    loop, *_ = build(
        [
            discovery(),
            batch(call("runQuery", "q", sql=sql)),
            batch(
                call(
                    "finalizeAnswer",
                    "f",
                    answer="The top two departments are shown.",
                    tables=[{"result_id": "q", "caption": "Top two"}],
                    capability_refs=[],
                    evidence=["q"],
                )
            ),
        ]
    )
    out = await run(loop)
    assert "LIMIT 2" in out.answer_tables[0]["sql"]


@pytest.mark.parametrize("available", [True, False])
async def test_capability_registry_is_reloaded_on_http_resume(monkeypatch, available):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import httpx

    import data_agent.runtime.app as app_module
    from data_agent.runtime.capabilities.client import CapabilityDefinition
    from data_agent.runtime.config import RuntimeSettings

    monkeypatch.setattr(app_module, "_extract_credentials", lambda **kw: CREDS)
    definition = CapabilityDefinition(
        name="open_page",
        version="1",
        kind="navigation",
        description="Open the page.",
        parameters=(),
        metadata={},
    )
    provider = SimpleNamespace(
        get_definition=AsyncMock(side_effect=[definition, definition if available else None]),
        hydrate=AsyncMock(return_value={"name": "open_page", "kind": "navigation", "metadata": {}}),
    )
    model = ScriptedModelClient(
        [
            batch(call("getCapabilityTool", "g", tool_name="open_page")),
            batch(
                call("askUser", "ask", question="Which group?", options=["Sales", "Engineering"])
            ),
            batch(call("open_page", "p")),
            batch(
                call(
                    "finalizeAnswer",
                    "f",
                    answer="Use the option to view the page."
                    if available
                    else "I cannot find an available option.",
                    tables=[],
                    capability_refs=["open_page"] if available else [],
                    evidence=["p"] if available else [],
                )
            ),
        ]
    )
    store = InMemorySessionStore()
    app = app_module.create_app(
        settings=RuntimeSettings(
            _env_file=None,
            openai_api_key="",
            capability_tools_enabled=True,
            capability_prefetch_enabled=False,
            measurement_review_enabled=False,
            discovery_emulation_enabled=False,
            otlp_endpoint="",
        ),
        session_store=store,
        mcp_client=FakeMCPClient(),
        model_client=model,
        catalog=CATALOG,
        capability_client=provider,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Session-Id": CREDS.session_id, "Authorization": "Bearer test"},
    ) as client:
        first = await client.post("/turn", json={"message": "Take me to the page."})
        assert "paused_ask_user" in first.text
        assert provider.hydrate.await_count == 0
        second = await client.post("/turn/resume", json={"answer": "Sales"})
        assert '"status": "done"' in second.text
        assert ("open_page" in second.text) is available
        assert provider.get_definition.await_count == 2
        assert provider.hydrate.await_count == int(available)
    assert any(
        e.tool_call_id.startswith("restore-")
        for e in (await store.get_or_create_session(CREDS.session_id)).tool_trail
    )


async def test_sql_evidence_cannot_be_bound_as_product_guidance():
    from data_agent.runtime.loop.proposal import deliverable_evidence

    loop, store, *_ = build(
        [
            discovery(),
            query(),
            batch(
                call(
                    "updateAnalysisState",
                    "declare",
                    intents=[{"description": "Count"}, {"description": "Product instructions"}],
                )
            ),
            batch(
                call(
                    "askUser", "pause", question="Which process?", options=["Time off", "Expenses"]
                )
            ),
        ]
    )
    await run(loop)
    doc = await store.get_or_create_session(CREDS.session_id)
    _, error = deliverable_evidence(
        doc.analysis_state,
        {
            "evidence": ["q"],
            "deliverables": [
                {
                    "intent_id": "i2",
                    "answer": "Unsupported steps",
                    "result_ids": ["q"],
                    "evidence_type": "product",
                }
            ],
        },
        doc.tool_trail,
    )
    assert "wrong type" in error


def test_join_check_covers_both_directions_and_ignores_min_max():
    sql = "SELECT sum(p.EmployeeId) FROM hr.employee e JOIN hr.payments p ON e.Id=p.EmployeeId"
    probes = cardinality_probes(sql)
    assert len(probes) == 1 and "hr.employee" in probes[0]
    assert cardinality_probes(sql.replace("sum(p.EmployeeId)", "max(p.EmployeeId)")) == []


async def test_invalid_finalizer_shape_is_repairable():
    loop, store, *_ = build(
        [
            batch(
                call(
                    "finalizeAnswer",
                    "bad",
                    answer="Use this option.",
                    tables=[],
                    capability_refs=[{}],
                    evidence=[],
                )
            ),
            batch(
                call(
                    "finalizeAnswer",
                    "fixed",
                    answer="I cannot find an available option.",
                    tables=[],
                    capability_refs=[],
                    evidence=[],
                )
            ),
        ]
    )
    await run(loop, "Take me to the page.")
    assert (
        next(
            e
            for e in (await store.get_or_create_session(CREDS.session_id)).tool_trail
            if e.tool_call_id == "bad"
        ).error_code
        == "INVALID_TOOL_ARGUMENTS"
    )


async def test_discovery_unavailable_allows_honest_subsequent_sql_fallback():
    class Unavailable(Tool):
        async def run(self, *args, **kwargs):
            return ToolResult(
                "error",
                "searchBlueprints",
                "RETRIEVAL_TOOL_UNAVAILABLE",
                True,
                "Search unavailable.",
                frozenset(),
                None,
                None,
            )

    loop, store, *_ = build(
        [discovery(), query(), batch(finish())],
        extra={"searchBlueprints": Unavailable("searchBlueprints")},
    )
    await run(loop)
    assert (
        next(
            e
            for e in (await store.get_or_create_session(CREDS.session_id)).tool_trail
            if e.tool_call_id == "q"
        ).status
        == "ok"
    )


async def test_measurement_review_is_about_one_execution_not_whole_answer():
    from data_agent.runtime.loop.measurement import MeasurementReviewer

    model = ScriptedModelClient(
        [
            batch(
                call(
                    "record_measurement_review",
                    "r",
                    request_alignment="matches_requested_part",
                    findings=[],
                    contract={
                        "metric": "headcount",
                        "population": "Sales",
                        "period": "current",
                        "units": "people",
                        "grain": "department",
                        "join_cardinality": "none",
                    },
                )
            )
        ]
    )
    result = await MeasurementReviewer(model).review(
        "Sales and Engineering counts", {"department": "Sales"}, []
    )
    assert result["approved"] and result["reviewed"]
    assert "Whole-answer coverage" in model.calls[0].messages[0]["content"]


async def test_raw_result_reference_reconstructs_same_query_in_history():
    from data_agent.runtime.session_history import project_history

    loop, store, *_ = build(
        [
            discovery(),
            query(),
            batch(
                call(
                    "finalizeAnswer",
                    "a",
                    answer="The counts are shown.",
                    tables=[{"result_id": "q", "caption": "Count"}],
                    capability_refs=[],
                    evidence=["q"],
                )
            ),
        ]
    )
    out = await run(loop)
    doc = await store.get_or_create_session(CREDS.session_id)
    history = project_history(doc.messages, doc.tool_trail, CREDS.column_scope, None)
    assert history["turns"][0]["answer_tables"][0]["sql"] == out.answer_tables[0]["sql"]


async def test_unwired_discovery_also_allows_subsequent_fallback():
    loop, store, *_ = build([discovery(), query(), batch(finish())])
    del loop._runtime_tools["searchBlueprints"]
    await run(loop)
    assert (
        next(
            e
            for e in (await store.get_or_create_session(CREDS.session_id)).tool_trail
            if e.tool_call_id == "q"
        ).status
        == "ok"
    )


def test_withheld_first_result_does_not_duplicate_restored_batch():
    calls = [
        {"id": v, "type": "function", "function": {"name": "read", "arguments": "{}"}}
        for v in ["a", "b"]
    ]
    envelope = {
        "id": "batch",
        "scope_hash": "same",
        "tool_calls": calls,
        "content": "Private text",
        "reasoning_metadata": {"reasoning_content": "Private reasoning"},
    }
    messages = [
        {"role": "assistant", "tool_calls": [calls[0]]},
        {"role": "tool", "tool_call_id": "a", "content": "result withheld: provenance unavailable"},
        {"role": "assistant", "tool_calls": [calls[1]], "_model_response": envelope},
        {"role": "tool", "tool_call_id": "b", "content": "ok"},
    ]
    replay = restore_response_batches(messages, scope_hash="same", use_reasoning_metadata=True)
    assert len(replay) == 3
    assert [m["tool_call_id"] for m in replay if m["role"] == "tool"] == ["a", "b"]
    assert replay[0]["content"] is None and "reasoning_content" not in replay[0]


async def test_blueprint_executor_checks_join_keys_before_consumer_execution():
    from data_agent.runtime.blueprint.executor import ExecFailed
    from data_agent.runtime.mcp.scratch_client import FakeScratchClient
    from tests.runtime.blueprint.test_executor_table_intermediate import _creds, _executor, _rq

    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Sales"]]),
                _rq(["EmployeeCode", "earnings"], [["1001", 100.0]]),
                _rq(["row_count", "distinct_count"], [[2, 1]]),
            ]
        }
    )
    outcome = await _executor(mcp, scratch_client=FakeScratchClient()).execute(
        blueprint_id="bp-table", slot_bindings={"department": "Sales"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed) and outcome.error_code == "AGGREGATION_RISK"
    assert len(mcp.calls) == 3 and "uniqExact" in mcp.calls[-1].args["sql"]


def test_each_join_key_set_is_probed_separately():
    sql = "SELECT sum(a.Salary) FROM hr.employee a JOIN hr.payments b ON a.Id=b.EmployeeId JOIN hr.employee c ON b.OtherId=c.Id"
    probes = cardinality_probes(sql)
    assert any("uniqExact((b.EmployeeId))" in p for p in probes)
    assert any("uniqExact((b.OtherId))" in p for p in probes)
    assert all("uniqExact((b.EmployeeId, b.OtherId))" not in p for p in probes)


def test_semi_join_is_a_valid_cardinality_preserving_repair():
    assert (
        cardinality_probes(
            "SELECT count(*) FROM hr.employee e LEFT SEMI JOIN hr.payments p ON e.Id=p.EmployeeId"
        )
        == []
    )


def test_measurement_input_does_not_reuse_previous_refusals_as_catalog():
    from data_agent.runtime.loop.measurement import catalog_evidence

    messages = [
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "tool_name": "getBlueprint",
                    "status": "ok",
                    "result_preview": {"definition": "valid"},
                }
            ),
        },
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "tool_name": "runBlueprint",
                    "status": "error",
                    "user_message": "Wrong previous feedback",
                }
            ),
        },
    ]
    evidence = catalog_evidence(messages)
    assert len(evidence) == 1 and evidence[0]["tool_name"] == "getBlueprint"


def test_emulated_catalogue_can_ground_metadata_answer():
    from data_agent.runtime.loop.proposal import context_catalog_entries, deliverable_evidence

    message = {
        "role": "tool",
        "tool_call_id": "emulated-tables",
        "content": json.dumps(
            {
                "tool_name": "listTables",
                "status": "ok",
                "result_preview": {
                    "columns": ["table"],
                    "row_count": 1,
                    "truncated": False,
                    "preview_rows": [["employee"]],
                },
            }
        ),
    }
    entries = context_catalog_entries([message], {"emulated-tables"}, 0)
    parts, error = deliverable_evidence(
        None,
        {"answer": "Employee information is available.", "evidence": ["emulated-tables"]},
        entries,
    )
    assert error is None and parts[0]["evidence"][0]["kind"] == "catalog"
    assert context_catalog_entries([message], set(), 0) == []


async def test_discovery_streak_prompts_delivery_without_discarding_evidence():
    steps = [
        batch(call("getTableSchema", f"schema{i}", database="hr", table=f"table{i}"))
        for i in range(6)
    ]
    steps.append(
        batch(
            call(
                "finalizeAnswer",
                "done",
                answer="Employee information is available.",
                tables=[],
                capability_refs=[],
                evidence=["schema0"],
            )
        )
    )
    loop, _, model, _, _ = build(
        steps,
        extra={"getTableSchema": Tool("getTableSchema", {"description": "Employee information"})},
    )
    outcome = await run(loop, "What employee information is available?")
    assert outcome.status == "done"
    assert any("six consecutive rounds" in str(m.get("content")) for m in model.calls[-1].messages)
    assert any(m.get("tool_call_id") == "schema0" for m in model.calls[-1].messages)


async def test_judge_brief_failure_does_not_abort_an_initial_answer():
    judge = Judge()
    loop, store, _, _, _ = build([discovery(), query(), batch(finish())], judge)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("temporary store read failure")

    loop._judge_results = unavailable
    outcome = await run(loop)
    assert outcome.status == "done"
    assert judge.briefs == []
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.review_states["0"]["calls"] == 1


async def test_final_judge_receives_the_users_clarification_answer():
    judge = Judge()
    loop, _, _, _, _ = build(
        [
            batch(
                call(
                    "askUser", "ask", question="Which department?", options=["Sales", "Engineering"]
                )
            ),
            discovery(),
            query(),
            batch(finish()),
        ],
        judge,
    )
    paused = await run(loop, "How many employees in my chosen department?")
    assert paused.status == "paused_ask_user"
    done = await loop.resume(session_id=CREDS.session_id, credentials=CREDS, answer="Sales")
    assert done.status == "done"
    assert judge.briefs[-1].clarification_answers == ("Sales",)


async def test_repair_can_remove_a_table_from_live_answer_and_history():
    from data_agent.runtime.session_history import project_history

    judge = Judge(
        [
            JudgeVerdict(
                False,
                "unexplained_gap",
                "Present the scalar in prose.",
                True,
                repair_type="presentation",
            )
        ]
    )
    initial = call(
        "finalizeAnswer",
        "first",
        answer="There are 120 employees.",
        tables=[{"result_id": "q"}],
        capability_refs=[],
        evidence=["q"],
    )
    loop, store, _, _, _ = build([discovery(), query(), batch(initial), batch(finish())], judge)
    outcome = await run(loop, "How many employees?")
    assert outcome.status == "done" and outcome.answer_tables is None
    assert len(judge.briefs) == 2 and judge.briefs[-1].designated_tables == ()
    doc = await store.get_or_create_session(CREDS.session_id)
    history = project_history(doc.messages, doc.tool_trail, frozenset(), None)
    assert history["turns"][0]["answer_tables"] is None
    tables, _ = await loop._compute_turn_answer_tables(CREDS.session_id, 0)
    assert tables == []


@pytest.mark.parametrize("reviewed", [False, True])
async def test_data_display_requires_actual_approval_when_judge_is_enabled(reviewed):
    payload = {
        "prepared": True,
        "capability_ref": "show_profile",
        "name": "show_profile",
        "_agent_evidence": {"kind": "data_widget"},
    }
    judge = Judge([JudgeVerdict(True, reviewed=reviewed)])
    loop, _, _, _, _ = build(
        [
            batch(call("show_profile", "prepare")),
            batch(
                call(
                    "finalizeAnswer",
                    "done",
                    answer="Use this option to view the profile.",
                    tables=[],
                    capability_refs=["show_profile"],
                    evidence=[],
                )
            ),
        ],
        judge,
        extra={"show_profile": Tool("show_profile", payload)},
    )
    outcome = await run(loop, "Show the employee profile.")
    assert outcome.status == "done"
    assert bool(outcome.capability_cards) is reviewed
