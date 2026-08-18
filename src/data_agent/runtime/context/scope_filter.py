"""D44 scope re-filter — drop trail entries whose provenance is not a subset of scope.

Semantics (must match the MCP's own D80(b) exactly):
  - `column_scope` empty == allow-all; every determined entry is kept.
  - non-empty == allowlist: an entry is kept iff every `(database.table, column)` pair
    maps to a `"database.table.column"` member of `column_scope` — EXCEPT
    `scratch.`-prefixed pairs, which are session-gated rather than scope-gated
    (D64/D80) and are excluded from the check regardless of scope.
  - provenance `None` (undetermined, D63) is ALWAYS dropped, however open the scope:
    "never assume in-scope" is read literally.

The same logic (`is_provenance_in_scope`) gates conversational ASSISTANT messages, not
just the tool trail; USER messages carry no warehouse-derived data and are always kept.

TURN-SCOPED CONTINUITY, STATUS-GATED: `filter_trail`'s optional `current_turn_index`
exempts ONLY non-`"ok"` entries of the turn IN PROGRESS from the `None` drop — a
denied/errored entry carries no result rows, so surfacing it leaks nothing and the model
can self-correct. A SUCCESSFUL current-turn entry is NEVER exempt: it is data-bearing,
and `sampleRows` provenance is declaratively "all columns of the table", frequently not
a subset of a narrow scope. Cross-turn behaviour is unchanged in every case.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from data_agent.runtime.session.models import TrailEntry, TurnMessage

_SCRATCH_PREFIX = "scratch."


def is_provenance_in_scope(
    provenance: frozenset[tuple[str, str]] | None, column_scope: frozenset[str]
) -> bool:
    """The core D44 subset/`None` semantics, shared by `TrailEntry.provenance` AND
        `TurnMessage.provenance` — the single source of truth for "is this provenance
        replayable under this scope", never duplicated between the two filters.
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

        Order-preserving; pure, no I/O.

        *current_turn_index*, when given, keeps an entry with that `turn_index` AND
        `status != "ok"` regardless of its provenance (see module docstring). A SUCCESSFUL
        current-turn entry is never exempt. `None` applies the strict check to every entry.
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
    """Return True iff *message* may be replayed under *column_scope*. User messages are
        always kept (no warehouse-derived data); assistant messages use the identical
        subset/`None` semantics as `is_entry_in_scope`.
    """
    if message.role == "user":
        return True
    return is_provenance_in_scope(message.provenance, column_scope)


def filter_messages(
    messages: Sequence[TurnMessage], column_scope: frozenset[str]
) -> list[TurnMessage]:
    """Return the subsequence of *messages* replayable under *column_scope* — the
        conversational counterpart of `filter_trail`. Order-preserving; pure, no I/O.
    """
    return [message for message in messages if is_message_in_scope(message, column_scope)]


def compute_scope_hash(column_scope: frozenset[str]) -> str:
    """Stable hash of *column_scope*, for cache keys and telemetry (D25 — never log raw scope)."""
    canonical = "\n".join(sorted(column_scope))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
