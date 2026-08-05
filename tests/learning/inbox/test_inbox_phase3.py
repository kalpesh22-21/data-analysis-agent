"""Phase-3 inbox — the VALIDATED listing + the VERIFY and PROMOTE actions.

  * the VALIDATED listing surfaces validated (promotable) learning nodes with the
    `verified` flag exposed;
  * VERIFY flips `verified=true` on BOTH the landed node and the envelope, and requires
    status=validated;
  * PROMOTE requires `verified=true` (rejects an unverified node), emits MCP YAML whose
    `id` equals the landing node id, and moves the candidate → `promoted`. Blueprint AND
    knowledge.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
import yaml

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.inbox import InboxTransitionError, ReviewInbox
from data_agent.learning.promotion import PromotionPolicy, PromotionScheduler
from data_agent.learning.promotion.landing import landing_id

from ..promotion.helpers import (
    FakeHitCountReader,
    FakeLandingWriter,
    FakeWarehouseProbe,
    make_blueprint_candidate,
    with_type,
)

KEY = "sha256:single-bp"


def _wired_inbox(store, writer=None) -> ReviewInbox:
    scheduler = PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({KEY: 5}),
        policy=PromotionPolicy(blueprint_hit_threshold=3),
        landing_writer=writer,
        require_landing=writer is not None,
        clock=lambda: "2026-08-01T00:00:00+00:00",
    )
    return ReviewInbox(store, scheduler=scheduler)


def _validated_blueprint(*, verified: bool):
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY)
    return replace(env, verified=verified)


def _validated_knowledge(*, verified: bool):
    env = with_type(
        make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY),
        "global_knowledge",
    )
    payload = dict(env.payload)
    payload["statement"] = "the fiscal year starts in April"
    payload["scope"] = "fiscal calendar"
    return replace(env, payload=payload, verified=verified)


# --- the VALIDATED (promotable) listing ----------------------------------------


async def test_validated_listing_exposes_verified_flag() -> None:
    store = InMemoryCandidateStore()
    await store.put(replace(_validated_blueprint(verified=True),
                            candidate_id="candidate::v::1", content_hash="h1"))
    await store.put(replace(_validated_blueprint(verified=False),
                            candidate_id="candidate::v::0", content_hash="h0"))

    items = await ReviewInbox(store).list(status=CandidateStatus.VALIDATED)

    by_id = {it.candidate_id: it for it in items}
    assert by_id["candidate::v::1"].verified is True
    assert by_id["candidate::v::0"].verified is False
    assert all(it.status == "validated" for it in items)


# --- VERIFY --------------------------------------------------------------------


async def test_verify_flips_node_and_envelope_true() -> None:
    store = InMemoryCandidateStore()
    env = _validated_blueprint(verified=False)
    await store.put(env)
    writer = FakeLandingWriter()
    inbox = _wired_inbox(store, writer)

    result, node_stamped = await inbox.verify(env.candidate_id)

    assert result.verified is True  # envelope flipped
    assert node_stamped is True  # the neo4j node was stamped
    assert [e.candidate_id for e in writer.verified] == [env.candidate_id]  # node flipped
    assert (await store.get(env.candidate_id)).verified is True
    assert (await store.get(env.candidate_id)).status == CandidateStatus.VALIDATED


async def test_verify_reports_node_stamped_false_when_node_write_fails() -> None:
    """The node write FAILS OPEN: the envelope still flips `verified=true` (inbox
    source-of-truth) but `node_stamped=False` is reported so the UI can prompt a
    re-verify — never a silent 'verified' over an unverified node."""
    store = InMemoryCandidateStore()
    env = _validated_blueprint(verified=False)
    await store.put(env)
    writer = FakeLandingWriter(verify_fail=RuntimeError("neo4j down"), verify_fail_times=99)
    inbox = _wired_inbox(store, writer)

    result, node_stamped = await inbox.verify(env.candidate_id)

    assert result.verified is True  # envelope still flipped (fail-open)
    assert node_stamped is False  # but the node write did NOT land


async def test_verify_reports_node_stamped_false_when_node_not_landed() -> None:
    """A verify of a never-landed node (MATCH-by-id misses) reports `node_stamped=False`
    even though `mark_verified` ran without error."""
    store = InMemoryCandidateStore()
    env = _validated_blueprint(verified=False)
    await store.put(env)
    writer = FakeLandingWriter(verify_stamped=False)
    inbox = _wired_inbox(store, writer)

    _result, node_stamped = await inbox.verify(env.candidate_id)
    assert node_stamped is False


async def test_verify_requires_validated() -> None:
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    with pytest.raises(InboxTransitionError):
        await ReviewInbox(store).verify(env.candidate_id)


# --- PROMOTE -------------------------------------------------------------------


async def test_promote_rejects_unverified() -> None:
    store = InMemoryCandidateStore()
    env = _validated_blueprint(verified=False)  # validated but NOT verified
    await store.put(env)
    with pytest.raises(InboxTransitionError, match="not verified"):
        await ReviewInbox(store).promote(env.candidate_id)
    # The candidate was NOT moved.
    assert (await store.get(env.candidate_id)).status == CandidateStatus.VALIDATED


async def test_promote_blueprint_emits_yaml_and_moves_to_promoted() -> None:
    store = InMemoryCandidateStore()
    env = _validated_blueprint(verified=True)
    await store.put(env)

    emit = await ReviewInbox(store).promote(env.candidate_id)

    doc = yaml.safe_load(emit.yaml)
    assert doc["id"] == landing_id(env)  # the node id verbatim
    assert doc["status"] == "validated"
    assert "verified" not in doc and "source" not in doc
    assert emit.target_path == "app/corpus/data/blueprints/"
    # Terminal move — it drops out of the validated listing.
    assert (await store.get(env.candidate_id)).status == CandidateStatus.PROMOTED


async def test_promote_knowledge_emits_yaml_and_moves_to_promoted() -> None:
    store = InMemoryCandidateStore()
    env = _validated_knowledge(verified=True)
    await store.put(env)

    emit = await ReviewInbox(store).promote(env.candidate_id, doc_id="hr-policy-fiscal")

    doc = yaml.safe_load(emit.yaml)
    assert doc["id"] == landing_id(env)
    assert set(doc) == {"id", "title", "doc_id", "status", "text"}
    assert doc["doc_id"] == "hr-policy-fiscal"
    assert emit.target_path == "app/corpus/data/knowledge/"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.PROMOTED


async def test_promote_requires_validated_or_promoted() -> None:
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    with pytest.raises(InboxTransitionError):
        await ReviewInbox(store).promote(env.candidate_id)


async def test_promote_is_idempotent_reemit_from_promoted_without_move() -> None:
    """A re-promote of an already-`promoted` candidate REGENERATES the same YAML with NO
    status move (the emit is one-shot in the HTTP response, so a lost/abandoned/revived
    PR must be recoverable). The `id` and target path are byte-stable across re-emits."""
    store = InMemoryCandidateStore()
    env = _validated_blueprint(verified=True)
    await store.put(env)
    inbox = ReviewInbox(store)

    first = await inbox.promote(env.candidate_id)
    assert (await store.get(env.candidate_id)).status == CandidateStatus.PROMOTED

    # A second promote (now from `promoted`) re-emits WITHOUT moving the status.
    second = await inbox.promote(env.candidate_id)
    assert second.yaml == first.yaml
    assert (await store.get(env.candidate_id)).status == CandidateStatus.PROMOTED
