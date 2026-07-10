"""CandidateEnvelope + InMemoryCandidateStore (D101) — ref-only persistence,
content-hash-derived id idempotency, put/get/list_by_status. Matrix rows;
task items 8 (ref-only) + 9 (idempotency).
"""

from __future__ import annotations

import json
from dataclasses import replace

from data_agent.learning.candidate import (
    CandidateStatus,
    InMemoryCandidateStore,
    build_envelope,
    mint_candidate_id,
)
from data_agent.learning.extractor.validation import to_candidate

from ..extractor.helpers import blueprint_raw, make_summary

_SECRET = "SSN-999-88-7777-Jane-Doe"


def _extracted(summary=None, *, quote=_SECRET):
    raw = blueprint_raw(evidence=[{"turn_ref": 0, "tool_call_ref": "tc1", "quote": quote}])
    out = to_candidate(raw, summary or make_summary(), known_rules=frozenset())
    assert not isinstance(out, type(None))
    return out


# --- item 9: id minting + idempotency ---------------------------------------


def test_mint_candidate_id_is_deterministic_by_content_hash_and_ordinal():
    assert mint_candidate_id("hash-1", 0) == "candidate::hash-1::0"
    assert mint_candidate_id("hash-1", 0) == mint_candidate_id("hash-1", 0)
    assert mint_candidate_id("hash-1", 0) != mint_candidate_id("hash-1", 1)
    assert mint_candidate_id("hash-2", 0) != mint_candidate_id("hash-1", 0)


async def test_reput_same_id_upserts_not_duplicates():
    store = InMemoryCandidateStore()
    summary = make_summary(content_hash="hash-X")
    cand = _extracted(summary)
    cid = mint_candidate_id(summary.content_hash, 0)

    env1 = build_envelope(cand, summary, candidate_id=cid, evidence_refs=("evidence::sess-1::a",))
    env2 = build_envelope(cand, summary, candidate_id=cid, evidence_refs=("evidence::sess-1::b",))
    await store.put(env1)
    await store.put(env2)

    # Same id ⇒ one doc (the re-processing UPSERTs, best-effort idempotency, D101).
    assert len(store.all_candidates()) == 1
    assert (await store.get(cid)).evidence_refs == ("evidence::sess-1::b",)


# --- item 8 (store layer): the persisted envelope is REF-ONLY ---------------


async def test_persisted_envelope_carries_refs_not_quotes():
    summary = make_summary()
    cand = _extracted(summary, quote=_SECRET)
    cid = mint_candidate_id(summary.content_hash, 0)
    env = build_envelope(cand, summary, candidate_id=cid,
                         evidence_refs=("evidence::sess-1::abc",))

    doc = env.to_doc()
    blob = json.dumps(doc)
    # The entity-bearing quote must appear NOWHERE in the persisted candidate.
    assert _SECRET not in blob
    # Only the evidence_ref (KV key into learning_audit) is carried.
    assert doc["provenance"]["evidence_ref"] == ["evidence::sess-1::abc"]
    assert env.status == CandidateStatus.EXTRACTED


async def test_envelope_status_is_extracted_and_round_trips():
    from data_agent.learning.candidate import CandidateEnvelope

    summary = make_summary()
    cand = _extracted(summary)
    env = build_envelope(cand, summary, candidate_id="candidate::h::0",
                         evidence_refs=("evidence::sess-1::abc",))
    assert env.status == "extracted"
    assert CandidateEnvelope.from_doc(env.to_doc()) == env


# --- fake put / get / list_by_status ----------------------------------------


async def test_get_missing_returns_none():
    store = InMemoryCandidateStore()
    assert await store.get("candidate::nope::0") is None


async def test_list_by_status_filters_and_orders():
    store = InMemoryCandidateStore()
    summary = make_summary()
    cand = _extracted(summary)
    for ordinal in range(3):
        env = build_envelope(cand, summary, candidate_id=f"candidate::h::{ordinal}",
                             evidence_refs=("evidence::sess-1::x",))
        await store.put(env)

    extracted = await store.list_by_status(CandidateStatus.EXTRACTED)
    assert len(extracted) == 3
    assert all(c.status == "extracted" for c in extracted)
    # A different status returns nothing (S3 only ever writes `extracted`).
    assert await store.list_by_status(CandidateStatus.VALIDATED) == []


async def test_list_by_status_respects_limit():
    store = InMemoryCandidateStore()
    summary = make_summary()
    cand = _extracted(summary)
    for ordinal in range(5):
        env = build_envelope(cand, summary, candidate_id=f"candidate::h::{ordinal}",
                             evidence_refs=("evidence::sess-1::x",))
        await store.put(env)
    assert len(await store.list_by_status(CandidateStatus.EXTRACTED, limit=2)) == 2


# --- order= (review queue ASC vs. rejected archive DESC) --------------------


async def _put_dated(store, summary, cand, *, status, ids_and_dates):
    for cid, created_at in ids_and_dates:
        env = replace(
            build_envelope(cand, summary, candidate_id=cid,
                           evidence_refs=("evidence::sess-1::x",)),
            status=status, created_at=created_at,
        )
        await store.put(env)


async def test_list_by_status_defaults_to_created_at_asc():
    """Default order (the review queue) is oldest-first — FIFO drain, unchanged."""
    store = InMemoryCandidateStore()
    summary = make_summary()
    cand = _extracted(summary)
    await _put_dated(store, summary, cand, status=CandidateStatus.IN_REVIEW,
                     ids_and_dates=[
                         ("candidate::h::1", "2026-07-03T00:00:01+00:00"),
                         ("candidate::h::0", "2026-07-03T00:00:00+00:00"),
                         ("candidate::h::2", "2026-07-03T00:00:02+00:00"),
                     ])
    got = await store.list_by_status(CandidateStatus.IN_REVIEW)
    assert [c.candidate_id for c in got] == [
        "candidate::h::0", "candidate::h::1", "candidate::h::2"
    ]
    # Explicit order="asc" is identical to the default.
    got_asc = await store.list_by_status(CandidateStatus.IN_REVIEW, order="asc")
    assert [c.candidate_id for c in got_asc] == [c.candidate_id for c in got]


async def test_rejected_archive_lists_newest_first_with_order_desc():
    """The durable archive lists DESC so a LIMIT trims OLD history, not present
    rejects — the most-recent rejections stay visible (ui-inbox-type-archive §Retention)."""
    store = InMemoryCandidateStore()
    summary = make_summary()
    cand = _extracted(summary)
    await _put_dated(store, summary, cand, status=CandidateStatus.REJECTED,
                     ids_and_dates=[
                         ("candidate::h::0", "2026-07-03T00:00:00+00:00"),
                         ("candidate::h::2", "2026-07-03T00:00:02+00:00"),
                         ("candidate::h::1", "2026-07-03T00:00:01+00:00"),
                     ])
    got = await store.list_by_status(CandidateStatus.REJECTED, order="desc")
    assert [c.candidate_id for c in got] == [
        "candidate::h::2", "candidate::h::1", "candidate::h::0"
    ]
    # DESC is the exact reverse of ASC (mirror, no divergent tiebreak).
    got_asc = await store.list_by_status(CandidateStatus.REJECTED, order="asc")
    assert [c.candidate_id for c in got] == list(
        reversed([c.candidate_id for c in got_asc])
    )


async def test_order_desc_limit_keeps_newest_not_oldest():
    """DESC + LIMIT keeps the NEWEST N — the regression the archive change guards."""
    store = InMemoryCandidateStore()
    summary = make_summary()
    cand = _extracted(summary)
    await _put_dated(store, summary, cand, status=CandidateStatus.REJECTED,
                     ids_and_dates=[
                         (f"candidate::h::{i}", f"2026-07-03T00:00:0{i}+00:00")
                         for i in range(5)
                     ])
    got = await store.list_by_status(CandidateStatus.REJECTED, limit=2, order="desc")
    assert [c.candidate_id for c in got] == ["candidate::h::4", "candidate::h::3"]
