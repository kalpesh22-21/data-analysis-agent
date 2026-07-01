"""D44 scope re-filter — drop trail entries whose provenance is not a subset of scope.

Semantics (must match the MCP's own D80(b) exactly — see design §5):
    - `column_scope` empty (`frozenset()`) == allow-all. Every determined
      entry is kept.
    - `column_scope` non-empty == allowlist. An entry is kept iff every
      `(database.table, column)` pair in its provenance maps to
      `"database.table.column"` being a member of `column_scope` —
      EXCEPT scratch-table pairs (`database.table` prefixed `scratch.`),
      which are session-gated, not scope-gated (D64/D80), and are therefore
      excluded from the allowlist check regardless of `column_scope` —
      mirroring `clickhouse-api`'s own `run_query` forbidden-column
      computation (`app/service.py`: `not db_tbl.startswith("scratch.")`).
    - provenance `None` (undetermined, D63 fail-closed) is ALWAYS dropped,
      regardless of how open `column_scope` is — "never assume in-scope"
      (D44) is read literally: an undetermined USES set carries no safety
      guarantee even under an allow-all token.

2026-07-01 clarification (B1): this same subset/`None` logic — factored into
`is_provenance_in_scope` below so it is never duplicated — also gates
conversational **assistant** `TurnMessage`s (`is_message_in_scope`/
`filter_messages`), not just the tool trail. See `TurnMessage.provenance`'s
docstring (`session/models.py`) and `loop/agent_loop.py::_build_canonical_messages`
for how assistant messages are tagged and where this filter is applied.
**User** messages carry no warehouse-derived data and are always kept.

2026-07-01 clarification (turn-scoped continuity, STATUS-GATED): D44's
replay filter governs REPLAY of PRIOR turns under a (possibly
since-narrowed) scope. `filter_trail`'s optional `current_turn_index` param
exempts ONLY non-`"ok"` (denied/errored) entries belonging to the turn
CURRENTLY in progress from the provenance-`None` drop — a denied/errored
entry carries no result rows regardless (`dispatch/tool_dispatcher.py` never
sets `result_preview`/`result_full` on a non-`"ok"` result), so surfacing it
leaks nothing. This lets the model see its own current-turn denials/errors
(design §3.4 self-correction) without weakening cross-turn D44 in any way
— a PRIOR turn's undetermined/denied entry is still always dropped. A
**successful** (`status == "ok"`) current-turn entry is NEVER exempt — it IS
data-bearing (`result_preview`/`result_full` are populated), and for
`sampleRows`/`getTableSchema` the MCP does not itself column-scope the
result (only `runQuery` does), so `provenance/capture.py` computes their
provenance declaratively as "all columns of the table", which is frequently
NOT a subset of a narrow `column_scope` — such an entry always goes through
the ordinary strict `is_entry_in_scope` check, even within the current turn.
Default `None` preserves the original all-strict behavior exactly (no
exemption is ever applied).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from data_agent.runtime.session.models import TrailEntry, TurnMessage

_SCRATCH_PREFIX = "scratch."


def is_provenance_in_scope(
    provenance: frozenset[tuple[str, str]] | None, column_scope: frozenset[str]
) -> bool:
    """The core D44 subset/`None` semantics, shared by `TrailEntry.provenance`
    AND `TurnMessage.provenance` (2026-07-01 clarification) — the single
    source of truth for "is this provenance set replayable under this scope",
    never duplicated divergently between the trail filter and the message
    filter.
    """
    if provenance is None:
        return False

    if not column_scope:
        return True

    for db_table, column in provenance:
        if db_table.startswith(_SCRATCH_PREFIX):
            continue
        if f"{db_table}.{column}" not in column_scope:
            return False
    return True


def is_entry_in_scope(entry: TrailEntry, column_scope: frozenset[str]) -> bool:
    """Return True iff *entry* may be replayed under *column_scope* (D44)."""
    return is_provenance_in_scope(entry.provenance, column_scope)


def filter_trail(
    trail: Sequence[TrailEntry],
    column_scope: frozenset[str],
    current_turn_index: int | None = None,
) -> list[TrailEntry]:
    """Return the subsequence of *trail* whose provenance is in *column_scope*.

    Order-preserving; pure function, no I/O — the primary Layer-1 test target
    for D44 (design §8).

    *current_turn_index* (turn-scoped continuity, 2026-07-01, STATUS-GATED):
    when given, an entry with `entry.turn_index == current_turn_index` AND
    `entry.status != "ok"` (a denied/errored, no-result-rows entry) is kept
    regardless of its provenance (see module docstring) — the current
    in-progress turn's own denials/errors are exempt from the strict D44
    drop. A **successful** current-turn entry (`status == "ok"`) is never
    exempt — it carries real result rows, so it is always subject to the
    ordinary strict `is_entry_in_scope` check, exactly like any prior-turn
    entry. Every OTHER entry (any prior turn, or any non-exempt current-turn
    entry) is gated by the unchanged strict `is_entry_in_scope` check.
    Default `None` applies the strict check to every entry, unchanged from
    before this parameter existed.
    """
    return [
        entry
        for entry in trail
        if (
            current_turn_index is not None
            and entry.turn_index == current_turn_index
            and entry.status != "ok"
        )
        or is_entry_in_scope(entry, column_scope)
    ]


def is_message_in_scope(message: TurnMessage, column_scope: frozenset[str]) -> bool:
    """Return True iff *message* may be replayed under *column_scope*
    (2026-07-01 D44 clarification). User messages are always kept (no
    warehouse-derived data); assistant messages use the identical
    subset/`None` semantics as `is_entry_in_scope`.
    """
    if message.role == "user":
        return True
    return is_provenance_in_scope(message.provenance, column_scope)


def filter_messages(
    messages: Sequence[TurnMessage], column_scope: frozenset[str]
) -> list[TurnMessage]:
    """Return the subsequence of *messages* replayable under *column_scope*
    (2026-07-01 D44 clarification) — the conversational-message counterpart
    of `filter_trail`. Order-preserving; pure function, no I/O.
    """
    return [message for message in messages if is_message_in_scope(message, column_scope)]


def compute_scope_hash(column_scope: frozenset[str]) -> str:
    """Stable hash of *column_scope*, for cache keys and telemetry (D25 — never log raw scope)."""
    canonical = "\n".join(sorted(column_scope))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
