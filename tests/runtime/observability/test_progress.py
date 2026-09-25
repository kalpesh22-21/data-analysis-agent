"""Unit tests for observability/progress.py (Layer 1, pure/async — no infra)."""

from __future__ import annotations

from data_agent.runtime.observability.progress import (
    ProgressEmitter,
    combine_observers,
    to_progress_event,
)


def test_to_progress_event_renders_known_events() -> None:
    event = to_progress_event("tool_dispatch_start", {"tool_name": "runQuery"})
    assert event is not None
    assert event.step == "finding the requested information…"
    assert event.shape == {"tool_name": "runQuery"}


def test_to_progress_event_unknown_event_returns_none() -> None:
    assert to_progress_event("some_internal_event", {"sql": "SELECT 1"}) is None


def test_to_progress_event_never_carries_sql_or_credentials() -> None:
    payload = {
        "tool_name": "runQuery",
        "sql": "SELECT * FROM employee WHERE Name = 'Jane Doe'",
        "jwt": "secret-jwt-value",
        "session_id": "sess-1",
        "column_scope": ["dbpcm_warehouse.employee.Name"],
    }
    event = to_progress_event("tool_dispatch_start", payload)
    assert event is not None
    assert "sql" not in event.shape
    assert "jwt" not in event.shape
    assert "session_id" not in event.shape
    assert "column_scope" not in event.shape
    blob = str(event.shape) + event.step
    assert "Jane Doe" not in blob
    assert "secret-jwt-value" not in blob


def test_to_progress_event_falls_back_to_bare_label_on_missing_placeholder() -> None:
    event = to_progress_event("tool_dispatch_start", {})
    assert event is not None
    assert event.step == "working on the next part of your question…"  # no KeyError raised


def test_tool_progress_summary_uses_summary_verbatim_bypassing_labels() -> None:
    # The value-rich LLM line goes straight into `step` (verbatim, D25-relaxed
    # channel) — not templated from `_STEP_LABELS`, not allowlist-stripped. The
    # machine-readable tool name still rides `shape.tool_name`.
    event = to_progress_event(
        "tool_progress_summary",
        {"summary": "Querying overtime pay by department", "tool_name": "runQuery"},
    )
    assert event is not None
    assert event.step == "Querying overtime pay by department"
    assert event.shape == {"tool_name": "runQuery"}


def test_tool_progress_summary_step_is_never_format_templated() -> None:
    # A `{` in the value-rich text must NOT be `.format()`-ed (would KeyError or
    # mangle) — it is used verbatim.
    event = to_progress_event(
        "tool_progress_summary",
        {"summary": "Filtering rows where {dept} = sales", "tool_name": "runQuery"},
    )
    assert event is not None
    assert event.step == "Filtering rows where {dept} = sales"


def test_tool_progress_summary_drops_blank_or_missing_summary() -> None:
    assert to_progress_event("tool_progress_summary", {"tool_name": "runQuery"}) is None
    assert (
        to_progress_event("tool_progress_summary", {"summary": "   ", "tool_name": "runQuery"})
        is None
    )


def test_tool_progress_summary_shape_stays_allowlisted() -> None:
    # Only allowlisted keys reach `shape`; a stray value key does not.
    event = to_progress_event(
        "tool_progress_summary",
        {"summary": "Reading employee table", "tool_name": "getTableSchema", "sql": "SECRET"},
    )
    assert event is not None
    assert "sql" not in event.shape
    assert event.shape == {"tool_name": "getTableSchema"}


async def test_progress_emitter_streams_events_until_closed() -> None:
    emitter = ProgressEmitter()
    emitter.observe("tool_dispatch_start", {"tool_name": "listDatabases"})
    emitter.observe("loop_turn_done", {"tool_calls_made": 1})
    emitter.close()

    events = [e async for e in emitter.stream()]
    assert [e.step for e in events] == ["checking what information is available…", "done"]


async def test_progress_emitter_drops_unknown_events_silently() -> None:
    emitter = ProgressEmitter()
    emitter.observe("some_internal_event", {"x": 1})
    emitter.close()

    events = [e async for e in emitter.stream()]
    assert events == []


async def test_progress_emitter_ignores_observe_after_close() -> None:
    emitter = ProgressEmitter()
    emitter.close()
    emitter.observe("tool_dispatch_start", {"tool_name": "runQuery"})  # must not hang stream()

    events = [e async for e in emitter.stream()]
    assert events == []


def test_combine_observers_fans_out_to_all() -> None:
    seen_a: list[tuple[str, dict]] = []
    seen_b: list[tuple[str, dict]] = []

    combined = combine_observers(
        lambda e, p: seen_a.append((e, p)), lambda e, p: seen_b.append((e, p))
    )
    combined("tool_dispatch_start", {"tool_name": "runQuery"})

    assert seen_a == [("tool_dispatch_start", {"tool_name": "runQuery"})]
    assert seen_b == seen_a


def test_all_dispatch_stages_hide_internal_names_even_without_summary_model() -> None:
    for tool in (
        "searchBlueprints",
        "getBlueprint",
        "runBlueprint",
        "runQuery",
        "getTableSchema",
        "unknownInternalTool",
    ):
        for stage in ("start", "ok", "denied", "error"):
            event = to_progress_event(f"tool_dispatch_{stage}", {"tool_name": tool})
            assert event is not None
            assert tool not in event.step
            assert "blueprint" not in event.step.lower()
            assert event.shape["tool_name"] == tool


async def test_summary_start_and_completion_remain_distinct_events():
    emitter = ProgressEmitter()
    emitter.observe(
        "tool_progress_summary",
        {
            "tool_name": "runBlueprint",
            "tool_call_id": "headcount",
            "summary": "Counting active staff by department for September",
        },
    )
    emitter.observe(
        "tool_dispatch_start", {"tool_name": "runBlueprint", "tool_call_id": "headcount"}
    )
    # An independent call still gets its start, and completion/failure stays visible.
    emitter.observe(
        "tool_dispatch_start", {"tool_name": "getTableSchema", "tool_call_id": "salary"}
    )
    emitter.observe(
        "tool_dispatch_error", {"tool_name": "runBlueprint", "tool_call_id": "headcount"}
    )
    emitter.close()
    events = [event async for event in emitter.stream()]
    assert len(events) == 4
    assert events[0].step == "Counting active staff by department for September"
    assert events[1].shape["tool_call_id"] == "headcount"
    assert events[2].shape["tool_call_id"] == "salary"
    assert "did not finish" in events[3].step
