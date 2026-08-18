"""Emulated discovery — replay `listDatabases`+`listTables` as if the agent had already
called them, so no turn burns completion round-trips on pure discovery.

That catalogue is scope-independent metadata, so the runtime sweeps it ONCE PER SESSION
through the SAME `ToolDispatcher` the model would have used (credentials, scope, denial
mapping and telemetry all stay consistent, D5/D57) and injects synthetic
`assistant(tool_calls=...) + tool(result)` pairs. Emulating the CALLS rather than
summarizing them matters: both tools have determined-EMPTY provenance so their pairs
replay cleanly, and both are `IDEMPOTENT_READ_TOOLS`, so a model re-call is served
locally from the seeded read guard instead of re-dispatched.

Depth and breadth are deliberately narrow: `listDatabases` in full, `listTables` for the
ONE `base_database` only. `getTableSchema` stays model-driven (scope-sensitive and
per-table), and a `listTables` on a non-base database is a real dispatch — correct,
since nothing was injected for it to be served from.

Shape fidelity (load-bearing): each synthetic entry goes through the SAME
`context/budget.py::_render_entry` a real replayed read uses, so the model sees an
identical shape by construction.

Degrade-not-fail: any failure returns an EMPTY `EmulatedDiscovery` and the turn proceeds
exactly as before; this never raises out, and nothing is persisted to the trail. The
injected pairs are PINNED by `fit_request_to_budget` rather than budgeted — as
prior-turn units they would be the first thing dropped, stranding the model with a guard
that answers "already served" for a listing no longer in its context.

D5: no credential enters this module's output — the JWT is consumed only by
`ToolDispatcher.dispatch`, at the MCP transport boundary.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from data_agent.runtime.context.budget import _render_entry
from data_agent.runtime.loop.read_guard import idempotent_read_signature
from data_agent.runtime.session.models import TrailEntry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from data_agent.runtime.auth.credentials import RuntimeCredentials
    from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult

    Observer = Callable[[str, dict[str, Any]], None]

_logger = logging.getLogger(__name__)

# Default rendered-entry preview truncation — matches `context/budget.py`'s
# default. The real value is threaded in from `settings.preview_row_count` via
# `build_emulated_discovery(..., preview_row_count=...)` so an emulated entry
# re-truncates identically to a real replayed read under a non-default setting.
_DEFAULT_PREVIEW_ROW_COUNT = 20
# Default base database — the one whose tables are emulated. The real value is
# threaded in from `settings.base_database` via
# `build_emulated_discovery(..., base_database=...)`; this literal only backs
# direct callers (tests) and matches `catalog/loader.py::DEFAULT_DATABASE`.
_DEFAULT_BASE_DATABASE = "dbpcm_warehouse"
# Placeholder timestamp — `_render_entry` never reads `ts`; the synthetic entries
# are ephemeral and never persisted, so this value is inert.
_EMULATED_TS = "1970-01-01T00:00:00+00:00"

__all__ = ["EmulatedDiscovery", "EmulatedDiscoveryCache", "build_emulated_discovery"]


class EmulatedDiscoveryCache:
    """Process-wide, session-keyed, bounded cache of the ONCE-PER-SESSION sweep.

        `_run_loop_body` is re-entered by `run()`, `resume()` AND the blueprint
        approval-resume, so an uncached sweep re-dispatches both tools on every budget
        window — and, because the pairs are re-spliced per rebuild, the model watches a
        fresh block of discovery calls appear mid-session.

        Only a NON-EMPTY sweep is cached: memoizing a degraded one (MCP blip, denial, base
        database absent) would disable discovery for the whole remaining session on one
        transient failure, so the next window retries instead.

        Bounded by `max_size` with FIFO eviction — an evicted session simply re-sweeps once.
        The lock is double-checked, so concurrent windows of the SAME session issue at most
        one in-flight sweep and the first result wins. D5: only `session_id` is a key.
    """

    def __init__(self, max_size: int = 512) -> None:
        self._max_size = max_size
        self._store: dict[str, EmulatedDiscovery] = {}
        self._order: list[str] = []
        self._lock = asyncio.Lock()

    async def get_or_build(
        self, session_id: str, build: Callable[[], Awaitable[EmulatedDiscovery]]
    ) -> EmulatedDiscovery:
        """Return this session's cached sweep, or run *build* once and cache it."""
        cached = self._store.get(session_id)
        if cached is not None:
            return cached
        async with self._lock:
            # Double-checked: a racing window may have populated it while we waited.
            cached = self._store.get(session_id)
            if cached is not None:
                return cached
            result = await build()
            # Degrade-not-fail: never memoize an empty/degraded sweep (see above).
            if result.entries:
                if len(self._store) >= self._max_size:
                    evicted = self._order.pop(0)
                    del self._store[evicted]
                self._store[session_id] = result
                self._order.append(session_id)
            return result


@dataclass(frozen=True)
class EmulatedDiscovery:
    """The output of one `build_emulated_discovery` sweep.

        `entries` are synthetic rendered-entry dicts in the exact `_render_entry` shape
        `loop/agent_loop.py::_tool_trail_entry_to_canonical` consumes — `listDatabases`
        first, then a single `listTables` for the base database; empty means nothing to
        inject. `read_signatures` are the matching `idempotent_read_signature(tool_name,
        args)` values, used to SEED the loop's repeated-idempotent-read guard, and are kept
        1:1 with `entries` (a skipped or failed call contributes neither).
    """

    entries: list[dict[str, Any]] = field(default_factory=list)
    read_signatures: set[tuple[str, str]] = field(default_factory=set)


def _database_names(payload: Any) -> list[str]:
    """Extract database names from a `listDatabases` payload (a bare list of
    `{"name": <db>}` dicts). Anything malformed is skipped."""
    names: list[str] = []
    if not isinstance(payload, list):
        return names
    for item in payload:
        if isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return names


def _row_count(payload: Any) -> int:
    """Table count for the shape-only observer event — the length of a `listTables`
    payload (a bare list). Non-list payloads contribute zero."""
    return len(payload) if isinstance(payload, list) else 0


def _rendered_entry(
    tool_call_id: str,
    tool_name: str,
    args: dict[str, Any],
    result: ToolResult,
    preview_row_count: int,
) -> dict[str, Any]:
    """Render one dispatched `ok` discovery result into the SAME model-facing dict a real
        replayed read produces, by feeding a `TrailEntry` through `_render_entry` rather
        than duplicating its JSON logic. *preview_row_count* is threaded from settings so
        the emulated entry re-truncates to the SAME row count a real replayed read would.
    """
    entry = TrailEntry(
        turn_index=0,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        args=dict(args),
        status=result.status,
        error_code=result.error_code,
        provenance=result.provenance,
        result_preview=result.result_preview,
        result_full_ref=None,
        ts=_EMULATED_TS,
    )
    return _render_entry(entry, preview_row_count)


def _emit_degraded(observer: Observer | None) -> EmulatedDiscovery:
    """Emit the shape-only `discovery_emulated` event on a degrade path (zero counts +
        `degraded: true`) and return the EMPTY result. The event fires on BOTH success and
        degrade, so a silent MCP blip is observable in a trace at the same shape-only
        posture (integer counts + the flag, no SQL or PII).
    """
    if observer is not None:
        observer(
            "discovery_emulated",
            {"database_count": 0, "table_count": 0, "degraded": True},
        )
    return EmulatedDiscovery()


async def build_emulated_discovery(
    dispatcher: ToolDispatcher,
    credentials: RuntimeCredentials,
    *,
    base_database: str = _DEFAULT_BASE_DATABASE,
    preview_row_count: int = _DEFAULT_PREVIEW_ROW_COUNT,
    observer: Observer | None = None,
) -> EmulatedDiscovery:
    """Sweep `listDatabases` + `listTables(base_database)` through *dispatcher* and build
        the synthetic rendered entries + guard signatures, or an EMPTY result to degrade.

        *base_database* is the ONE database whose tables are emulated; only the `listTables`
        fan-out is narrowed, `listDatabases` is still emulated in full.

        *preview_row_count* is the row count each emulated entry re-truncates to, so it
        matches a real replayed read under a non-default setting — otherwise a listing
        longer than the preview would be capped while the guard seed blocked the model's
        same-args re-call, making the tail undiscoverable for the turn.

        Never raises. Returns an empty result when `listDatabases` is not `ok`, empty, or
        not a list, and on any unexpected exception. When *base_database* is absent from the
        result, or its `listTables` call is not `ok`, the `listDatabases` pair is STILL
        injected on its own. Every degrade path emits `discovery_emulated` with
        `degraded: true`.
    """
    try:
        # `emit_progress=False` on BOTH sweep dispatches (ratified): this is
        # synthetic context replay the model never asked for, run before the turn's
        # first round-trip. Narrating it painted "running listDatabases…" /
        # "running listTables…" at turn start — work the user did not request, named
        # in tool vocabulary, and contradicting the progress summarizer's own static
        # phrasing for those tools ("checking what data is available"). The
        # `discovery_emulated` observer event below is the operator signal and is
        # unaffected, as are spans, scope enforcement and every degrade path.
        db_result = await dispatcher.dispatch("listDatabases", {}, credentials, emit_progress=False)
        if db_result.status != "ok":
            _logger.warning(
                "emulated discovery: listDatabases not ok (status=%s) — injecting nothing",
                db_result.status,
            )
            return _emit_degraded(observer)
        db_names = _database_names(db_result.result_full)
        if not db_names:
            _logger.debug("emulated discovery: no databases returned — injecting nothing")
            return _emit_degraded(observer)

        entries: list[dict[str, Any]] = [
            _rendered_entry(
                "emulated-listDatabases", "listDatabases", {}, db_result, preview_row_count
            )
        ]
        read_signatures: set[tuple[str, str]] = {idempotent_read_signature("listDatabases", {})}

        injected_dbs = 0
        table_count = 0
        # `listTables` for the base database ONLY (see the module docstring). Gate on
        # it actually being in the `listDatabases` result: on a token whose scope /
        # the server's ALLOWED_DATABASES allowlist excludes it, dispatching anyway
        # would spend a round-trip to earn a denial.
        if base_database not in db_names:
            _logger.warning(
                "emulated discovery: base database %r not in listDatabases result — "
                "injecting the listDatabases pair only",
                base_database,
            )
        else:
            args = {"database": base_database}
            table_result = await dispatcher.dispatch(
                "listTables", args, credentials, emit_progress=False
            )
            if table_result.status != "ok":
                _logger.warning(
                    "emulated discovery: listTables(%s) not ok (status=%s) — injecting the "
                    "listDatabases pair only",
                    base_database,
                    table_result.status,
                )
            else:
                entries.append(
                    _rendered_entry(
                        f"emulated-listTables-{base_database}",
                        "listTables",
                        args,
                        table_result,
                        preview_row_count,
                    )
                )
                read_signatures.add(idempotent_read_signature("listTables", args))
                injected_dbs = 1
                table_count = _row_count(table_result.result_full)

        if observer is not None:
            observer(
                "discovery_emulated",
                {"database_count": injected_dbs, "table_count": table_count},
            )
        return EmulatedDiscovery(entries=entries, read_signatures=read_signatures)
    except Exception:
        # Degrade-not-fail: never let a sweep failure break the turn — the model
        # falls back to calling listDatabases/listTables itself.
        _logger.exception("emulated discovery failed — injecting nothing")
        return _emit_degraded(observer)
