"""The composition root builds (or refuses to build) the coverage judge (plan §3b).

The judge can DISCARD an analyst's session, so the preconditions for building one at all
are part of the safety story and not just wiring hygiene. Three of them, and the last is
the one that matters:

  * the kill-switch — an operator's deliberate choice;
  * a prior-art index — with nothing to be covered BY, a judge could only ever answer
    `new`, at the price of a model call;
  * a durable audit store — without a record store a drop is invisible, and an invisible
    drop is exactly the risk the record was agreed as the mitigation for. A judge that
    cannot write is not a degraded judge; it is the failure mode.

Slugs:
  * J-factory-preconditions   — each absence yields no judge.
  * J-factory-shared-audit    — the judge writes to the SAME store the consumer
                                snapshots evidence into; a split would put the drop
                                records where nobody queries them.
  * J-factory-shared-instance — the consumer's pre-extraction judge and the dedup
                                stage's post-extraction judge are ONE object.
"""

from __future__ import annotations

import logging

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.config import LearningSettings
from data_agent.learning.dedup import InMemoryBlueprintCorpus
from data_agent.learning.factory import build_learning_consumer
from data_agent.learning.judge import CoverageJudge
from data_agent.learning.priorart import InMemoryPriorArtIndex
from data_agent.learning.user import InMemoryUserKnowledgeStore
from data_agent.runtime.model.scripted_client import ScriptedModelClient


class _Queue:
    pass


class _Store:
    pass


def _build(*, judge_enabled: bool = True, prior_art=True, **overrides):
    settings = LearningSettings(learning_judge_enabled=judge_enabled)
    return build_learning_consumer(
        settings,
        session_store=_Store(),  # type: ignore[arg-type]
        queue=_Queue(),  # type: ignore[arg-type]
        model_client=ScriptedModelClient([]),
        audit_store=overrides.get("audit_store", InMemoryAuditStore()),
        candidate_store=InMemoryCandidateStore(),
        blueprint_corpus=InMemoryBlueprintCorpus(),
        user_store=InMemoryUserKnowledgeStore(),
        catalog_schema={},
        prior_art=InMemoryPriorArtIndex([]) if prior_art else None,
    )


def _build_with(settings: LearningSettings, **overrides):
    """A fully-provisioned consumer over an explicit settings object."""
    return build_learning_consumer(
        settings,
        session_store=_Store(),  # type: ignore[arg-type]
        queue=_Queue(),  # type: ignore[arg-type]
        model_client=ScriptedModelClient([]),
        audit_store=InMemoryAuditStore(),
        candidate_store=InMemoryCandidateStore(),
        blueprint_corpus=InMemoryBlueprintCorpus(),
        user_store=InMemoryUserKnowledgeStore(),
        catalog_schema={},
        prior_art=InMemoryPriorArtIndex([]),
        **overrides,
    )


def _judge_of(consumer) -> CoverageJudge | None:
    return consumer._judge


def _dedup_judge_of(consumer) -> CoverageJudge | None:
    dedup = next(s for s in consumer._stages if s.stage_id == "dedup")
    return dedup._judge


def test_a_fully_configured_consumer_gets_a_judge_on_both_sides() -> None:
    consumer = _build()
    judge = _judge_of(consumer)
    assert isinstance(judge, CoverageJudge)
    # ONE object, two stages. A split would let the two halves of one candidate's
    # lifetime run under different thresholds and write to different audit stores.
    assert _dedup_judge_of(consumer) is judge


def test_the_judge_writes_to_the_same_audit_store_the_consumer_uses() -> None:
    audit = InMemoryAuditStore()
    consumer = _build(audit_store=audit)
    assert _judge_of(consumer)._audit is audit
    assert consumer._audit is audit


def test_the_kill_switch_builds_no_judge() -> None:
    consumer = _build(judge_enabled=False)
    assert _judge_of(consumer) is None
    assert _dedup_judge_of(consumer) is None


def test_no_prior_art_index_builds_no_judge() -> None:
    """Nothing to be covered BY — the judge could only ever answer `new`."""
    consumer = _build(prior_art=False)
    assert _judge_of(consumer) is None
    assert _dedup_judge_of(consumer) is None


def test_the_settings_thresholds_reach_the_judge() -> None:
    settings = LearningSettings(
        learning_judge_pre_drop_confidence=0.81,
        learning_judge_post_drop_confidence=0.42,
        learning_judge_band_low=0.11,
        learning_judge_band_high=0.99,
        learning_judge_model="tiny-judge-1",
    )
    consumer = build_learning_consumer(
        settings,
        session_store=_Store(),  # type: ignore[arg-type]
        queue=_Queue(),  # type: ignore[arg-type]
        model_client=ScriptedModelClient([]),
        audit_store=InMemoryAuditStore(),
        candidate_store=InMemoryCandidateStore(),
        blueprint_corpus=InMemoryBlueprintCorpus(),
        user_store=InMemoryUserKnowledgeStore(),
        catalog_schema={},
        prior_art=InMemoryPriorArtIndex([]),
    )
    config = _judge_of(consumer)._config
    assert config.pre_drop_confidence == 0.81
    assert config.post_drop_confidence == 0.42
    assert config.band_low == 0.11
    assert config.band_high == 0.99
    # `LEARNING_JUDGE_MODEL` is set but no separate client was injected, so the
    # EXTRACTOR's client will answer — and the recorded id says so. See the test below.
    assert config.model_id == "claude-opus-4-8"


def test_the_recorded_model_follows_the_client_not_the_setting(caplog) -> None:
    """`JudgeRecord.model` exists so the dataset never silently pools two judges, and
    reading it off `LEARNING_JUDGE_MODEL` unconditionally would defeat that at the first
    opportunity: the shipped entrypoint only builds a separate client when the setting
    names a different model, but `build_learning_consumer` is also called from places
    (both demo scripts) that pass one client and never look at the setting. Those runs
    would have stamped the configured id while the extractor's model answered — the
    exact mislabel the field exists to prevent, asserted with a straight face.

    So the id follows the client, and a configured-but-unused setting is LOUD."""
    with caplog.at_level(logging.WARNING):
        consumer = _build_with(
            LearningSettings(
                learning_judge_model="tiny-judge-1",
                learning_extractor_model="big-extractor-9",
            )
        )
    assert _judge_of(consumer)._config.model_id == "big-extractor-9"
    assert "no separate judge model client was passed" in caplog.text


def test_an_injected_judge_client_with_no_configured_id_is_labelled_honestly() -> None:
    """A caller injected a client for a model we cannot name. `"unknown-injected-judge-
    client"` is honest; silently borrowing the extractor's id would be a fabrication in
    the same field."""
    consumer = _build_with(
        LearningSettings(), judge_model_client=ScriptedModelClient([])
    )
    assert _judge_of(consumer)._config.model_id == "unknown-injected-judge-client"


def test_an_explicit_judge_model_client_is_used_instead_of_the_extractors() -> None:
    judge_client = ScriptedModelClient([])
    extractor_client = ScriptedModelClient([])
    consumer = build_learning_consumer(
        LearningSettings(),
        session_store=_Store(),  # type: ignore[arg-type]
        queue=_Queue(),  # type: ignore[arg-type]
        model_client=extractor_client,
        judge_model_client=judge_client,
        audit_store=InMemoryAuditStore(),
        candidate_store=InMemoryCandidateStore(),
        blueprint_corpus=InMemoryBlueprintCorpus(),
        user_store=InMemoryUserKnowledgeStore(),
        catalog_schema={},
        prior_art=InMemoryPriorArtIndex([]),
    )
    assert _judge_of(consumer)._model_client is judge_client
