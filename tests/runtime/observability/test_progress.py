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
    assert event.step == "running runQuery…"
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
    assert event.step == "running {tool_name}…"  # no KeyError raised


async def test_progress_emitter_streams_events_until_closed() -> None:
    emitter = ProgressEmitter()
    emitter.observe("tool_dispatch_start", {"tool_name": "listDatabases"})
    emitter.observe("loop_turn_done", {"tool_calls_made": 1})
    emitter.close()

    events = [e async for e in emitter.stream()]
    assert [e.step for e in events] == ["running listDatabases…", "done"]


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
