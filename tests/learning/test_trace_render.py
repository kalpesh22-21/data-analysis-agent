"""Unit tests for the pure renderers (learning/trace/render.py).

`render_session_trace` and `render_session_trace_json` do NO I/O and NO store
access — they format a `SessionTrace`. These tests build a trace via the real
reconstructor (so the rendered shape is realistic) and pin: the key identifiers a
human reads for (session_id, candidate_ids, intent, a quote), JSON validity +
top-level keys, the no-color / no-ANSI contract, the `pending` leakage sentinel
rendering without raising, and the session-not-found rendering.
"""

from __future__ import annotations

import json

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.verdicts import LeakageVerdict
from data_agent.learning.trace.reconstruct import reconstruct_session_trace
from data_agent.learning.trace.render import render_session_trace, render_session_trace_json

from ._trace_fixtures import (
    ReadOnlySessionStore,
    make_candidate,
    make_evidence,
    make_session,
)

HASH = "hash-1"
SID = "sess-1"
_INTENT = "total earnings for a department in a given year"
_ANSI = "\033["  # == "\x1b[" — the CSI prefix of every ANSI escape this module emits


async def _rich_trace():
    """Reconstruct a realistic trace: a done session + two candidates, one with a
    settled leakage verdict + resolved evidence quote."""
    session = make_session(SID, learning_content_hash=HASH, learning_status="done")
    session_store = ReadOnlySessionStore(session)
    candidates = InMemoryCandidateStore()
    audit = InMemoryAuditStore()

    ref = "evidence::sess-1::a"
    await audit.snapshot(ref, make_evidence(ref, quote="Jane Doe earns $85,000"))
    await candidates.put(make_candidate(HASH, 0, status="extracted", evidence_refs=(ref,)))
    await candidates.put(
        make_candidate(
            HASH,
            1,
            status="validated",
            entity_scan=LeakageVerdict(result="pass", scanner="regex+ner+llm").to_doc(),
        )
    )
    return await reconstruct_session_trace(SID, session_store, candidates, audit)


# --- 1 (render side). key identifiers present in the human report ------------


async def test_render_text_contains_key_identifiers():
    """The human report surfaces the session_id, both candidate ids, the learned
    intent, and a resolved evidence quote — the whole point of the ONE view."""
    trace = await _rich_trace()
    out = render_session_trace(trace, use_color=False)

    assert SID in out
    assert "candidate::hash-1::0" in out
    assert "candidate::hash-1::1" in out
    assert _INTENT in out
    assert "Jane Doe earns $85,000" in out


# --- 10. render is pure / no color ------------------------------------------


async def test_render_no_color_has_no_ansi_escapes():
    """`use_color=False` ⇒ the report contains NO ANSI escape sequence (pipe-safe)."""
    trace = await _rich_trace()
    out = render_session_trace(trace, use_color=False)
    assert _ANSI not in out
    assert "\x1b" not in out


async def test_render_with_color_emits_ansi_escapes():
    """(positive control) `use_color=True` DOES colorize — proving the no-color test
    above is meaningful and not vacuous."""
    trace = await _rich_trace()
    out = render_session_trace(trace, use_color=True)
    assert _ANSI in out


# --- 9. render JSON validity + top-level keys -------------------------------


async def test_render_json_is_parseable_with_expected_keys():
    """`render_session_trace_json` returns a JSON string that `json.loads` round-trips,
    with the stable top-level keys and the reconstructed content faithfully carried."""
    trace = await _rich_trace()
    raw = render_session_trace_json(trace)

    doc = json.loads(raw)  # must not raise
    assert set(doc) == {"session_id", "session", "candidates", "errors"}
    assert doc["session_id"] == SID
    assert doc["session"]["session_id"] == SID
    assert [c["envelope"]["candidate_id"] for c in doc["candidates"]] == [
        "candidate::hash-1::0",
        "candidate::hash-1::1",
    ]
    # The evidence quote survives into the JSON snapshot of the first candidate.
    assert doc["candidates"][0]["evidence"][0]["quote"] == "Jane Doe earns $85,000"


# --- 6 (render side). pending leakage renders "pending", never raises --------


async def test_render_pending_leakage_shows_pending_and_does_not_raise():
    """A candidate whose `entity_scan` is the S3 `{"result": "pending"}` sentinel
    renders as `pending` (via the `is_settled` guard) and NEVER raises — a naive
    `LeakageVerdict.from_doc` on the pending shape would throw."""
    session = make_session(SID, learning_content_hash=HASH)
    session_store = ReadOnlySessionStore(session)
    candidates = InMemoryCandidateStore()
    await candidates.put(make_candidate(HASH, 0, entity_scan={"result": "pending"}))

    trace = await reconstruct_session_trace(SID, session_store, candidates, InMemoryAuditStore())
    out = render_session_trace(trace, use_color=False)  # must not raise

    assert "leakage(entity_scan): pending" in out


# --- session-not-found renders cleanly (text + JSON) ------------------------


async def test_render_missing_session_is_clean_text_and_json():
    """A trace with no session (reconstruct found nothing) renders a clear
    'not found' report and a valid JSON doc with `session: null` — no crash."""
    session_store = ReadOnlySessionStore(None)
    trace = await reconstruct_session_trace(SID, session_store, InMemoryCandidateStore(), None)

    text = render_session_trace(trace, use_color=False)
    assert SID in text
    assert "not found" in text

    doc = json.loads(render_session_trace_json(trace))
    assert doc["session"] is None
    assert doc["candidates"] == []
    assert doc["errors"]  # the not-found note is carried through


async def test_render_notes_section_lists_errors():
    """Errors collected during reconstruction surface in a NOTES section of the
    human report (e.g. the no-audit-store note)."""
    session = make_session(SID, learning_content_hash=HASH)
    session_store = ReadOnlySessionStore(session)
    candidates = InMemoryCandidateStore()
    await candidates.put(make_candidate(HASH, 0, evidence_refs=("evidence::sess-1::x",)))

    trace = await reconstruct_session_trace(SID, session_store, candidates, None)
    out = render_session_trace(trace, use_color=False)

    assert "NOTES" in out
    assert "audit store not configured" in out
