"""answer_table.py — dormant hook seams for the two answer-table failure modes.

`answerWithTable` designates the query the UI pages as the answer
(`composite/answer_with_table.py`). Two conditions were identified as real but
deliberately left unhandled, because handling either properly is a larger piece of
work than the tool itself. Rather than leave them as comments, each gets a named
seam so the handling can be added later WITHOUT reopening the loop:

  UNRESOLVED DESIGNATION
      The model designated `blueprint_id=X`, but X names no blueprint that ran
      successfully THIS turn. There is nothing to resolve to a pageable query, so
      `answer_sql` stays null and the user gets prose with no table. A hook may
      supply a replacement query (for example by looking X up in the corpus and
      rendering it against the turn's slot bindings).

  EPHEMERAL DESIGNATION
      The resolved query references the session-scoped `scratch` database — the
      D93 materialization area a composed blueprint writes into. It is real and
      correct right now, and it STOPS WORKING when the scratch TTL lapses, at which
      point paging returns an ordinary query error and the user's table disappears
      mid-scroll. A hook may supply a durable replacement (for example by
      re-materializing the answer somewhere with a longer life).

CONTRACT (D72, docs/12-extensibility.md "What hooks may never do"):

  * A hook NEVER receives the JWT, the raw scope token, or the raw session id. The
    event carries a HASHED session id only, so a hook cannot key anything to a real
    session identifier or forward one.
  * A hook NEVER sees cell values — an event carries SQL and identifiers, never
    result rows.
  * A hook may only REPLACE the designated query string. It cannot bypass column
    scope: whatever it returns is executed through the same scope-enforced
    `POST /query/page` path under the caller's own credentials, so a hook cannot
    widen access to a column the caller could not already read.

DEGRADE-NOT-FAIL: a hook that raises is logged and skipped, and the runtime
continues exactly as if it had returned `None`. An extension point must never be
able to break a turn.

DORMANT BY DEFAULT: `AnswerTableHooks()` starts empty and every `resolve_*` call
returns `None` on an empty registry without touching anything. `app.py` does not
wire a populated registry — activating a hook is a deliberate registration.
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

    D5: `session_id_hash` is a HASH, never the raw session id, and there is no JWT
    or scope token field at all — a hook cannot capture credentials it is never
    given. `blueprint_id` and `sql` are model/runtime-authored identifiers and
    query text; neither carries warehouse cell values.
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
    """Called when the resolved query references the session-scoped `scratch`
    database and will therefore stop working at TTL.

    Return a durable replacement query, or `None` to accept the ephemeral one.
    """

    def __call__(self, event: AnswerTableEvent) -> str | None: ...


def references_scratch(sql: str | None) -> bool:
    """True iff *sql* appears to read from the scratch database.

    Deliberately a cheap textual check on `scratch.` rather than a sqlglot parse:
    this only decides whether to FIRE AN OBSERVATIONAL HOOK, never whether to run
    or reject a query, so a false positive costs one no-op hook call and a false
    negative costs nothing that was not already the status quo. Parsing here would
    duplicate `query_page.build_page_sql`'s work on every designation to answer a
    question with no safety weight.
    """
    return bool(sql) and f"{SCRATCH_DATABASE}." in sql.lower()


class AnswerTableHooks:
    """Registry for the two answer-table hook points. Empty (dormant) by default.

    Hooks run in registration order; the FIRST non-`None` return wins and the rest
    are skipped — so an earlier, more specific handler takes precedence over a
    later fallback, and registration order is the priority order.
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
