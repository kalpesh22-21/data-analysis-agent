"""Dormant hook seams for the two answer-table failure modes: a designated
`blueprint_id` that resolved to no successful run this turn, and a resolved query
reading the session-scoped `scratch` database that will stop working at its TTL.

CONTRACT (D72, docs/12-extensibility.md): a hook never receives the JWT, the raw scope
token, or the raw session id (the event carries a HASHED id only), never sees cell
values, and may only REPLACE the designated query string — the replacement executes
through the same scope-enforced `POST /query/page` path under the caller's own
credentials, so a hook cannot widen access to a column the caller could not read. A hook
that raises is logged and skipped: an extension point must never break a turn. Dormant
by default — an empty registry returns `None` from every `resolve_*` without doing work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

_logger = logging.getLogger(__name__)

# The database the D93 scratch side-channel materializes into
# (`mcp/scratch_client.py` names tables `scratch.s_<sid>_bp_<uuid>`). A designated
# query touching it is session-scoped and TTL-bound — see EPHEMERAL DESIGNATION.
SCRATCH_DATABASE = "scratch"

__all__ = [
    "SCRATCH_DATABASE",
    "AnswerTableEvent",
    "AnswerTableHooks",
    "EphemeralDesignationHook",
    "UnresolvedDesignationHook",
    "references_scratch",
]


@dataclass(frozen=True)
class AnswerTableEvent:
    """What a hook is told about one answer-table designation.

        D5: `session_id_hash` is a HASH, never the raw session id, and there is no JWT or
        scope-token field at all. `blueprint_id` and `sql` carry no warehouse cell values.
    """

    session_id_hash: str
    turn_index: int
    # The `blueprint_id` the model designated, when it designated one. `None` for a
    # raw-SQL designation.
    blueprint_id: str | None = None
    # The resolved query, when there is one. `None` on the UNRESOLVED path — that is
    # precisely what makes it unresolved.
    sql: str | None = None


class UnresolvedDesignationHook(Protocol):
    """Called when a `blueprint_id` designation resolved to nothing.

        Return a replacement query to page, or `None` to leave the answer table absent.
    """

    def __call__(self, event: AnswerTableEvent) -> str | None: ...


class EphemeralDesignationHook(Protocol):
    """Called when the resolved query references the session-scoped `scratch` database
        and will therefore stop working at TTL.

        Return a durable replacement query, or `None` to accept the ephemeral one.
    """

    def __call__(self, event: AnswerTableEvent) -> str | None: ...


def references_scratch(sql: str | None) -> bool:
    """True iff *sql* appears to read from the scratch database.

        A cheap textual check on `scratch.`, not a parse: it only decides whether to fire an
        OBSERVATIONAL hook, never whether to run or reject a query, so a false positive
        costs one no-op hook call and a false negative costs nothing.
    """
    return bool(sql) and f"{SCRATCH_DATABASE}." in sql.lower()


class AnswerTableHooks:
    """Registry for the two answer-table hook points. Empty (dormant) by default.

        Hooks run in registration order and the FIRST non-`None` return wins — registration
        order IS the priority order.
    """

    def __init__(
        self,
        *,
        on_unresolved: Sequence[UnresolvedDesignationHook] | None = None,
        on_ephemeral: Sequence[EphemeralDesignationHook] | None = None,
    ) -> None:
        self._on_unresolved: list[UnresolvedDesignationHook] = list(on_unresolved or ())
        self._on_ephemeral: list[EphemeralDesignationHook] = list(on_ephemeral or ())

    @property
    def is_dormant(self) -> bool:
        """True when nothing is registered — the shipped default."""
        return not self._on_unresolved and not self._on_ephemeral

    def register_unresolved(self, hook: UnresolvedDesignationHook) -> None:
        self._on_unresolved.append(hook)

    def register_ephemeral(self, hook: EphemeralDesignationHook) -> None:
        self._on_ephemeral.append(hook)

    def resolve_unresolved(self, event: AnswerTableEvent) -> str | None:
        """Fire ON_ANSWER_TABLE_UNRESOLVED. Returns a replacement query or `None`."""
        return self._first_non_none(self._on_unresolved, event, "unresolved")

    def resolve_ephemeral(self, event: AnswerTableEvent) -> str | None:
        """Fire ON_ANSWER_TABLE_EPHEMERAL. Returns a durable replacement or `None`."""
        return self._first_non_none(self._on_ephemeral, event, "ephemeral")

    @staticmethod
    def _first_non_none(
        hooks: Sequence[UnresolvedDesignationHook | EphemeralDesignationHook],
        event: AnswerTableEvent,
        label: str,
    ) -> str | None:
        for hook in hooks:
            try:
                replacement = hook(event)
            except Exception:
                # Degrade-not-fail: an extension point must never break a turn.
                _logger.exception(
                    "answer-table %s hook raised — skipping it (session=%s)",
                    label,
                    event.session_id_hash,
                )
                continue
            if isinstance(replacement, str) and replacement.strip():
                return replacement.strip()
        return None
