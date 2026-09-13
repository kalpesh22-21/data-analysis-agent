"""InMemorySessionStore — the Layer-1 SessionStore fake (dict-backed, D45 CAS emulation).

CAS emulation: an integer version counter per session_id, bumped on every mutating
write. `get_session_with_cas` returns `(doc_copy, version)`; `resume_checkpoint` commits
only if that version is still current, else `CASMismatchError`. Under cooperative
asyncio two "concurrent" resumers both observe the same version and the second
deterministically loses — the real Couchbase race, without threads or a container.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from data_agent.timeutil import now_iso as _now

from .models import (
    MAX_FINALIZATION_BLOCKS_PER_WINDOW,
    AnalysisState,
    FinalizationBlockKind,
    PauseCheckpoint,
    SessionDoc,
    TrailEntry,
    TurnMessage,
    finalization_block_key,
    live_analysis_state,
)
from .store import AlreadyConsumedError, CASMismatchError


class InMemorySessionStore:
    """Dict-backed `SessionStore` fake — no I/O, deterministic, Layer-1 only."""

    def __init__(self) -> None:
        self._docs: dict[str, SessionDoc] = {}
        self._versions: dict[str, int] = {}
        # Scoped per session_id (outer key) so results from two different
        # sessions can never collide/overwrite each other in this fake store,
        # even though the returned `ref` string itself is session-agnostic
        # (matching CouchbaseSessionStore's flat `session_results` collection
        # keying — see couchbase_store.py's `_result_key`). This is what
        # `write_full_result`'s `session_id` parameter is for below.
        self._results: dict[str, dict[str, dict[str, Any] | list[Any]]] = {}

    def _bump_version(self, session_id: str) -> None:
        self._versions[session_id] = self._versions.get(session_id, 0) + 1

    async def get_or_create_session(self, session_id: str) -> SessionDoc:
        if session_id in self._docs:
            return self._docs[session_id]
        now = _now()
        doc = SessionDoc(session_id=session_id, created_at=now, last_activity=now)
        self._docs[session_id] = doc
        self._versions[session_id] = 0
        return doc

    async def load_trail(self, session_id: str) -> list[TrailEntry]:
        doc = self._docs.get(session_id)
        return list(doc.tool_trail) if doc is not None else []

    async def append_message(self, session_id: str, message: TurnMessage) -> None:
        doc = await self.get_or_create_session(session_id)
        doc.messages.append(message)
        doc.last_activity = _now()
        self._bump_version(session_id)

    async def append_trail_entry(self, session_id: str, entry: TrailEntry) -> None:
        doc = await self.get_or_create_session(session_id)
        doc.tool_trail.append(entry)
        doc.last_activity = _now()
        self._bump_version(session_id)

    async def bump_last_activity(self, session_id: str) -> None:
        doc = await self.get_or_create_session(session_id)
        doc.last_activity = _now()
        self._bump_version(session_id)

    async def write_full_result(
        self, session_id: str, result_id: str, result_full: dict[str, Any] | list[Any]
    ) -> str:
        ref = f"result::{result_id}"
        self._results.setdefault(session_id, {})[ref] = copy.deepcopy(result_full)
        return ref

    async def read_full_result(
        self, session_id: str, result_full_ref: str
    ) -> dict[str, Any] | None:
        # READ-ONLY (D72): dict lookup scoped by session_id (mirrors the fake's
        # per-session results map). A missing/purged ref → `None`. Deep-copied so
        # a caller mutating the returned dict cannot corrupt store state (LOW-a —
        # parity with Couchbase's fresh-parse + `get_session_with_cas`'s deepcopy).
        stored = self._results.get(session_id, {}).get(result_full_ref)
        if stored is not None and not isinstance(stored, dict):
            logging.getLogger(__name__).warning(
                "Full result %r decoded as %s; using preview",
                result_full_ref,
                type(stored).__name__,
            )
        return copy.deepcopy(stored) if isinstance(stored, dict) else None

    async def write_pause_checkpoint(self, session_id: str, checkpoint: PauseCheckpoint) -> None:
        doc = await self.get_or_create_session(session_id)
        doc.pause_checkpoint = checkpoint
        doc.last_activity = _now()
        self._bump_version(session_id)

    async def apply_analysis_state(
        self,
        session_id: str,
        turn_index: int,
        merge: Callable[[AnalysisState | None], AnalysisState],
    ) -> AnalysisState:
        """In-memory counterpart of the merge-callback contract.

        There is no CAS retry to drive here (this fake is single-writer by
        construction), but the ORDER matters and mirrors the real store: *merge* runs
        FIRST against the live state and may raise, and only a merge that returned
        normally mutates the doc — so a rejected call leaves the document
        byte-identical in both implementations.
        """
        doc = await self.get_or_create_session(session_id)
        new_state = merge(live_analysis_state(doc, turn_index))
        doc.analysis_state = new_state
        doc.last_activity = _now()
        self._bump_version(session_id)
        return new_state

    async def claim_finalization_block(
        self,
        session_id: str,
        turn_index: int,
        window_count: int,
        kind: FinalizationBlockKind,
    ) -> bool:
        """In-memory counterpart of the claim. The limit is checked and the counter
        incremented in ONE step, so two claimants for the same (turn, window, kind) can
        never both succeed — and two claimants for DIFFERENT kinds never contend at all.
        """
        doc = await self.get_or_create_session(session_id)
        blocks = dict(doc.finalization_blocks or {})
        key = finalization_block_key(turn_index, window_count, kind)
        if blocks.get(key, 0) >= MAX_FINALIZATION_BLOCKS_PER_WINDOW:
            return False
        blocks[key] = blocks.get(key, 0) + 1
        doc.finalization_blocks = blocks
        doc.last_activity = _now()
        self._bump_version(session_id)
        return True

    async def get_session_with_cas(self, session_id: str) -> tuple[SessionDoc, int]:
        doc = await self.get_or_create_session(session_id)
        cas = self._versions[session_id]
        # Return a deep copy so a caller mutating the returned doc cannot corrupt
        # store state outside the CAS-guarded resume_checkpoint write path
        # (mirrors Couchbase get(with_cas=True) returning an independent copy).
        return copy.deepcopy(doc), cas

    async def resume_checkpoint(self, session_id: str, cas: int, answer: str) -> SessionDoc:
        doc = self._docs.get(session_id)
        if doc is None or doc.pause_checkpoint is None or doc.pause_checkpoint.consumed:
            raise AlreadyConsumedError(f"No pending checkpoint for session {session_id!r}.")

        current_cas = self._versions[session_id]
        if cas != current_cas:
            raise CASMismatchError(
                f"CAS mismatch for session {session_id!r}: expected {cas}, found {current_cas}."
            )

        doc.pause_checkpoint = replace(doc.pause_checkpoint, consumed=True)
        next_turn_index = doc.messages[-1].turn_index if doc.messages else 0
        doc.messages.append(
            TurnMessage(
                turn_index=next_turn_index,
                role="user",
                content=answer,
                ts=_now(),
            )
        )
        doc.last_activity = _now()
        self._bump_version(session_id)
        return copy.deepcopy(doc)

    async def reopen_failed_resume(self, session_id: str, answer: str) -> bool:
        doc = self._docs.get(session_id)
        if (
            doc is None
            or doc.pause_checkpoint is None
            or not doc.pause_checkpoint.consumed
            or not doc.messages
        ):
            return False
        latest = doc.messages[-1]
        if latest.role != "user" or latest.content != answer:
            return False
        doc.messages.pop()
        doc.pause_checkpoint = replace(doc.pause_checkpoint, consumed=False)
        doc.last_activity = _now()
        self._bump_version(session_id)
        return True

    # --- Learning loop (Track-B Slice 1, D96) ---

    async def scan_idle_sessions(
        self,
        *,
        statuses: list[str],
        last_activity_before: str,
        limit: int,
    ) -> list[tuple[SessionDoc, int]]:
        status_set = set(statuses)
        matches: list[tuple[SessionDoc, int]] = []
        for session_id, doc in self._docs.items():
            if doc.learning_status not in status_set:
                continue
            # ISO-8601 timestamps are lexicographically ordered for a fixed
            # offset, matching the N1QL string comparison the real store uses.
            if doc.last_activity >= last_activity_before:
                continue
            matches.append((copy.deepcopy(doc), self._versions[session_id]))
        # Deterministic order (oldest-idle first), then cap.
        matches.sort(key=lambda pair: pair[0].last_activity)
        return matches[:limit]

    async def transition_learning_status(
        self,
        session_id: str,
        expected_from: str,
        to: str,
        cas: int,
        *,
        content_hash: str | None = None,
        assert_from: bool = True,
    ) -> int:
        doc = self._docs.get(session_id)
        if doc is None:
            raise CASMismatchError(f"No session {session_id!r} to transition.")

        current_cas = self._versions[session_id]
        if cas != current_cas:
            raise CASMismatchError(
                f"CAS mismatch for session {session_id!r}: expected {cas}, found {current_cas}."
            )
        if assert_from and doc.learning_status != expected_from:
            raise CASMismatchError(
                f"learning_status for session {session_id!r} is "
                f"{doc.learning_status!r}, expected {expected_from!r}."
            )

        doc.learning_status = to
        if content_hash is not None:
            doc.learning_content_hash = content_hash
        self._bump_version(session_id)
        return self._versions[session_id]
