"""CouchbaseSessionStore — the real `SessionStore` (Layer 2/3), on `acouchbase`.

Session documents live at `session::<session_id>`, full tool results at `result::<uuid>`
in a second collection; both share the single `SESSION_TTL`, applied via `expiry=` on
every write that creates or refreshes a document.

CAS (D45): `get_session_with_cas` reads the SDK's `Result.cas`, and `resume_checkpoint`
replaces under it, translating `CasMismatchException` into this package's
`CASMismatchError` so callers never import SDK exception types. EVERY mutating write
goes through `_mutate_with_cas_retry` — a bare read/mutate/upsert silently loses a
concurrent write to the same session. After exhausting retries it raises rather than
dropping the write, so a persistently-contended session fails loudly.

CONNECT: every public coroutine opens with `await self._ensure_connected()` — see
`runtime/couchbase_connect.py` for the invariant and the introspection test that keeps a
newly-added method honest. `__init__` performs NO I/O and touches NO event loop, so this
store can be constructed at module import.

Layer-2 only: its tests skip unless `couchbase` is importable and `RUN_COUCHBASE_TESTS`
is set. The `_mutate_with_cas_retry` loop itself is unit-testable with a mocked
collection.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace as dc_replace
from typing import Any

from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.couchbase_connect import CouchbaseStoreBase, get_or_none
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

_MAX_CAS_RETRIES = 5
_CAS_RETRY_BACKOFF_SECONDS = 0.02

# The availability flag + the shared SDK symbols live in `couchbase_connect`; these
# are the extra types only this store writes with. Same guard posture: the module
# stays importable with no `couchbase` package installed (the constructor refuses).
try:  # pragma: no cover - exercised only when the couchbase SDK is installed
    from couchbase.exceptions import CasMismatchException, ValueFormatException
    from couchbase.options import QueryOptions, ReplaceOptions, UpsertOptions
except ImportError:  # pragma: no cover
    pass


def _session_key(session_id: str) -> str:
    return f"session::{session_id}"


def _result_key(result_id: str) -> str:
    return f"result::{result_id}"


class CouchbaseSessionStore(CouchbaseStoreBase):
    """Real `SessionStore` backed by a Couchbase cluster."""

    def __init__(
        self,
        settings: RuntimeSettings,
        cluster: Any = None,
        *,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        # NO I/O and NO event loop here (`CouchbaseStoreBase`): with no injected
        # cluster the SDK handles are built by the first `_ensure_connected()`, so
        # this store can be constructed at module import — which is how uvicorn
        # loads `scripts/run_ui_runtime*.py`'s `app`.
        self._init_couchbase_store(
            cluster=cluster,
            connection_string=settings.couchbase_connection_string,
            username=settings.couchbase_username,
            password=settings.couchbase_password,
            bucket=settings.couchbase_bucket,
            ttl_seconds=settings.session_ttl_seconds,
        )
        # Injectable so a Layer-1 CAS-retry test never actually sleeps.
        self._sleep = sleep

    def _bind_collections(self, bucket: Any) -> None:
        """TWO collections in a NAMED scope (sessions + full results), unlike the
        KV-only learning stores' single default collection."""
        scope = bucket.scope(self._settings.couchbase_scope)
        self._sessions = scope.collection(self._settings.couchbase_sessions_collection)
        self._results = scope.collection(self._settings.couchbase_results_collection)

    async def _get_doc(self, session_id: str) -> tuple[SessionDoc | None, Any]:
        result = await get_or_none(self._sessions, _session_key(session_id))
        if result is None:
            return None, None
        return SessionDoc.from_doc(result.content_as[dict]), result.cas

    async def _upsert_doc(self, session_id: str, doc: SessionDoc) -> None:
        await self._sessions.upsert(
            _session_key(session_id), doc.to_doc(), UpsertOptions(expiry=self._ttl)
        )

    async def _mutate_with_cas_retry(
        self, session_id: str, mutate: Callable[[SessionDoc], None]
    ) -> SessionDoc:
        """Read-with-CAS -> apply *mutate* in place -> CAS'd `replace`, with a bounded
        retry-on-`CasMismatchException` loop.

        *mutate* must be a pure in-place mutation of the freshly-read `doc`. It may be
        called more than once — once per retry, against a freshly re-read document — so
        it must not carry any state of its own across calls.
        """
        last_exc: CasMismatchException | None = None
        for attempt in range(_MAX_CAS_RETRIES):
            doc, cas = await self._get_doc(session_id)
            if doc is None:
                doc = await self._create_session(session_id)
                # Re-read ONLY to obtain the CAS `_create_session` does not return.
                # A concurrent `remove` (or a TTL expiry) landing in that window
                # gives back None again — and this loop must not dereference it:
                # the old code went straight into `mutate(doc)` and turned a
                # competing-writer race into an `AttributeError` from inside a
                # caller's mutate callback. Retrying re-creates and re-reads; if a
                # deleter keeps winning for the whole budget the loop exits below
                # with `CASMismatchError`, which is the right family (we lost to a
                # concurrent writer) and is what every caller already handles.
                doc, cas = await self._get_doc(session_id)
                if doc is None:
                    continue

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

    async def _create_session(self, session_id: str) -> SessionDoc:
        """Create (or return the already-present) session document — the shared
        create-on-miss body the public paths reuse once their own `_get_doc` missed.
        """
        await self._ensure_connected()
        doc, _ = await self._get_doc(session_id)
        if doc is not None:
            return doc
        now = _now()
        new_doc = SessionDoc(session_id=session_id, created_at=now, last_activity=now)
        await self._upsert_doc(session_id, new_doc)
        return new_doc

    async def get_or_create_session(self, session_id: str) -> SessionDoc:
        await self._ensure_connected()
        doc, _ = await self._get_doc(session_id)
        if doc is not None:
            return doc
        return await self._create_session(session_id)

    async def load_trail(self, session_id: str) -> list[TrailEntry]:
        await self._ensure_connected()
        doc, _ = await self._get_doc(session_id)
        return list(doc.tool_trail) if doc is not None else []

    async def append_message(self, session_id: str, message: TurnMessage) -> None:
        await self._ensure_connected()
        await self._mutate_with_cas_retry(session_id, lambda doc: doc.messages.append(message))

    async def append_trail_entry(self, session_id: str, entry: TrailEntry) -> None:
        await self._ensure_connected()
        await self._mutate_with_cas_retry(session_id, lambda doc: doc.tool_trail.append(entry))

    async def bump_last_activity(self, session_id: str) -> None:
        await self._ensure_connected()
        await self._mutate_with_cas_retry(session_id, lambda _doc: None)

    async def write_full_result(
        self, session_id: str, result_id: str, result_full: dict[str, Any] | list[Any]
    ) -> str:
        await self._ensure_connected()
        result_id = result_id or str(uuid.uuid4())
        key = _result_key(result_id)
        await self._results.upsert(key, result_full, UpsertOptions(expiry=self._ttl))
        return key

    async def read_full_result(
        self, session_id: str, result_full_ref: str
    ) -> dict[str, Any] | None:
        # READ-ONLY (D72): a plain KV get of the D46 full result. A TTL-expired /
        # purged result surfaces as `None` (the caller degrades to preview shape),
        # never an exception. `session_id` is unused for the flat `session_results`
        # keyspace but kept in the signature to match the in-memory fake's
        # per-session scoping and the Protocol.
        await self._ensure_connected()
        result = await get_or_none(self._results, result_full_ref)
        if result is None:
            return None
        try:
            content = result.value
        except (ValueError, ValueFormatException):
            logging.getLogger(__name__).warning(
                "Full result %r failed JSON decode; using preview", result_full_ref
            )
            return None
        if isinstance(content, dict):
            return content
        logging.getLogger(__name__).warning(
            "Full result %r decoded as %s; using preview", result_full_ref, type(content).__name__
        )
        return None

    async def write_pause_checkpoint(self, session_id: str, checkpoint: PauseCheckpoint) -> None:
        await self._ensure_connected()
        await self._mutate_with_cas_retry(
            session_id, lambda doc: setattr(doc, "pause_checkpoint", checkpoint)
        )

    async def apply_analysis_state(
        self,
        session_id: str,
        turn_index: int,
        merge: Callable[[AnalysisState | None], AnalysisState],
    ) -> AnalysisState:
        """Merge-callback read-modify-write of `analysis_state`.

        *merge* is re-invoked on every CAS retry against the FRESHLY re-read doc, so a
        concurrent state write cannot be clobbered by a value derived from a stale read.
        `applied` is reset at the top of each invocation rather than appended across
        them, so the returned state is the one the winning attempt actually wrote.
        """
        await self._ensure_connected()
        applied: list[AnalysisState] = []

        def _mutate(doc: SessionDoc) -> None:
            applied.clear()
            new_state = merge(live_analysis_state(doc, turn_index))
            doc.analysis_state = new_state
            applied.append(new_state)

        await self._mutate_with_cas_retry(session_id, _mutate)
        return applied[-1]

    async def claim_finalization_block(
        self,
        session_id: str,
        turn_index: int,
        window_count: int,
        kind: FinalizationBlockKind,
    ) -> bool:
        """CAS-guarded claim of the (turn, window, kind)'s ONE forced re-round.

        *kind* discriminates INDEPENDENT allowances on the one map, so two different
        gates refusing in the same window contend for nothing.

        The check and the increment happen inside the SAME `_mutate_with_cas_retry`
        callback, so a concurrent claimant cannot also see "unspent": whichever write
        lands first bumps the count, and the loser's callback re-runs against the fresh
        doc and returns `False`. `claimed` is reset at the top of each invocation, never
        appended across retries.

        A refused claim still writes (the helper always replaces the doc, bumping
        `last_activity`) — one benign write on the exhausted path buys a single atomic
        code path, and that path ends the turn anyway.
        """
        await self._ensure_connected()
        claimed: list[bool] = []
        key = finalization_block_key(turn_index, window_count, kind)

        def _mutate(doc: SessionDoc) -> None:
            claimed.clear()
            blocks = dict(doc.finalization_blocks or {})
            if blocks.get(key, 0) >= MAX_FINALIZATION_BLOCKS_PER_WINDOW:
                claimed.append(False)
                return
            blocks[key] = blocks.get(key, 0) + 1
            doc.finalization_blocks = blocks
            claimed.append(True)

        await self._mutate_with_cas_retry(session_id, _mutate)
        return claimed[-1]

    async def get_session_with_cas(self, session_id: str) -> tuple[SessionDoc, Any]:
        await self._ensure_connected()
        doc, cas = await self._get_doc(session_id)
        if doc is None:
            doc = await self._create_session(session_id)
            _, cas = await self._get_doc(session_id)
        return doc, cas

    async def resume_checkpoint(self, session_id: str, cas: Any, answer: str) -> SessionDoc:
        await self._ensure_connected()
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

    async def reopen_failed_resume(self, session_id: str, answer: str) -> bool:
        await self._ensure_connected()
        reopened: list[bool] = []

        def _mutate(doc: SessionDoc) -> None:
            reopened.clear()
            checkpoint = doc.pause_checkpoint
            if checkpoint is None or not checkpoint.consumed or not doc.messages:
                reopened.append(False)
                return
            latest = doc.messages[-1]
            if latest.role != "user" or latest.content != answer:
                reopened.append(False)
                return
            doc.messages.pop()
            doc.pause_checkpoint = dc_replace(checkpoint, consumed=False)
            reopened.append(True)

        await self._mutate_with_cas_retry(session_id, _mutate)
        return reopened[-1]

    # --- Learning loop (Track-B Slice 1, D96) ---

    async def scan_idle_sessions(
        self,
        *,
        statuses: list[str],
        last_activity_before: str,
        limit: int,
    ) -> list[tuple[SessionDoc, Any]]:
        """N1QL scan for idle sessions. `META().cas` is selected so each returned CAS is
        usable directly by `transition_learning_status`'s CAS-guarded `replace`.

        N1QL is NOT exempt from the connect gate: `AsyncClusterImpl.query` calls the
        SDK's own `_ensure_connected()` exactly like a KV op does.
        """
        await self._ensure_connected()
        keyspace = (
            f"`{self._settings.couchbase_bucket}`"
            f".`{self._settings.couchbase_scope}`"
            f".`{self._settings.couchbase_sessions_collection}`"
        )
        statement = (
            "SELECT META(s).id AS _meta_id, META(s).cas AS _meta_cas, s.* "
            f"FROM {keyspace} s "
            "WHERE s.learning_status IN $statuses "
            "AND s.last_activity < $cutoff "
            "ORDER BY s.last_activity ASC "
            "LIMIT $limit"
        )
        result = self._cluster.query(
            statement,
            QueryOptions(
                named_parameters={
                    "statuses": list(statuses),
                    "cutoff": last_activity_before,
                    "limit": int(limit),
                }
            ),
        )
        out: list[tuple[SessionDoc, Any]] = []
        async for row in result:
            cas = row.pop("_meta_cas")
            row.pop("_meta_id", None)
            out.append((SessionDoc.from_doc(row), cas))
        return out

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
        """Single-shot CAS transition (D96 single-writer-per-session).

        Reads fresh, asserts the `from` state, then `replace`s under the CALLER's *cas*
        (the scan snapshot). Deliberately NOT the retrying `_mutate_with_cas_retry`
        path: a lost transition race must be a SKIP, not a retry that would force the
        write. The lifecycle flag is the ONLY field written — bumping `last_activity`
        would resurrect the idle session (D72 read-only).
        """
        await self._ensure_connected()
        doc, _ = await self._get_doc(session_id)
        if doc is None:
            raise CASMismatchError(f"No session {session_id!r} to transition.")
        if assert_from and doc.learning_status != expected_from:
            raise CASMismatchError(
                f"learning_status for session {session_id!r} is "
                f"{doc.learning_status!r}, expected {expected_from!r}."
            )
        doc.learning_status = to
        if content_hash is not None:
            doc.learning_content_hash = content_hash
        try:
            result = await self._sessions.replace(
                _session_key(session_id),
                doc.to_doc(),
                # LOW-1: preserve the existing TTL — a learning-loop lifecycle
                # transition must NOT re-arm the 7-day session TTL (an idle
                # session being learned should still expire on its original
                # clock). `preserve_expiry=True` keeps the current expiry;
                # OMITTING expiry entirely would CLEAR the TTL (worse). If a
                # couchbase SDK < 4.1 without `preserve_expiry` is ever used,
                # this raises loudly at Layer 2 rather than silently mis-TTLing.
                ReplaceOptions(cas=cas, preserve_expiry=True),
            )
        except CasMismatchException as exc:
            raise CASMismatchError(
                f"CAS mismatch for session {session_id!r}: a peer advanced learning_status."
            ) from exc
        return result.cas
