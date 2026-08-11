"""The PRE-extraction coverage judge (plan §3b) — the drop, the record, the fail-opens.

The stage sits between triage and the extractor, and it is the only place in the loop
that can discard an analyst's session before anything is written down. Everything
asserted here is about that authority being fenced: the four conditions a drop requires,
the record that must land before it, and the eight ways the whole thing declines to act.

**These tests prove plumbing, not judgement.** The verdicts are scripted. See
`test_judge_limits_qa.py`.

Slugs:
  * J-pre-drop                   — a high-confidence `duplicate` cancels extraction.
  * J-pre-drop-record            — and leaves a durable, queryable audit row.
  * J-pre-record-first           — a record that cannot be written REFUSES the drop.
  * J-pre-only-duplicate-drops   — `existing-plus-delta` never drops, at any confidence.
  * J-pre-bar                    — the bar is inclusive and configurable.
  * J-pre-hallucinated-id        — a `covered_by` nobody showed can never drop.
  * J-pre-unsourced-tier         — an unsourced node can never authorize a drop.
  * J-pre-free-gates             — unavailable / no cards / below the floor cost NO
                                   model call, and record NOTHING.
  * J-pre-fail-open              — a model error, a timeout and a malformed response all
                                   proceed to extraction.
  * J-pre-idempotent             — a redelivery reuses the stored assessment.
"""

from __future__ import annotations

import asyncio

import pytest

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.audit.judgement import JUDGE_RECORD_TYPE, pre_extraction_ref
from data_agent.learning.judge import (
    OUTCOME_DROPPED,
    OUTCOME_FAILED,
    OUTCOME_PROCEEDED,
    OUTCOME_RECORD_WRITE_FAILED,
    OUTCOME_SKIPPED_BELOW_FLOOR,
    OUTCOME_SKIPPED_NO_PRIOR_ART,
    OUTCOME_SKIPPED_UNAVAILABLE,
    JudgeConfig,
)
from data_agent.runtime.model.client import ModelTurnResult

from .helpers import BoomModelClient, card, free_text_turn, make_judge, make_summary, verdict_turn

# Pin the retrieval score so the band gate is a stated intent, not an artifact of the
# fake index's token overlap.
_QUERY = "what did Analytics earn in total? SELECT sum(AnnualSalary) AS total FROM dbpcm_warehouse.employee WHERE Department = 'Analytics'"


def _scores(card_id: str = "bp::abc", score: float = 0.85) -> dict[tuple[str, str], float]:
    return {(_QUERY, card_id): score}


async def test_a_high_confidence_duplicate_drops_the_session() -> None:
    judge, client, audit, index = make_judge(
        [verdict_turn("duplicate", confidence=0.95)], cards=[card()], scores=_scores()
    )
    outcome = await judge.screen_session(make_summary())
    assert outcome.drop is True
    assert outcome.outcome == OUTCOME_DROPPED
    assert client.calls_made == 1


async def test_the_drop_leaves_a_durable_queryable_record() -> None:
    """The non-negotiable mitigation. A wrong drop is invisible in a way a wrong keep is
    not, so "we dropped N candidates last quarter" has to be a query — which means every
    field that question needs is on the row, flat, with a discriminator."""
    judge, _client, audit, _index = make_judge(
        [verdict_turn("duplicate", covered_by="bp::abc", reason="same total", confidence=0.93)],
        cards=[card()],
        scores=_scores(),
    )
    summary = make_summary()
    await judge.screen_session(summary)

    # Keyed on a CONTENT fingerprint, never on a position — see
    # `judgement_fingerprint`. The ref is re-derived from the stored row rather than
    # hardcoded, so this test cannot pin a key spelling.
    (stored,) = audit.judgements
    assert stored.judgement_ref == pre_extraction_ref(stored.fingerprint)
    assert await audit.read_judgement(stored.judgement_ref) == stored
    doc = stored.to_doc()
    assert doc["record_type"] == JUDGE_RECORD_TYPE
    assert doc["dropped"] is True
    assert doc["session_id"] == "sess-1"
    assert doc["verdict"] == "duplicate"
    assert doc["covered_by"] == "bp::abc"
    assert doc["covered_by_tier"] == "mcp"
    assert doc["reason"] == "same total"
    assert doc["confidence"] == 0.93
    assert doc["stage"] == "pre_extraction"
    # The bar in force at the time — without it a stored verdict cannot be re-read after
    # an operator retunes the threshold.
    assert doc["threshold"] == 0.90
    assert doc["best_similarity"] == pytest.approx(0.85)
    assert doc["cards_shown"] == 1
    assert doc["candidate_id"] is None  # a pre-extraction drop has no candidate


async def test_a_record_that_cannot_be_written_refuses_the_drop() -> None:
    """The record is a PRECONDITION of the drop, not a consequence. A drop with no audit
    row is exactly the invisible loss the row exists to prevent, so a write failure
    turns the drop into a proceed — the strictly cheaper failure."""
    judge, _client, _audit, _index = make_judge(
        [verdict_turn("duplicate", confidence=0.99)],
        cards=[card()],
        scores=_scores(),
        audit=InMemoryAuditStore(fail_judgements=True),
    )
    outcome = await judge.screen_session(make_summary())
    assert outcome.drop is False
    assert outcome.outcome == OUTCOME_RECORD_WRITE_FAILED


async def test_a_proceed_survives_a_failed_record_write() -> None:
    """The other direction of the same asymmetry: refusing to extract because an
    analytics row did not land would be a self-inflicted outage."""
    judge, _client, _audit, _index = make_judge(
        [verdict_turn("new", covered_by="", confidence=0.99)],
        cards=[card()],
        scores=_scores(),
        audit=InMemoryAuditStore(fail_judgements=True),
    )
    outcome = await judge.screen_session(make_summary())
    assert outcome.drop is False
    assert outcome.outcome == OUTCOME_PROCEEDED


async def test_existing_plus_delta_never_drops_however_confident() -> None:
    """By construction there is something left to learn. Dropping on it would discard
    the increment while recording that we knew it was there."""
    judge, _client, audit, _index = make_judge(
        [verdict_turn("existing-plus-delta", confidence=1.0)],
        cards=[card()],
        scores=_scores(),
    )
    outcome = await judge.screen_session(make_summary())
    assert outcome.drop is False
    assert outcome.outcome == OUTCOME_PROCEEDED
    # Still RECORDED — this is the verdict the composable-blueprint decision hangs on,
    # and it only ever appears on the non-drop path.
    assert audit.judgements[0].assessment.verdict == "existing-plus-delta"


async def test_new_never_drops() -> None:
    judge, _client, _audit, _index = make_judge(
        [verdict_turn("new", covered_by="", confidence=1.0)], cards=[card()], scores=_scores()
    )
    assert (await judge.screen_session(make_summary())).drop is False


async def test_the_confidence_bar_is_inclusive_and_configurable() -> None:
    for confidence, expect_drop in ((0.89, False), (0.90, True), (0.91, True)):
        judge, _c, _a, _i = make_judge(
            [verdict_turn("duplicate", confidence=confidence)],
            cards=[card()],
            scores=_scores(),
        )
        assert (await judge.screen_session(make_summary())).drop is expect_drop

    # Raise the bar and the same verdict stops dropping.
    judge, _c, _a, _i = make_judge(
        [verdict_turn("duplicate", confidence=0.91)],
        cards=[card()],
        scores=_scores(),
        config=JudgeConfig(pre_drop_confidence=0.99),
    )
    assert (await judge.screen_session(make_summary())).drop is False


async def test_an_id_the_judge_was_never_shown_can_never_drop() -> None:
    """`covered_by` exists so a human can look the artifact up. An id that is not in the
    corpus makes the audit row unauditable, and a drop nobody can review is exactly as
    invisible as a drop with no row at all."""
    judge, _client, audit, _index = make_judge(
        [verdict_turn("duplicate", covered_by="bp::i-made-this-up", confidence=1.0)],
        cards=[card("bp::abc")],
        scores=_scores(),
    )
    outcome = await judge.screen_session(make_summary())
    assert outcome.drop is False
    stored = audit.judgements[0]
    assert stored.covered_by_known is False
    assert stored.assessment.covered_by_tier == ""


async def test_an_empty_covered_by_can_never_drop() -> None:
    """Handled by the SAME membership test as a hallucinated id — no separate emptiness
    check exists, because the operation the value's real consumer performs already
    distinguishes usable from unusable."""
    judge, _client, _audit, _index = make_judge(
        [verdict_turn("duplicate", covered_by="", confidence=1.0)],
        cards=[card()],
        scores=_scores(),
    )
    assert (await judge.screen_session(make_summary())).drop is False


async def test_an_unsourced_artifact_can_never_authorize_a_drop() -> None:
    """`priorart/models.py` already states this for the dedup structural layer: an
    unsourced node is one no writer we control stamped — a hand edit or a foreign
    writer — and is never treated as canon. Discarding an analyst's session because such
    a node appears to cover it is the same claim on softer evidence."""
    judge, _client, audit, _index = make_judge(
        [verdict_turn("duplicate", covered_by="bp::ghost", confidence=1.0)],
        cards=[card("bp::ghost", tier="unsourced")],
        scores=_scores("bp::ghost"),
    )
    outcome = await judge.screen_session(make_summary())
    assert outcome.drop is False
    # Recorded with the real tier, so the rate of near-misses against unsourced nodes is
    # visible rather than silently swallowed.
    assert audit.judgements[0].assessment.covered_by_tier == "unsourced"


async def test_a_landed_learning_tier_artifact_may_authorize_a_drop() -> None:
    judge, _client, _audit, _index = make_judge(
        [verdict_turn("duplicate", covered_by="bp::landed", confidence=0.95)],
        cards=[card("bp::landed", tier="learning")],
        scores=_scores("bp::landed"),
    )
    assert (await judge.screen_session(make_summary())).drop is True


# --- the free gates: no model call, and NOTHING recorded ---------------------------


async def test_an_unavailable_index_is_not_judged_at_all() -> None:
    """An empty block would read to the model as "nothing exists". That is the
    outage-becomes-a-novelty-claim conflation the whole prior-art port was shaped to
    prevent, and here it would run in the DROP direction."""
    judge, client, audit, _index = make_judge(
        [verdict_turn("duplicate", confidence=1.0)], cards=[card()], index_fails=True
    )
    outcome = await judge.screen_session(make_summary())
    assert outcome.drop is False
    assert outcome.outcome == OUTCOME_SKIPPED_UNAVAILABLE
    assert client.calls_made == 0
    assert audit.judgements == ()


async def test_an_empty_corpus_costs_no_model_call_and_records_nothing() -> None:
    """The verdict would be `new` by construction. Asking for it is pure cost, and
    RECORDING it would be worse: a fabricated verdict is indistinguishable in the store
    from one a judge gave, and the store is the dataset."""
    judge, client, audit, _index = make_judge([verdict_turn()], cards=[])
    outcome = await judge.screen_session(make_summary())
    assert outcome.outcome == OUTCOME_SKIPPED_NO_PRIOR_ART
    assert client.calls_made == 0
    assert audit.judgements == ()


async def test_a_distant_best_match_costs_no_model_call() -> None:
    judge, client, audit, _index = make_judge(
        [verdict_turn()], cards=[card()], scores=_scores(score=0.31)
    )
    outcome = await judge.screen_session(make_summary())
    assert outcome.outcome == OUTCOME_SKIPPED_BELOW_FLOOR
    assert client.calls_made == 0
    assert audit.judgements == ()


async def test_the_pre_extraction_judge_has_no_upper_free_pass() -> None:
    """`band_high` gates the POST-extraction judge only. Pre-extraction the score
    compares a session's raw text against entity-free corpus intents, which is a much
    noisier comparison than intent-against-intent, so a 0.99 there is not the settled
    answer it is on the other side of the extractor."""
    judge, client, _audit, _index = make_judge(
        [verdict_turn("duplicate", confidence=0.95)], cards=[card()], scores=_scores(score=0.995)
    )
    outcome = await judge.screen_session(make_summary())
    assert client.calls_made == 1
    assert outcome.drop is True


# --- fail-open ---------------------------------------------------------------------


async def test_a_model_error_proceeds_to_extraction() -> None:
    judge, client, audit, _index = make_judge(
        [], cards=[card()], scores=_scores(), model_client=BoomModelClient()
    )
    outcome = await judge.screen_session(make_summary())
    assert outcome.drop is False
    assert outcome.outcome == OUTCOME_FAILED
    assert audit.judgements == ()


async def test_a_malformed_response_proceeds_to_extraction_without_a_retry() -> None:
    """No retries, deliberately: the judge's whole justification is that it costs less
    than the call it cancels, and a retry budget spends the saving to salvage an
    optimization. The extractor retries because its output IS the product."""
    judge, client, _audit, _index = make_judge(
        [free_text_turn()], cards=[card()], scores=_scores()
    )
    outcome = await judge.screen_session(make_summary())
    assert outcome.outcome == OUTCOME_FAILED
    assert client.calls_made == 1


async def test_a_timeout_proceeds_to_extraction() -> None:
    class _SlowClient:
        async def send_turn(self, messages, tools) -> ModelTurnResult:
            await asyncio.sleep(10)
            raise AssertionError("unreachable: the timeout should have fired")

    judge, _client, _audit, _index = make_judge(
        [],
        cards=[card()],
        scores=_scores(),
        model_client=_SlowClient(),
        config=JudgeConfig(timeout_seconds=0.01),
    )
    outcome = await judge.screen_session(make_summary())
    assert outcome.drop is False
    assert outcome.outcome == OUTCOME_FAILED


# --- idempotency -------------------------------------------------------------------


async def test_a_redelivery_reuses_the_stored_assessment_and_calls_no_model() -> None:
    """An LLM call is not idempotent and the loop's redelivery path is real. The
    deterministic `learning_audit` key is the mechanism — the second pass never reaches
    the model."""
    audit = InMemoryAuditStore()
    judge, client, _a, _i = make_judge(
        [verdict_turn("duplicate", confidence=0.95)],
        cards=[card()],
        scores=_scores(),
        audit=audit,
    )
    first = await judge.screen_session(make_summary())
    assert first.drop is True and client.calls_made == 1

    # A second, identical delivery. The scripted client has ONE turn left unscripted, so
    # a second model call would raise AssertionError inside the judge's blanket handler
    # and surface as `failed` — this assertion would then fail on `drop`.
    second = await judge.screen_session(make_summary())
    assert second.drop is True
    assert second.outcome == OUTCOME_DROPPED
    assert client.calls_made == 1


async def test_the_drop_gate_is_re_applied_on_the_reuse_path() -> None:
    """What is cached is the model's ASSESSMENT, not the outcome. The gate is cheap,
    deterministic and made of thresholds an operator can retune between two deliveries
    of the same message, so a retune takes effect on the next delivery rather than being
    frozen into a stored decision."""
    audit = InMemoryAuditStore()
    judge, _c, _a, _i = make_judge(
        [verdict_turn("duplicate", confidence=0.92)],
        cards=[card()],
        scores=_scores(),
        audit=audit,
    )
    assert (await judge.screen_session(make_summary())).drop is True

    stricter, client2, _a2, _i2 = make_judge(
        [],
        cards=[card()],
        scores=_scores(),
        audit=audit,
        config=JudgeConfig(pre_drop_confidence=0.99),
    )
    outcome = await stricter.screen_session(make_summary())
    assert outcome.drop is False
    assert outcome.outcome == OUTCOME_PROCEEDED
    assert client2.calls_made == 0


async def test_a_failed_idempotency_read_re_judges_rather_than_dropping() -> None:
    """A skip is indistinguishable from an absence. The only safe reading of an
    unreadable cache is "ask again" — never "assume a prior drop"."""
    judge, client, _audit, _index = make_judge(
        [verdict_turn("new", covered_by="", confidence=0.5)],
        cards=[card()],
        scores=_scores(),
        audit=InMemoryAuditStore(fail_judgement_reads=True),
    )
    outcome = await judge.screen_session(make_summary())
    assert outcome.outcome == OUTCOME_PROCEEDED
    assert client.calls_made == 1


async def test_a_different_session_content_gets_its_own_judgement() -> None:
    """Keyed on `content_hash`, the loop's own idempotency key — a session whose content
    CHANGED is legitimately a new question."""
    audit = InMemoryAuditStore()
    judge, client, _a, _i = make_judge(
        [verdict_turn("duplicate", confidence=0.95), verdict_turn("new", covered_by="")],
        cards=[card()],
        scores={(_QUERY, "bp::abc"): 0.85},
        audit=audit,
    )
    await judge.screen_session(make_summary(content_hash="hash-1"))
    await judge.screen_session(make_summary(content_hash="hash-2"))
    assert client.calls_made == 2
    assert len(audit.judgements) == 2
