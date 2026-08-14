"""The inbox service wires the corpus terminal-status writer (PriorArt Slice 2).

**This is the process where humans actually reject.** `reject` and `retract` reach the
promotion scheduler through the INBOX SERVICE's write plane, not through
`scripts/run_learning_scheduler.py`. The first cut of this slice wired
`corpus_status=corpus` in both scheduler-entrypoint postures and missed this one, which
made the prerequisite the whole slice exists to satisfy — "rejected artifacts stop
surfacing as live prior art" — a no-op in deployment.

It was also SILENT: `PromotionScheduler._stamp_corpus_status` returns without logging
when no writer is wired, and the scheduler-level tests all construct the scheduler with
the writer already present. Only a COMPOSITION test catches an omission at the
composition root, so that is what this is.

Slug: PA-inbox-service-stamps-terminal-artifacts.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import data_agent.learning.inbox.service as service_module


class _FakeCorpus:
    """Stands in for `CouchbaseBlueprintCorpus`, which duck-types BOTH the
    `HitCountReader` and `CorpusStatusWriter` ports — the reason the service can (and
    must) pass one object for both."""

    def __init__(self) -> None:
        self.status_calls: list[tuple[str, str]] = []

    async def hit_count(self, canonical_key: str) -> int:
        return 0

    async def set_status(self, canonical_key: str, status: str) -> None:
        self.status_calls.append((canonical_key, status))


@pytest.fixture
def full_plane(monkeypatch):
    """Drive `_build_inbox_from_env` down its FULL write-plane branch with fakes, and
    hand back the scheduler it built plus the corpus it should have wired."""
    corpus = _FakeCorpus()
    captured: dict = {}

    class _Learning:
        learning_candidates_username = "u"
        learning_candidates_password = "p"
        learning_corpus_username = "u"
        learning_corpus_password = "p"
        learning_trace_verbose = False
        # Plan §4: the composition root builds a `PromotionPolicy` from settings, so this
        # stub must model those fields too. Values are the shipped defaults.
        learning_promotion_routing_threshold = 1
        learning_promotion_recurrence_weight = 0.0
        learning_promotion_scan_limit = 200
        learning_promotion_interval_seconds = 300.0
        learning_drift_freshness_seconds = 86_400.0
        learning_replay_recheck_interval_seconds = 43_200.0
        learning_review_score_cutoff = 0.0
        # The fail-to-review completion plane re-runs the write-router stages, so this
        # stub must model the dedup bands the composition root reads. Shipped defaults.
        learning_dedup_merge_threshold = 0.95
        learning_dedup_conflict_threshold = 0.83
        learning_recurrence_similarity_threshold = 0.90

    class _Runtime:
        mcp_url = "http://mcp"
        token_service_url = "http://token"
        token_issuer_api_key = "k"
        # The MCP requires these three on every call; the composition root gates
        # write-plane readiness on them and hands them to the minter.
        tenant_client_code = "CLIENT_A"
        tenant_proc_center = "PC01"
        tenant_jti = "TESTJTI001"
        neo4j_url = "bolt://neo4j:7687"
        neo4j_username = "neo4j"
        neo4j_password = "pw"
        neo4j_timeout_seconds = 10.0
        embedding_api_url = "http://embed"
        embedding_api_key = ""
        embedding_model = "all-mpnet-base-v2"
        embedding_timeout_seconds = 10.0

        # The fail-to-review completion plane grounds itself in the SAME frozen catalog
        # export the consumer reads, so this double models the resolver the real settings
        # expose. Pointed at the committed fixture (not a stub path) so the service builds
        # a REAL completer here — a double that returned nothing would make this fixture
        # exercise the fail-open branch and quietly stop covering the wiring.
        @staticmethod
        def catalog_fixture_file():
            return (
                Path(__file__).resolve().parents[2] / "fixtures" / "catalog_export.json"
            )

    import data_agent.learning.config as learning_config
    import data_agent.runtime.config as runtime_config

    monkeypatch.setattr(learning_config, "LearningSettings", _Learning)
    monkeypatch.setattr(runtime_config, "RuntimeSettings", _Runtime)

    import neo4j

    import data_agent.learning.candidate.couchbase_candidate_store as cand_mod
    import data_agent.learning.dedup.couchbase_corpus as corpus_mod
    import data_agent.learning.promotion.token_minter as minter_mod
    import data_agent.runtime.mcp.real_client as mcp_mod
    import data_agent.runtime.model.embedding_client as embed_mod
    from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore

    monkeypatch.setattr(cand_mod, "CouchbaseCandidateStore", lambda s: InMemoryCandidateStore())
    monkeypatch.setattr(corpus_mod, "CouchbaseBlueprintCorpus", lambda s: corpus)
    monkeypatch.setattr(mcp_mod, "RealMCPClient", lambda url: object())
    # `**kw` (not a fixed signature): the real minter takes a REQUIRED keyword-only
    # `tenant`, and a double that pins the old two-positional shape would keep this
    # test green while the wiring it stands in for stopped compiling.
    monkeypatch.setattr(minter_mod, "HttpTokenMinter", lambda *a, **kw: object())
    monkeypatch.setattr(embed_mod, "HttpEmbeddingClient", lambda **k: SimpleNamespace(**k))
    monkeypatch.setattr(
        neo4j.AsyncGraphDatabase, "driver", staticmethod(lambda *a, **k: object())
    )

    import data_agent.learning.factory as factory_module

    real = factory_module.build_promotion_write_plane

    def _capture(*args, **kwargs):
        captured["kwargs"] = kwargs
        scheduler, inbox = real(*args, **kwargs)
        captured["scheduler"] = scheduler
        return scheduler, inbox

    monkeypatch.setattr(factory_module, "build_promotion_write_plane", _capture)

    inbox, mode, driver = service_module._build_inbox_from_env()
    assert mode == "full"
    return SimpleNamespace(inbox=inbox, corpus=corpus, captured=captured, driver=driver)


def test_the_full_write_plane_passes_the_corpus_as_the_terminal_status_writer(full_plane):
    kwargs = full_plane.captured["kwargs"]
    assert kwargs["corpus_status"] is full_plane.corpus


def test_it_is_the_same_object_as_the_hit_count_reader(full_plane):
    """A split would let a reject kill one store while the promotion guard read the
    count from another — the artifact identity is the canonical key, and both ports must
    address the same documents."""
    kwargs = full_plane.captured["kwargs"]
    assert kwargs["corpus_status"] is kwargs["hit_counts"]


def test_the_scheduler_the_inbox_delegates_to_actually_holds_the_writer(full_plane):
    """Asserted on the constructed SCHEDULER, not just on the kwargs: the factory could
    accept the argument and drop it, and `_stamp_corpus_status` would then no-op in
    silence exactly as it did before the fix."""
    assert full_plane.captured["scheduler"]._corpus_status is full_plane.corpus


async def test_a_reject_through_the_inbox_stamps_the_artifact_terminal(full_plane):
    """End to end through the composition root: the human action, not the port."""
    from data_agent.learning.candidate.models import CandidateStatus

    from ..promotion.helpers import make_blueprint_candidate

    scheduler = full_plane.captured["scheduler"]
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW)
    await scheduler.store.put(env)

    await full_plane.inbox.reject(env.candidate_id)

    assert full_plane.corpus.status_calls == [("sha256:single-bp", CandidateStatus.REJECTED)]


def test_offline_dev_mode_deliberately_wires_no_status_writer(monkeypatch):
    """Correct omission, not a second instance of the bug: offline mode has no
    `learning_corpus` store at all (its RBAC creds are part of what `write_plane_ready`
    tests), so there is no artifact to stamp. Pinned so the two omissions stay
    distinguishable to a future reader."""

    class _Learning:
        learning_candidates_username = ""
        learning_candidates_password = ""
        learning_corpus_username = ""
        learning_corpus_password = ""
        learning_trace_verbose = False

    class _Runtime:
        mcp_url = ""
        token_service_url = ""
        token_issuer_api_key = ""
        tenant_client_code = ""
        tenant_proc_center = ""
        tenant_jti = ""
        neo4j_url = ""
        neo4j_username = ""
        neo4j_password = ""
        neo4j_timeout_seconds = 10.0
        embedding_api_url = ""

    import data_agent.learning.config as learning_config
    import data_agent.runtime.config as runtime_config

    monkeypatch.setattr(learning_config, "LearningSettings", _Learning)
    monkeypatch.setattr(runtime_config, "RuntimeSettings", _Runtime)

    inbox, mode, driver = service_module._build_inbox_from_env()
    assert mode == "offline"
    assert driver is None
    assert inbox._scheduler._corpus_status is None
