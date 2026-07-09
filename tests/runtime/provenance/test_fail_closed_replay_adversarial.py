"""Adversarial fail-closed provenance coverage: `ToolDispatcher` -> `TrailEntry`
-> `scope_filter` end-to-end (QA hardening pass).

`tests/runtime/provenance/test_capture.py` already proves `capture_provenance`
returns `None` (undetermined) for an uncatalogued `sampleRows` table and for a
`runQuery` whose SQL the runtime's own extractor rejects (and that
`getTableSchema` — MCP-scope-filtered metadata — is safe-empty `frozenset()`,
NOT fail-closed `None`, since its result can carry no out-of-scope cell data).
`tests/runtime/context/test_scope_filter.py` already proves `None` provenance
is always dropped by `filter_trail`. This file proves the FULL chain those two
suites individually assume: a live tool call that the (fake) MCP itself lets
through, but whose provenance the runtime cannot determine, produces a
`TrailEntry` that is dropped from replay under EVERY scope, including
allow-all — and is critically never silently coerced into the trivially-safe
`frozenset()` ("determined, zero columns") shape, which would defeat the
fail-closed guarantee.
"""

from __future__ import annotations

import json

from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.context.scope_filter import is_entry_in_scope
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import TrailEntry

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})

SESSION_ID = "sess-fail-closed-test"

# A SQL string that ClickHouse itself would happily execute (and that our
# FakeMCPClient — standing in for a live MCP that already gated the query at
# call-time — happily "returns success" for) but that the runtime's own
# independent sqlglot re-parse rejects (mirrors the exact case already proven
# in test_capture.py, reused here to keep the extractor's own contract
# untouched per the QA brief).
_UNPARSEABLE_SQL = "SELECT * FROM generateRandom('a UInt8', 1, 10, 2)"


def _entry_to_result_blob(entry: TrailEntry) -> str:
    return json.dumps(entry.to_doc(), default=str)


async def _dispatch_and_persist(store: InMemorySessionStore, mcp: FakeMCPClient, catalog, *, tool_name, args, call_id) -> TrailEntry:
    dispatcher = ToolDispatcher(mcp, catalog)
    result = await dispatcher.dispatch(tool_name, args, credentials=_creds())
    entry = TrailEntry(
        turn_index=0,
        tool_call_id=call_id,
        tool_name=tool_name,
        args=dict(args),
        status=result.status,
        error_code=result.error_code,
        provenance=result.provenance,
        result_preview=result.result_preview,
        result_full_ref=None,
        ts="2026-07-01T00:00:00+00:00",
    )
    await store.append_trail_entry(SESSION_ID, entry)
    return entry


def _creds():
    from data_agent.runtime.auth.credentials import RuntimeCredentials

    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt-not-under-test", column_scope=frozenset())


async def test_run_query_extractor_rejection_produces_none_not_empty_frozenset() -> None:
    store = InMemorySessionStore()
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                {"columns": ["a"], "rows": [[1]], "row_count": 1, "truncated": False}
            ]
        }
    )
    entry = await _dispatch_and_persist(
        store, mcp, CATALOG, tool_name="runQuery", args={"sql": _UNPARSEABLE_SQL}, call_id="call_bad_sql"
    )

    # Fail-closed contract: undetermined must be represented as None, never
    # silently downgraded to "determined, zero columns" (frozenset()) — the
    # two have opposite replay semantics (frozenset() is trivially in-scope
    # and always kept; None is never kept).
    assert entry.provenance is None
    assert entry.provenance != frozenset()


async def test_run_query_extractor_rejection_dropped_from_replay_under_every_scope() -> None:
    store = InMemorySessionStore()
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                {"columns": ["a"], "rows": [[1]], "row_count": 1, "truncated": False}
            ]
        }
    )
    await _dispatch_and_persist(
        store, mcp, CATALOG, tool_name="runQuery", args={"sql": _UNPARSEABLE_SQL}, call_id="call_bad_sql"
    )

    assembler = ContextAssembler(store, history_token_budget=100_000)
    for scope in (
        frozenset(),  # allow-all — the widest possible scope
        frozenset({f"{_E}.EmployeeCode"}),
        frozenset({f"{_E}.EmployeeCode", f"{_E}.Department"}),
    ):
        assembled = await assembler.assemble(SESSION_ID, scope)
        blob = json.dumps(assembled.messages, default=str)
        assert "call_bad_sql" not in blob, f"undetermined runQuery entry replayed under scope={scope!r}"
        assert assembled.dropped_by_scope_count == 1


async def test_sample_rows_uncatalogued_table_produces_none_and_is_dropped() -> None:
    store = InMemorySessionStore()
    mcp = FakeMCPClient(
        scripted={
            "sampleRows": [
                {"columns": ["x"], "rows": [[1]], "row_count": 1, "truncated": False}
            ]
        }
    )
    entry = await _dispatch_and_persist(
        store,
        mcp,
        CATALOG,
        tool_name="sampleRows",
        args={"database": "dbpcm_warehouse", "table": "an_uncatalogued_ghost_table"},
        call_id="call_ghost_table",
    )
    assert entry.provenance is None

    assembler = ContextAssembler(store, history_token_budget=100_000)
    # Allow-all scope — the single most permissive replay condition — must
    # still drop this entry.
    assembled = await assembler.assemble(SESSION_ID, frozenset())
    blob = json.dumps(assembled.messages, default=str)
    assert "call_ghost_table" not in blob
    assert assembled.dropped_by_scope_count == 1


async def test_get_table_schema_is_safe_empty_and_replayable_under_any_scope() -> None:
    """getTableSchema returns MCP-scope-filtered column METADATA (no cell
    values), so — unlike sampleRows — it is NEVER fail-closed to `None`. It is
    recorded with safe-empty `frozenset()` provenance and is KEPT by
    `filter_trail` under every scope, including a narrow one that grants only a
    subset of the table's columns. This is the fix for the fetched-schema-
    vanishes bug (2026-07-09): a successfully fetched schema must stay in the
    replayed context even under a restricted scope."""
    store = InMemorySessionStore()
    mcp = FakeMCPClient(
        scripted={
            "getTableSchema": [
                {"database": "dbpcm_warehouse", "table": "employee", "columns": []}
            ]
        }
    )
    entry = await _dispatch_and_persist(
        store,
        mcp,
        CATALOG,
        tool_name="getTableSchema",
        args={"database": "dbpcm_warehouse", "table": "employee"},
        call_id="call_schema_ok",
    )
    assert entry.provenance == frozenset()
    # Kept under allow-all AND under a narrow scope granting only one column.
    assert is_entry_in_scope(entry, frozenset()) is True
    assert is_entry_in_scope(entry, frozenset({f"{_E}.Department"})) is True

    assembler = ContextAssembler(store, history_token_budget=100_000)
    assembled = await assembler.assemble(SESSION_ID, frozenset({f"{_E}.Department"}))
    blob = json.dumps(assembled.messages, default=str)
    assert "call_schema_ok" in blob
    assert assembled.dropped_by_scope_count == 0


async def test_denied_tool_call_never_persists_fabricated_provenance() -> None:
    """A denied call (MCPToolError) short-circuits before provenance capture —
    `ToolResult.provenance` must be None, not an empty/fabricated set, so a
    denial can never accidentally be replayed as "successfully queried,
    zero columns"."""
    from data_agent.runtime.mcp.client import MCPToolError

    store = InMemorySessionStore()
    mcp = FakeMCPClient(scripted={"runQuery": [MCPToolError("COLUMN_SCOPE_VIOLATION", "denied")]})
    entry = await _dispatch_and_persist(
        store, mcp, CATALOG, tool_name="runQuery", args={"sql": "SELECT Salary FROM employee"}, call_id="call_denied"
    )
    assert entry.status == "denied"
    assert entry.provenance is None

    assembler = ContextAssembler(store, history_token_budget=100_000)
    assembled = await assembler.assemble(SESSION_ID, frozenset())
    blob = json.dumps(assembled.messages, default=str)
    assert "call_denied" not in blob
    assert assembled.dropped_by_scope_count == 1
