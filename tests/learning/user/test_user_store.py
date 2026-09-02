"""S8 per-user knowledge store + auto-commit stage (D17) — Layer-1.

Covers: provisioning + the D95-style RBAC boundary (denied on other keyspaces),
per-user scoping (no cross-user surface), auto-commit, and the `control="drop"`
that keeps a user_knowledge candidate out of the review inbox.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.stage import StageContext
from data_agent.learning.triage import TriageVerdict
from data_agent.learning.user import (
    InMemoryUserKnowledgeStore,
    UserKnowledgeAccessError,
    UserKnowledgeCommitStage,
    UserKnowledgeRecord,
    mint_record_id,
)

from ..extractor.helpers import make_summary

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "learning"
_KEEP = TriageVerdict(decision="keep", reason="K1", target_hints=("user",))


def _user_candidate() -> CandidateEnvelope:
    with (_FIXTURES / "s3_user_knowledge.json").open() as fh:
        return CandidateEnvelope.from_doc(json.load(fh))


def _ctx() -> StageContext:
    return StageContext(summary=make_summary(), verdict=_KEEP)


# --- RBAC boundary (D95-style) -----------------------------------------------


async def test_store_open_keyspace_denies_other_keyspaces():
    store = InMemoryUserKnowledgeStore(keyspace="`pcm_iwant`.`user`.`knowledge`")
    # its own keyspace is allowed ...
    assert store.open_keyspace("`pcm_iwant`.`user`.`knowledge`") is store
    # ... every other keyspace is denied (the scoped RBAC role). Note the FIRST three:
    # they share the granted BUCKET and differ only in scope/collection, which is the
    # shared-bucket layout the guard has to survive. A bucket-name compare would have
    # admitted all three.
    for other in (
        "`pcm_iwant`.`learning`.`audit`",
        "`pcm_iwant`.`learning`.`candidates`",
        "`pcm_iwant`.`sessions`.`sessions`",
        "`pcm_iwant`.`user`.`_default`",
        "`user_knowledge`.`_default`.`_default`",
        "`pcm_iwant`",
    ):
        with pytest.raises(UserKnowledgeAccessError):
            store.open_keyspace(other)


async def test_store_reports_its_single_granted_keyspace():
    store = InMemoryUserKnowledgeStore(keyspace="`pcm_iwant`.`user`.`knowledge`")
    assert store.keyspace() == "`pcm_iwant`.`user`.`knowledge`"


async def test_default_keyspace_is_the_bucket_per_store_layout():
    """The shipped default reproduces the pre-scope deployment: the dedicated
    `user_knowledge` bucket's default scope + collection."""
    assert InMemoryUserKnowledgeStore().keyspace() == "`user_knowledge`.`_default`.`_default`"


# --- per-user scoping --------------------------------------------------------


async def test_list_for_user_is_scoped_no_cross_user_surface():
    store = InMemoryUserKnowledgeStore()
    a = UserKnowledgeRecord.from_candidate(_user_candidate(), user_id="user-1")
    b_env = replace(
        _user_candidate(),
        candidate_id="candidate::hash-userk-b::0",
        payload={"statement": "I mean APAC", "scope": "user", "user_id": "user-2"},
    )
    b = UserKnowledgeRecord.from_candidate(b_env, user_id="user-2")
    await store.commit(a)
    await store.commit(b)

    only_user1 = await store.list_for_user("user-1")
    assert [r.user_id for r in only_user1] == ["user-1"]
    only_user2 = await store.list_for_user("user-2")
    assert [r.user_id for r in only_user2] == ["user-2"]


async def test_record_id_is_deterministic_idempotent():
    env = _user_candidate()
    rec = UserKnowledgeRecord.from_candidate(env, user_id="user-1")
    assert rec.record_id == mint_record_id("user-1", env.candidate_id)
    store = InMemoryUserKnowledgeStore()
    await store.commit(rec)
    await store.commit(rec)  # re-commit upserts the same key
    assert len(store.all_records()) == 1


# --- auto-commit + drop-control ----------------------------------------------


async def test_commit_stage_auto_commits_and_drops():
    store = InMemoryUserKnowledgeStore()
    stage = UserKnowledgeCommitStage(store=store)
    env = _user_candidate()
    result = await stage.process(env, _ctx())

    # auto-committed to the per-user store ...
    assert store.commit_calls == 1
    committed = await store.list_for_user("user-1")
    assert len(committed) == 1
    assert committed[0].statement.startswith("I usually mean the NA region")
    # ... and dropped (never reaches the candidate holding store / inbox)
    assert result.control == "drop"
    assert result.envelope.status == "validated"


async def test_commit_stage_rejects_anonymous_candidate_without_poisoning_pipeline():
    store = InMemoryUserKnowledgeStore()
    stage = UserKnowledgeCommitStage(store=store)
    ctx = StageContext(summary=replace(make_summary(), user_id=""), verdict=_KEEP)

    result = await stage.process(_user_candidate(), ctx)

    assert result.control == "route_inbox"
    assert result.envelope.status == CandidateStatus.REJECTED
    assert result.envelope.route_reason == "missing_authenticated_user"
    assert store.commit_calls == 0


async def test_commit_stage_passes_through_non_user_targets():
    store = InMemoryUserKnowledgeStore()
    stage = UserKnowledgeCommitStage(store=store)
    bp = replace(_user_candidate(), type="blueprint")
    result = await stage.process(bp, _ctx())
    assert result.control == "continue"
    assert store.commit_calls == 0


async def test_record_round_trips_through_doc():
    rec = UserKnowledgeRecord.from_candidate(_user_candidate(), user_id="user-1")
    assert UserKnowledgeRecord.from_doc(rec.to_doc()) == rec
