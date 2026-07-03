"""D30-message-is-reference (design §4, matrix row 9, task item 11).

The enqueued envelope carries the session id + hashes + a best-effort CAS ONLY —
NEVER the transcript (messages / tool_trail / results) and NEVER a raw JWT or
`column_scope` (only a `scope_ref` id/hash may travel).
"""

from __future__ import annotations

from dataclasses import fields

from data_agent.learning.models import LearningJob, compute_content_hash

from .conftest import make_message, make_trail_entry

# The complete allow-list of fields that may appear on the wire (design §4).
_ALLOWED_FIELDS = {
    "session_id",
    "couchbase_doc_id",
    "content_hash",
    "cas",
    "user_id",
    "scope_ref",
    "trace_id",
    "session_closed_at",
}
# Anything transcript- or secret-bearing that must NEVER appear.
_FORBIDDEN_FIELDS = {
    "messages", "tool_trail", "trail", "content", "args", "result_preview",
    "result_full_ref", "results", "jwt", "token", "column_scope", "scope",
    "provenance", "context_summary_cache", "pause_checkpoint",
}


def test_dataclass_has_only_reference_fields():
    names = {f.name for f in fields(LearningJob)}
    assert names == _ALLOWED_FIELDS
    assert names & _FORBIDDEN_FIELDS == set()


async def test_enqueued_wire_fields_are_reference_only(store, queue, seed_session):
    doc = seed_session(
        store, "sess-ref",
        messages=[
            make_message(0, "user", "Jane Doe's salary?"),
            make_message(0, "assistant", "Jane Doe earns $85,000."),
        ],
        tool_trail=[make_trail_entry(args={"sql": "SELECT salary FROM hr.pay WHERE name='Jane Doe'"})],
    )
    job = LearningJob.from_doc(doc, content_hash=compute_content_hash(doc))
    await queue.enqueue(job)

    # The in-memory queue stores the SAME LearningJob the real queue XADDs; the
    # serialized wire shape is `to_fields()`.
    stored = queue._entries[queue._dedup[job.content_hash]]
    wire = stored.to_fields()

    assert set(wire.keys()) <= _ALLOWED_FIELDS
    assert set(wire.keys()) & _FORBIDDEN_FIELDS == set()

    # No transcript content leaks into ANY field value.
    blob = " ".join(wire.values())
    assert "Jane Doe" not in blob
    assert "85,000" not in blob
    assert "salary" not in blob
    assert "SELECT" not in blob

    # The reference points BACK at the doc (the consumer re-reads it).
    assert wire["session_id"] == "sess-ref"
    assert wire["couchbase_doc_id"] == "session::sess-ref"
    assert wire["content_hash"] == job.content_hash


def test_none_optionals_are_dropped_not_encoded_empty():
    # Slice-1 docs carry no user/scope/trace; those must be ABSENT keys, not "".
    job = LearningJob(session_id="s", couchbase_doc_id="session::s", content_hash="h")
    wire = job.to_fields()
    assert "user_id" not in wire
    assert "scope_ref" not in wire
    assert "trace_id" not in wire
    assert wire == {"session_id": "s", "couchbase_doc_id": "session::s", "content_hash": "h"}


def test_wire_round_trips():
    job = LearningJob(
        session_id="s", couchbase_doc_id="session::s", content_hash="h",
        cas="123", scope_ref="scope-abc", trace_id="t-1", session_closed_at="2026-07-01T00:00:00+00:00",
    )
    assert LearningJob.from_fields(job.to_fields()) == job
