"""CouchbaseSessionStore — the real `SessionStore` (Layer 2/3, design §6).

Uses the official async Couchbase Python SDK (`acouchbase`). Session documents
live at key `session::<session_id>` in `couchbase_sessions_collection`; full
tool results live at key `result::<uuid>` in `couchbase_results_collection`.
Both collections share the single `SESSION_TTL` (`RuntimeSettings.
session_ttl_seconds`) applied via `expiry=` on every write that creates or
refreshes a document — matching design §6's "single TTL, no partial purge".

CAS (D45): `get_session_with_cas` reads via `collection.get()` (the SDK's
`Result.cas` property); `resume_checkpoint` writes with
`collection.replace(key, doc, ReplaceOptions(cas=cas, ...))`, which raises
`couchbase.exceptions.CasMismatchException` on a losing race — translated
here to this package's `CASMismatchError` so callers never need to import the
Couchbase SDK's exception types directly.

B2 (2026-07-01, CRITICAL fix): EVERY mutating write (`append_message`,
`append_trail_entry`, `bump_last_activity`, `write_pause_checkpoint`) used to
do a bare read -> mutate -> `upsert()` with no CAS guard at all, so two
concurrent writes to the SAME session (e.g. two tool calls dispatched close
together, or a resume racing a still-finishing prior write) could silently
lose one of them — the second `upsert()` simply clobbers whatever the first
wrote, with no error, no retry, no signal. `_mutate_with_cas_retry` below
fixes this: every one of those four methods now reads-with-CAS, applies its
mutation, and writes via `collection.replace(key, doc, ReplaceOptions(cas=...))`
— on a losing race (`CasMismatchException`) it re-reads the LATEST doc and
retries the mutation from scratch, up to `_MAX_CAS_RETRIES` times (small
fixed backoff), mirroring the CAS pattern `resume_checkpoint` already used
correctly. After exhausting retries it raises `CASMismatchError` rather than
silently dropping the write — a persistently-contended session fails loudly
instead of losing data.

This module is exercised at Layer 2 only (a running Couchbase cluster is
required); its own tests (`tests/runtime/session/test_couchbase_store.py`)
are skipped automatically when `couchbase` is not importable or
`RUN_COUCHBASE_TESTS` is unset, so `uv run pytest` stays green with zero
infrastructure (design §8). `_mutate_with_cas_retry`'s retry LOOP itself is
unit-testable at Layer 1 with a mocked Couchbase collection (no real cluster
needed) — see `tests/runtime/session/test_couchbase_store_cas_retry.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import replace as dc_replace
from datetime import UTC, datetime, timedelta
from typing import Any

from data_agent.runtime.config import RuntimeSettings

from .models import PauseCheckpoint, SessionDoc, TrailEntry, TurnMessage
from .store import AlreadyConsumedError, CASMismatchError

_MAX_CAS_RETRIES = 5
_CAS_RETRY_BACKOFF_SECONDS = 0.02

try:  # pragma: no cover - exercised only when the couchbase SDK is installed
    from acouchbase.cluster import Cluster
    from couchbase.auth import PasswordAuthenticator
    from couchbase.exceptions import CasMismatchException, DocumentNotFoundException
    from couchbase.options import ClusterOptions, GetOptions, ReplaceOptions, UpsertOptions

    COUCHBASE_AVAILABLE = True
except ImportError:  # pragma: no cover
    COUCHBASE_AVAILABLE = False


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _session_key(session_id: str) -> str:
    return f"session::{session_id}"


def _result_key(result_id: str) -> str:
    return f"result::{result_id}"


class CouchbaseSessionStore:
    """Real `SessionStore` backed by a Couchbase cluster."""

    def __init__(
        self,
        settings: RuntimeSettings,
        cluster: Any = None,
        *,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        if not COUCHBASE_AVAILABLE:
            raise RuntimeError(
                "The 'couchbase' package is not installed. "
                "Install it (see pyproject.toml) to use CouchbaseSessionStore."
            )
        self._settings = settings
        self._cluster = cluster or Cluster(
            settings.couchbase_connection_string,
            ClusterOptions(
                PasswordAuthenticator(settings.couchbase_username, settings.couchbase_password)
            ),
        )
        bucket = self._cluster.bucket(settings.couchbase_bucket)
        scope = bucket.scope(settings.couchbase_scope)
        self._sessions = scope.collection(settings.couchbase_sessions_collection)
        self._results = scope.collection(settings.couchbase_results_collection)
        self._ttl = timedelta(seconds=settings.session_ttl_seconds)
        # Injectable so a Layer-1 CAS-retry test never actually sleeps.
        self._sleep = sleep

    async def _get_doc(self, session_id: str) -> tuple[SessionDoc | None, Any]:
        try:
            result = await self._sessions.get(_session_key(session_id), GetOptions())
        except DocumentNotFoundException:
            return None, None
        return SessionDoc.from_doc(result.content_as[dict]), result.cas

    async def _upsert_doc(self, session_id: str, doc: SessionDoc) -> None:
        await self._sessions.upsert(
            _session_key(session_id), doc.to_doc(), UpsertOptions(expiry=self._ttl)
        )

    async def _mutate_with_cas_retry(
        self, session_id: str, mutate: Callable[[SessionDoc], None]
    ) -> SessionDoc:
        """Read-with-CAS -> apply *mutate* in place -> CAS'd `replace`, with a
        bounded retry-on-`CasMismatchException` loop (B2).

        *mutate* must be a pure in-place mutation of the freshly-read `doc`
        (e.g. `doc.messages.append(...)`) — it may be called more than once
        (once per retry) against a freshly re-read document, so it must not
        carry any state of its own across calls.
        """
        last_exc: CasMismatchException | None = None
        for attempt in range(_MAX_CAS_RETRIES):
            doc, cas = await self._get_doc(session_id)
            if doc is None:
                doc = await self.create_session(session_id)
                doc, cas = await self._get_doc(session_id)

            mutate(doc)
            doc.last_activity = _now()
            try:
                await self._sessions.replace(
                    _session_key(session_id),
                    doc.to_doc(),
                    ReplaceOptions(cas=cas, expiry=self._ttl),
                )
                return doc
            except CasMismatchException as exc:
                last_exc = exc
                if attempt < _MAX_CAS_RETRIES - 1:
                    await self._sleep(_CAS_RETRY_BACKOFF_SECONDS * (attempt + 1))

        raise CASMismatchError(
            f"CAS mismatch for session {session_id!r}: gave up after "
            f"{_MAX_CAS_RETRIES} retries under sustained concurrent writes."
        ) from last_exc

    async def create_session(self, session_id: str) -> SessionDoc:
        doc, _ = await self._get_doc(session_id)
        if doc is not None:
            return doc
        now = _now()
        new_doc = SessionDoc(session_id=session_id, created_at=now, last_activity=now)
        await self._upsert_doc(session_id, new_doc)
        return new_doc

    async def get_or_create_session(self, session_id: str) -> SessionDoc:
        doc, _ = await self._get_doc(session_id)
        if doc is not None:
            return doc
        return await self.create_session(session_id)

    async def load_trail(self, session_id: str) -> list[TrailEntry]:
        doc, _ = await self._get_doc(session_id)
        return list(doc.tool_trail) if doc is not None else []

    async def append_message(self, session_id: str, message: TurnMessage) -> None:
        await self._mutate_with_cas_retry(session_id, lambda doc: doc.messages.append(message))

    async def append_trail_entry(self, session_id: str, entry: TrailEntry) -> None:
        await self._mutate_with_cas_retry(session_id, lambda doc: doc.tool_trail.append(entry))

    async def bump_last_activity(self, session_id: str) -> None:
        await self._mutate_with_cas_retry(session_id, lambda _doc: None)

    async def write_full_result(
        self, session_id: str, result_id: str, result_full: dict[str, Any]
    ) -> str:
        result_id = result_id or str(uuid.uuid4())
        key = _result_key(result_id)
        await self._results.upsert(key, result_full, UpsertOptions(expiry=self._ttl))
        return key

    async def write_pause_checkpoint(self, session_id: str, checkpoint: PauseCheckpoint) -> None:
        await self._mutate_with_cas_retry(
            session_id, lambda doc: setattr(doc, "pause_checkpoint", checkpoint)
        )

    async def get_session_with_cas(self, session_id: str) -> tuple[SessionDoc, Any]:
        doc, cas = await self._get_doc(session_id)
        if doc is None:
            doc = await self.create_session(session_id)
            _, cas = await self._get_doc(session_id)
        return doc, cas

    async def resume_checkpoint(self, session_id: str, cas: Any, answer: str) -> SessionDoc:
        doc, _ = await self._get_doc(session_id)
        if doc is None or doc.pause_checkpoint is None or doc.pause_checkpoint.consumed:
            raise AlreadyConsumedError(f"No pending checkpoint for session {session_id!r}.")

        doc.pause_checkpoint = dc_replace(doc.pause_checkpoint, consumed=True)
        next_turn_index = doc.messages[-1].turn_index if doc.messages else 0
        doc.messages.append(
            TurnMessage(turn_index=next_turn_index, role="user", content=answer, ts=_now())
        )
        doc.last_activity = _now()
        try:
            await self._sessions.replace(
                _session_key(session_id),
                doc.to_doc(),
                ReplaceOptions(cas=cas, expiry=self._ttl),
            )
        except CasMismatchException as exc:
            raise CASMismatchError(
                f"CAS mismatch for session {session_id!r}: a concurrent resume already won."
            ) from exc
        return doc
