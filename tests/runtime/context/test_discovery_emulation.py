"""Unit tests for context/discovery_emulation.py — the emulated listDatabases+
listTables sweep that injects synthetic assistant/tool pairs as if the model had
already made those discovery calls.

Covers: the happy path builds a listDatabases entry + exactly ONE listTables entry
(for the base database — NOT one per database returned) with the correct
tool_name/args/tool_call_id and a `result_preview` that is byte-identical to a REAL
replayed read (`context/budget.py::_render_entry`); `read_signatures` seed the loop's
guard 1:1 with the entries; a listDatabases denial/empty degrades to an empty result;
a base-database listTables denial (or a base database absent from the listDatabases
result) still injects the listDatabases pair on its own; and the sweep never raises.
"""

from __future__ import annotations

import asyncio
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.budget import _render_entry
from data_agent.runtime.context.discovery_emulation import (
    EmulatedDiscovery,
    EmulatedDiscoveryCache,
    build_emulated_discovery,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolResult
from data_agent.runtime.loop.read_guard import idempotent_read_signature
from data_agent.runtime.session.models import ResultPreview, TrailEntry

_CREDS = RuntimeCredentials(session_id="s1", jwt="jwt-secret", column_scope=frozenset())


def _list_preview(payload: list[dict[str, Any]]) -> ResultPreview:
    """The `_build_preview` shape a real dispatcher produces for a bare-list
    (listDatabases/listTables) result: no columns, one preview row per item."""
    return ResultPreview(
        columns=[],
        row_count=len(payload),
        truncated=False,
        preview_rows=[[item] for item in payload],
    )


def _ok(tool_name: str, payload: list[dict[str, Any]]) -> ToolResult:
    return ToolResult(
        status="ok",
        tool_name=tool_name,
        error_code=None,
        retryable=None,
        user_message=None,
        provenance=frozenset(),
        result_preview=_list_preview(payload),
        result_full=payload,
    )


def _denied(tool_name: str, code: str = "PERMISSION_DENIED") -> ToolResult:
    return ToolResult(
        status="denied",
        tool_name=tool_name,
        error_code=code,
        retryable=False,
        user_message="Access denied.",
        provenance=None,
        result_preview=None,
        result_full=None,
    )


class _StubDispatcher:
    """Records dispatched calls and returns scripted `ToolResult`s keyed by
    `(tool_name, database)` — mirrors the real dispatcher's `dispatch` signature."""

    def __init__(self, responses: dict[tuple[str, str | None], ToolResult]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def dispatch(
        self,
        tool_name: str,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        *,
        emit_progress: bool = True,
    ) -> ToolResult:
        # The sweep is synthetic context replay, not work the user asked for, so
        # neither dispatch may emit UI progress (see the progress test at the end
        # of this file). Asserted here so EVERY scripted path enforces it.
        assert emit_progress is False, "the discovery sweep leaked UI progress"
        self.calls.append((tool_name, dict(model_args)))
        db = model_args.get("database")
        return self._responses[(tool_name, db)]


def _expected_rendered(
    tool_call_id: str, tool_name: str, args: dict[str, Any], result: ToolResult
) -> dict[str, Any]:
    """The EXACT dict a real replayed read renders to — proves the emulated entry
    is byte-identical in shape to what `context/assembly.py` produces from a trail."""
    entry = TrailEntry(
        turn_index=0,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        args=args,
        status=result.status,
        error_code=result.error_code,
        provenance=result.provenance,
        result_preview=result.result_preview,
        result_full_ref=None,
        ts="1970-01-01T00:00:00+00:00",
    )
    return _render_entry(entry, 20)


async def test_happy_path_lists_tables_for_the_base_database_only() -> None:
    # BREADTH: `listTables` is emulated for the ONE base database, NOT for every
    # database `listDatabases` returned. The others on a live warehouse are not
    # analysis surface, so sweeping them spent an MCP round-trip per turn to inject
    # listings the model should never query from.
    db_result = _ok("listDatabases", [{"name": "warehouse_a"}, {"name": "warehouse_b"}])
    tables_a = _ok(
        "listTables",
        [{"database": "warehouse_a", "name": "employee", "engine": "MergeTree"}],
    )
    tables_b = _ok(
        "listTables",
        [
            {"database": "warehouse_b", "name": "payroll", "engine": "MergeTree"},
            {"database": "warehouse_b", "name": "audit", "engine": "MergeTree"},
        ],
    )
    dispatcher = _StubDispatcher(
        {
            ("listDatabases", None): db_result,
            ("listTables", "warehouse_a"): tables_a,
            ("listTables", "warehouse_b"): tables_b,
        }
    )

    discovery = await build_emulated_discovery(
        dispatcher, _CREDS, base_database="warehouse_a"
    )

    # listDatabases (in full) + exactly ONE listTables, for the base database.
    assert [e["tool_name"] for e in discovery.entries] == ["listDatabases", "listTables"]
    assert [e["tool_call_id"] for e in discovery.entries] == [
        "emulated-listDatabases",
        "emulated-listTables-warehouse_a",
    ]
    assert [e["args"] for e in discovery.entries] == [{}, {"database": "warehouse_a"}]

    # The non-base database was never dispatched at all — this is the round-trip
    # the narrowing exists to save, so assert on the CALLS, not just the entries.
    assert dispatcher.calls == [
        ("listDatabases", {}),
        ("listTables", {"database": "warehouse_a"}),
    ]

    # Each entry is byte-identical to a REAL replayed read's rendered shape,
    # including the result_preview sub-shape (status/error_code/user_message).
    assert discovery.entries[0] == _expected_rendered(
        "emulated-listDatabases", "listDatabases", {}, db_result
    )
    assert discovery.entries[1] == _expected_rendered(
        "emulated-listTables-warehouse_a", "listTables", {"database": "warehouse_a"}, tables_a
    )
    # An ok entry carries no denial user_message and a real result_preview.
    assert discovery.entries[1]["user_message"] is None
    assert discovery.entries[1]["status"] == "ok"
    assert discovery.entries[1]["result_preview"]["row_count"] == 1


async def test_read_signatures_match_the_loop_guard_1to1_with_entries() -> None:
    dispatcher = _StubDispatcher(
        {
            ("listDatabases", None): _ok("listDatabases", [{"name": "db1"}]),
            ("listTables", "db1"): _ok("listTables", [{"database": "db1", "name": "t1"}]),
        }
    )

    discovery = await build_emulated_discovery(dispatcher, _CREDS, base_database="db1")

    # Exactly the guard signatures the loop computes for a model re-call.
    assert discovery.read_signatures == {
        idempotent_read_signature("listDatabases", {}),
        idempotent_read_signature("listTables", {"database": "db1"}),
    }
    # One signature per injected entry.
    assert len(discovery.read_signatures) == len(discovery.entries)


async def test_list_databases_denied_returns_empty_result() -> None:
    dispatcher = _StubDispatcher({("listDatabases", None): _denied("listDatabases")})

    discovery = await build_emulated_discovery(dispatcher, _CREDS)

    assert discovery == EmulatedDiscovery()
    assert discovery.entries == []
    assert discovery.read_signatures == set()
    # It never attempted listTables once discovery was denied.
    assert [c[0] for c in dispatcher.calls] == ["listDatabases"]


async def test_list_databases_empty_returns_empty_result() -> None:
    dispatcher = _StubDispatcher({("listDatabases", None): _ok("listDatabases", [])})

    discovery = await build_emulated_discovery(dispatcher, _CREDS)

    assert discovery.entries == []
    assert discovery.read_signatures == set()


async def test_base_db_list_tables_failure_still_injects_the_listdatabases_pair() -> None:
    # A denied `listTables` on the base database must not throw away the
    # `listDatabases` discovery we DID get — the model keeps that and falls back to
    # calling listTables itself. No listTables guard signature is seeded, so that
    # fallback call really reaches the MCP rather than being served locally.
    dispatcher = _StubDispatcher(
        {
            ("listDatabases", None): _ok("listDatabases", [{"name": "base_db"}]),
            ("listTables", "base_db"): _denied("listTables"),
        }
    )

    discovery = await build_emulated_discovery(dispatcher, _CREDS, base_database="base_db")

    assert [e["tool_call_id"] for e in discovery.entries] == ["emulated-listDatabases"]
    assert discovery.read_signatures == {idempotent_read_signature("listDatabases", {})}
    assert idempotent_read_signature("listTables", {"database": "base_db"}) not in (
        discovery.read_signatures
    )


async def test_base_db_absent_from_listdatabases_skips_the_listtables_dispatch() -> None:
    # On a token whose scope (or the server's ALLOWED_DATABASES allowlist) excludes
    # the base database, dispatching listTables anyway would spend a round-trip to
    # earn a denial. Gate on the listDatabases result instead.
    dispatcher = _StubDispatcher(
        {("listDatabases", None): _ok("listDatabases", [{"name": "some_other_db"}])}
    )

    discovery = await build_emulated_discovery(
        dispatcher, _CREDS, base_database="dbpcm_warehouse"
    )

    assert [e["tool_call_id"] for e in discovery.entries] == ["emulated-listDatabases"]
    assert discovery.read_signatures == {idempotent_read_signature("listDatabases", {})}
    # No listTables round-trip was spent at all.
    assert [c[0] for c in dispatcher.calls] == ["listDatabases"]


async def test_never_raises_on_dispatcher_exception() -> None:
    class _ExplodingDispatcher:
        async def dispatch(self, *_a: Any, **_k: Any) -> ToolResult:
            raise RuntimeError("transport blew up")

    events: list[tuple[str, dict[str, Any]]] = []

    # Degrade-not-fail: an unexpected exception is swallowed and returns empty,
    # AND the degrade is observable (shape-only, zero counts, degraded=True).
    discovery = await build_emulated_discovery(
        _ExplodingDispatcher(), _CREDS, observer=lambda e, p: events.append((e, p))
    )
    assert discovery == EmulatedDiscovery()
    assert events == [
        ("discovery_emulated", {"database_count": 0, "table_count": 0, "degraded": True})
    ]


async def test_denied_discovery_emits_degraded_event() -> None:
    # Item 6: a listDatabases denial must not degrade silently — it emits the same
    # shape-only `discovery_emulated` event with zero counts + degraded=True so an
    # MCP blip is observable in a trace.
    dispatcher = _StubDispatcher({("listDatabases", None): _denied("listDatabases")})
    events: list[tuple[str, dict[str, Any]]] = []

    discovery = await build_emulated_discovery(
        dispatcher, _CREDS, observer=lambda e, p: events.append((e, p))
    )

    assert discovery == EmulatedDiscovery()
    assert events == [
        ("discovery_emulated", {"database_count": 0, "table_count": 0, "degraded": True})
    ]


async def test_preview_row_count_is_honored_end_to_end() -> None:
    # Item 2: a non-default preview_row_count (50) must be threaded into the emulated
    # entry so a table listing longer than the default 20 is NOT capped — otherwise
    # tables 21-50 become undiscoverable for the turn while the guard seed blocks the
    # model's same-args re-call.
    tables = [{"database": "db1", "name": f"t{i}"} for i in range(30)]
    dispatcher = _StubDispatcher(
        {
            ("listDatabases", None): _ok("listDatabases", [{"name": "db1"}]),
            ("listTables", "db1"): _ok("listTables", tables),
        }
    )

    discovery = await build_emulated_discovery(
        dispatcher, _CREDS, base_database="db1", preview_row_count=50
    )

    lt_entry = discovery.entries[1]
    assert lt_entry["tool_name"] == "listTables"
    # All 30 rows present (not capped at 20) and not falsely marked truncated.
    assert len(lt_entry["result_preview"]["preview_rows"]) == 30
    assert lt_entry["result_preview"]["truncated"] is False


async def test_default_preview_row_count_caps_at_20() -> None:
    # Contrast to the test above: with the DEFAULT (20) the same 30-table listing IS
    # capped + marked truncated — proving `preview_row_count` is what lifts the cap.
    tables = [{"database": "db1", "name": f"t{i}"} for i in range(30)]
    dispatcher = _StubDispatcher(
        {
            ("listDatabases", None): _ok("listDatabases", [{"name": "db1"}]),
            ("listTables", "db1"): _ok("listTables", tables),
        }
    )

    discovery = await build_emulated_discovery(dispatcher, _CREDS, base_database="db1")

    lt_entry = discovery.entries[1]
    assert len(lt_entry["result_preview"]["preview_rows"]) == 20
    assert lt_entry["result_preview"]["truncated"] is True


async def test_observer_receives_shape_only_event() -> None:
    dispatcher = _StubDispatcher(
        {
            ("listDatabases", None): _ok("listDatabases", [{"name": "db1"}]),
            ("listTables", "db1"): _ok(
                "listTables",
                [{"database": "db1", "name": "t1"}, {"database": "db1", "name": "t2"}],
            ),
        }
    )
    events: list[tuple[str, dict[str, Any]]] = []

    discovery = await build_emulated_discovery(
        dispatcher, _CREDS, base_database="db1", observer=lambda e, p: events.append((e, p))
    )

    assert discovery.entries  # non-empty
    assert events == [("discovery_emulated", {"database_count": 1, "table_count": 2})]


# ---------------------------------------------------------------------------
# EmulatedDiscoveryCache — the ONCE-PER-SESSION memo
# ---------------------------------------------------------------------------


def _nonempty() -> EmulatedDiscovery:
    return EmulatedDiscovery(entries=[{"tool_name": "listDatabases"}], read_signatures={("a", "b")})


async def test_cache_sweeps_once_per_session_and_serves_the_rest() -> None:
    # THE POINT of the cache: `_run_loop_body` is re-entered by run()/resume()/the
    # blueprint approval-resume, so an uncached sweep re-dispatched listDatabases +
    # listTables to the MCP on EVERY budget window.
    cache = EmulatedDiscoveryCache()
    builds = 0

    async def _build() -> EmulatedDiscovery:
        nonlocal builds
        builds += 1
        return _nonempty()

    first = await cache.get_or_build("sess-1", _build)
    second = await cache.get_or_build("sess-1", _build)
    third = await cache.get_or_build("sess-1", _build)

    assert builds == 1
    assert first is second is third


async def test_cache_is_keyed_per_session() -> None:
    cache = EmulatedDiscoveryCache()
    builds = 0

    async def _build() -> EmulatedDiscovery:
        nonlocal builds
        builds += 1
        return _nonempty()

    await cache.get_or_build("sess-1", _build)
    await cache.get_or_build("sess-2", _build)

    assert builds == 2


async def test_cache_does_not_memoize_a_degraded_sweep() -> None:
    # Degrade-not-fail: caching an empty result would disable discovery for the whole
    # remaining session on ONE transient MCP blip. The next window must retry — and
    # once it succeeds, THAT result is the one that sticks.
    cache = EmulatedDiscoveryCache()
    results = [EmulatedDiscovery(), EmulatedDiscovery(), _nonempty()]
    builds = 0

    async def _build() -> EmulatedDiscovery:
        nonlocal builds
        result = results[builds]
        builds += 1
        return result

    assert (await cache.get_or_build("s", _build)).entries == []
    assert (await cache.get_or_build("s", _build)).entries == []
    good = await cache.get_or_build("s", _build)
    assert good.entries  # the retry succeeded
    assert builds == 3

    # Now it is memoized — no fourth build.
    assert await cache.get_or_build("s", _build) is good
    assert builds == 3


async def test_cache_concurrent_windows_of_one_session_build_once() -> None:
    # Double-checked lock: concurrent windows must not each spend the 1+N MCP
    # round-trips. Without the lock all three racers would miss the cache together.
    cache = EmulatedDiscoveryCache()
    builds = 0

    async def _build() -> EmulatedDiscovery:
        nonlocal builds
        builds += 1
        await asyncio.sleep(0)  # yield, so a lock-free impl would interleave
        return _nonempty()

    await asyncio.gather(*(cache.get_or_build("s", _build) for _ in range(3)))

    assert builds == 1


async def test_cache_evicts_fifo_at_max_size() -> None:
    cache = EmulatedDiscoveryCache(max_size=2)
    builds = 0

    async def _build() -> EmulatedDiscovery:
        nonlocal builds
        builds += 1
        return _nonempty()

    await cache.get_or_build("a", _build)
    await cache.get_or_build("b", _build)
    await cache.get_or_build("c", _build)  # evicts "a"
    assert builds == 3

    await cache.get_or_build("b", _build)  # still cached
    assert builds == 3
    await cache.get_or_build("a", _build)  # evicted -> re-sweeps
    assert builds == 4


# --- UI progress: the sweep is not user-visible work -------------------------


async def test_the_sweep_emits_no_ui_progress_events() -> None:
    """Ratified decision: emulated discovery is synthetic context replay the model
    never asked for, run before the turn's first round-trip. Left ungated it opened
    every turn with "running listDatabases…" / "running listTables…" — work the
    user did not request, named in tool vocabulary, and contradicting the progress
    summarizer's own phrasing for those tools ("checking what data is available").

    Asserted over the REAL `ToolDispatcher` (the stub above pins the flag itself),
    and on what the UI would RENDER, so a refactor of how the gate is spelled
    cannot quietly reopen it.
    """
    from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
    from data_agent.runtime.mcp.fake_client import FakeMCPClient
    from data_agent.runtime.observability.progress import to_progress_event
    from data_agent.runtime.provenance.catalog_handle import CatalogHandle

    events: list[tuple[str, dict[str, Any]]] = []
    mcp = FakeMCPClient(
        scripted={
            "listDatabases": [[{"name": "dbpcm_warehouse"}]],
            "listTables": [[{"database": "dbpcm_warehouse", "name": "employee"}]],
        }
    )
    dispatcher = ToolDispatcher(
        mcp, CatalogHandle({}), observer=lambda e, p: events.append((e, dict(p)))
    )

    discovery = await build_emulated_discovery(
        dispatcher, _CREDS, base_database="dbpcm_warehouse"
    )

    # The sweep really ran (both round-trips), and injected both entries.
    assert [c.tool_name for c in mcp.calls] == ["listDatabases", "listTables"]
    assert len(discovery.entries) == 2
    # ...and produced nothing the UI would show.
    assert [name for name, _payload in events if name.startswith("tool_dispatch_")] == []
    assert all(to_progress_event(name, payload) is None for name, payload in events)
