"""Emulated discovery — replay `listDatabases`+`listTables` as if the agent had
already called them (design §4.1 optimization).

Motivation: on every turn the model was prompted to call the two discovery tools
`listDatabases` + `listTables` before it could do real work, burning a couple of
completion round-trips on pure discovery. Since that catalog is scope-independent
warehouse metadata (never row/cell data), the runtime can fetch it up front — ONCE
PER SESSION (memoized by `EmulatedDiscoveryCache` below) — through the SAME
`ToolDispatcher` the model would have used
(so credentials/scope/denial-mapping/telemetry all stay consistent, D5/D57) and
inject it into the model's message list as if the model had ALREADY made those
calls: synthetic `assistant(tool_calls=...) + tool(result)` pairs, one per call.

Why emulate the calls instead of a system-message summary: `listDatabases`/
`listTables` have determined-EMPTY provenance (`frozenset()`, see
`provenance/capture.py`), so their tool-result shape replays cleanly, and both
names already live in `loop/read_guard.py::IDEMPOTENT_READ_TOOLS` — so a model
RE-call of either is caught by the loop's repeated-idempotent-read guard (seeded
from `read_signatures` below) and served locally, never re-dispatched to the MCP.

Depth is deliberately shallow: `listDatabases` + `listTables` ONLY. `getTableSchema`
stays model-driven (it is scope-sensitive and per-table — pre-fetching every table's
schema would be large and mostly wasted).

BREADTH is deliberately narrow too: `listTables` is emulated for the ONE
`base_database` (`settings.base_database`, default `dbpcm_warehouse`) — NOT for every
database `listDatabases` returns. The other databases on a live warehouse are not
analysis surface (`dbpcm_warehouse_security` is the access-control side, `scratch` is
the D93 per-session materialization area), so sweeping them cost an MCP round-trip
each per turn to inject listings the model should never query from. `listDatabases`
is still emulated in full, so the model still sees that the others EXIST and can list
them itself if it ever genuinely needs to — that path is simply no longer pre-paid.

Note the guard interaction of that choice: only the emulated `listTables(base)` call
seeds the read guard, so a model `listTables` on a NON-base database is a real
dispatch (correct — nothing was injected for it to be served from).

Shape fidelity (load-bearing): the injected `tool` message must render through the
SAME path a real replayed read uses so the model sees an identical shape
(status/error_code/user_message/result_preview). We therefore build each synthetic
`entry` by feeding a `TrailEntry` through `context/budget.py::_render_entry` — the
exact function `context/assembly.py` uses for a real trail entry — then let the loop
run each through `loop/agent_loop.py::_tool_trail_entry_to_canonical` (the same
assistant/tool synthesis a real replay uses). The shape is identical by construction.

Degrade-not-fail (design §2): this is pure injected context. If the MCP is
unreachable, denies discovery, or returns an unexpected shape,
`build_emulated_discovery` returns an EMPTY `EmulatedDiscovery` and the turn proceeds
exactly as before — the model simply falls back to calling the two tools itself. It
never raises out. It is never persisted to the trail, and the injected pairs are
PINNED by `context/budget.py::fit_request_to_budget` rather than budgeted: anchored
at the session's first question they are otherwise a prior-turn unit, the first thing
dropped under pressure — which would strand the model with a guard that says
"already served" for a listing no longer in its context.

D5: the JWT/session_id never enter this module's output. Credentials are consumed
only by `ToolDispatcher.dispatch`, which attaches them at the MCP transport boundary;
the returned `ToolResult` carries no credentials and the rendered entries are
warehouse metadata (database + table names) only.
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
    approval-resume, so an uncached sweep re-dispatched `listDatabases` +
    `listTables` to the MCP on every budget window — and, because the pairs are
    ephemeral and re-spliced per rebuild, the model watched a fresh block of
    discovery calls appear mid-session AFTER it had already read schemas. The
    warehouse catalogue is scope-independent metadata that does not change within a
    session, so it is swept once and served from here for every later window.

    Only a NON-EMPTY sweep is cached. A degraded one (MCP blip, denial, base
    database absent) is deliberately NOT memoized — caching it would disable
    discovery for the whole remaining session on one transient failure, so the next
    window retries. This preserves the module's degrade-not-fail posture.

    Bounded by `max_size` with FIFO eviction: an evicted session simply re-sweeps
    once. The lock makes concurrent windows of the SAME session issue at most one
    in-flight sweep (double-checked, mirroring `catalog/export_client.py::
    CatalogCache`); the first result wins.

    D5: only `session_id` is used as the key — no JWT/credential material is stored.
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

    `entries`: synthetic rendered-entry dicts in the exact shape
    `loop/agent_loop.py::_tool_trail_entry_to_canonical` consumes (the
    `context/budget.py::_render_entry` shape) — `listDatabases` first, then a single
    `listTables` for the base database. An empty list means nothing to inject.

    `read_signatures`: the `idempotent_read_signature(tool_name, args)` of each
    emulated call, used by the loop to SEED its repeated-idempotent-read guard so a
    model re-call of `listDatabases`/`listTables` is served locally, never
    re-dispatched to the MCP. Kept 1:1 with `entries` (a skipped/failed db
    contributes neither an entry nor a signature).
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
    """Render one dispatched `ok` discovery result into the SAME model-facing dict a
    real replayed read produces — by feeding a `TrailEntry` through the exact
    `context/budget.py::_render_entry` the assembler uses (no duplicated JSON logic).
    *preview_row_count* is threaded from `settings.preview_row_count` so the emulated
    entry re-truncates to the SAME row count a real replayed read would."""
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
    """Emit the shape-only `discovery_emulated` event on a degrade path (zero
    counts + `degraded: true`) and return the EMPTY result. The event fires on
    BOTH success and degrade so a silent MCP blip is observable in a trace, at the
    SAME shape-only D25/D61 posture (no SQL/PII — only integer counts + the flag)."""
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
    """Sweep `listDatabases` + `listTables(base_database)` through *dispatcher* and
    build the synthetic rendered entries + guard signatures, or an EMPTY result to
    degrade.

    *base_database* (threaded from `settings.base_database`): the ONE database whose
    tables are emulated. `listDatabases` is still emulated in full — only the
    `listTables` fan-out is narrowed, from one call per returned database to exactly
    one. See the module docstring for why the other databases are not analysis
    surface.

    *preview_row_count* (threaded from `settings.preview_row_count`): the row-count
    each emulated entry re-truncates to, so it matches a real replayed read under a
    non-default setting (otherwise a table listing longer than 20 would be capped
    while the guard seed blocked the model's same-args re-call — the tail becomes
    undiscoverable for the turn).

    Returns an empty `EmulatedDiscovery` (never raises) when: `listDatabases` is not
    `ok` / empty / not a list; or any unexpected exception occurred. When
    *base_database* is absent from the `listDatabases` result, or its `listTables`
    call is not `ok`, the `listDatabases` pair is STILL injected on its own (the
    model keeps the discovery it did get, and falls back to calling `listTables`
    itself). Every degrade path emits the shape-only `discovery_emulated` observer
    event with `degraded: true` so a silent MCP blip is observable.
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
