"""QA attack suite on the coverage judge's drop authority (plan §3b).

The judge is the only component in the loop that DISCARDS an analyst's session, and the
discard is irreversible by design — nothing downstream ever sees the thing that did not
happen. The agreed mitigation is the durable audit row, so exactly two properties carry
the whole safety story and both are attacked here:

  1. the drop gate is a conjunction of FIVE positive facts, so every hostile shape that
     is not all five must proceed;
  2. the record is a PRECONDITION of the drop, so a write that did not return must turn
     the drop into a proceed.

Complements `test_pre_extraction_judge.py` / `test_post_extraction_adjudication.py`,
which pin the happy shapes of the same two properties. Nothing here duplicates those;
every test below is a shape they do not reach.

**Strict xfails are REAL holes**, per this repo's convention (`test_adversarial_*`).
Each asserts the behaviour the module's own docstring promises; when the hole is closed
the xfail flips to XPASS and fails CI.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace

import pytest

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.audit.judgement import CoverageAssessment, pre_extraction_ref
from data_agent.learning.judge import (
    OUTCOME_DROPPED,
    OUTCOME_FAILED,
    OUTCOME_PROCEEDED,
    OUTCOME_RECORD_WRITE_FAILED,
    JudgeConfig,
)
from data_agent.learning.judge.schema import parse_assessment
from data_agent.learning.priorart import PriorArtCard
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest

from .helpers import card, make_envelope, make_judge, make_summary, verdict_turn

# The exact text `prior_art_query_text` builds from `make_summary()`. Pinning the
# retrieval score keeps every band gate below a stated intent rather than an artifact of
# the fake index's token overlap.
_QUERY = (
    "what did Analytics earn in total? SELECT sum(AnnualSalary) AS total "
    "FROM dbpcm_warehouse.employee WHERE Department = 'Analytics'"
)


def _scores(card_id: str = "bp::abc", score: float = 0.85) -> dict[tuple[str, str], float]:
    return {(_QUERY, card_id): score}


# ===========================================================================
# 1. The five-fact drop gate, attacked one fact at a time.
# ===========================================================================


async def test_an_id_shown_only_in_a_different_sessions_block_can_never_drop() -> None:
    """The membership test is scoped to THIS judgement's cards, not to "an id that
    exists somewhere in the corpus".

    A judge that resolved `covered_by` against the corpus at large would authorize a
    drop on an artifact this session's reader never surfaced — the audit row would name
    something real, so the drop would look auditable, while the retrieval that was
    supposed to justify it never returned that artifact at all."""
    shown = card("bp::shown", intent="headcount by office")
    judge, _client, audit, _index = make_judge(
        [verdict_turn("duplicate", covered_by="bp::other-session", confidence=1.0)],
        cards=[shown],
        scores=_scores("bp::shown"),
    )
    outcome = await judge.screen_session(make_summary())

    assert outcome.drop is False
    assert outcome.outcome == OUTCOME_PROCEEDED
    # Recorded anyway — the confabulation RATE is the honest measure of how far the
    # judge can be trusted, so it is a row rather than a discard.
    assert len(audit.judgements) == 1
    assert audit.judgements[0].covered_by_known is False
    assert audit.judgements[0].assessment.covered_by_tier == ""


async def test_a_corpus_origin_card_can_never_drop_on_the_pre_extraction_path_either() -> None:
    """`DROP_ELIGIBLE_ORIGINS` is enforced in the SHARED body, so it holds on the
    pre-extraction side too — not only in the dedup stage where the union of graph and
    bucket cards is assembled.

    An unlanded `learning_corpus` sibling may still be rejected or fail its landing
    gates, so discarding an analyst's whole session because of an artifact that might
    never exist is the weakest basis for a drop in the system. The pre-extraction reader
    happens to be graph-only today; this pins the guarantee to the gate rather than to
    that accident."""
    in_flight = PriorArtCard(
        id="bp::abc",
        kind="blueprint",
        tier="learning",
        status="extracted",
        verified=None,
        drift_status="",
        intent="total earnings for a department",
        result_grain=(),
        uses_rules=(),
        structural_key="",
        embedding_model="all-mpnet-base-v2",
        similarity=0.85,
        model_matched=True,
        origin="corpus",
    )
    judge, _client, audit, _index = make_judge(
        [verdict_turn("duplicate", confidence=1.0)], cards=[in_flight], scores=_scores()
    )
    outcome = await judge.screen_session(make_summary())

    assert outcome.drop is False
    assert outcome.outcome == OUTCOME_PROCEEDED
    # The id WAS known and the tier IS droppable — origin is the only fact that refused.
    assert audit.judgements[0].covered_by_known is True
    assert audit.judgements[0].assessment.covered_by_tier == "learning"


@pytest.mark.parametrize(
    ("confidence", "drops"),
    [
        (0.90, True),                            # the bar is inclusive
        (math.nextafter(0.90, 1.0), True),       # one ulp above
        (math.nextafter(0.90, 0.0), False),      # one ulp below
    ],
)
async def test_the_bar_is_inclusive_to_within_one_ulp(confidence: float, drops: bool) -> None:
    """`>=` against a float, exercised at the representable neighbours of the bar.

    Pinned because the comparison is the last thing standing between a model's number
    and a discarded session: an off-by-one-ulp in the wrong direction is invisible in
    every test that uses round numbers."""
    judge, _client, _audit, _index = make_judge(
        [verdict_turn("duplicate", confidence=confidence)], cards=[card()], scores=_scores()
    )
    outcome = await judge.screen_session(make_summary())
    assert outcome.drop is drops


@pytest.mark.parametrize(
    "verdict",
    [
        "Duplicate",
        "DUPLICATE",
        " duplicate",
        "duplicate ",
        "duplicate\n",
        "duplicate​",   # a zero-width space the eye cannot see
        "duplłicate",   # a homoglyph-ish near miss
    ],
)
async def test_a_verdict_that_is_not_exactly_the_vocabulary_never_drops(verdict: str) -> None:
    """`verdict not in COVERAGE_VERDICTS` is exact-match membership, so every near miss
    fails OPEN and records NOTHING.

    Both halves matter. Case-folding or stripping would let a model's formatting drift
    authorize a drop; defaulting the unrecognized value to `new` would fabricate a
    verdict that is indistinguishable in the store from one a judge actually gave."""
    judge, _client, audit, _index = make_judge(
        [verdict_turn(verdict, confidence=1.0)], cards=[card()], scores=_scores()
    )
    outcome = await judge.screen_session(make_summary())

    assert outcome.drop is False
    assert outcome.outcome == OUTCOME_FAILED
    assert audit.judgements == ()


@pytest.mark.parametrize("tier", ["MCP", "Learning", "mcp ", "", "unsourced"])
async def test_a_tier_that_is_not_exactly_droppable_never_drops(tier: str) -> None:
    """`covered_by_tier in DROP_ELIGIBLE_TIERS` is exact-match too, and the tier is
    taken from the CARD rather than from anything the model said — so this is an attack
    on a hand-edited or foreign-written node, not on the model."""
    hostile = replace(card(), tier=tier)  # type: ignore[arg-type]
    judge, _client, _audit, _index = make_judge(
        [verdict_turn("duplicate", confidence=1.0)], cards=[hostile], scores=_scores()
    )
    outcome = await judge.screen_session(make_summary())
    assert outcome.drop is False


async def test_a_covered_by_past_the_render_cap_can_never_drop() -> None:
    """`render_prior_art_block` caps a rendered id at 120 chars and the parser caps the
    echoed value at 160, so an id longer than the cap is unmatchable — and therefore
    unable to authorize a drop. Fails CLOSED, and pinned so it stays that way if either
    cap moves."""
    at_cap = "bp::" + "a" * 156        # 160 — the parser's limit, still matchable
    past_cap = "bp::" + "a" * 157      # 161 — truncated + ellipsised, never matchable

    for artifact_id, expected in ((at_cap, True), (past_cap, False)):
        judge, _client, _audit, _index = make_judge(
            [verdict_turn("duplicate", covered_by=artifact_id, confidence=1.0)],
            cards=[card(artifact_id)],
            scores=_scores(artifact_id),
        )
        outcome = await judge.screen_session(make_summary())
        assert outcome.drop is expected, f"id of {len(artifact_id)} chars"


async def test_a_shadowed_id_resolves_most_restrictive_wins() -> None:
    """RAISED BY QA AS A DESIGN POINT, NOW FIXED, and kept as the regression guard.

    `_resolve_covered_by` used to build `{c.id: c for c in cards}` — positional, so with
    two cards sharing an id the LAST one decided the tier and the origin: `[unsourced,
    mcp]` authorized a drop and `[mcp, unsourced]` did not. Safe in practice, because the
    two producers cannot collide (`DedupStage._corpus_cards` excludes ids the graph
    already returned) — but a discard must not depend on the order two producers were
    concatenated in, and "safe because of an invariant two modules away" is precisely the
    coupling this codebase keeps getting bitten by.

    The rule is now most-restrictive-wins, so BOTH orderings refuse."""
    canon = card("bp::abc", tier="mcp")
    shadow = replace(canon, tier="unsourced")
    env = make_envelope()

    judge_a, _c, _a, _i = make_judge([verdict_turn("duplicate", confidence=1.0)])
    unsourced_first = await judge_a.adjudicate_candidate(env, make_summary(), [shadow, canon])

    judge_b, _c, _a, _i = make_judge([verdict_turn("duplicate", confidence=1.0)])
    canon_first = await judge_b.adjudicate_candidate(env, make_summary(), [canon, shadow])

    assert unsourced_first.drop is False
    assert canon_first.drop is False
    # The restrictive card governs the recorded tier in BOTH orders, so the row explains
    # the refusal rather than contradicting it.
    assert unsourced_first.assessment.covered_by_tier == "unsourced"
    assert canon_first.assessment.covered_by_tier == "unsourced"


async def test_the_two_bars_do_not_leak_across_the_extractor() -> None:
    """The pre bar (0.90) is higher than the post bar (0.75) because the pre-extraction
    judge sees strictly less. One assessment, one config, both stages: 0.80 must drop on
    exactly one side."""
    config = JudgeConfig()
    assert config.pre_drop_confidence == 0.90
    assert config.post_drop_confidence == 0.75

    pre_judge, _c, _a, _i = make_judge(
        [verdict_turn("duplicate", confidence=0.80)],
        cards=[card()],
        scores=_scores(),
        config=config,
    )
    pre = await pre_judge.screen_session(make_summary())

    post_judge, _c, _a, _i = make_judge(
        [verdict_turn("duplicate", confidence=0.80)], config=config
    )
    post = await post_judge.adjudicate_candidate(make_envelope(), make_summary(), [card()])

    assert pre.drop is False
    assert post.drop is True


async def test_a_stored_pre_verdict_is_never_served_to_the_post_stage() -> None:
    """The two stages key into DISJOINT namespaces, so a pre-extraction assessment —
    taken against raw SQL and held to the higher bar — can never be replayed through the
    lower one.

    The keys are now `judgement::{pre,post}::<content fingerprint>`, and BOTH halves of
    the separation are load-bearing: the prefix differs, and the stage is itself an
    input to `judgement_fingerprint`, so the two namespaces cannot collide even if the
    two stages were somehow shown byte-identical briefs. (The keys were originally
    `<content_hash>` and `<candidate_id>`; that spelling bound the post key to a
    POSITION — `candidate::<hash>::<ordinal>` — and a re-extraction that reordered
    candidates could serve one candidate a verdict rendered about another. See
    `judgement_fingerprint`.)"""
    audit = InMemoryAuditStore()
    pre_judge, _c, _a, _i = make_judge(
        [verdict_turn("duplicate", confidence=0.99)],
        cards=[card()],
        scores=_scores(),
        audit=audit,
    )
    await pre_judge.screen_session(make_summary())
    assert [r.judgement_ref for r in audit.judgements] == [
        pre_extraction_ref(audit.judgements[0].fingerprint)
    ]

    post_judge, post_client, _a, _i = make_judge(
        [verdict_turn("existing-plus-delta", confidence=0.99)], audit=audit
    )
    await post_judge.adjudicate_candidate(make_envelope(), make_summary(), [card()])

    # The post stage asked its own model rather than reading the pre row.
    assert post_client.calls_made == 1
    refs = sorted(r.judgement_ref for r in audit.judgements)
    assert len(refs) == 2
    assert sum(r.startswith("judgement::pre::") for r in refs) == 1
    assert sum(r.startswith("judgement::post::") for r in refs) == 1
    # And the two fingerprints differ, so the namespaces are separated by CONTENT and
    # not merely by the prefix string.
    assert len({r.fingerprint for r in audit.judgements}) == 2


async def test_the_authorizing_card_need_not_be_the_one_that_opened_the_band() -> None:
    """DOCUMENTS a gap between the gate and the row.

    The band gate is applied to the BEST card; the drop is authorized by whichever card
    the model names. So a drop can be taken on an artifact whose retrieval score is far
    below `band_low`, while the row's `best_similarity` reports the unrelated card that
    opened the gate. The row is still auditable (the id resolves), but the score stored
    beside the drop is not the score of the artifact that caused it — which is exactly
    the number "would this still drop at a tighter band?" needs."""
    near = card("bp::near", intent="unrelated headcount rollup", similarity=0.85)
    far = card("bp::far", intent="something else entirely", similarity=0.05)

    judge, _client, audit, _index = make_judge(
        [verdict_turn("duplicate", covered_by="bp::far", confidence=1.0)]
    )
    outcome = await judge.adjudicate_candidate(make_envelope(), make_summary(), [near, far])

    assert outcome.drop is True
    assert audit.judgements[0].assessment.covered_by == "bp::far"
    assert audit.judgements[0].best_similarity == 0.85     # the OTHER card's score


# ===========================================================================
# 2. The record is a precondition of the drop.
# ===========================================================================


class _NoOpJudgementStore(InMemoryAuditStore):
    """A store that accepts `record_judgement` and persists nothing — a silently
    misconfigured bucket, a durability setting that does not durably write."""

    async def record_judgement(self, record) -> None:  # noqa: ANN001
        return None


async def test_a_store_that_accepts_the_write_and_persists_nothing_still_drops() -> None:
    """DOCUMENTS the residual risk the precondition cannot cover.

    "The record is written FIRST and the drop happens only if the write returned" is a
    guarantee about the CALL, not about the document. A store that returns cleanly
    without persisting produces the exact outcome the mitigation exists to prevent — an
    invisible drop — and no amount of ordering detects it. Closing it needs a read-back
    or a write-then-verify on the drop path only; until then this is the honest
    statement of what the mitigation does and does not buy."""
    store = _NoOpJudgementStore()
    judge, _client, _audit, _index = make_judge(
        [verdict_turn("duplicate", confidence=0.99)],
        cards=[card()],
        scores=_scores(),
        audit=store,
    )
    outcome = await judge.screen_session(make_summary())

    assert outcome.drop is True
    assert store.judgements == ()          # the work is gone and nothing records it


class _PartialWriteStore(InMemoryAuditStore):
    """A store that writes the row with the human-facing fields missing."""

    def __init__(self) -> None:
        super().__init__()
        self.docs: list[dict] = []

    async def record_judgement(self, record) -> None:  # noqa: ANN001
        doc = record.to_doc()
        doc.pop("covered_by", None)
        doc.pop("reason", None)
        self.docs.append(doc)


async def test_a_partial_write_still_drops_and_leaves_an_unauditable_row() -> None:
    """Same class as the no-op store, one step less severe: the row exists, so the
    count is right, but `covered_by` — the field whose whole job is "a human opens the
    row and looks the artifact up" — is gone. The drop proceeded on it anyway."""
    store = _PartialWriteStore()
    judge, _client, _audit, _index = make_judge(
        [verdict_turn("duplicate", confidence=0.99)],
        cards=[card()],
        scores=_scores(),
        audit=store,
    )
    outcome = await judge.screen_session(make_summary())

    assert outcome.drop is True
    assert "covered_by" not in store.docs[0]


async def test_a_failed_write_refuses_the_drop_on_the_reuse_path_too() -> None:
    """The gate is re-applied on the reuse path, so the record write is re-attempted
    there — and must refuse the drop the same way. A redelivery is exactly when a store
    is most likely to be unhealthy, so a reuse path that skipped the precondition would
    fail open on the one delivery that matters."""
    store = InMemoryAuditStore()
    seed_judge, _c, _a, _i = make_judge(
        [verdict_turn("duplicate", confidence=0.99)],
        cards=[card()],
        scores=_scores(),
        audit=store,
    )
    assert (await seed_judge.screen_session(make_summary())).drop is True

    store._fail_judgements = True   # the store goes unhealthy between deliveries
    replay_judge, replay_client, _a, _i = make_judge(
        [], cards=[card()], scores=_scores(), audit=store
    )
    outcome = await replay_judge.screen_session(make_summary())

    assert outcome.drop is False
    assert outcome.outcome == OUTCOME_RECORD_WRITE_FAILED
    assert replay_client.calls_made == 0      # served from the stored assessment


class _ExplodingTracer:
    """A tracer whose span construction raises — a misconfigured exporter."""

    def start_as_current_span(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("exporter pipeline is broken")


async def test_a_broken_tracer_after_a_written_drop_leaves_a_row_for_a_drop_that_did_not_happen(
) -> None:
    """The INVERSE ordering hazard — FOUND BY QA AS A LIVE DEFECT, NOW CLOSED. Kept, and
    kept under its original name, because the assertion is what stops it reopening.

    `_persist` runs before `_observe`, so a raise inside the span used to escape
    `_judge`. Both call sites are fail-open, so the caller caught it and extracted — and
    the audit store was left holding a `dropped=true` row for a session that was NOT
    dropped. Safe for the analyst, wrong for the dataset: the store is documented as the
    numerator for "how many did we drop", and this over-counted it permanently, in the
    one dataset the whole drop mitigation depends on.

    `_observe` now catches. Telemetry may never rewrite history: the judgement stands as
    recorded, the span is lost, and the loss is logged."""
    audit = InMemoryAuditStore()
    judge, _client, _a, _i = make_judge(
        [verdict_turn("duplicate", confidence=0.99)],
        cards=[card()],
        scores=_scores(),
        audit=audit,
        tracer=_ExplodingTracer(),
    )
    outcome = await judge.screen_session(make_summary())

    # The verdict survives the broken exporter, and the row and the caller AGREE.
    assert outcome.drop is True
    assert len(audit.judgements) == 1
    assert audit.judgements[0].dropped is True


class _HangingAuditStore(InMemoryAuditStore):
    def __init__(self, *, hang_write: bool = False, hang_read: bool = False) -> None:
        super().__init__()
        self._hang_write = hang_write
        self._hang_read = hang_read

    async def record_judgement(self, record) -> None:  # noqa: ANN001
        if self._hang_write:
            await asyncio.sleep(3600)
        await super().record_judgement(record)

    async def read_judgement(self, ref: str):  # noqa: ANN201
        if self._hang_read:
            await asyncio.sleep(3600)
        return await super().read_judgement(ref)


# FOUND BY QA AS A LIVE HOLE, NOW CLOSED. `JudgeConfig.timeout_seconds` bounded the
# MODEL call only, so a hung audit write blocked the judge — and therefore the learning
# consumer — for as long as the store hung, against this class's own claim that "the
# judge is an OPTIMIZATION; it must never be the reason a session takes longer than it
# used to". `_persist` is deadline-bound now, and a timeout counts as a FAILED write,
# which on the drop path REFUSES the drop: a write that did not return is one we cannot
# claim landed.
async def test_a_hung_record_write_must_not_outlive_the_judges_own_deadline() -> None:
    judge, _client, _a, _i = make_judge(
        [verdict_turn("duplicate", confidence=0.99)],
        cards=[card()],
        scores=_scores(),
        audit=_HangingAuditStore(hang_write=True),
        config=JudgeConfig(timeout_seconds=0.05),
    )
    async with asyncio.timeout(1.0):
        outcome = await judge.screen_session(make_summary())
    assert outcome.drop is False


# FOUND BY QA AS A LIVE HOLE, NOW CLOSED (the read half of the one above). A hung
# Couchbase `get` stalled every KEEP-triaged session indefinitely, and this read exists
# purely to SAVE a model call. `_reuse` is deadline-bound now; a timeout re-judges.
async def test_a_hung_idempotency_read_must_not_outlive_the_judges_own_deadline() -> None:
    judge, _client, _a, _i = make_judge(
        [verdict_turn("duplicate", confidence=0.99)],
        cards=[card()],
        scores=_scores(),
        audit=_HangingAuditStore(hang_read=True),
        config=JudgeConfig(timeout_seconds=0.05),
    )
    async with asyncio.timeout(1.0):
        await judge.screen_session(make_summary())


# ===========================================================================
# 3. The response parser — new untrusted model output.
# ===========================================================================


def _args(**overrides):
    base = {
        "verdict": "duplicate",
        "covered_by": "bp::abc",
        "reason": "same total at the same grain",
        "confidence": 0.95,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "covered_by",
    [["bp::abc"], {"id": "bp::abc"}, 42, None, True, ("bp::abc",)],
)
def test_a_wrong_typed_covered_by_degrades_to_the_no_id_value(covered_by: object) -> None:
    """`covered_by` is only ever READ (membership, a log line, a stored leaf), so a
    wrong type degrades to `""` rather than rejecting the whole assessment — and `""`
    is handled by the same membership test that handles a hallucinated id.

    A list is the case that matters: `str(["bp::abc"])` would have produced
    `"['bp::abc']"`, and `"".join(...)` over one would have produced a plausible-looking
    id. Neither happens."""
    assessment = parse_assessment(verdict_turn(arguments=_args(covered_by=covered_by)))
    assert assessment is not None
    assert assessment.covered_by == ""


@pytest.mark.parametrize(
    "verdict",
    [["duplicate"], {"duplicate": 1}, {"duplicate"}, 0, None, True],
)
def test_an_unhashable_or_wrong_typed_verdict_fails_open_without_raising(
    verdict: object,
) -> None:
    """`verdict not in COVERAGE_VERDICTS` runs against a TUPLE, so an unhashable value
    is compared rather than hashed. The set-membership variant of this line would raise
    `TypeError: unhashable type: 'list'` inside a queue worker — the crash class this
    repo has now hit seven times."""
    assert parse_assessment(verdict_turn(arguments=_args(verdict=verdict))) is None


@pytest.mark.parametrize(
    "confidence",
    ["0.95", "", None, [0.95], {"v": 0.95}, float("nan"), float("inf"),
     float("-inf"), 10**400, -0.0001, 1.0001, True, False],
)
def test_an_unusable_confidence_is_refused_rather_than_coerced(confidence: object) -> None:
    """Every non-number, every out-of-range number, both booleans, both infinities, NaN
    and an int too large to convert. Refused, never clamped: clamping `5.0` to `1.0`
    would turn a malfunction into the most confident drop the system can express."""
    assert parse_assessment(verdict_turn(arguments=_args(confidence=confidence))) is None


@pytest.mark.parametrize("missing", ["verdict", "covered_by", "reason", "confidence"])
def test_a_missing_required_field_never_produces_a_droppable_assessment(missing: str) -> None:
    """The two fields the DECISION is computed from reject; the two that are only read
    degrade. Split asserted here rather than assumed, because the two halves have
    opposite failure directions."""
    args = _args()
    args.pop(missing)
    assessment = parse_assessment(verdict_turn(arguments=args))
    if missing in ("verdict", "confidence"):
        assert assessment is None
    else:
        assert assessment is not None
        assert getattr(assessment, missing) == ""


def test_extra_and_adversarially_named_fields_are_ignored() -> None:
    """The parser reads four keys by name and constructs a frozen dataclass, so a model
    that invents `drop`, `covered_by_tier` or `__proto__` cannot reach the gate through
    them. `covered_by_tier` is the one that matters: it is the CALLER's resolution of a
    real card, and a model-asserted value must never survive."""
    assessment = parse_assessment(
        verdict_turn(
            arguments=_args(
                drop=True,
                dropped=True,
                covered_by_tier="mcp",
                covered_by_known=True,
                threshold=0.0,
                __proto__={"verdict": "duplicate"},
            )
        )
    )
    assert assessment is not None
    assert assessment.covered_by_tier == ""


def test_a_two_hundred_kilobyte_reason_is_capped_before_it_reaches_the_document() -> None:
    """The doc has a TTL but no size limit of its own, and the audit bucket is shared
    with the evidence snapshots. A runaway generation must not be able to inflate it one
    document at a time."""
    assessment = parse_assessment(verdict_turn(arguments=_args(reason="x" * 200_000)))
    assert assessment is not None
    assert len(assessment.reason) == 601        # 600 + the ellipsis


@pytest.mark.parametrize(
    "reason",
    [
        "line one\nline two",
        "carriage\rreturn",
        "null\x00byte",
        "bidi ‮ override",
        "zero​width",
        "line separator",
        "para separator",
    ],
)
def test_control_and_bidi_characters_are_flattened_out_of_the_reason(reason: str) -> None:
    """`reason` is interpolated into a log record and stored as a JSON leaf. A newline
    forges a second log record; `\\u2028` survives a naive `"\\n" not in text` check and
    still breaks a line-oriented reader; a bidi override reverses how the rest of the
    line renders in a reviewer's terminal."""
    assessment = parse_assessment(verdict_turn(arguments=_args(reason=reason)))
    assert assessment is not None
    for forbidden in ("\n", "\r", "\x00", "‮", "​", " ", " "):
        assert forbidden not in assessment.reason


@pytest.mark.parametrize(
    "covered_by",
    ["​bp::abc", "bp::abc‮", "bp::abc\xa0", " bp::abc ", "bp::abc\n"],
)
async def test_an_id_wrapped_in_invisible_characters_still_resolves_to_the_real_card(
    covered_by: str,
) -> None:
    """DOCUMENTS a deliberate leniency, checked because it points the other way from
    every other guard here.

    `_clean` flattens Cc/Cf/Zl/Zp to spaces and then collapses whitespace, so an id
    padded with a zero-width space, a bidi override or a non-breaking space normalizes
    back onto the real card and DOES authorize a drop. That is acceptable — the
    resolved id is a real artifact a human can look up, which is the property the
    membership test exists to protect — but it is leniency, so it is pinned rather than
    left to be rediscovered."""
    judge, _client, audit, _index = make_judge(
        [verdict_turn("duplicate", covered_by=covered_by, confidence=0.99)],
        cards=[card()],
        scores=_scores(),
    )
    outcome = await judge.screen_session(make_summary())
    assert outcome.drop is True
    assert audit.judgements[0].assessment.covered_by == "bp::abc"


def test_a_tool_call_that_is_not_the_expected_one_fails_open_even_alongside_a_good_one(
) -> None:
    """A second, differently-named call in the same turn is ignored; a turn with ONLY
    the wrong tool fails open. Neither may be salvaged by position."""
    only_wrong = ModelTurnResult(
        tool_calls=[ToolCallRequest(id="c1", name="record_candidate", arguments=_args())]
    )
    assert parse_assessment(only_wrong) is None

    wrong_first = ModelTurnResult(
        tool_calls=[
            ToolCallRequest(id="c1", name="record_candidate", arguments=_args(verdict="new")),
            ToolCallRequest(id="c2", name="record_coverage", arguments=_args()),
        ]
    )
    assessment = parse_assessment(wrong_first)
    assert assessment is not None and assessment.verdict == "duplicate"


def test_arguments_delivered_as_a_json_string_fail_open_rather_than_being_parsed() -> None:
    """Some providers hand back `arguments` as an unparsed JSON string. The judge does
    not decode it — which is the safe direction (fail open), and is asserted so that
    "the model called the tool but nothing happened" is a known shape rather than a
    mystery in a log."""
    raw = '{"verdict":"duplicate","covered_by":"bp::abc","reason":"r","confidence":0.99}'
    assert parse_assessment(verdict_turn(arguments=raw)) is None


# FOUND BY QA AS A LIVE HOLE, NOW CLOSED. `parse_assessment` iterates
# `result.tool_calls` with no container check — a bare string char-explodes into
# `AttributeError: 'str' object has no attribute 'name'` and `None` raises `TypeError`
# — and it was called OUTSIDE `_ask`'s try/except, so both escaped the judge. The
# function itself is unchanged (it is a pure parser and `ModelTurnResult` is typed);
# what changed is that `_ask` now wraps the parse, so ANY shape a Protocol
# implementation can produce fails open with the judge's own log and span.
@pytest.mark.parametrize("tool_calls", ["record_coverage", None, 7, {"a": 1}])
def test_a_non_sequence_tool_calls_must_fail_open_not_raise(tool_calls: object) -> None:
    assert parse_assessment(ModelTurnResult(tool_calls=tool_calls)) is None  # type: ignore[arg-type]


# ===========================================================================
# 4. Fail-open completeness — every failure proceeds, and none of them drops.
# ===========================================================================


class _NoneReturningClient:
    """A `ModelClient` that returns `None` — a wrapper that forgot to return, a
    provider shim that swallows an error."""

    def __init__(self) -> None:
        self.calls = 0

    async def send_turn(self, messages, tools):  # noqa: ANN001, ANN201
        self.calls += 1
        return None


class _ForeignResultClient:
    """A `ModelClient` that returns a dict instead of a `ModelTurnResult`."""

    async def send_turn(self, messages, tools):  # noqa: ANN001, ANN201
        return {"tool_calls": [], "assistant_text": "sure"}


class _BadBeginTurnClient:
    """A client whose per-turn handle cannot be minted (pool exhausted)."""

    async def send_turn(self, messages, tools):  # noqa: ANN001, ANN201
        return verdict_turn()

    def begin_turn(self):  # noqa: ANN201
        raise RuntimeError("connection pool exhausted")


# FOUND BY QA AS A LIVE HOLE, NOW CLOSED. `parse_assessment(result)` and
# `begin_turn_client(...)` sat OUTSIDE `_ask`'s try/except, so a client that returned
# None, returned a foreign object, or failed to mint a turn handle raised straight out
# of `screen_session`. The callers caught it and extraction proceeded — but the judge's
# own fail-open log never ran and NO `learning.judge` span was emitted, breaking the
# "the span is the DENOMINATOR" invariant the drop cross-check rests on. The whole turn
# is inside the try now.
@pytest.mark.parametrize(
    "client",
    [_NoneReturningClient(), _ForeignResultClient(), _BadBeginTurnClient()],
    ids=["returns-none", "returns-foreign-object", "begin_turn-raises"],
)
async def test_a_broken_model_client_is_handled_inside_the_judge(client: object) -> None:
    judge, _client, audit, _index = make_judge(
        [], cards=[card()], scores=_scores(), model_client=client
    )
    outcome = await judge.screen_session(make_summary())

    assert outcome.drop is False
    assert outcome.outcome == OUTCOME_FAILED
    assert audit.judgements == ()


class _BaseExceptionClient:
    """A client raising a `BaseException` subclass — an SDK that derives its own root
    error type off `BaseException`, or a cancellation-shaped wrapper."""

    async def send_turn(self, messages, tools):  # noqa: ANN001, ANN201
        raise _ProviderAbort("provider aborted the request")


class _ProviderAbort(BaseException):
    pass


# FOUND BY QA AS A LIVE HOLE, NOW CLOSED. `_ask` caught `Exception`, and so do BOTH call
# sites AND the consumer's own per-message isolation — so a `BaseException` from a model
# client escaped `run_once`, killed the whole batch, and left the session stuck at
# `processing`, a state a redelivery can only dead-letter. `_ask` now catches
# `BaseException` and re-raises exactly three: `CancelledError` (swallowing it would make
# the judge uncancellable and break graceful shutdown), `KeyboardInterrupt` and
# `SystemExit` (the operator asking the process to stop).
async def test_a_base_exception_from_the_model_client_does_not_escape_the_judge() -> None:
    judge, _client, _audit, _index = make_judge(
        [], cards=[card()], scores=_scores(), model_client=_BaseExceptionClient()
    )
    outcome = await judge.screen_session(make_summary())
    assert outcome.drop is False


async def test_a_failed_prior_art_read_never_drops_at_either_stage() -> None:
    """"Never drop on a failed read" — a skip is indistinguishable from an absence.

    The pre stage learns this from `PriorArtLookup.available`; the post stage learns it
    from an empty card list, because `DedupStage` contributes an empty half rather than
    raising when a source is down. Both must decline to judge at all."""
    pre_judge, pre_client, pre_audit, _i = make_judge(
        [verdict_turn("duplicate", confidence=1.0)], cards=[card()], index_fails=True
    )
    pre = await pre_judge.screen_session(make_summary())
    assert pre.drop is False
    assert pre_client.calls_made == 0
    assert pre_audit.judgements == ()

    post_judge, post_client, post_audit, _i = make_judge(
        [verdict_turn("duplicate", confidence=1.0)]
    )
    post = await post_judge.adjudicate_candidate(make_envelope(), make_summary(), [])
    assert post.drop is False
    assert post_client.calls_made == 0
    assert post_audit.judgements == ()


async def test_no_free_gate_and_no_failure_path_ever_writes_a_verdict() -> None:
    """The store must contain ONLY verdicts a judge actually gave. A loop-invented `new`
    would be indistinguishable from a model's in the dataset that decides whether
    composable blueprints get built."""
    scenarios = {
        "unavailable": dict(cards=[card()], index_fails=True, turns=[]),
        "no_cards": dict(cards=[], turns=[]),
        "below_floor": dict(cards=[card()], turns=[], scores=_scores(score=0.10)),
        "model_error": dict(cards=[card()], scores=_scores(), turns=[]),
    }
    for name, kwargs in scenarios.items():
        turns = kwargs.pop("turns")
        if name == "model_error":
            from .helpers import BoomModelClient

            judge, _c, audit, _i = make_judge(turns, model_client=BoomModelClient(), **kwargs)
        else:
            judge, _c, audit, _i = make_judge(turns, **kwargs)
        outcome = await judge.screen_session(make_summary())
        assert outcome.drop is False, name
        assert outcome.assessment is None, name
        assert audit.judgements == (), name


async def test_a_malformed_response_records_nothing_and_costs_exactly_one_call() -> None:
    """No retry budget, deliberately: the judge's justification is that it costs less
    than the call it cancels, and a retry spends the saving to salvage an optimization.
    Pinned alongside "records nothing" because the two together are what keep a broken
    prompt from both costing money and poisoning the dataset."""
    judge, client, audit, _i = make_judge(
        [verdict_turn(arguments={"nonsense": True}), verdict_turn("duplicate", confidence=1.0)],
        cards=[card()],
        scores=_scores(),
    )
    outcome = await judge.screen_session(make_summary())

    assert outcome.outcome == OUTCOME_FAILED
    assert client.calls_made == 1
    assert audit.judgements == ()


# ===========================================================================
# 5. The span as the denominator.
# ===========================================================================


@pytest.fixture
def tracer_and_exporter():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("qa"), exporter


async def test_every_reachable_non_recorded_outcome_still_emits_a_span(
    tracer_and_exporter,
) -> None:
    """The store is the numerator and the span is the denominator, so a judgement that
    is not recorded MUST be counted somewhere — otherwise the judge's own miss rate is
    unreadable and a rising `failed` rate looks like a healthy quiet period."""
    from .helpers import BoomModelClient

    tracer, exporter = tracer_and_exporter
    cases = [
        ("skipped_unavailable", dict(turns=[], cards=[card()], index_fails=True)),
        ("skipped_no_prior_art", dict(turns=[], cards=[])),
        ("skipped_below_floor", dict(turns=[], cards=[card()], scores=_scores(score=0.10))),
        ("failed", dict(turns=[], cards=[card()], scores=_scores(),
                        model_client=BoomModelClient())),
        ("record_write_failed", dict(turns=[verdict_turn("duplicate", confidence=1.0)],
                                     cards=[card()], scores=_scores(),
                                     audit=InMemoryAuditStore(fail_judgements=True))),
    ]
    for _expected, kwargs in cases:
        turns = kwargs.pop("turns")
        judge, _c, _a, _i = make_judge(turns, tracer=tracer, **kwargs)
        await judge.screen_session(make_summary())

    outcomes = [
        s.attributes["learning.judge.outcome"]
        for s in exporter.get_finished_spans()
        if s.name == "learning.judge"
    ]
    assert outcomes == [expected for expected, _ in cases]


# FOUND BY QA AS A LIVE HOLE, NOW CLOSED — the observable half of the parse-escape
# defect above. A judgement that failed left NO trace in either the store or the span,
# so the drop-rate cross-check documented on `judge_span` could not see the class at
# all. It emits `outcome=failed` now.
async def test_a_judge_that_raises_still_leaves_a_denominator(tracer_and_exporter) -> None:
    tracer, exporter = tracer_and_exporter
    judge, _c, audit, _i = make_judge(
        [], cards=[card()], scores=_scores(),
        model_client=_NoneReturningClient(), tracer=tracer,
    )
    try:
        await judge.screen_session(make_summary())
    except Exception:  # noqa: BLE001 - the defect itself; the assertion is below
        pass

    assert audit.judgements == ()
    assert [s.name for s in exporter.get_finished_spans()] == ["learning.judge"]


async def test_the_stamped_assessment_is_never_a_loop_invention() -> None:
    """`CandidateEnvelope.judge is None` means "the judge did not run" and must stay
    distinguishable from a stored `new`, which is a positive statement a model made."""
    judge, _c, _a, _i = make_judge([verdict_turn("new", confidence=0.4)])
    ran = await judge.adjudicate_candidate(make_envelope(), make_summary(), [card()])
    assert ran.assessment == CoverageAssessment(
        verdict="new",
        covered_by="bp::abc",
        covered_by_tier="mcp",
        reason="the corpus blueprint computes the same total at the same grain",
        confidence=0.4,
    )

    skipped_judge, _c, _a, _i = make_judge([])
    skipped = await skipped_judge.adjudicate_candidate(make_envelope(), make_summary(), [])
    assert skipped.assessment is None
    assert skipped.outcome != OUTCOME_DROPPED


# ===========================================================================
# 6. The card list is a second untrusted boundary.
# ===========================================================================


# FOUND BY QA AS A LIVE HOLE, NOW CLOSED. `_best_card` claimed totality on the strength
# of `isinstance(c, PriorArtCard)`, which says nothing about the FIELDS — and both
# downstream operations are picky: the sort evaluates `-c.confidence` (a computed
# property that raises on a str `similarity`) with an `id` tiebreak that only fires on a
# confidence TIE, and `_resolve_covered_by` HASHES the id. `_usable_cards` now derives
# the guard from those two operations, exactly as `lookup_prior_art` does for its own
# untrusted sequence.
@pytest.mark.parametrize(
    ("label", "mutation"),
    [
        ("mixed id types on a confidence tie", dict(id=7)),
        ("unhashable id", dict(id=["bp::abc"])),
        ("non-numeric similarity", dict(similarity="high")),
        ("null similarity", dict(similarity=None)),
    ],
)
async def test_a_malformed_card_field_must_not_raise_out_of_the_judge(
    label: str, mutation: dict
) -> None:
    good = card("bp::abc")
    judge, _client, _audit, _index = make_judge(
        [verdict_turn("duplicate", confidence=0.99)]
    )
    outcome = await judge.adjudicate_candidate(
        make_envelope(), make_summary(), [good, replace(good, **mutation)]
    )
    assert outcome.drop in (True, False)     # any answer is fine; a raise is not
