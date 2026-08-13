"""Raw routing telemetry for `analysisState` (Release 1, doc 06).

EMIT NOW, PROJECT LATER. Nothing in Release 1 reads these events except the
Layer-4 eval harness (07), so the only thing that can protect them from drifting
is a test that pins each event to its site and its payload shape.

Two kinds of assertion here, and the second is the one that matters most:

  1. each event fires exactly once at its site with the expected keys;
  2. **no `description` string reaches any payload.** `intent_id` is
     runtime-assigned and carries no user content; `description` is MODEL-AUTHORED
     FROM THE USER'S QUESTION and is never safe. The scan below walks every
     payload of every event a turn emits, looking for the fixture's intent text —
     the same technique `test_tool_dispatcher.py` uses to scan every `ToolResult`
     field for the JWT, for the same reason.

The two mechanisms are independent: the emitters never put `description` on a
payload, and `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST` would drop it if one ever did
(`tests/runtime/observability/test_tracing.py`).

WHAT IS DELIBERATELY *NOT* HERE (06, "the inferred corpus-gap signal"): no
loop-side detector for either derived signal, and no counter for a fact the
existing `tool_dispatch_*` events already carry.

  * CORPUS GAP — "a turn ran `searchBlueprints` and then completed the intent on
    `runQuery` evidence" is `loop_intent_completed{evidence_tool_name}` joined to
    `tool_dispatch_ok{tool_name}`. Inferred from what happened rather than
    declared by the model, which is the stronger form of the same fact and needs
    no field.
  * RE-DERIVATION AFTER AN AUTHORITATIVE RESULT — a `runQuery` dispatched after a
    `runBlueprint` whose trail entry is `authoritative`, within one turn, is
    already visible in the trail.

Both are derived in the Layer-4 harness (07). A loop-side detector for the second
would have to know WHICH INTENT a query serves, which nothing in the runtime does.
"""

from __future__ import annotations

import json
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import UpdateAnalysisStateTool
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.observability.tracing import DEFAULT_DROP_SPAN_NAMES
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

SESSION_ID = "sess-analysis-telemetry"
_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})

STATE = "updateAnalysisState"
ANSWER = "answerWithTable"

# The D25 canary. These are the ONLY place these strings appear, so any occurrence
# in a telemetry payload is a leak of model-authored text derived from the user's
# question — the exact thing 06 forbids.
LEAK_ONE = "zzcanaryone headcount by department"
LEAK_TWO = "zzcanarytwo attrition by cohort"
CANARIES = ("zzcanaryone", "zzcanarytwo")

# Every `loop_*` event this deliverable is responsible for, with the payload keys
# each must carry. Written out rather than derived, because the point is to notice
# when an emitter's shape changes.
EXPECTED_KEYS: dict[str, set[str]] = {
    "loop_analysis_state_initialized": {"intent_count", "turn_index"},
    "loop_analysis_state_transition": {
        "intent_id",
        "from_status",
        "to_status",
        "reason_code",
    },
    # `evidence_binding` (call-time tagging): HOW the binding was established —
    # `tagged` (the model named the intent on the call) or `auto_bound` (the model
    # named nothing and the runtime found the one call that could have served it).
    # Route derivation reads `evidence_tool_name`; this says how much to trust the
    # join, and `auto_bound` is the weaker of the two by construction.
    "loop_intent_completed": {"intent_id", "evidence_tool_name", "evidence_binding"},
    # The block-side mirror. `evidence_tool_name` is what separates a NO_ACCESS
    # earned on the user's own data from one manufactured by a single
    # `getTableSchema(<scratch_db>, …)` probe (04 §B.4) — without it the two are
    # telemetrically identical, and the release's mitigation for that hole IS
    # measurement. Distinct from `loop_intent_force_blocked`, which is 05's
    # runtime-forced counterpart and has no evidence at all.
    "loop_intent_blocked": {
        "intent_id",
        "reason_code",
        "evidence_tool_name",
        "evidence_binding",
    },
    "loop_evidence_reused": {"intent_id", "tool_call_id"},
    "loop_zero_row_block": {"intent_id"},
    "loop_zero_row_completion": {"intent_id"},
    "loop_analysis_state_rejected": {"reason", "intent_count"},
    "loop_analysis_state_late_init_rejected": {"proposed_count", "blocking_tool_name"},
    "loop_metadata_evidence_completion": {"intent_id"},
    "loop_finalization_refused": {"exit", "pending_count"},
    "loop_finalization_block_spent": {"window"},
    # Degrade-not-fail, never silently: the claim's CAS write failed against a real
    # store and the turn finalized anyway. Every key is allowlisted in
    # `observability/tracing.py`, so the span carries something.
    "loop_finalization_block_claim_failed": {"turn_index", "window", "reason"},
    "loop_intent_force_blocked": {"intent_id", "reason_code"},
    "loop_enforcement_exhausted": {"intent_count"},
}


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return []


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=frozenset())


def _build(
    turns: list[ModelTurnResult],
    *,
    mcp: FakeMCPClient | None = None,
    max_loop_iterations: int = 15,
    max_budget_windows: int = 3,
) -> tuple[AgentLoop, InMemorySessionStore, list[tuple[str, dict[str, Any]]]]:
    store = InMemorySessionStore()
    events: list[tuple[str, dict[str, Any]]] = []

    def _observe(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    loop = AgentLoop(
        model_client=ScriptedModelClient(turns),
        tool_dispatcher=ToolDispatcher(mcp or FakeMCPClient(), CATALOG, observer=_observe),
        context_assembler=ContextAssembler(store, history_token_budget=100_000),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=max_loop_iterations,
        max_wall_clock_seconds=60,
        max_budget_windows=max_budget_windows,
        observer=_observe,
        runtime_tools={
            STATE: UpdateAnalysisStateTool(session_store=store, observer=_observe),
            ANSWER: AnswerWithTableTool(),
        },
    )
    return loop, store, events


def _init_call(call_id: str, *descriptions: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name=STATE,
        arguments={"intents": [{"description": d} for d in descriptions]},
    )


def _update_call(call_id: str, *updates: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=STATE, arguments={"intents": list(updates)})


def _query_call(call_id: str) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name="runQuery", arguments={"sql": f"SELECT '{call_id}'"})


def _answer_call(call_id: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id, name=ANSWER,
        arguments={"answer": "Here it is.", "tables": [{"sql": "SELECT 1"}]},
    )


def _query_mcp(count: int = 10, *, row_count: int = 1) -> FakeMCPClient:
    return FakeMCPClient(
        scripted={
            "runQuery": [
                {
                    "columns": ["x"],
                    "rows": [[1]] * row_count,
                    "row_count": row_count,
                    "truncated": False,
                }
            ]
            * count
        }
    )


def _named(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in events if event == name]


# --- the 05-side events -----------------------------------------------------


async def test_the_finalization_events_fire_at_their_sites_with_the_expected_keys() -> None:
    """`loop_finalization_refused`, `loop_finalization_block_spent`,
    `loop_intent_force_blocked`, `loop_enforcement_exhausted` — plus the
    `loop_analysis_state_transition` emitted by 05's own forcing path, which is the
    one 03's tool cannot emit because the runtime, not the model, made the change."""
    loop, _store, events = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", LEAK_ONE)]),
            ModelTurnResult(assistant_text="a first draft"),
            ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a1")]),
            ModelTurnResult(assistant_text="giving up"),
        ]
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    assert _named(events, "loop_finalization_refused") == [
        {"exit": "no_tool_calls", "pending_count": 1}
    ]
    assert _named(events, "loop_finalization_block_spent") == [{"window": 1}]
    assert _named(events, "loop_enforcement_exhausted") == [{"intent_count": 1}]
    assert _named(events, "loop_intent_force_blocked") == [
        {"intent_id": "i1", "reason_code": "ENFORCEMENT_EXHAUSTED"}
    ]
    # 05's forcing path emits the SAME transition shape 03's tool does, so the
    # projector needs no special case for a runtime-made change.
    assert {
        "intent_id": "i1",
        "from_status": "pending",
        "to_status": "blocked",
        "reason_code": "ENFORCEMENT_EXHAUSTED",
    } in _named(events, "loop_analysis_state_transition")


async def test_exit_two_refusal_names_its_exit() -> None:
    """`exit` is a closed enum of the two terminal exits — the harness reads it to
    tell "the model wrote prose" from "the model called answerWithTable"."""
    loop, _store, events = _build(
        [
            ModelTurnResult(assistant_text=None, tool_calls=[_init_call("s1", LEAK_ONE)]),
            ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a1")]),
            ModelTurnResult(assistant_text="ok"),
        ]
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    assert _named(events, "loop_finalization_refused") == [
        {"exit": "answer_with_table", "pending_count": 1}
    ]


async def test_every_emitted_analysis_state_event_carries_exactly_its_declared_keys() -> None:
    """One turn exercising the 03 tool's events and 05's together: initialize,
    a completion on a `runQuery` (which is what makes the ROUTE derivable), evidence
    reuse across two intents, a rejection, and the forced block."""
    loop, _store, events = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_init_call("s1", LEAK_ONE, LEAK_TWO), _query_call("q1")],
            ),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    # i1 and i2 both cite q1 — permitted for completion, flagged.
                    _update_call(
                        "s2",
                        {
                            "intent_id": "i1",
                            "status": "completed",
                            "evidence_tool_call_id": "q1",
                        },
                        {
                            "intent_id": "i2",
                            "status": "completed",
                            "evidence_tool_call_id": "q1",
                        },
                    ),
                    # ...and a malformed call, so the rejection event fires too.
                    ToolCallRequest(id="s3", name=STATE, arguments={"nope": 1}),
                ],
            ),
            ModelTurnResult(assistant_text="both answered"),
        ],
        mcp=_query_mcp(),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    seen = {name for name, _ in events if name in EXPECTED_KEYS}
    assert {
        "loop_analysis_state_initialized",
        "loop_analysis_state_transition",
        "loop_intent_completed",
        "loop_evidence_reused",
        "loop_analysis_state_rejected",
    } <= seen
    for name, payload in events:
        if name in EXPECTED_KEYS:
            assert set(payload) == EXPECTED_KEYS[name], f"{name} payload shape changed"
    assert _named(events, "loop_analysis_state_initialized") == [
        {"intent_count": 2, "turn_index": 0}
    ]
    # `evidence_tool_name` is what makes the route derivable: runBlueprint =>
    # blueprint, runQuery => ad-hoc, getTableSchema => metadata.
    assert all(
        payload["evidence_tool_name"] == "runQuery"
        for payload in _named(events, "loop_intent_completed")
    )
    assert _named(events, "loop_analysis_state_rejected") == [
        {"reason": "unknown_top_level_key", "intent_count": 0}
    ]


# --- the D25 regression guard ----------------------------------------------


def _scan(events: list[tuple[str, dict[str, Any]]]) -> str:
    """Every emitted payload, rendered as one scannable blob — values AND keys."""
    return json.dumps([[name, payload] for name, payload in events], default=str)


async def test_no_intent_description_reaches_any_telemetry_payload() -> None:
    """THE D25 REGRESSION GUARD. Scans EVERY payload of EVERY event a rich turn
    emits — not a hand-picked list of the events thought to be risky, which is the
    shape of hole that let `loop_paused_ask_user`'s `question` through once."""
    loop, _store, events = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_init_call("s1", LEAK_ONE, LEAK_TWO), _query_call("q1")],
            ),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _update_call(
                        "s2",
                        {
                            "intent_id": "i1",
                            "status": "completed",
                            "evidence_tool_call_id": "q1",
                        },
                    ),
                    # An over-long description: the rejection path builds its own
                    # message from the payload, so it is a plausible leak site.
                    ToolCallRequest(
                        id="s3",
                        name=STATE,
                        arguments={"intents": [{"description": LEAK_TWO * 40}]},
                    ),
                ],
            ),
            # ...and a refused finalization, so 05's events are in the scan too.
            ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a1")]),
            ModelTurnResult(assistant_text="done"),
        ],
        mcp=_query_mcp(),
    )

    await loop.run(
        session_id=SESSION_ID,
        credentials=_credentials(),
        user_message=f"give me {LEAK_ONE} and {LEAK_TWO}",
    )

    blob = _scan(events)
    assert len(events) > 10, "the scan proved nothing — no events were emitted"
    for canary in CANARIES:
        assert canary not in blob, (
            f"an intent description leaked into telemetry: {canary!r} appears in an "
            "observer payload. `description` is model-authored from the user's "
            "question and must never be emitted (06, D25)."
        )
    # The question text itself is the same class of content, on the same events.
    assert "give me" not in blob


async def test_the_zero_row_pair_is_emitted_for_the_ratio() -> None:
    """04 §B.4: an empty result set treated as a BLOCK versus as an ANSWER. Zero
    rows fires on honest work too, so this pair is how the skew is measured rather
    than assumed."""
    loop, _store, events = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_init_call("s1", LEAK_ONE, LEAK_TWO), _query_call("q1")],
            ),
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    _update_call(
                        "s2",
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
                    )
                ],
            ),
            ModelTurnResult(assistant_text="none found"),
        ],
        mcp=_query_mcp(row_count=0),
    )

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    assert _named(events, "loop_zero_row_completion") == [{"intent_id": "i1"}]
    assert _named(events, "loop_zero_row_block") == [{"intent_id": "i2"}]
    # Through the real loop, the block names the tool that produced its evidence
    # — the same join key the completion event carries.
    assert _named(events, "loop_intent_blocked") == [
        {
            "intent_id": "i2",
            "reason_code": "REQUIRED_DATA_UNAVAILABLE",
            "evidence_tool_name": "runQuery",
            "evidence_binding": "auto_bound",
        }
    ]


def test_no_analysis_state_event_is_dropped_by_the_default_span_filter() -> None:
    """A different mechanism from the attribute allowlist, one layer later:
    `OTLP_DROP_SPAN_NAMES` filters span NAMES before export. Passing it says
    nothing about whether the attributes survived — and vice versa — so both are
    checked."""
    assert DEFAULT_DROP_SPAN_NAMES == frozenset(
        {"context.assembly", "loop_model_call_start", "loop_turn_done"}
    )
    for name in EXPECTED_KEYS:
        assert name not in DEFAULT_DROP_SPAN_NAMES


def test_every_event_name_is_a_loop_event() -> None:
    """`_tracing_observer` IGNORES non-`loop_` events, so an event named otherwise
    reaches the SSE progress stream and nothing else — invisible in Phoenix."""
    for name in EXPECTED_KEYS:
        assert name.startswith("loop_")
