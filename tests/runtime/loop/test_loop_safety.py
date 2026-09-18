"""RC1–RC6 interaction tests through the real loop, plus shape-only telemetry."""

import json

from data_agent.runtime.dispatch.tool_dispatcher import ToolResult
from data_agent.runtime.loop.loop_safety import HELP_BREAKER_TEXT
from tests.runtime.test_harness_improvements import (
    CREDS,
    Tool,
    batch,
    build,
    call,
    discovery,
    run,
)


def final():
    return batch(
        call(
            "finalizeAnswer",
            "final",
            answer="I could not verify the requested information.",
            tables=[],
            capability_refs=[],
            evidence=[],
        )
    )


async def test_rc1_repeated_search_points_to_serving_call_and_round_then_finishes():
    loop, store, model, _, events = build(
        [
            discovery(),
            batch(call("searchBlueprints", "repeat", query="headcount")),
            final(),
        ]
    )
    out = await run(loop)
    assert out.status == "done"
    entry = next(e for e in await store.load_trail(CREDS.session_id) if e.tool_call_id == "repeat")
    assert "Serving result_id: s; serving round: 1" in entry.denial_detail
    assert entry.result_full_ref is None
    assert any(e == "loop_repeated_idempotent_read_guarded" for e, _ in events)
    assert "Serving result_id: s; serving round: 1" in json.dumps(model.calls[-1].messages)


async def test_rc2_mutating_schema_arguments_hits_name_ceiling_and_declines():
    loop, store, _, _, events = build(
        [
            *[batch(call("getTableSchema", f"schema{i}", table=f"table{i}")) for i in range(5)],
            final(),
        ],
        extra={"getTableSchema": Tool("getTableSchema", {"found": True})},
    )
    loop._max_no_progress_rounds = 10  # Isolate RC2 from the independent zero-gain backstop.
    out = await run(loop)
    assert out.status == "done"
    entries = [
        e for e in await store.load_trail(CREDS.session_id) if e.tool_name == "getTableSchema"
    ]
    assert [e.status for e in entries] == ["ok"] * 3 + ["error"] * 2
    assert all(e.error_code == "READ_REFETCH_LIMIT" for e in entries[3:])
    limits = [p for e, p in events if e == "loop_read_refetch_limit"]
    assert [p["phase"] for p in limits] == ["coach", "decline"]
    assert all(p["attempts"] == p["limit"] == 3 for p in limits)


async def test_rc3_identical_reads_stop_early_and_persist_honest_notice():
    loop, store, _, _, events = build(
        [
            *[
                batch(call("searchBlueprints", "functions.searchBlueprints:0", query="headcount"))
                for _ in range(10)
            ],
        ]
    )
    out = await run(loop)
    assert out.status == "stopped_no_progress"
    assert out.pending_question is None
    assert "no longer adding information" in out.assistant_text
    stops = [p for e, p in events if e == "loop_no_new_evidence_stop"]
    assert stops == [{"iteration": 5, "window": 1, "stagnant_rounds": 4, "exit": "no_progress"}]
    assert not any(e in {"loop_paused_budget_cap", "loop_hard_ceiling_stop"} for e, _ in events)
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.messages[-1].content == out.assistant_text
    assert sum(e.tool_name == "searchBlueprints" and not e.error_code for e in doc.tool_trail) == 1


async def test_rc3_progressing_control_of_same_length_survives_with_rename_events():
    steps = [discovery()]
    for i in range(6):
        steps.append(
            batch(
                call(
                    "runQuery",
                    "functions.runQuery:0",
                    sql=f"SELECT count() + {i} AS n FROM hr.employee",
                )
            )
        )
    steps.append(
        batch(
            call(
                "finalizeAnswer",
                "final",
                answer="The count is 125.",
                tables=[],
                capability_refs=[],
                evidence=["functions.runQuery:0#5"],
            )
        )
    )
    loop, store, _, mcp, events = build(
        steps, rows=[{"columns": ["n"], "rows": [[120 + i]], "row_count": 1} for i in range(6)]
    )
    out = await run(loop)
    assert out.status == "done"
    assert not any(e == "loop_no_new_evidence_stop" for e, _ in events)
    assert len([e for e, _ in events if e == "loop_tool_call_id_reminted"]) == 5
    assert len({json.dumps(c.args, sort_keys=True) for c in mcp.calls}) == len(mcp.calls)


class DownHelp:
    def __init__(self):
        self.calls = 0

    async def run(self, *args, **kwargs):
        self.calls += 1
        return ToolResult(
            "error",
            "searchHelpCenter",
            "HELP_CENTER_UNAVAILABLE",
            True,
            "Temporarily unavailable",
            frozenset(),
            None,
            None,
        )


async def test_rc4_breaker_limits_dispatch_removes_schema_and_survives_resume():
    down = DownHelp()
    loop, store, model, _, events = build(
        [
            *[batch(call("searchHelpCenter", f"hc{i}", query="setup")) for i in range(3)],
            batch(
                call("askUser", "pause", question="Which period?", options=["Current", "Previous"])
            ),
            batch(call("searchHelpCenter", "hc-after", query="setup again")),
            final(),
        ],
        extra={"searchHelpCenter": down},
    )

    async def schemas(_):
        return [{"type": "function", "name": "searchHelpCenter", "parameters": {}}]

    loop._tools_provider = schemas
    out = await run(loop)
    assert out.status == "paused_ask_user"
    out = await loop.resume(session_id=CREDS.session_id, credentials=CREDS, answer="Current")
    assert out.status == "done"
    assert down.calls == 2
    assert len([e for e, _ in events if e == "loop_help_center_circuit_opened"]) == 1
    for request in model.calls[2:]:
        assert all(t.get("name") != "searchHelpCenter" for t in request.tools)
        assert any(m.get("content") == HELP_BREAKER_TEXT for m in request.messages)
    trail = await store.load_trail(CREDS.session_id)
    assert (
        next(e for e in trail if e.tool_call_id == "hc-after").error_code
        == "HELP_CENTER_CIRCUIT_OPEN"
    )


async def test_rc6_ask_user_drop_names_only_and_has_no_execution():
    loop, store, _, mcp, events = build(
        [
            batch(
                call("runQuery", "deferred", sql="SELECT count() FROM hr.employee"),
                call("askUser", "ask", question="Which period?", options=["Current", "Previous"]),
            ),
        ]
    )
    out = await run(loop)
    assert out.status == "paused_ask_user"
    event = next(p for e, p in events if e == "loop_ask_user_batch_calls_dropped")
    assert json.loads(event["dropped_tool_names"]) == ["runQuery"]
    assert json.loads(event["dropped_tool_call_ids"]) == ["deferred"]
    assert event["iteration"] == 1 and event["dropped_count"] == 1
    assert mcp.calls == []
    deferred = next(
        e for e in await store.load_trail(CREDS.session_id) if e.tool_call_id == "deferred"
    )
    assert deferred.error_code == "TOOL_NOT_EXECUTED"
    assert deferred.result_full_ref is None
    assert "SELECT" not in json.dumps(event)


async def test_rc6_event_reaches_progress_stream_and_exported_span():
    from data_agent.runtime.observability.progress import ProgressEmitter, combine_observers
    from data_agent.runtime.observability.tracing import get_tracer, guardrail_observer
    from tests.runtime.observability.test_tracing import _provider_with_memory_exporter

    provider, exporter = _provider_with_memory_exporter()
    progress = ProgressEmitter()
    loop, _, _, _, _ = build(
        [
            batch(
                call("getTableSchema", "later", table="employee"),
                call("askUser", "ask", question="Which period?", options=["Current", "Previous"]),
            ),
        ]
    )
    loop._observer = combine_observers(progress.observe, guardrail_observer(get_tracer(provider)))
    await run(loop)
    progress.close()
    events = [e async for e in progress.stream()]
    event = next(e for e in events if "dropped_tool_call_ids" in e.shape)
    assert json.loads(event.shape["dropped_tool_call_ids"]) == ["later"]
    span = next(
        s for s in exporter.get_finished_spans() if s.name == "loop_ask_user_batch_calls_dropped"
    )
    assert span.attributes["dropped_tool_call_ids"] == '["later"]'
    assert span.attributes["dropped_tool_names"] == '["getTableSchema"]'


async def test_rc3_stop_resolves_pending_intents_without_claiming_data_absent():
    loop, store, _, _, _ = build(
        [
            batch(
                call(
                    "updateAnalysisState",
                    "state",
                    intents=[{"description": "Active employees"}, {"description": "Pay summary"}],
                )
            ),
            *[batch(call("searchBlueprints", f"s{i}", query="headcount")) for i in range(8)],
        ]
    )
    out = await run(loop)
    assert out.status == "stopped_no_progress"
    doc = await store.get_or_create_session(CREDS.session_id)
    assert all(i.status == "blocked" for i in doc.analysis_state.intents)
    assert "no longer adding information" in out.assistant_text


def test_rc4_success_resets_consecutive_failures_and_other_tools_do_not():
    from data_agent.runtime.loop.loop_safety import LoopSafety

    events = []
    safety = LoopSafety(
        read_limit=3,
        no_progress_limit=4,
        help_failure_limit=2,
        observer=lambda e, p: events.append((e, p)),
    )
    safety.observe_help("searchHelpCenter", "HELP_CENTER_UNAVAILABLE")
    safety.observe_help("searchHelpCenter", None)
    safety.observe_help("searchHelpCenter", "HELP_CENTER_UNAVAILABLE")
    assert not safety.help_open
    safety.observe_help("runQuery", None)
    safety.observe_help("getHelpCenterDocument", "HELP_CENTER_UNAVAILABLE")
    assert safety.help_open
    assert safety.check("getHelpCenterDocument").error_code == "HELP_CENTER_CIRCUIT_OPEN"
    assert len(events) == 1


def test_rc_guard_events_export_shape_fields_without_message_payloads():
    from data_agent.runtime.observability.progress import to_progress_event
    from tests.runtime.observability.test_tracing import _observed_attributes

    cases = [
        (
            "loop_tool_call_id_reminted",
            {"old_id": "functions.read:0", "new_id": "functions.read:0#1"},
        ),
        ("loop_read_refetch_limit", {"attempts": 3, "limit": 3, "phase": "coach"}),
        ("loop_help_center_circuit_opened", {"failures": 2, "limit": 2}),
        ("loop_no_new_evidence_stop", {"stagnant_rounds": 4, "iteration": 5}),
    ]
    for name, shape in cases:
        payload = {**shape, "sql": "secret-sql", "answer": "secret-answer", "jwt": "secret-key"}
        attributes = _observed_attributes(name, payload)
        assert all(attributes[k] == v for k, v in shape.items())
        progress = to_progress_event(name, payload)
        assert all(progress.shape[k] == v for k, v in shape.items())
        assert "secret-" not in json.dumps(attributes)
        assert "secret-" not in json.dumps(progress.shape)
