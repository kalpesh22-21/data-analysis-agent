"""B5 integration coverage: TOOL spans are actually emitted on the live path,
D25 SQL-literal masking lands on a real span, and the askUser question text
never reaches any span attribute (Layer 1 — real OTel SDK + InMemorySpanExporter,
no live Phoenix collector).

Before this fix, `app.py`'s `_tracing_observer` dropped every
`tool_dispatch_*` event (`if not event.startswith("loop_"): return`), so
`ToolDispatcher` never produced a TOOL span at all — `tracing.tool_span`,
`redaction.redact_tool_args`, and `redaction.mask_sql` were dead code on the
real request path despite being unit-tested in isolation. This file proves
the wiring itself, not just the pure functions.
"""

from __future__ import annotations

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.answer_scrub import ANSWER_PROSE_REDACTED_EVENT
from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import (
    ANALYSIS_STATE_AUTO_BOUND_EVENT,
    UpdateAnalysisStateTool,
)
from data_agent.runtime.context.assembly import (
    _REPEATED_IDEMPOTENT_READ_NUDGE,
    ContextAssembler,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop, TurnContext
from data_agent.runtime.loop.finalization import (
    ANSWER_SHAPE_EXHAUSTED_EVENT,
    ANSWER_SHAPE_REFUSED_EVENT,
    EMPTY_ANSWER_EXHAUSTED_EVENT,
    EMPTY_ANSWER_REFUSED_EVENT,
)
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.observability import tracing
from data_agent.runtime.observability.progress import combine_observers
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import ResultPreview, TrailEntry

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Salary": "Decimal(18,2)"}})

SESSION_ID = "sess-tool-span-e2e"
JWT = "jwt-secret-should-never-leak"
PII_NAME = "Jane Doe"
PII_SALARY = "128000"
PII_SQL = f"SELECT EmployeeCode FROM employee WHERE Name = '{PII_NAME}' AND Salary > {PII_SALARY}"
PII_QUESTION = f"Did you mean the record for {PII_NAME} (salary {PII_SALARY})?"


def _tracer_with_memory_exporter() -> tuple[tracing.Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return tracing.get_tracer(provider), exporter


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=JWT, column_scope=frozenset())


async def test_tool_span_emitted_per_dispatched_call_with_sql_literal_masked() -> None:
    """(a) a TOOL span is created per dispatched call; (b) its SQL attribute
    is literal-masked (D25), never the raw PII-laden SQL."""
    tracer, exporter = _tracer_with_memory_exporter()
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                {"columns": ["EmployeeCode"], "rows": [["E1"]], "row_count": 1, "truncated": False}
            ]
        }
    )
    dispatcher = ToolDispatcher(mcp, CATALOG, tracer=tracer)

    result = await dispatcher.dispatch("runQuery", {"sql": PII_SQL}, _credentials())
    assert result.status == "ok"

    spans = exporter.get_finished_spans()
    tool_spans = [
        s
        for s in spans
        if s.attributes.get(SpanAttributes.OPENINFERENCE_SPAN_KIND)
        == OpenInferenceSpanKindValues.TOOL.value
    ]
    assert len(tool_spans) == 1
    attrs = tool_spans[0].attributes
    assert attrs["tool.name"] == "runQuery"
    assert attrs["tool.status"] == "ok"

    masked_sql = attrs["tool.args.sql"]
    assert PII_NAME not in masked_sql
    assert PII_SALARY not in masked_sql
    assert "SELECT EmployeeCode FROM employee" in masked_sql  # shape preserved


async def test_tool_span_emitted_for_denied_and_error_dispatches_too() -> None:
    """A TOOL span is created on EVERY dispatch outcome, not just success —
    denied (MCPToolError) and error (B4 raw transport exception) paths too."""
    from data_agent.runtime.mcp.client import MCPToolError

    tracer, exporter = _tracer_with_memory_exporter()
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                MCPToolError("COLUMN_SCOPE_VIOLATION", "denied"),
                ConnectionError("connection reset by peer"),
            ]
        }
    )
    dispatcher = ToolDispatcher(mcp, CATALOG, tracer=tracer)

    denied = await dispatcher.dispatch("runQuery", {"sql": "SELECT 1"}, _credentials())
    assert denied.status == "denied"
    errored = await dispatcher.dispatch("runQuery", {"sql": "SELECT 1"}, _credentials())
    assert errored.status == "error"

    spans = exporter.get_finished_spans()
    tool_spans = [
        s
        for s in spans
        if s.attributes.get(SpanAttributes.OPENINFERENCE_SPAN_KIND)
        == OpenInferenceSpanKindValues.TOOL.value
    ]
    assert len(tool_spans) == 2
    assert tool_spans[0].attributes["tool.status"] == "denied"
    assert tool_spans[0].attributes["tool.error_code"] == "COLUMN_SCOPE_VIOLATION"
    assert tool_spans[1].attributes["tool.status"] == "error"
    assert tool_spans[1].attributes["tool.error_code"] == "INTERNAL_TRANSPORT_ERROR"
    # Never the raw transport exception text, on a span any more than in the
    # user-facing message (B4).
    blob = str(dict(tool_spans[1].attributes))
    assert "connection reset by peer" not in blob


async def test_ask_user_question_never_appears_in_any_span_attribute() -> None:
    """(c) the askUser question text never reaches any span attribute — the
    loop's own GUARDRAIL-observer wiring (`tracing.guardrail_observer`, the
    exact function `app.py` wires in) must strip it, not merely the tool
    spans (askUser never reaches ToolDispatcher at all)."""
    tracer, exporter = _tracer_with_memory_exporter()
    guardrail_observer = tracing.guardrail_observer(tracer)

    store = InMemorySessionStore()
    mcp = FakeMCPClient()  # askUser must never reach the MCP transport
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="call_1", name="askUser", arguments={"question": PII_QUESTION})
                ]
            )
        ]
    )
    dispatcher = ToolDispatcher(mcp, CATALOG, observer=guardrail_observer, tracer=tracer)
    assembler = ContextAssembler(store, tracer=tracer)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=combine_observers(guardrail_observer),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Show me payroll."
    )
    assert outcome.status == "paused_ask_user"
    assert outcome.pending_question["question"] == PII_QUESTION  # UI DOES get it (not telemetry)

    spans = exporter.get_finished_spans()
    assert len(spans) >= 1  # at least the loop_paused_ask_user GUARDRAIL span fired
    for finished_span in spans:
        for value in finished_span.attributes.values():
            assert PII_NAME not in str(value)
            assert PII_SALARY not in str(value)
            assert PII_QUESTION not in str(value)


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return [
        {
            "type": "function",
            "name": "askUser",
            "description": "",
            "parameters": {"type": "object", "properties": {"question": {"type": "string"}}},
        }
    ]


async def _schema_tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return [{"type": "function", "name": "getTableSchema", "description": "", "parameters": {}}]


class _RepeatSchemaModel:
    """Re-issue the identical `getTableSchema(employee)` until its dedup nudge is
    visible, then answer — the exact cold-start hang the read guard fixes."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._n = 0

    async def send_turn(self, messages: list[dict], tools: list[dict]) -> ModelTurnResult:
        self.calls.append({"messages": messages})
        saw_nudge = any(
            m.get("role") == "tool"
            and isinstance(m.get("content"), str)
            and _REPEATED_IDEMPOTENT_READ_NUDGE in m["content"]
            for m in messages
        )
        if saw_nudge:
            return ModelTurnResult(assistant_text="Using the schema I have.", usage={"total_tokens": 1})
        self._n += 1
        return ModelTurnResult(
            tool_calls=[
                ToolCallRequest(
                    id=f"gts_{self._n}",
                    name="getTableSchema",
                    arguments={"database": "dbpcm_warehouse", "table": "employee"},
                )
            ],
            usage={"total_tokens": 1},
        )

    def begin_turn(self) -> _RepeatSchemaModel:
        return self


async def test_repeated_read_guard_span_is_legible_and_distinct_from_first_dispatch() -> None:
    """A guarded (deduped) repeat is NOT dispatched, so it produces no
    `tool.getTableSchema` TOOL span. To keep a reader from concluding "the FIRST
    read was blocked", the guard's own GUARDRAIL span must self-describe: exactly
    one real TOOL span (the first dispatch) sits beside one
    `loop_repeated_idempotent_read_guarded` span carrying tool_name / deduped /
    table / a human-readable note — routed through the REAL `guardrail_observer`
    allowlist (proving the payload keys actually export)."""
    tracer, exporter = _tracer_with_memory_exporter()
    guardrail_observer = tracing.guardrail_observer(tracer)

    store = InMemorySessionStore()
    mcp = FakeMCPClient(
        scripted={
            "getTableSchema": [
                {"database": "dbpcm_warehouse", "table": "employee", "columns": ["EmployeeCode"]}
                for _ in range(10)
            ]
        }
    )
    model = _RepeatSchemaModel()
    dispatcher = ToolDispatcher(mcp, CATALOG, observer=guardrail_observer, tracer=tracer)
    assembler = ContextAssembler(store, tracer=tracer)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_schema_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=combine_observers(guardrail_observer),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Describe employee."
    )
    assert outcome.status == "done"

    spans = exporter.get_finished_spans()

    # Exactly ONE real getTableSchema TOOL span — the FIRST call WAS dispatched
    # (so a reader must not read the guard as "the first was blocked").
    tool_spans = [s for s in spans if s.name == "tool.getTableSchema"]
    assert len(tool_spans) == 1

    # Exactly ONE self-describing guard span for the deduped SECOND call.
    guard_spans = [s for s in spans if s.name == "loop_repeated_idempotent_read_guarded"]
    assert len(guard_spans) == 1
    attrs = dict(guard_spans[0].attributes)
    assert attrs["tool_name"] == "getTableSchema"
    assert attrs["deduped"] is True
    # UPDATED with the trim-aware exemption: "already served" alone no longer
    # decides the dedup — the result must ALSO still be readable in the rebuilt
    # window. The span says which decision was made, or a trace would misreport it.
    assert attrs["guard_reason"] == "already_served_and_still_readable"
    assert attrs["database"] == "dbpcm_warehouse"
    assert attrs["table"] == "employee"
    assert attrs["dedup_target"] == "dbpcm_warehouse.employee"
    assert "duplicate getTableSchema(dbpcm_warehouse.employee)" in attrs["note"]
    assert "not re-dispatched" in attrs["note"]


async def test_the_auto_bind_event_survives_the_real_guardrail_observer() -> None:
    """THE PREFIX IS THE TEST. `guardrail_observer` drops every event whose name
    does not start with `loop_`, silently — so an event named
    `analysis_state_auto_bound` (as this one first was) fires perfectly in every
    raw-recorder unit test and reaches NOTHING in production. That is the same
    class of gap this whole file exists for: `app.py`'s observer used to drop
    every `tool_dispatch_*` event, and the pure functions were unit-tested green
    while dead on the request path.

    So this drives `UpdateAnalysisStateTool` with the EXACT observer `app.py`
    wires (`tracing.guardrail_observer`) and asserts the span comes out the other
    end. A recorder-based assertion cannot fail for this reason, which is why one
    is deliberately not used here.

    The auto-bind counter is the only signal that says how often the model is
    failing to tag its own work — the backstop rescuing a turn and the backstop
    papering over a systematic problem look identical without it.
    """
    tracer, exporter = _tracer_with_memory_exporter()
    observer = tracing.guardrail_observer(tracer)

    store = InMemorySessionStore()
    tool = UpdateAnalysisStateTool(session_store=store, observer=observer)
    secret = "salaries for the Sales team"
    await tool.run(
        {"intents": [{"description": secret}, {"description": "headcount"}]},
        _credentials(),
        turn=TurnContext(turn_index=0),
    )
    # One untagged qualifying call: rule 1 binds it and announces the bind.
    await store.append_trail_entry(
        SESSION_ID,
        TrailEntry(
            turn_index=0,
            tool_call_id="call_q1",
            tool_name="runQuery",
            args={"sql": "SELECT 1"},
            status="ok",
            error_code=None,
            provenance=frozenset(),
            result_preview=ResultPreview(
                columns=["x"], row_count=1, truncated=False, preview_rows=[]
            ),
            result_full_ref=None,
            ts="2026-08-12T00:00:00+00:00",
        ),
    )
    result = await tool.run(
        {"intents": [{"intent_id": "i1", "status": "completed"}]},
        _credentials(),
        turn=TurnContext(turn_index=0),
    )
    assert result.status == "ok", result.denial_detail

    spans = exporter.get_finished_spans()
    auto_bound = [s for s in spans if s.name == ANALYSIS_STATE_AUTO_BOUND_EVENT]
    assert len(auto_bound) == 1, (
        "the auto-bind event did not survive guardrail_observer — check the "
        f"`loop_` prefix on {ANALYSIS_STATE_AUTO_BOUND_EVENT!r}"
    )
    assert dict(auto_bound[0].attributes)["intent_id"] == "i1"
    # The binding's provenance reaches telemetry too, on the completion event, so
    # a route derivation can tell a tagged close from a guessed one.
    completed = [s for s in spans if s.name == "loop_intent_completed"]
    assert len(completed) == 1
    assert dict(completed[0].attributes)["evidence_binding"] == "auto_bound"
    # D25, on the same pass: `description` is model-authored from the user's
    # question and reaches no span attribute on any of these events.
    for finished_span in spans:
        for value in finished_span.attributes.values():
            assert secret not in str(value)


async def _query_tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return [{"type": "function", "name": "runQuery", "description": "", "parameters": {}}]


async def test_the_answer_shape_events_survive_the_real_guardrail_observer() -> None:
    """THE PREFIX IS THE TEST, second instance (05 §J, 06). The answer-shape gate
    is a MEASUREMENT feature as much as a correction: it exists because the
    unconditional "present a table" rule failed live and nobody could see it
    failing. If `loop_answer_shape_refused` did not survive `guardrail_observer` —
    which drops every event lacking the `loop_` prefix, silently — the gate would
    correct turns in production and report nothing, and the raw-recorder tests in
    `tests/runtime/loop/test_answer_shape_gate.py` would stay green throughout.
    That is exactly how `loop_analysis_state_auto_bound` shipped mute for a review
    round, so this drives the REAL observer instead.

    It also proves the payload EXPORTS: `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST` is a
    strict allowlist, so a correctly-named span can still arrive carrying nothing,
    and `multi_row_calls` is the only attribute this event has.
    """
    tracer, exporter = _tracer_with_memory_exporter()
    observer = tracing.guardrail_observer(tracer)

    store = InMemorySessionStore()
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                {
                    "columns": ["Department", "n"],
                    "rows": [["Sales", 3], ["Eng", 2], ["Ops", 1]],
                    "row_count": 3,
                    "truncated": False,
                }
            ]
        }
    )
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="q1",
                        name="runQuery",
                        arguments={"sql": "SELECT EmployeeCode FROM employee"},
                    )
                ]
            ),
            # Bare-text finish holding three untabled rows -> refused once...
            ModelTurnResult(assistant_text="Sales 3, Eng 2, Ops 1."),
            # ...then again on the round handed back -> exhausted, and it passes.
            ModelTurnResult(assistant_text="Sales 3, Eng 2, Ops 1."),
        ]
    )
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(mcp, CATALOG, observer=observer, tracer=tracer),
        context_assembler=ContextAssembler(store, tracer=tracer),
        session_store=store,
        tools_provider=_query_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=combine_observers(observer),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount by department"
    )
    assert outcome.status == "done"

    spans = exporter.get_finished_spans()
    refused = [s for s in spans if s.name == ANSWER_SHAPE_REFUSED_EVENT]
    assert len(refused) == 1, (
        "the answer-shape refusal did not survive guardrail_observer — check the "
        f"`loop_` prefix on {ANSWER_SHAPE_REFUSED_EVENT!r}"
    )
    assert dict(refused[0].attributes)["multi_row_calls"] == 1, (
        "the refusal span exported no `multi_row_calls` — the key is missing from "
        "_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST, so the span carries nothing"
    )
    exhausted = [s for s in spans if s.name == ANSWER_SHAPE_EXHAUSTED_EVENT]
    assert len(exhausted) == 1, (
        "the exhausted counter did not survive guardrail_observer — check the "
        f"`loop_` prefix on {ANSWER_SHAPE_EXHAUSTED_EVENT!r}"
    )
    # D25: the gate reads row counts and emits a count. No SQL, no cell value, no
    # question text reaches any span this turn.
    for finished_span in spans:
        for value in finished_span.attributes.values():
            assert "headcount by department" not in str(value)


async def test_the_empty_answer_events_survive_the_real_guardrail_observer() -> None:
    """THE PREFIX IS THE TEST, fourth instance (05 §K, 06) — and the instance where
    it matters most, because THIS GATE'S EVENTS ARE THE ONLY ARTIFACT ITS FAILURE
    HAS. A silent finish persists no assistant message and makes no tool call; if
    these two spans were dropped for a missing `loop_` prefix, the runtime would go
    back to correcting silent turns in production while reporting nothing at all,
    and the raw-recorder tests in `tests/runtime/loop/test_empty_answer_gate.py`
    would stay green throughout.

    It also proves `incomplete_reason` EXPORTS. `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST`
    is a strict allowlist, so a correctly-named span can arrive carrying nothing —
    and this field is the entire diagnostic value of both events: it is what
    separates "the model chose to say nothing" from "the completion was cut off at
    the token cap", two failures with opposite fixes.
    """
    tracer, exporter = _tracer_with_memory_exporter()
    observer = tracing.guardrail_observer(tracer)

    store = InMemorySessionStore()
    model = ScriptedModelClient(
        [
            # Silent finish -> refused once...
            ModelTurnResult(assistant_text=None, incomplete_reason="max_output_tokens"),
            # ...and silent again on the round handed back -> exhausted, substituted.
            ModelTurnResult(assistant_text=None, incomplete_reason="max_output_tokens"),
        ]
    )
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG, observer=observer, tracer=tracer),
        context_assembler=ContextAssembler(store, tracer=tracer),
        session_store=store,
        tools_provider=_query_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=combine_observers(observer),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="headcount by department"
    )
    assert outcome.status == "done"

    spans = exporter.get_finished_spans()
    refused = [s for s in spans if s.name == EMPTY_ANSWER_REFUSED_EVENT]
    assert len(refused) == 1, (
        "the empty-answer refusal did not survive guardrail_observer — check the "
        f"`loop_` prefix on {EMPTY_ANSWER_REFUSED_EVENT!r}"
    )
    assert dict(refused[0].attributes)["incomplete_reason"] == "max_output_tokens", (
        "the refusal span exported no `incomplete_reason` — the key is missing from "
        "_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST, so the span carries nothing"
    )
    exhausted = [s for s in spans if s.name == EMPTY_ANSWER_EXHAUSTED_EVENT]
    assert len(exhausted) == 1, (
        "the exhausted counter did not survive guardrail_observer — check the "
        f"`loop_` prefix on {EMPTY_ANSWER_EXHAUSTED_EVENT!r}"
    )
    # D25: a provider status word and nothing else. The user's question never
    # reaches a span on this turn.
    for finished_span in spans:
        for value in finished_span.attributes.values():
            assert "headcount by department" not in str(value)


async def test_the_answer_prose_scrub_event_survives_the_observer_without_its_tokens() -> None:
    """THE PREFIX IS THE TEST, third instance (ISSUES I1) — with a second edge the
    other two do not have.

    The scrub is the one guardrail whose telemetry could UNDO it. Its event fires
    precisely when an identifier has been withheld from a user, so a payload
    carrying that identifier would republish it through the tracing side door,
    where it is far more durable than the answer was. `redaction_count` is
    therefore the only thing added to `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST`, and
    this drives the REAL observer to prove both halves at once: the span arrives
    with its count, and NO span on the turn carries the redacted token.
    """
    tracer, exporter = _tracer_with_memory_exporter()
    observer = tracing.guardrail_observer(tracer)

    store = InMemorySessionStore()
    loop = AgentLoop(
        model_client=ScriptedModelClient(
            [ModelTurnResult(assistant_text=f"Read from {_E}, joined employee_master.")]
        ),
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG, observer=observer, tracer=tracer),
        context_assembler=ContextAssembler(store, tracer=tracer),
        session_store=store,
        tools_provider=_query_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=combine_observers(observer),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="where from?"
    )
    assert outcome.assistant_text is not None
    assert "employee_master" not in outcome.assistant_text

    spans = exporter.get_finished_spans()
    redacted = [s for s in spans if s.name == ANSWER_PROSE_REDACTED_EVENT]
    assert len(redacted) == 1, (
        "the answer-prose scrub event did not survive guardrail_observer — check "
        f"the `loop_` prefix on {ANSWER_PROSE_REDACTED_EVENT!r}"
    )
    attributes = dict(redacted[0].attributes)
    assert attributes["redaction_count"] == 2, (
        "the span exported no `redaction_count` — the key is missing from "
        "_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST, so the span carries nothing"
    )
    assert attributes["exit"] == "no_tool_calls"
    # The point: what the answer withheld, the telemetry withholds too.
    for finished_span in spans:
        for value in finished_span.attributes.values():
            assert "employee_master" not in str(value)
            assert _E not in str(value)
