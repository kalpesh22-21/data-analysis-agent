"""D25 leak hunting on the SPAN surface, not the observer surface.

`test_analysis_state_telemetry.py` scans the observer PAYLOADS with fixture
canaries. That is one of two surfaces and the weaker of the two guards:

  - a substring canary is defeated by a description that happens to collide with
    a legitimate shape value, or by any content the scan did not think to look
    for (SQL, cell values, `column_scope` members);
  - and a payload that never reaches a span is a different question from a
    payload that does. `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST` grew twelve keys in
    Release 1, each of which is a DECLARATION that the key is shape-only.

So these tests drive the SHIPPED composition — `tracing.guardrail_observer` over
a real `TracerProvider` with an in-memory exporter, plus the tool's own tracer —
and assert two independent things: nothing content-bearing reaches a span, and
every string that does matches a closed shape (which a substring scan cannot
tell you).
"""

from __future__ import annotations

import json
import re
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import (
    EVIDENCE_BINDINGS,
    REJECTION_REASONS,
    TAG_DROP_REASONS,
    UpdateAnalysisStateTool,
)
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.observability import tracing
from data_agent.runtime.observability.progress import combine_observers
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import INTENT_STATUSES, REASON_CODES

SESSION_ID = "sess-span-leak-qa"
_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})
STATE = "updateAnalysisState"
ANSWER = "answerWithTable"

# The content classes D25 forbids, each planted somewhere the turn will carry it.
QUESTION = "how many people are in Radiology and what is their attrition"
DESC_ONE = "headcount for Radiology"
DESC_TWO = "attrition for Radiology"
SQL = "SELECT count() FROM dbpcm_warehouse.employee WHERE Department = 'Radiology'"
CELL_VALUE = "Radiology-CELL-MARKER"
SCOPE_COLUMN = f"{_E}.Department"
CANARIES = (QUESTION, DESC_ONE, DESC_TWO, SQL, CELL_VALUE, "Radiology")


def _provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(
        session_id=SESSION_ID, jwt="jwt", column_scope=frozenset({SCOPE_COLUMN})
    )


async def _tools_provider(_c: RuntimeCredentials) -> list[dict]:
    return []


def _init_call(call_id: str, *descriptions: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id, name=STATE, arguments={"intents": [{"description": d} for d in descriptions]}
    )


def _script() -> list[ModelTurnResult]:
    """A realistic multi-intent turn: declare two intents, run a query, complete
    one on it, block the other on the same query's emptiness, try to finalize with
    one still pending (refusal + nudge), then finish."""
    return [
        ModelTurnResult(
            assistant_text=None,
            tool_calls=[
                _init_call("s1", DESC_ONE, DESC_TWO),
                ToolCallRequest(id="q1", name="runQuery", arguments={"sql": SQL}),
            ],
        ),
        # A finalization attempt with both intents pending — 05's refusal path.
        ModelTurnResult(
            assistant_text=None,
            tool_calls=[
                ToolCallRequest(
                    id="a1", name=ANSWER, arguments={"answer": f"{CELL_VALUE} has 12", "sql": SQL}
                )
            ],
        ),
        ModelTurnResult(
            assistant_text=None,
            tool_calls=[
                ToolCallRequest(
                    id="s2",
                    name=STATE,
                    arguments={
                        "intents": [
                            {
                                "intent_id": "i1",
                                "status": "completed",
                                "evidence_tool_call_id": "q1",
                            },
                            {
                                "intent_id": "i2",
                                "status": "blocked",
                                "reason_code": "REQUIRED_DATA_UNAVAILABLE",
                                "evidence_tool_call_id": "q1",
                            },
                        ]
                    },
                ),
                # ...and a rejected call, so the rejection event is in the scan.
                ToolCallRequest(id="s3", name=STATE, arguments={"description": DESC_ONE}),
            ],
        ),
        ModelTurnResult(assistant_text=f"{CELL_VALUE}: 12 people."),
    ]


async def _run_turn() -> tuple[list[Any], list[tuple[str, dict[str, Any]]]]:
    provider, exporter = _provider()
    tracer = tracing.get_tracer(provider)
    events: list[tuple[str, dict[str, Any]]] = []

    def _record(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    observer = combine_observers(_record, tracing.guardrail_observer(tracer))
    store = InMemorySessionStore()
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                {
                    "columns": ["Department"],
                    "rows": [[CELL_VALUE]],
                    "row_count": 0,
                    "truncated": False,
                }
            ]
        }
    )
    loop = AgentLoop(
        model_client=ScriptedModelClient(_script()),
        tool_dispatcher=ToolDispatcher(mcp, CATALOG, observer=observer, tracer=tracer),
        context_assembler=ContextAssembler(store, history_token_budget=100_000),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=observer,
        runtime_tools={
            STATE: UpdateAnalysisStateTool(
                session_store=store, observer=observer, tracer=tracer
            ),
            ANSWER: AnswerWithTableTool(),
        },
    )
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message=QUESTION)
    return list(exporter.get_finished_spans()), events


async def test_no_span_from_a_multi_intent_turn_carries_content() -> None:
    """THE SPAN-SIDE D25 GUARD. Scans every attribute of every span the shipped
    composition emits for a rich turn: intent descriptions, the user's question,
    the SQL, a cell value, and the `column_scope` member. The `runQuery` tool span
    is the one legitimate near-miss (it carries a REDACTED `sql`), which is why the
    SQL string itself is a canary rather than just the word "SELECT"."""
    spans, events = await _run_turn()

    assert spans, "the scan proved nothing — no spans were exported"
    assert any(name.startswith("loop_analysis_state") for name, _ in events)
    blob = json.dumps(
        [[span.name, dict(span.attributes or {})] for span in spans], default=str
    )
    for canary in CANARIES:
        assert canary not in blob, (
            f"{canary!r} reached a span attribute. Telemetry is shape-only (D25): "
            "counts, enums, tool names, runtime-assigned ids — never question text, "
            "descriptions, SQL, cell values or column_scope contents."
        )
    # The scope itself is only ever exported as a hash.
    assert SCOPE_COLUMN not in blob


async def test_the_observer_payloads_carry_no_content_either() -> None:
    """The payload surface, widened past the existing description-only scan to
    every D25 class: SQL, cell values and the scope member are checked too.

    `loop_paused_ask_user.question` is the known, deliberate exception (it is the
    SSE progress channel, and the attribute allowlist is what stops it reaching a
    span) — this turn does not pause, so nothing here is exempted."""
    _spans, events = await _run_turn()

    loop_events = [(name, payload) for name, payload in events if name.startswith("loop_")]
    assert len(loop_events) > 8
    blob = json.dumps(loop_events, default=str)
    for canary in CANARIES:
        assert canary not in blob, f"{canary!r} leaked into an observer payload"


_SHAPE_EVENTS = frozenset(
    {
        "loop_analysis_state_initialized",
        "loop_analysis_state_transition",
        "loop_analysis_state_rejected",
        "loop_analysis_state_late_init_rejected",
        "loop_intent_blocked",
        "loop_intent_completed",
        "loop_intent_force_blocked",
        # Call-time intent tagging. `loop_intent_tag_dropped` is the one event in
        # the family that fires on a value the model INVENTED, so it is scanned
        # here specifically: it must carry the rule name and the tool name, and
        # never the tag itself.
        "loop_intent_tag_dropped",
        "loop_metadata_evidence_completion",
        "loop_zero_row_block",
        "loop_zero_row_completion",
        "loop_finalization_refused",
        "loop_finalization_block_spent",
        "loop_enforcement_exhausted",
    }
)

# Every string an analysisState-family payload is allowed to carry, as a CLOSED
# ENUMERATION — imported from the modules that define them, so it cannot drift.
# This is the guard a substring canary cannot give you: a description that
# collides with a legitimate value ("pending"), or one written in a script the
# canary list does not contain, still has to be a MEMBER of one of these sets.
_TOOL_NAMES = frozenset(
    {
        "runQuery",
        "runBlueprint",
        "getTableSchema",
        "sampleRows",
        "resolveValues",
        "searchBlueprints",
        "searchKnowledge",
        "getBlueprint",
        "listDatabases",
        "listTables",
        "explainQuery",
        "askUser",
        "recordAssumptions",
        "answerWithTable",
        "updateAnalysisState",
    }
)
_ALLOWED_STRINGS = (
    INTENT_STATUSES
    | REASON_CODES
    | REJECTION_REASONS
    # Call-time intent tagging: `evidence_binding` (how the binding was
    # established) and `loop_intent_tag_dropped.reason` (why a tag was dropped).
    # BOTH are imported closed enums for the same reason every other set here is —
    # the guard has to be structural. The dropped TAG VALUE is deliberately absent
    # from every payload: a valid tag is a runtime-assigned `intent_id` and matches
    # `_INTENT_ID` below, but an invalid one is arbitrary model text, so it is
    # reported by rule name only.
    | EVIDENCE_BINDINGS
    | TAG_DROP_REASONS
    | _TOOL_NAMES
    | frozenset({"answer_with_table", "no_tool_calls"})  # the two exits
)
_INTENT_ID = re.compile(r"^i\d+$")  # runtime-assigned, never model-supplied


async def test_every_string_on_an_analysis_state_event_matches_a_closed_shape() -> None:
    """STRUCTURAL, so it cannot be defeated the way a canary list can.

    Rather than asking "is the fixture's description absent", this asks "is every
    string that IS present one of the shapes 06 declared" — ids, statuses, reason
    codes, exits, rule names and tool names. Arbitrary model-authored text
    (including unicode, and including text that happens to contain a legitimate
    value as a substring) cannot satisfy it.

    `tool_call_id` is EXCLUDED and that exclusion is itself a finding: the id is
    MODEL-supplied, echoed verbatim on `loop_evidence_reused`, and `tool_call_id`
    is on the span attribute allowlist — so it is the one field on these events a
    hostile model can put arbitrary text into. Pre-existing (the allowlist entry
    predates Release 1 and `loop_repeated_idempotent_read_guarded` already used
    it), but Release 1 adds an emit site, so it is recorded here rather than
    quietly excluded."""
    _spans, events = await _run_turn()

    checked = 0
    for name, payload in events:
        if name not in _SHAPE_EVENTS:
            continue
        for key, value in payload.items():
            if key == "tool_call_id" or not isinstance(value, str):
                continue
            checked += 1
            assert value in _ALLOWED_STRINGS or _INTENT_ID.match(value), (
                f"{name}.{key} = {value!r} is not one of the closed shapes 06 "
                "declares for this event family"
            )
        assert "description" not in payload
    assert checked > 5, "the shape check proved nothing — no string values were seen"


def test_a_rogue_payload_key_never_reaches_a_span() -> None:
    """"An emitter that adds an unexpected key" — the failure mode the allowlist
    exists for, exercised directly. It is deliberately NOT a type filter: a bare
    `isinstance` check leaked `loop_paused_ask_user`'s `question` once. So a new
    emit site that forgets D25 and sends `description`/`sql`/`column_scope` is
    silently dropped rather than exported."""
    provider, exporter = _provider()
    observer = tracing.guardrail_observer(tracing.get_tracer(provider))

    observer(
        "loop_analysis_state_transition",
        {
            "intent_id": "i1",
            "to_status": "blocked",
            "description": DESC_ONE,
            "sql": SQL,
            "column_scope": [SCOPE_COLUMN],
            "question": QUESTION,
        },
    )

    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes or {})
    assert {k: v for k, v in attrs.items() if not k.startswith("openinference.")} == {
        "intent_id": "i1",
        "to_status": "blocked",
    }
    blob = json.dumps([span.name, attrs], default=str)
    for canary in (DESC_ONE, SQL, SCOPE_COLUMN, QUESTION):
        assert canary not in blob


def test_the_allowlist_does_not_contain_description() -> None:
    """The one key that must never be added. Asserted structurally so a future
    "just add the description, it is useful for debugging" diff fails here with a
    reason attached, rather than in a canary scan that only fires if the fixture
    text happens to be scanned for."""
    assert "description" not in tracing._GUARDRAIL_OBSERVER_ATTR_ALLOWLIST  # noqa: SLF001
    assert "sql" not in tracing._GUARDRAIL_OBSERVER_ATTR_ALLOWLIST  # noqa: SLF001
    assert "question" not in tracing._GUARDRAIL_OBSERVER_ATTR_ALLOWLIST  # noqa: SLF001
    assert "column_scope" not in tracing._GUARDRAIL_OBSERVER_ATTR_ALLOWLIST  # noqa: SLF001
