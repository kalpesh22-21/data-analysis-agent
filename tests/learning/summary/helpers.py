"""Shared builders for the Slice-2 loader tests (design §1/§2).

Construct `SessionDoc`s + a `LearningJob` reference and drive `load_session_summary`
through an `InMemorySessionStore` (which carries the read-only `read_full_result`,
D46). Reuses the message/trail builders from the parent learning conftest.
"""

from __future__ import annotations

from data_agent.learning.models import LearningJob
from data_agent.runtime.session.models import SessionDoc

from ..conftest import make_message, make_trail_entry  # re-exported for the tests

__all__ = ["make_message", "make_trail_entry", "make_doc", "make_job"]

_TS = "2026-07-01T00:00:00+00:00"


def make_doc(
    session_id: str = "sess",
    *,
    messages=(),
    tool_trail=(),
    last_activity: str = _TS,
    created_at: str = _TS,
    learning_status: str = "queued",
) -> SessionDoc:
    return SessionDoc(
        session_id=session_id,
        created_at=created_at,
        last_activity=last_activity,
        learning_status=learning_status,
        messages=list(messages),
        tool_trail=list(tool_trail),
    )


def make_job(
    session_id: str = "sess",
    *,
    user_id: str = "user-1",
    scope_ref: str = "scope-abc",
    trace_id: str = "trace-1",
    content_hash: str = "hash-1",
) -> LearningJob:
    return LearningJob(
        session_id=session_id,
        couchbase_doc_id=f"session::{session_id}",
        content_hash=content_hash,
        user_id=user_id,
        scope_ref=scope_ref,
        trace_id=trace_id,
    )
