"""SessionStore Protocol — the Couchbase persistence + DI seam (D22/D44/D45, §8).

Two implementations exist:
  - `memory_store.InMemorySessionStore` (Layer 1, dict-backed, emulates CAS
    with an in-memory version counter).
  - `couchbase_store.CouchbaseSessionStore` (Layer 2/3, real Couchbase SDK).

Both apply a single `SESSION_TTL` to the session doc and to the
`session_results` side collection it references (design §6 "Retention").

CAS / exactly-once resume (D45):
    `get_session_with_cas` returns the doc alongside an opaque CAS token.
    `resume_checkpoint` re-reads under the hood and only commits if the CAS
    token still matches the currently-stored version — mirroring the
    `bucket.replace(session_id, doc, cas=doc_cas)` snippet in design §6.
    Concurrent resumers race: exactly one wins (`CASMismatchError` for the
    loser); a checkpoint that is already `consumed` (or absent) raises
    `AlreadyConsumedError` regardless of CAS.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from .models import (
    AnalysisState,
    FinalizationBlockKind,
    PauseCheckpoint,
    SessionDoc,
    TrailEntry,
    TurnMessage,
)


class AlreadyConsumedError(Exception):
    """Raised when `resume_checkpoint` is called on a checkpoint that is
    absent or already consumed (D45 exactly-once)."""


class CASMismatchError(Exception):
    """Raised when a concurrent write already advanced the session doc's
    version between `get_session_with_cas` and `resume_checkpoint` (D45)."""


class SessionStore(Protocol):
    """The persistence seam `context/assembly.py` and Pass B's `AgentLoop` depend on."""

    async def create_session(self, session_id: str) -> SessionDoc:
        """Create (or return the existing) session document for *session_id*."""
        ...

    async def get_or_create_session(self, session_id: str) -> SessionDoc:
        """Load *session_id*, creating a fresh document if none exists yet."""
        ...

    async def load_trail(self, session_id: str) -> list[TrailEntry]:
        """Return the `tool_trail` for *session_id* (empty list if none)."""
        ...

    async def append_message(self, session_id: str, message: TurnMessage) -> None:
        """Append one user/assistant message and bump `last_activity`."""
        ...

    async def append_trail_entry(self, session_id: str, entry: TrailEntry) -> None:
        """Append one `TrailEntry` and bump `last_activity`."""
        ...

    async def bump_last_activity(self, session_id: str) -> None:
        """Refresh `last_activity` (and, transitively, the TTL) without other writes."""
        ...

    async def write_full_result(
        self, session_id: str, result_id: str, result_full: dict[str, Any]
    ) -> str:
        """Persist a full (non-preview) tool result to the results side-collection.

        Returns the `result_full_ref` string to store on the owning `TrailEntry`.
        Applies the same `SESSION_TTL` as the parent session doc (design §6).
        """
        ...

    async def read_full_result(
        self, session_id: str, result_full_ref: str
    ) -> dict[str, Any] | None:
        """Return the full (non-preview) tool result at *result_full_ref* (a
        `result::<uuid>` key), or `None` if absent/expired.

        The read-back counterpart of `write_full_result`, added for the D46 full
        tool I/O trail (the Slice-2 learning loader needs the full result for
        shape/inspection). READ-ONLY (D72): never mutates the session or the
        result doc. A TTL-expired result is a tolerated `None`, not an error.
        """
        ...

    async def write_pause_checkpoint(self, session_id: str, checkpoint: PauseCheckpoint) -> None:
        """Set a new pause checkpoint (unconditional write — pause creation, not resume)."""
        ...

    async def apply_analysis_state(
        self,
        session_id: str,
        turn_index: int,
        merge: Callable[[AnalysisState | None], AnalysisState],
    ) -> AnalysisState:
        """Read-modify-write the turn's `analysis_state`, returning the new value.

        Takes the MERGE, not the result (03 §B.1). `_mutate_with_cas_retry`
        documents its precondition plainly: the callback "may be called more than
        once (once per retry) against a freshly re-read document, so it must not
        carry any state of its own across calls". A caller that loads the state,
        computes a merged `AnalysisState`, and hands that OBJECT to a `setattr`
        callback breaks exactly that: on a CAS conflict the callback re-runs
        against a fresh doc but writes a value derived from the STALE read,
        clobbering the winner — the lost-update class the helper exists to
        prevent. Passing the merge instead means the recomputation happens
        against whatever the retry actually read.

        *merge* receives the LIVE state (`live_analysis_state`, so `None` when
        absent OR from another turn) and returns the state to persist. It runs
        INSIDE the retry, so it is the right place for validation that depends on
        the current state (mode, known ids, block-evidence distinctness): raising
        from it aborts the write with nothing persisted. Validation that depends
        only on the payload or on the trail belongs OUTSIDE, before the call.
        """
        ...

    async def claim_finalization_block(
        self,
        session_id: str,
        turn_index: int,
        window_count: int,
        kind: FinalizationBlockKind,
    ) -> bool:
        """Claim THE forced finalization re-round of *kind* for (*turn_index*,
        *window_count*) (05 §C.1, §J.3).

        Returns `True` when this caller got it, `False` when that turn-and-window's
        allowance for that KIND (`MAX_FINALIZATION_BLOCKS_PER_WINDOW`) is spent.

        *kind* IS PART OF THE KEY, NOT A LABEL. `intents` and `answer_shape` hold
        INDEPENDENT per-window allowances, so a turn refused for pending intents can
        still be refused once for answer shape in the same window. They shared one
        allowance until 2026-08-12; live measurement showed the intents nudge
        consuming it first in 2 of 4 three-part runs and starving the shape gate
        (05 §J.3). It is REQUIRED, with no default: this Protocol has four
        implementations, two of them hand-written proxies in `scripts/`, and a
        defaulted parameter is exactly what a proxy forwards silently and wrongly.

        IT MUST BE PERSISTED, and that is the whole reason this method exists. A
        counter local to `_run_loop_body` does NOT give "per window": that
        function is re-entered once per `run()` AND once per resume of any kind —
        an `askUser` resume and a `_resume_blueprint` both keep `window_count`
        unchanged — so a local counter resets on every resume while the window
        number stands still, and forced re-rounds become unbounded (user-paced,
        but unbounded). An exit-#1 refusal leaves NO persisted artifact by design
        (05 §B.2), so it cannot be reconstructed from the trail either.

        Keyed by (TURN, WINDOW, KIND) on `SessionDoc.finalization_blocks`
        (`{"0:2:intents": 1}`, via `models.finalization_block_key`) so "one per
        budget window" is literal WITHIN a turn AND within a kind, and NOT on
        `AnalysisState`, which is model-writable and unknown-key-rejecting.

        THE TURN INDEX IS NOT DECORATION. `window_count` restarts at 1 for every
        external turn while this map persists on the document and is never cleared,
        so a window-only key silently killed the whole mechanism from a session's
        second block-spending turn onward — see `finalization_block_key`.
        """
        ...

    async def get_session_with_cas(self, session_id: str) -> tuple[SessionDoc, Any]:
        """Return `(doc, cas_token)` for CAS-guarded resume (D45)."""
        ...

    async def resume_checkpoint(self, session_id: str, cas: Any, answer: str) -> SessionDoc:
        """CAS-consume the pause checkpoint and append the user's *answer*.

        Raises:
            AlreadyConsumedError: checkpoint is absent or already consumed.
            CASMismatchError: a concurrent resume already won the race.
        """
        ...

    # --- Learning loop (Track-B Slice 1, D96) — additive, read-only w.r.t.
    # request-path data: the only write is advancing the lifecycle flag. ---

    async def scan_idle_sessions(
        self,
        *,
        statuses: list[str],
        last_activity_before: str,
        limit: int,
    ) -> list[tuple[SessionDoc, Any]]:
        """Return `(doc, cas)` for sessions whose `learning_status` is in
        *statuses* and whose `last_activity` is strictly older than
        *last_activity_before* (an ISO-8601 cutoff), capped at *limit*.

        The sweeper (D96 §6) uses this to detect idle sessions to claim. Each
        returned CAS is a best-effort snapshot for the sweeper's subsequent
        CAS-guarded `transition_learning_status`; a doc that changes between the
        scan and the transition simply mismatches and is skipped.
        """
        ...

    async def transition_learning_status(
        self,
        session_id: str,
        expected_from: str,
        to: str,
        cas: Any,
        *,
        content_hash: str | None = None,
        assert_from: bool = True,
    ) -> Any:
        """CAS-guarded `learning_status` transition (D96 single-writer-per-session).

        Reads the doc under *cas*, asserts `learning_status == expected_from`
        (unless *assert_from* is False — the `* → dead_letter` transition #5 has
        no `from` assertion), sets `learning_status = to`, optionally records
        *content_hash* on `learning_content_hash`, and writes back CAS-guarded.

        Returns the new CAS token on success.

        Raises:
            CASMismatchError: the CAS no longer matches (a peer won the race) OR
                *assert_from* is set and the current status is not *expected_from*
                (the session was resumed / already advanced) — both are "skip
                this session", so the sweeper/consumer treats them identically.
        """
        ...
