"""Runtime integration contracts from remote-developer.md, using explicit answer tools."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.capabilities.client import (
    CapabilityError,
    CapabilityServiceError,
    HttpCapabilityClient,
    _decode_presentation,
)
from data_agent.runtime.capabilities.digest import (
    card_display_fields,
    metadata_data_digest,
    presentation_label,
)
from data_agent.runtime.capabilities.hydrate_trace import record_hydrate_event
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.composite.answer_with_text import AnswerWithTextTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.loop.answer_judge import (
    APPROVED,
    JUDGE_TOOL_NAME,
    AnswerJudge,
    JudgeBrief,
    JudgeVerdict,
    _fit_payload,
)
from data_agent.runtime.loop.answer_rules import SCOPE_REFUSAL_TEXT, first_match
from data_agent.runtime.loop.dispatch_gates import BlueprintSearchGate
from data_agent.runtime.loop.finalization import EMPTY_ANSWER_FALLBACK_TEXT
from data_agent.runtime.loop.judge_ship_guard import CARD_FATAL_VIOLATIONS, JudgeShipGuard
from data_agent.runtime.loop.turn_accumulators import TurnAccumulators
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.observability.tracing import shutdown_tracing
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import TurnMessage
from data_agent.runtime.session_history import project_history

CREDS = RuntimeCredentials(
    session_id="remote-contract", jwt="secret-token", column_scope=frozenset()
)
SQL = "SELECT employee_code FROM dbpcm_warehouse.employee"
CATALOG = CatalogHandle({"dbpcm_warehouse.employee": {"employee_code": "String"}})


def call(name, ident="call-1", **arguments):
    return ToolCallRequest(id=ident, name=name, arguments=arguments)


def turn(*calls, text=None):
    return ModelTurnResult(tool_calls=list(calls), assistant_text=text)


def ok(name, payload=None, terminal=False):
    return ToolResult(
        status="ok",
        tool_name=name,
        error_code=None,
        retryable=None,
        user_message=None,
        provenance=frozenset(),
        result_preview=None,
        result_full=payload,
        terminal=terminal,
    )


class CardTool:
    async def run(self, arguments, credentials, turn=None, tool_call_id=None):
        return ok(
            "show_profile",
            {
                "name": "show_profile",
                "metadata": {"preamble_url": "ember:EmployeeCard"},
                "_agent_evidence": {"kind": "data_widget", "parameters": []},
                "answer": arguments.get("answer", "This option may help."),
            },
            terminal=True,
        )


class Judge:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.briefs = []

    async def review(self, brief):
        self.briefs.append(brief)
        return self.verdicts.pop(0) if self.verdicts else APPROVED


def build(turns, *, judge=None, tools=None, summarizer=None, iterations=8):
    store = InMemorySessionStore()
    events = []

    def observer(name, payload):
        events.append((name, payload))

    runtime = {
        "answerWithText": AnswerWithTextTool(observer=observer),
        "answerWithTable": AnswerWithTableTool(observer=observer),
        **(tools or {}),
    }
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                {"columns": ["employee_code"], "rows": [["E1"]], "row_count": 1, "truncated": False}
            ]
            * 8,
            "searchBlueprints": [{"blueprints": []}] * 8,
        }
    )

    async def schemas(credentials):
        return [
            {"type": "function", "name": name, "description": "", "parameters": {}}
            for name in ["runQuery", "searchBlueprints", *runtime]
        ]

    model = ScriptedModelClient(turns)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(mcp, CATALOG, observer=observer),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=schemas,
        max_loop_iterations=iterations,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        runtime_tools=runtime,
        observer=observer,
        answer_judge=judge,
        progress_summarizer=summarizer,
    )
    return loop, store, events, model, mcp


async def run(loop, question="go"):
    return await loop.run(session_id=CREDS.session_id, credentials=CREDS, user_message=question)


async def test_double_silence_is_persisted_and_visible_in_history():
    loop, store, _, _, _ = build([turn(), turn(text="  ")])
    outcome = await run(loop)
    assert outcome.assistant_text == EMPTY_ANSWER_FALLBACK_TEXT
    doc = await store.get_or_create_session(CREDS.session_id)
    assert (
        project_history(doc.messages, doc.tool_trail, frozenset(), None)["turns"][0]["answer"]
        == outcome.assistant_text
    )


async def test_bare_prose_is_not_a_model_answer_or_history_message():
    loop, store, _, _, _ = build([turn(text="unverified prose"), turn(), turn()])
    await run(loop)
    doc = await store.get_or_create_session(CREDS.session_id)
    assert "unverified prose" not in [m.content for m in doc.messages]


async def test_unknown_tool_is_local_and_has_no_progress_start():
    loop, store, events, _, mcp = build([turn(call("typoTool")), turn(), turn()])
    await run(loop)
    assert not mcp.calls
    trail = await store.load_trail(CREDS.session_id)
    assert trail[0].error_code == "UNKNOWN_TOOL"
    assert not any(name.startswith("tool_dispatch_") for name, _ in events)


async def test_ordered_summary_precedes_one_matching_dispatch_pair():
    class Summarizer:
        async def summarize(self, name, args):
            await asyncio.sleep(0)
            return "Preparing the option."

    loop, _, events, _, _ = build(
        [turn(call("show_profile", "card-7"))],
        tools={"show_profile": CardTool()},
        summarizer=Summarizer(),
    )
    assert (await run(loop)).status == "done"
    progress = [
        (name, p["tool_call_id"])
        for name, p in events
        if name.startswith("tool_dispatch_") or name == "tool_progress_summary"
    ]
    assert progress == [
        ("tool_progress_summary", "card-7"),
        ("tool_dispatch_start", "card-7"),
        ("tool_dispatch_ok", "card-7"),
    ]


async def test_unavailable_tool_gets_matching_start_and_error():
    loop, _, events, _, _ = build([turn(call("getBlueprint")), turn(), turn()])
    await run(loop)
    assert [(n, p["tool_call_id"]) for n, p in events if n.startswith("tool_dispatch_")] == [
        ("tool_dispatch_start", "call-1"),
        ("tool_dispatch_error", "call-1"),
    ]


@pytest.mark.parametrize("violation", sorted(CARD_FATAL_VIOLATIONS))
async def test_card_fatal_refusal_cannot_escape_through_fail_open(violation):
    judge = Judge(
        [JudgeVerdict(False, violation, "Choose an option covering the request.", reviewed=True)]
    )
    loop, store, events, _, _ = build(
        [
            turn(call("show_profile", "card-1")),
            turn(call("show_profile", "card-2")),
        ],
        judge=judge,
        tools={"show_profile": CardTool()},
    )
    result = await run(loop)
    assert result.capability_cards is None
    assert result.answer_tables is None
    assert "verified information" in result.assistant_text
    assert any(n == "loop_answer_judge_ship_guarded" for n, _ in events)
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.messages[-1].ship_disposition == "decline_only"


async def test_actual_approval_allows_replacement_card():
    judge = Judge(
        [
            JudgeVerdict(
                False, "unsupported_by_evidence", "Choose the requested option.", reviewed=True
            ),
            replace(APPROVED, reviewed=True),
        ]
    )
    loop, _, _, _, _ = build(
        [turn(call("show_profile", "c1")), turn(call("show_profile", "c2"))],
        judge=judge,
        tools={"show_profile": CardTool()},
    )
    assert (await run(loop)).capability_cards
    assert judge.briefs[-1].capability_presented[0]["kind"] == "data_widget"


@pytest.mark.parametrize(
    "site,violation,expected",
    [
        ("exit_table", "contradicts_result", "ship_tables_with_hedge"),
        ("exit_table", "leaks_sql_or_schema", "decline_only"),
        ("exit_capability", "unexplained_gap", "ship_cards_with_hedge"),
        ("exit_capability", "leaks_sql_or_schema", "decline_only"),
        ("exit_prose", "unsupported_by_evidence", "decline_only"),
    ],
)
def test_ship_disposition(site, violation, expected):
    guard = JudgeShipGuard()
    guard.note_refusal(site, violation, assumptions=["Before"])
    assert guard.disposition(site) == expected
    guard.note_approval()
    assert guard.disposition(site) is None


def test_fatal_override_can_only_disarm_and_fatality_is_sticky():
    guard = JudgeShipGuard()
    guard.note_refusal("exit_prose", "unsupported_by_evidence", card_fatal=True)
    assert not guard.card_fatal_pending
    guard.note_refusal("exit_capability", "capability_intent_mismatch")
    guard.note_refusal("exit_prose", "unexplained_gap")
    assert guard.disposition("exit_table") == "decline_only"


async def test_scope_refusal_never_ships_an_insisted_creative_answer():
    draft = "Here is a lovely poem about spring."
    loop, store, _, _, _ = build(
        [
            turn(call("answerWithText", "a1", answer=draft)),
            turn(call("answerWithText", "a2", answer=draft)),
        ]
    )
    outcome = await run(loop, "Write a poem about spring")
    assert outcome.assistant_text == SCOPE_REFUSAL_TEXT
    assert "product" in outcome.assistant_text.lower()
    assert (await store.get_or_create_session(CREDS.session_id)).messages[
        -1
    ].content == outcome.assistant_text


def test_scope_and_leak_precedence_with_help_evidence():
    assert first_match("Helpful explanation", [], "Write a poem").name == "out_of_scope_request"
    assert (
        first_match("Helpful explanation", [], "Write a poem", has_alternative_evidence=True)
        is None
    )
    assert first_match("SELECT * FROM hr.employee", [SQL]).name == "sql_in_answer"
    assert first_match("The hr.employee table", [SQL]).name == "schema_in_answer"
    assert first_match("See https://example.com/help", [SQL]) is None


def test_blueprint_gate_is_once_per_window_and_batch_order_independent():
    gate = BlueprintSearchGate("How many employees work here?", [], 0)
    assert gate.check("runQuery").error_code == "BLUEPRINT_NOT_SEARCHED"
    assert gate.check("runQuery") is None
    gate = BlueprintSearchGate("How many employees work here?", [], 0)
    gate.observe_batch([call("runQuery"), call("searchBlueprints")])
    assert gate.check("runQuery") is None


async def test_capability_auth_is_per_request_and_forward_header_is_hydration_only():
    requests = []

    def respond(request):
        requests.append(request)
        return (
            httpx.Response(200, json={"cards": []})
            if request.url.path.endswith("/search")
            else httpx.Response(404)
        )

    client = HttpCapabilityClient(
        base_url="https://capability.test", transport=httpx.MockTransport(respond)
    )
    await asyncio.gather(
        client.search("one", ("navigation",), end_user_jwt="first"),
        client.get_definition("two", end_user_jwt="second"),
    )
    await client.hydrate(
        "two", query="x", raw_arguments={}, end_user_jwt="third", forward_end_user=True
    )
    await client.get_definition("three")
    assert [r.headers.get("authorization") for r in requests] == [
        "Bearer first",
        "Bearer second",
        "Bearer third",
        None,
    ]
    assert [r.headers.get("x-end-user-authorization") for r in requests] == [
        None,
        None,
        "Bearer third",
        None,
    ]
    assert "third" not in repr(client.__dict__)


async def test_provider_error_code_does_not_become_arbitrary_telemetry():
    client = HttpCapabilityClient(
        base_url="https://capability.test",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(500, json={"error": {"code": "SECRET_EMPLOYEE_NAME"}})
        ),
    )
    with pytest.raises(CapabilityServiceError) as caught:
        await client.get_definition("profile")
    assert caught.value.code == "SERVICE_ERROR"


@pytest.mark.parametrize("recording", [True, False])
def test_hydrate_trace_contains_no_payload_values_or_arbitrary_keys(recording):
    events = []
    span = SimpleNamespace(
        is_recording=lambda: recording,
        add_event=lambda name, attributes: events.append((name, attributes)),
    )
    record_hydrate_event(
        span,
        card={
            "SECRET_KEY": "SECRET_VALUE",
            "metadata": {"gql": ["secret query"], "widgetName": "SECRET_WIDGET"},
        },
    )
    serialized = json.dumps(events)
    assert "SECRET" not in serialized
    assert bool(events) == recording
    record_hydrate_event(span, error=CapabilityServiceError(status_code=502, code="SECRET_CODE"))
    assert "SECRET" not in json.dumps(events)


def test_presentation_shape_and_digest_bounds():
    decoded = _decode_presentation(
        {"title": "Profile", "fields": [{"name": "x" * 100, "description": "d" * 100}] * 40}
    )
    assert len(decoded.fields) == 25
    labels = card_display_fields(SimpleNamespace(presentation=decoded))
    assert len(labels) <= 8 and all(len(label) <= 120 for label in labels)
    assert "javascript" not in presentation_label(
        "data_widget", preamble_url="ember:javascript:AlertCard"
    )
    assert _decode_presentation(None) is None
    with pytest.raises(CapabilityError):
        _decode_presentation({"fields": ["not a field"]})
    assert (
        metadata_data_digest(
            "data_widget", {"gql": [{"query": "secret SQL", "link": "secret URL"}]}
        )
        == ()
    )


async def test_array_full_results_are_legal_writes_but_degraded_reads(caplog):
    store = InMemorySessionStore()
    value = [{"private": "SECRET_VALUE"}]
    ref = await store.write_full_result("s", "array", value)
    value.clear()
    assert await store.read_full_result("s", ref) is None
    assert "SECRET_VALUE" not in caplog.text
    ref = await store.write_full_result("s", "object", {"rows": [[1]]})
    first = await store.read_full_result("s", ref)
    first["rows"].clear()
    assert (await store.read_full_result("s", ref))["rows"] == [[1]]


def test_shutdown_always_attempts_both_stages_without_logging_exception_text(caplog):
    class Provider:
        calls = []

        def force_flush(self, timeout_millis):
            self.calls.append(("flush", timeout_millis))
            raise RuntimeError("SECRET_TOKEN")

        def shutdown(self):
            self.calls.append(("shutdown",))
            raise RuntimeError("SECRET_TOKEN")

    provider = Provider()
    shutdown_tracing(provider, flush_timeout_millis=12)
    assert provider.calls == [("flush", 12), ("shutdown",)]
    assert "SECRET_TOKEN" not in caplog.text
    shutdown_tracing(None)


def test_fitter_terminates_when_subject_alone_exceeds_budget_and_preserves_pins():
    payload = {
        "question": "q" * 5000,
        "capability_presented": [{"name": "profile"}],
        "designated_tool_call_ids": ["chosen"],
        "results": [{"tool_call_id": str(i), "result_full": "x" * 2000} for i in range(5)],
    }
    fitted = _fit_payload(payload, 1)
    assert fitted["question"] == payload["question"]
    assert fitted["capability_presented"] == payload["capability_presented"]
    assert all(r["omitted_for_size"] for r in fitted["results"])
    assert "result_full" in payload["results"][0]


@pytest.mark.parametrize(
    "reply,reviewed",
    [
        (turn(), False),
        (turn(call(JUDGE_TOOL_NAME, approved=True, violation="", feedback="")), True),
    ],
)
async def test_only_real_judge_approval_is_reviewed(reply, reviewed):
    judge = AnswerJudge(model_client=ScriptedModelClient([reply]), token_budget=32_000)
    verdict = await judge.review(
        JudgeBrief(site="exit_prose", question="How many?", draft="Here is the answer.")
    )
    assert verdict.approved and verdict.reviewed is reviewed


def test_malformed_persisted_disposition_cannot_crash_history_decode():
    doc = TurnMessage(0, "assistant", "answer", "now").to_doc()
    doc["ship_disposition"] = []
    assert TurnMessage.from_doc(doc).ship_disposition is None


@pytest.mark.parametrize(
    "violation,keeps_table", [("contradicts_result", True), ("capability_intent_mismatch", False)]
)
async def test_table_ship_guard_and_history_agree(violation, keeps_table):
    judge = Judge([JudgeVerdict(False, violation, "The answer needs a correction.", reviewed=True)])
    loop, store, _, _, _ = build(
        [
            turn(
                call(
                    "answerWithTable",
                    "table-1",
                    answer="Here are the results.",
                    tables=[{"sql": SQL}],
                )
            ),
            turn(
                call(
                    "answerWithTable",
                    "table-2",
                    answer="Here are the results.",
                    tables=[{"sql": SQL}],
                )
            ),
        ],
        judge=judge,
    )
    outcome = await run(loop)
    assert bool(outcome.answer_tables) is keeps_table
    assert bool(outcome.sql_executed) is False  # A designation does not invent a query execution.
    doc = await store.get_or_create_session(CREDS.session_id)
    history = project_history(doc.messages, doc.tool_trail, frozenset(), None)["turns"][0]
    assert history["answer"] == outcome.assistant_text
    assert bool(history["answer_tables"]) is keeps_table


async def test_ship_guard_strips_only_assumptions_added_after_refusal():
    from data_agent.runtime.composite.answer_with_table import AnswerTable

    guard = JudgeShipGuard()
    guard.note_refusal("exit_capability", "capability_intent_mismatch", assumptions=["Before"])
    accum = TurnAccumulators(
        sql=[SQL],
        answer_tables=[AnswerTable(sql=SQL)],
        assumptions=["Before", "After"],
        capability_cards=[{"name": "profile"}],
        blueprint_use={"id": "bp1"},
        verification={"verified": True},
    )
    loop, _, _, _, _ = build([])
    outcome = await loop._finish(
        session_id=CREDS.session_id,
        turn_index=0,
        status="done",
        exit_label="answer_with_table",
        assistant_text="Unsupported claim",
        tool_calls_made=0,
        accum=accum,
        ship_guard=guard,
        judge_site="exit_table",
    )
    assert outcome.assumptions == ["Before"]
    assert outcome.answer_tables is outcome.capability_cards is outcome.sql_executed is None
    assert outcome.blueprint_use is outcome.verification is None


async def test_failed_summary_does_not_prevent_dispatch_or_emit_a_late_summary():
    class Summarizer:
        async def summarize(self, name, args):
            raise TimeoutError("private input")

    loop, _, events, _, _ = build(
        [turn(call("show_profile"))], tools={"show_profile": CardTool()}, summarizer=Summarizer()
    )
    assert (await run(loop)).capability_cards
    assert [n for n, _ in events if n.startswith("tool_dispatch_")] == [
        "tool_dispatch_start",
        "tool_dispatch_ok",
    ]
    assert not any(n == "tool_progress_summary" for n, _ in events)


async def test_runtime_exception_keeps_the_pair_and_canned_error():
    class Broken:
        async def run(self, arguments, credentials, turn=None, tool_call_id=None):
            raise RuntimeError("private input")

    loop, store, events, _, _ = build(
        [turn(call("broken", "broken-1")), turn(), turn()], tools={"broken": Broken()}
    )
    await run(loop)
    assert [(n, p["tool_call_id"]) for n, p in events if n.startswith("tool_dispatch_")] == [
        ("tool_dispatch_start", "broken-1"),
        ("tool_dispatch_error", "broken-1"),
    ]
    assert "private" not in ((await store.load_trail(CREDS.session_id))[0].denial_detail or "")


async def test_prefetch_receives_token_without_memoizing_it():
    from data_agent.runtime.capabilities.prefetch import CapabilityPrefetch
    from data_agent.runtime.capabilities.router import PrefetchRouter

    provider = AsyncMock(
        return_value=CapabilityPrefetch(route=PrefetchRouter().route("Show a profile"), cards=())
    )
    store = InMemorySessionStore()
    memo = {}
    assembler = ContextAssembler(store, capability_prefetch_provider=provider)
    await assembler.assemble(
        "s", frozenset(), user_message="Show a profile", user_jwt="SECRET_JWT", capability_memo=memo
    )
    provider.assert_awaited_once_with("Show a profile", "SECRET_JWT")
    assert "SECRET_JWT" not in repr(memo)


@pytest.mark.parametrize("value", [[{"secret": "VALUE"}], 7, "text", None])
async def test_couchbase_raw_value_non_objects_degrade(value, monkeypatch):
    from data_agent.runtime.session import couchbase_store as module

    store = object.__new__(module.CouchbaseSessionStore)
    store._ensure_connected = AsyncMock()
    store._results = object()
    monkeypatch.setattr(module, "get_or_none", AsyncMock(return_value=SimpleNamespace(value=value)))
    assert await store.read_full_result("s", "result::r") is None


async def test_couchbase_decode_error_degrades_but_transport_error_propagates(monkeypatch):
    from data_agent.runtime.session import couchbase_store as module

    store = object.__new__(module.CouchbaseSessionStore)
    store._ensure_connected = AsyncMock()
    store._results = object()

    class Invalid:
        @property
        def value(self):
            raise ValueError("private invalid JSON")

    monkeypatch.setattr(module, "get_or_none", AsyncMock(return_value=Invalid()))
    assert await store.read_full_result("s", "result::r") is None
    monkeypatch.setattr(
        module, "get_or_none", AsyncMock(side_effect=ConnectionError("unavailable"))
    )
    with pytest.raises(ConnectionError):
        await store.read_full_result("s", "result::r")


def test_session_attribute_exists_when_agent_span_opens():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from data_agent.runtime.observability.tracing import agent_span

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with agent_span(
        provider.get_tracer("test"), scope_hash="opaque", turn_index=0, session_id="session-7"
    ) as span:
        assert span.attributes["session.id"] == "session-7"
    assert exporter.get_finished_spans()[0].attributes["session.id"] == "session-7"
    provider.shutdown()


async def test_blueprint_advisory_refuses_once_then_dispatches_a_retry():
    loop, store, events, _, mcp = build(
        [
            turn(call("runQuery", "query-1", sql=SQL)),
            turn(call("runQuery", "query-2", sql=SQL)),
            turn(
                call(
                    "answerWithTable",
                    "answer-1",
                    answer="Here are the results.",
                    tables=[{"sql": SQL}],
                )
            ),
        ]
    )
    outcome = await run(loop, "How many employees work here?")
    trail = await store.load_trail(CREDS.session_id)
    assert [(t.tool_call_id, t.status, t.error_code) for t in trail[:2]] == [
        ("query-1", "error", "BLUEPRINT_NOT_SEARCHED"),
        ("query-2", "ok", None),
    ]
    assert outcome.answer_tables
    assert len(mcp.calls) == 1
    assert not any(
        p.get("tool_call_id") == "query-1" for n, p in events if n.startswith("tool_dispatch_")
    )


async def test_same_batch_blueprint_consultation_allows_query_even_when_query_is_first():
    class Search:
        async def run(self, arguments, credentials, turn=None, tool_call_id=None):
            return ok("searchBlueprints", {"blueprints": []})

    loop, store, _, _, _ = build(
        [
            turn(
                call("runQuery", "q1", sql=SQL), call("searchBlueprints", "s1", query="Employees")
            ),
            turn(
                call("answerWithTable", "a1", answer="Here are the results.", tables=[{"sql": SQL}])
            ),
        ],
        tools={"searchBlueprints": Search()},
    )
    await run(loop, "How many employees work here?")
    assert (await store.load_trail(CREDS.session_id))[0].status == "ok"


async def test_direct_resolver_failure_has_one_generated_correlation_id():
    from data_agent.runtime.composite.resolve_values import ResolveValuesComposite

    events = []
    tool = ResolveValuesComposite(
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG),
        catalog=CATALOG,
        observer=lambda n, p: events.append((n, p)),
    )
    tool._resolve_catalog = AsyncMock(side_effect=RuntimeError("private failure"))
    result = await tool.resolve(
        table="employee", column="employee_code", concept="Jane", period=None, credentials=CREDS
    )
    assert result.status == "error"
    assert [n for n, _ in events] == ["tool_dispatch_start", "tool_dispatch_error"]
    assert events[0][1]["tool_call_id"] == events[1][1]["tool_call_id"]
    assert events[0][1]["tool_call_id"]


def test_resumed_cards_retain_judge_kind_without_exposing_internal_evidence():
    payload = {
        "name": "profile",
        "metadata": {},
        "_agent_evidence": {"kind": "data_widget", "parameters": [{"name": "employees"}]},
    }
    accum = TurnAccumulators(capability_cards=[payload])
    assert "_agent_evidence" not in accum.capability_cards[0]
    assert accum.capability_judge_context[0]["kind"] == "data_widget"
    assert accum.capability_judge_context[0]["parameter_names"] == ["employees"]


@pytest.mark.parametrize("verdict,visible", [(APPROVED, False), (replace(APPROVED, reviewed=True), True), (JudgeVerdict(False, "unrecorded_assumption", "Clarify the employee filter.", reviewed=True), False)])
async def test_data_widget_needs_actual_approval_even_when_a_hedge_would_keep_navigation(verdict, visible):
    judge = Judge([verdict])
    script = [turn(call("show_profile", "c1"))]
    if not verdict.approved:
        script.append(turn(call("show_profile", "c2")))
    loop, _, _, _, _ = build(script, judge=judge, tools={"show_profile": CardTool()})
    outcome = await run(loop)
    assert bool(outcome.capability_cards) is visible
    if not visible:
        assert outcome.answer_tables is None


@pytest.mark.parametrize("draft", ["I can’t verify those instructions.", "I don’t have verified information.", "I cannot answer that request."])
def test_honest_declines_do_not_need_fabricated_evidence(draft):
    assert first_match(draft, []) is None


@pytest.mark.parametrize("draft", ["I’ll open Banking Center.", "I opened Banking Center.", "I've already opened the page."])
def test_navigation_claims_include_curly_apostrophes_and_past_tense(draft):
    from data_agent.runtime.loop.agent_loop import _claims_navigation_was_performed
    assert _claims_navigation_was_performed(draft, [{"metadata": {"preamble_url": "ember:GenericButton"}}])


@pytest.mark.parametrize('name,status,payload', [
    ('searchHelpCenter', 'error', None),
    ('searchHelpCenter', 'ok', {'documents': [], 'count': 0}),
    ('getHelpCenterDocument', 'ok', {'found': False}),
    ('getHelpCenterDocument', 'ok', {'found': True, 'content': ''}),
])
async def test_help_unavailable_cannot_launder_instructions(name, status, payload):
    from data_agent.runtime.loop.help_grounding import HELP_UNAVAILABLE_TEXT

    class HelpTool:
        async def run(self, arguments, credentials, turn=None, tool_call_id=None):
            return replace(ok(name, payload), status=status)

    loop, store, _, _, _ = build([
        turn(call(name)),
        turn(call('answerWithText', answer="I can't verify this, but open Time Clock and select Start.", evidence=[])),
    ], tools={name: HelpTool()}, judge=Judge([APPROVED]))
    outcome = await run(loop, 'How do I clock in?')
    assert outcome.assistant_text == HELP_UNAVAILABLE_TEXT
    doc = await store.get_or_create_session(CREDS.session_id)
    assert project_history(doc.messages, doc.tool_trail, frozenset(), None)['turns'][0]['answer'] == HELP_UNAVAILABLE_TEXT
    message = [m for m in doc.messages if m.role == 'assistant'][-1]
    assert message.ship_disposition == 'decline_only'
    assert message.retained_assumption_count == 0


def test_help_grounding_preserves_fetched_document_and_mixed_data():
    from data_agent.runtime.loop.help_grounding import HelpGrounding
    state = HelpGrounding()
    state.observe('searchHelpCenter', 'error', None)
    assert state.needs_decline(set(), False)
    assert not state.needs_decline({'runQuery'}, False)
    assert not state.needs_decline(set(), True)
    state.observe('getHelpCenterDocument', 'ok', {'found': True, 'content': 'Select Clock In.'})
    assert not state.needs_decline(set(), False)


@pytest.mark.parametrize('title,expected', [
    ('Forms: W-2', ('Forms: W-2',)), ('Q3: 2026 filing plan', ('Q3: 2026 filing plan',)),
    ('javascript: alert(1)', ()), ('ember:javascript:AlertCard', ()),
    ('java\x00script:alert(1)', ()), ('custom+scheme:payload', ()),
    ('../internal/page', ()), ('https://example.invalid/page', ()),
])
def test_v2_navigation_title_fallback(title, expected):
    assert metadata_data_digest('navigation', {'arguments': {'links': [{'webPage': title}]}}) == expected


@pytest.mark.parametrize('text,leak', [
    ('```SELECT name FROM employees```', True),
    ('`drop table employees`', True), ('select count(*) from employees', True),
    ('select name from employees order by name', True),
    ('select name from employees where status = 1', True),
    ('select name from employees;', True), ('SHOW DATABASES', True),
    ('We select from each group by hand.', False),
    ('You can select from the menu, limit 3 per person.', False),
    ('Select one from the plans where coverage > 80%.', False),
    ('We select from staff; contractors are excluded.', False),
    ('Select **the best** from the menu.', False),
])
def test_v2_sql_reference_distinguishes_statements_from_business_prose(text, leak):
    from data_agent.runtime.loop.answer_rules import contains_sql
    assert contains_sql(text) is leak


def test_v2_judge_has_one_consistent_prose_and_assumption_contract():
    from data_agent.runtime.loop.answer_judge import _system_prompt
    for site in ('exit_prose', 'exit_table', 'exit_capability'):
        prompt = _system_prompt(site)
        assert 'Formatting, markdown, tables, SQL or schema names' not in prompt
        assert '`recorded_assumptions` SHIP to the user VERBATIM' in prompt
        assert 'honest preface does not license unsupported steps' in prompt
        assert 'leaks_sql_or_schema' in prompt
    assert 'Disclosure alone does not justify a merely adjacent option' in _system_prompt('exit_capability')
    assert 'recorded_after_refusal' not in _system_prompt('ask_user')


def test_v2_pending_intent_nudge_marks_truncated_draft():
    from data_agent.runtime.loop.finalization import MAX_NUDGE_DRAFT_CHARS, finalization_nudge_text
    text = finalization_nudge_text('x' * (MAX_NUDGE_DRAFT_CHARS + 10), [])
    assert '…[truncated]' in text


@pytest.mark.parametrize('code', ['TIMEOUT', 'INTERNAL_ERROR', 'UNAVAILABLE'])
async def test_v2_observed_provider_codes_survive_closed_mapping(code):
    from data_agent.runtime.capabilities.client import safe_provider_code
    assert safe_provider_code(code) == code
    assert safe_provider_code(code.lower()) == 'SERVICE_ERROR'
    assert safe_provider_code('PRIVATE_UPPERCASE_SECRET') == 'SERVICE_ERROR'


async def test_v2_refused_card_repick_fixture():
    invented = 'Enrollment closes on Friday at midnight without exception.'
    class Navigation:
        def __init__(self, name):
            self.name = name
        async def run(self, arguments, credentials, turn=None, tool_call_id=None):
            return ok(self.name, {'name': self.name, 'metadata': {'preamble_url': 'ember:Button'},
                '_agent_evidence': {'kind': 'navigation'}, 'answer': arguments.get('answer', 'Use this option.')}, terminal=True)
    names = ['view_benefits_summary', 'enroll_in_benefits']
    judge = Judge([
        JudgeVerdict(False, 'unsupported_by_evidence', 'The deadline is unsupported.', reviewed=True),
        JudgeVerdict(False, 'capability_coverage_gap', 'The option cannot show the deadline.', reviewed=True),
    ])
    loop, store, events, model, _ = build([
        turn(call(names[0], 'cap_1', answer=invented)),
        turn(call(names[1], 'cap_2', answer=invented)),
        turn(call(names[1], 'cap_3')),
    ], judge=judge, tools={n: Navigation(n) for n in names})
    outcome = await run(loop, 'How do I enroll in benefits, and when does enrollment close?')
    assert outcome.status == 'done'
    assert not outcome.capability_cards
    assert invented not in outcome.assistant_text
    assert len(judge.briefs) == 2
    assert len(judge.briefs[1].capability_presented) == 1
    assert names[1] in json.dumps(judge.briefs[1].capability_presented)
    assert names[0] not in json.dumps(judge.briefs[1].capability_presented)
    trail = await store.load_trail(CREDS.session_id)
    assert [(t.tool_name, t.status) for t in trail] == [(names[0], 'ok'), (names[1], 'ok'), (names[1], 'ok')]
    guarded = [p for n,p in events if n == 'loop_answer_judge_ship_guarded']
    assert len(guarded) == 1 and guarded[0]['disposition'] == 'decline_only'


@pytest.mark.parametrize('ready', [True, False])
async def test_v2_same_batch_registration_and_declined_negative(ready):
    from data_agent.runtime.capabilities.client import CapabilityDefinition
    from data_agent.runtime.capabilities.tools import GetCapabilityTool
    definition = CapabilityDefinition(name='show_profile', version='1', kind='data_widget',
        description='Employee profile', parameters=(), metadata={'preamble_url': 'ember:EmployeeCard'})
    client = SimpleNamespace(get_definition=AsyncMock(return_value=definition))
    def hydrate(definition):
        if ready:
            loop._runtime_tools[definition.name] = CardTool()
        return ready
    lookup = GetCapabilityTool(client=client, hydrate=hydrate, visible_names=set())
    loop, store, events, _, _ = build([
        turn(call('getCapabilityTool', 'load', tool_name='show_profile'), call('show_profile', 'present')),
        turn(call('answerWithText', 'decline', answer="I can't show that information.", evidence=[])),
    ], tools={'getCapabilityTool': lookup}, judge=Judge([replace(APPROVED, reviewed=True)]))
    outcome = await run(loop)
    trail = await store.load_trail(CREDS.session_id)
    assert trail[0].tool_name == 'getCapabilityTool' and trail[0].status == 'ok'
    assert trail[1].error_code == (None if ready else 'UNKNOWN_TOOL')
    assert bool(outcome.capability_cards) is ready
    assert bool([n for n,p in events if n == 'loop_unknown_tool_rejected']) is not ready
