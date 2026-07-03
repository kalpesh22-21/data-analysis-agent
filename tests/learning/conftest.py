"""Shared Layer-1 fixtures + builders for the Track-B Slice-1 learning-loop
tests (design §12 test matrix, D95/D96).

Everything here is infra-free: the dict-backed `InMemorySessionStore` and the
`InMemoryLearningQueue` fake, both of which carry the SAME CAS / PEL / redelivery
/ dead-letter / content-hash-dedup semantics as their real counterparts (design
§9), so these unit tests exercise the real state transitions.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from data_agent.learning.config import LearningSettings
from data_agent.learning.memory_queue import InMemoryLearningQueue
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import (
    ResultPreview,
    SessionDoc,
    TrailEntry,
    TurnMessage,
)

# A timestamp comfortably OLDER than any sweep cutoff (now − 30 min): a session
# stamped with this is unconditionally "idle". ISO-8601 with a fixed +00:00
# offset so the lexicographic comparison in `scan_idle_sessions` matches N1QL.
IDLE_TS = "2000-01-01T00:00:00+00:00"


class Clock:
    """A deterministic monotonic clock (seconds) for driving the in-memory
    queue's `min-idle` reclaim staleness without sleeping."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def store() -> InMemorySessionStore:
    return InMemorySessionStore()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def queue(clock: Clock) -> InMemoryLearningQueue:
    return InMemoryLearningQueue(now_fn=clock)


@pytest.fixture
def settings() -> LearningSettings:
    """Fast defaults: tiny reclaim min-idle so the dead-letter tests advance the
    clock in small steps; N=5 dead-letter threshold as in D96. `_env_file=None`
    so a developer's `.env` never leaks into the unit run."""
    return LearningSettings(
        _env_file=None,
        learning_idle_threshold_seconds=1800,
        learning_reclaim_min_idle_seconds=1,
        learning_max_deliveries=5,
        learning_batch_size=10,
        learning_block_ms=0,
        learning_scan_limit=200,
    )


def make_message(
    turn_index: int,
    role: str,
    content: str,
    *,
    ts: str = "2026-07-01T00:00:00+00:00",
    provenance=frozenset(),
) -> TurnMessage:
    return TurnMessage(
        turn_index=turn_index, role=role, content=content, ts=ts, provenance=provenance
    )


def make_trail_entry(
    *,
    turn_index: int = 0,
    tool_call_id: str = "call_1",
    tool_name: str = "runQuery",
    args: dict | None = None,
    status: str = "ok",
    error_code: str | None = None,
    provenance=frozenset(),
    result_preview: ResultPreview | None = None,
    result_full_ref: str | None = None,
    ts: str = "2026-07-01T00:00:00+00:00",
) -> TrailEntry:
    return TrailEntry(
        turn_index=turn_index,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        args={"sql": "SELECT 1"} if args is None else args,
        status=status,
        error_code=error_code,
        provenance=provenance,
        result_preview=result_preview,
        result_full_ref=result_full_ref,
        ts=ts,
    )


@pytest.fixture
def seed_session() -> Callable[..., SessionDoc]:
    """Return `seed(store, session_id, ...) -> SessionDoc` that DIRECTLY inserts
    a fully-formed session doc (with a caller-chosen `last_activity` /
    `learning_status`) into the fake store at version 0.

    Using the fake's internals is intentional: the request-path API can only
    stamp `last_activity` with `now`, but the sweeper tests need docs pinned to
    an arbitrary idle/fresh timestamp.
    """

    def _seed(
        store: InMemorySessionStore,
        session_id: str,
        *,
        last_activity: str = IDLE_TS,
        created_at: str = IDLE_TS,
        learning_status: str = "active",
        messages: list[TurnMessage] | None = None,
        tool_trail: list[TrailEntry] | None = None,
        learning_content_hash: str | None = None,
    ) -> SessionDoc:
        doc = SessionDoc(
            session_id=session_id,
            created_at=created_at,
            last_activity=last_activity,
            learning_status=learning_status,
            messages=list(messages or []),
            tool_trail=list(tool_trail or []),
            learning_content_hash=learning_content_hash,
        )
        store._docs[session_id] = doc
        store._versions[session_id] = 0
        return doc

    return _seed
