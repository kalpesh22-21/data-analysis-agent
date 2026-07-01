"""Adversarial D44 mid-session scope-narrowing coverage at the `ContextAssembler`
integration level (QA hardening pass).

`tests/runtime/context/test_scope_filter.py` already proves `filter_trail`'s
pure-function semantics exhaustively; this file proves the SAME property one
layer up, through the full D50 pipeline (`SessionStore.load_trail` ->
`scope_filter.filter_trail` -> `budget.compact_trail` -> `render_messages`),
which is what `AgentLoop` actually depends on. Scenario: entries are
persisted (as if authorized/dispatched) under a WIDE scope, then the SAME
session is re-assembled under a NARROWED scope — simulating a user's
column-scope shrinking mid-session (e.g. a re-issued JWT with fewer grants).
Asserts the now-out-of-scope entries are dropped from the rendered messages
(not merely from an internal list), in-scope ones survive verbatim, and:
  - allow-all (`frozenset()`) keeps every determined entry.
  - undetermined-provenance (`None`) entries are ALWAYS dropped, even under
    the widest possible (allow-all) re-assembly scope.
  - determined-empty (`frozenset()`) provenance entries are always kept.
"""

from __future__ import annotations

import json

from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import ResultPreview, TrailEntry

_E = "dbpcm_warehouse.employee"
_P = "dbpcm_warehouse.payroll"

SESSION_ID = "sess-narrowing-test"


def _entry(
    tool_call_id: str,
    provenance: frozenset[tuple[str, str]] | None,
    *,
    sql_marker: str,
) -> TrailEntry:
    return TrailEntry(
        turn_index=0,
        tool_call_id=tool_call_id,
        tool_name="runQuery",
        args={"sql": f"SELECT 1 -- {sql_marker}"},
        status="ok",
        error_code=None,
        provenance=provenance,
        result_preview=ResultPreview(columns=["x"], row_count=1, truncated=False, preview_rows=[[1]]),
        result_full_ref=None,
        ts="2026-07-01T00:00:00+00:00",
    )


async def _assemble(store: InMemorySessionStore, scope: frozenset[str]):
    assembler = ContextAssembler(store, history_token_budget=100_000)
    return await assembler.assemble(SESSION_ID, scope)


async def test_narrowing_drops_out_of_scope_entries_from_rendered_messages() -> None:
    store = InMemorySessionStore()
    # Persisted (dispatched/authorized) under a WIDE scope covering both tables.
    wide_authorized_entry = _entry(
        "call_wide", frozenset({(_P, "Amount")}), sql_marker="WIDE_MARKER_PAYROLL"
    )
    narrow_surviving_entry = _entry(
        "call_narrow", frozenset({(_E, "Department")}), sql_marker="NARROW_MARKER_EMPLOYEE"
    )
    await store.append_trail_entry(SESSION_ID, wide_authorized_entry)
    await store.append_trail_entry(SESSION_ID, narrow_surviving_entry)

    # Sanity: under the original wide (allow-all) scope, both entries render.
    wide_assembled = await _assemble(store, frozenset())
    wide_blob = json.dumps(wide_assembled.messages, default=str)
    assert "WIDE_MARKER_PAYROLL" in wide_blob
    assert "NARROW_MARKER_EMPLOYEE" in wide_blob
    assert wide_assembled.dropped_by_scope_count == 0

    # Mid-session narrowing: re-assemble under a scope that only grants the
    # employee.Department column — the payroll entry must now be invisible to
    # the model, not merely "marked" — the SQL text/tool_call_id must not
    # appear anywhere in the rendered messages.
    narrow_scope = frozenset({f"{_E}.Department"})
    narrowed_assembled = await _assemble(store, narrow_scope)
    narrowed_blob = json.dumps(narrowed_assembled.messages, default=str)

    assert "WIDE_MARKER_PAYROLL" not in narrowed_blob
    assert "call_wide" not in narrowed_blob
    assert "NARROW_MARKER_EMPLOYEE" in narrowed_blob
    assert "call_narrow" in narrowed_blob
    assert narrowed_assembled.dropped_by_scope_count == 1


async def test_allow_all_rescope_keeps_every_determined_entry() -> None:
    store = InMemorySessionStore()
    entries = [
        _entry("c1", frozenset({(_E, "Department")}), sql_marker="M1"),
        _entry("c2", frozenset({(_P, "Amount")}), sql_marker="M2"),
        _entry("c3", frozenset(), sql_marker="M3"),  # determined-empty
    ]
    for e in entries:
        await store.append_trail_entry(SESSION_ID, e)

    # Session narrowed to nothing, THEN "widened back" to allow-all (e.g. a
    # fresh admin-scope JWT) — every determined entry must reappear.
    assembled = await _assemble(store, frozenset())
    blob = json.dumps(assembled.messages, default=str)
    for marker in ("M1", "M2", "M3"):
        assert marker in blob
    assert assembled.dropped_by_scope_count == 0


async def test_undetermined_provenance_dropped_even_under_allow_all_rescope() -> None:
    store = InMemorySessionStore()
    determined_entry = _entry("c_determined", frozenset({(_E, "Department")}), sql_marker="KEEP_ME")
    undetermined_entry = _entry("c_undetermined", None, sql_marker="NEVER_REPLAY_ME")
    await store.append_trail_entry(SESSION_ID, determined_entry)
    await store.append_trail_entry(SESSION_ID, undetermined_entry)

    # Even the widest possible re-assembly scope (allow-all) must never
    # resurrect an undetermined-provenance entry (D44 "never assume in-scope",
    # read literally).
    assembled = await _assemble(store, frozenset())
    blob = json.dumps(assembled.messages, default=str)

    assert "KEEP_ME" in blob
    assert "NEVER_REPLAY_ME" not in blob
    assert "c_undetermined" not in blob
    assert assembled.dropped_by_scope_count == 1


async def test_determined_empty_provenance_always_kept_across_any_narrowing() -> None:
    store = InMemorySessionStore()
    no_column_entry = _entry("c_none", frozenset(), sql_marker="SELECT_1_LITERAL")
    await store.append_trail_entry(SESSION_ID, no_column_entry)

    for scope in (frozenset(), frozenset({f"{_E}.Department"}), frozenset({"other.table.col"})):
        assembled = await _assemble(store, scope)
        blob = json.dumps(assembled.messages, default=str)
        assert "SELECT_1_LITERAL" in blob, f"determined-empty entry dropped under scope={scope!r}"
        assert assembled.dropped_by_scope_count == 0


async def test_progressive_narrowing_sequence_is_deterministic_and_reversible() -> None:
    """Re-assembling repeatedly with different scopes must be a pure read —
    no mutation of the underlying trail, so a later WIDENING re-grants access
    to the same entries without re-dispatching any tool call."""
    store = InMemorySessionStore()
    entry_a = _entry("a", frozenset({(_E, "Department")}), sql_marker="A_MARK")
    entry_b = _entry("b", frozenset({(_P, "Amount")}), sql_marker="B_MARK")
    await store.append_trail_entry(SESSION_ID, entry_a)
    await store.append_trail_entry(SESSION_ID, entry_b)

    narrow = frozenset({f"{_E}.Department"})
    wide = frozenset()

    step1 = await _assemble(store, narrow)
    assert "A_MARK" in json.dumps(step1.messages, default=str)
    assert "B_MARK" not in json.dumps(step1.messages, default=str)

    step2 = await _assemble(store, wide)  # widened back
    blob2 = json.dumps(step2.messages, default=str)
    assert "A_MARK" in blob2
    assert "B_MARK" in blob2  # re-appears — no data was lost/mutated by step1

    step3 = await _assemble(store, narrow)  # narrowed again — still deterministic
    blob3 = json.dumps(step3.messages, default=str)
    assert "A_MARK" in blob3
    assert "B_MARK" not in blob3
