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

from typing import Any, Protocol

from .models import PauseCheckpoint, SessionDoc, TrailEntry, TurnMessage


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
