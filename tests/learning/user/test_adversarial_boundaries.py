"""Adversarial QA on the S8 boundaries (invariant #5).

Two boundaries are load-bearing:
  * user-store RBAC — the entity-BEARING per-user store may touch ONLY its granted
    bucket, and a read never crosses users (no cross-user surface, D17).
  * schema_edit — the highest-stakes target NEVER auto-commits to the catalog and
    NEVER makes a real network call; the human MERGE is the gate (D18/D53).

These are pinned as passing hardening (the boundaries held under attack). Any real
hole would be filed as a strict-xfail; none was found here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.schema_edit import (
    PullRequestResult,
    PullRequestSpec,
    SchemaEditPRStage,
)
from data_agent.learning.stage import StageContext
from data_agent.learning.triage import TriageVerdict
from data_agent.learning.user import InMemoryUserKnowledgeStore, UserKnowledgeCommitStage
from data_agent.learning.user.models import UserKnowledgeRecord
from data_agent.learning.user.store import UserKnowledgeAccessError

from ..extractor.helpers import make_summary

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"
_KEEP = TriageVerdict(decision="keep", reason="K1", target_hints=("user",))


def _ctx() -> StageContext:
    return StageContext(summary=make_summary(), verdict=_KEEP)


def _record(user_id: str, rid: str) -> UserKnowledgeRecord:
    return UserKnowledgeRecord(
        record_id=rid, user_id=user_id, statement="mentions E12345",
        fact_type="frequent_entity", scope="user", structured={"emp": "E12345"},
        source_session="s", source_trace="t", evidence_refs=(),
    )


# --- user-store RBAC boundary -------------------------------------------------


def test_user_store_denies_any_bucket_but_its_grant():
    """The RBAC role is scoped to ONE bucket; open_bucket raises for anything else —
    the store cannot be steered to touch the audit / candidate / global buckets."""
    store = InMemoryUserKnowledgeStore(bucket="user_knowledge")
    assert store.open_bucket("user_knowledge") is store
    for forbidden in ("learning_audit", "learning_candidates", "global_knowledge", "neo4j"):
        with pytest.raises(UserKnowledgeAccessError):
            store.open_bucket(forbidden)


async def test_user_store_read_never_crosses_users():
    """list_for_user returns ONLY the requested user's rows — no cross-user surface,
    even though all users share the one bucket."""
    store = InMemoryUserKnowledgeStore()
    await store.commit(_record("user-A", "userknow::user-A::c1"))
    await store.commit(_record("user-A", "userknow::user-A::c2"))
    await store.commit(_record("user-B", "userknow::user-B::c9"))

    a_rows = await store.list_for_user("user-A")
    b_rows = await store.list_for_user("user-B")

    assert {r.record_id for r in a_rows} == {"userknow::user-A::c1", "userknow::user-A::c2"}
    assert {r.user_id for r in a_rows} == {"user-A"}
    assert {r.record_id for r in b_rows} == {"userknow::user-B::c9"}
    # a bogus user id sees nothing (no fall-through to all rows)
    assert await store.list_for_user("user-Z") == []


async def test_hostile_payload_user_id_cannot_write_into_another_user():
    """R6/D17: a poisoned `user_knowledge` candidate whose payload claims a FOREIGN
    `user_id` ('victim') must commit under the SESSION's authenticated user, not the
    payload's — the attacker can never write into another user's surface."""
    store = InMemoryUserKnowledgeStore()
    stage = UserKnowledgeCommitStage(store=store)
    doc = json.loads((FIXTURES / "s3_user_knowledge.json").read_text())
    doc["payload"] = {**doc["payload"], "user_id": "victim"}  # hostile payload claim
    env = CandidateEnvelope.from_doc(doc)

    # the authenticated session belongs to 'attacker' (make_summary → user_id)
    ctx = StageContext(summary=make_summary(session_id="s", user_id="attacker"), verdict=_KEEP)
    await stage.process(env, ctx)

    # the fact landed ONLY under the authenticated user; the victim's surface is empty
    assert [r.user_id for r in await store.list_for_user("attacker")] == ["attacker"]
    assert await store.list_for_user("victim") == []


async def test_user_knowledge_commit_drops_and_never_reaches_inbox():
    """The auto-commit stage writes to the per-user store then emits control='drop'
    so the entity-bearing envelope never lands in the shared candidate/inbox store."""
    store = InMemoryUserKnowledgeStore()
    stage = UserKnowledgeCommitStage(store=store)
    doc = json.loads((FIXTURES / "s3_user_knowledge.json").read_text())
    env = CandidateEnvelope.from_doc(doc)

    result = await stage.process(env, _ctx())

    assert result.control == "drop"
    assert store.commit_calls == 1
    # a non-user_knowledge candidate is a pass-through no-op (never committed)
    bp = replace(env, type="blueprint")
    result2 = await stage.process(bp, _ctx())
    assert result2.control == "continue"
    assert store.commit_calls == 1


# --- schema_edit: no catalog write, no real network ---------------------------


@dataclass
class RecordingGitClient:
    """Records PR requests; opens no branch, makes no network call."""

    specs: list[PullRequestSpec] = field(default_factory=list)

    async def open_pull_request(self, spec: PullRequestSpec) -> PullRequestResult:
        self.specs.append(spec)
        return PullRequestResult(url="https://example.test/pr/1", number=1, branch=spec.branch)


async def test_schema_edit_never_auto_commits_and_routes_to_human_merge():
    """A schema_edit produces a PR + in_review (human MERGE is the gate) — never a
    direct catalog write. The stage has NO catalog client to write with (structural
    guarantee), and the only side effect is the injected, non-network git double."""
    git = RecordingGitClient()
    stage = SchemaEditPRStage(git_client=git)
    env = CandidateEnvelope.from_doc(json.loads((FIXTURES / "s3_schema_edit.json").read_text()))

    result = await stage.process(env, _ctx())

    assert result.envelope.status == CandidateStatus.IN_REVIEW  # human gate, not committed
    assert result.control == "route_inbox"
    assert result.envelope.payload["schema_edit_review"]["pr_opened"] is True
    # exactly one PR request to the injected double; no other collaborator exists
    assert len(git.specs) == 1
    # the stage's only fields are the git client + checks — no catalog writer is even
    # reachable to auto-commit with (belt-and-suspenders on the D18 no-auto-commit rule)
    assert not hasattr(stage, "catalog") and not hasattr(stage, "catalog_client")
