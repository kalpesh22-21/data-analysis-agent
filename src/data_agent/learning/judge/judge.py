"""CoverageJudge — "does the corpus already do this?", asked before we pay to find out.

TWO STAGES, deliberately asymmetric. `screen_session` runs PRE-extraction, and above
`pre_drop_confidence` extraction NEVER RUNS. `adjudicate_candidate` runs POST-extraction inside
the dedup stage, only when the soft-layer cosine sits in the ambiguous band. The bars differ
because the evidence does: the pre-extraction judge sees raw SQL with no generalization,
parameterization or grain, so it is the cheapest place to drop and the least-informed one, and
is held to a HIGHER bar.

EVERYTHING FAILS OPEN — an unreachable index, a model error, a timeout, a malformed response,
an invented id all proceed to extraction exactly as a deployment with no judge would. A wrong
KEEP costs one extraction a human then sees; a wrong DROP is invisible. So the judge may only
ever REMOVE work, on positive evidence. NEVER DROP ON A FAILED READ: an unavailable index means
no judgement, zero cards means no model call and NO recorded verdict (the store must contain
only verdicts a judge actually gave), and a failed idempotency read means re-judge.

THE DURABLE RECORD IS A PRECONDITION OF THE DROP: it is written FIRST, and a failed write
converts the drop into a proceed. Every judgement is keyed in `learning_audit` on CONTENT — a
hash of the exact brief — never on a position; what is REUSED is the model's assessment, while
the drop GATE is re-applied every time so retuning a bar takes effect on the next delivery.
SHADOW MODE runs everything and forces the drop to False; a high confidence bar is not a
substitute, and disabling the judge is not either, because it records nothing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace

from data_agent.runtime.model.client import ModelClient, begin_turn_client
from data_agent.timeutil import now_iso as _now

from ..audit.judgement import (
    DROPPABLE_VERDICT,
    CoverageAssessment,
    JudgeRecord,
    JudgeStage,
    judgement_fingerprint,
    post_extraction_ref,
    pre_extraction_ref,
)
from ..audit.store import AuditStore
from ..candidate.models import CandidateEnvelope
from ..extractor.prior_art import (
    PriorArtLookup,
    lookup_prior_art,
    prior_art_query_text,
    render_prior_art_block,
)
from ..observability import judge_span
from ..priorart import TIER_LEARNING, TIER_MCP, PriorArtCard, PriorArtIndex
from ..summary.models import SessionSummary
from .prompt import SYSTEM_PROMPT, candidate_brief, session_brief
from .schema import build_judge_tool, parse_assessment

_logger = logging.getLogger(__name__)

# Which trust tiers may authorize a DROP. `unsourced` is excluded, and this is not a new
# rule — `priorart/models.py` already states it for the dedup structural layer: an
# unsourced node is one no writer we control stamped, i.e. a hand edit or a foreign
# writer, and it "is NEVER treated as canon, so it can never trigger the
# drop-the-candidate verdict". Discarding an analyst's work because a node of unknown
# provenance appears to cover it is the same claim, made on softer evidence.
DROP_ELIGIBLE_TIERS: frozenset[str] = frozenset({TIER_MCP, TIER_LEARNING})

# Which card ORIGINS may authorize a drop. `corpus` is excluded, and this is likewise a
# rule that already existed rather than a new one: `PriorArtCard.origin` states that a
# `learning_corpus` card "can at most route to a human", because it is usually an
# IN-FLIGHT sibling candidate that has not landed and may yet be rejected, dropped, or
# fail its landing gates. Discarding an analyst's session because of an artifact that
# might never exist is a weaker basis than any other drop this system takes. The cost of
# excluding it is bounded — the sibling that DID survive still carries the idea, and the
# soft layer still routes a close in-flight pair to a human as it did before this slice.
DROP_ELIGIBLE_ORIGINS: frozenset[str] = frozenset({"graph"})


class JudgeConfigError(ValueError):
    """A judge configuration that would silently disable or invert a gate."""


@dataclass(frozen=True)
class JudgeConfig:
    """The judge's knobs — everything threshold-shaped is config, not a constant.

    They live HERE and are built from `LearningSettings` by the factory, deliberately NOT on
    `PromotionPolicy`: a knob that can silently cancel extractions must be live from the first
    deploy.
    """

    # The PRE-extraction drop bar: a `duplicate` at or above this cancels extraction.
    # Higher than the post bar on purpose — see the module docstring.
    pre_drop_confidence: float = 0.90
    # The POST-extraction drop bar. Lower because the judge is better informed there.
    post_drop_confidence: float = 0.75
    # The ambiguous cosine band. Below `band_low` nothing in the corpus is close enough
    # to be worth asking about; above `band_high` the answer is obvious. The band gates
    # the POST-extraction call at both ends and the PRE-extraction call at the low end
    # only: pre-extraction the retrieval score compares a session's raw text against
    # entity-free corpus intents, which is a much noisier comparison than the
    # post-extraction intent-against-intent one, so a high score there is not the
    # settled answer it is on the other side of the extractor.
    band_low: float = 0.70
    band_high: float = 0.97
    # Prior-art cards shown per judgement. Matches the extractor's default so the two
    # see the same view of the corpus.
    prior_art_limit: int = 5
    # A hard ceiling on the judge's own latency. The judge is an OPTIMIZATION; it must
    # never be the reason a session takes longer than it used to. On expiry: fail open.
    timeout_seconds: float = 30.0
    # SHADOW MODE — see the module docstring. Everything runs and nothing is discarded;
    # the record carries `would_drop` so the rollout question ("what WOULD we have
    # thrown away?") is a query taken before anything is thrown away. Off by default:
    # the user's decision was to drop, taken with the risk stated.
    shadow: bool = False
    # The judge model id, recorded on every judgement. Verdicts are compared across
    # months and the model changes underneath them; without this the dataset silently
    # pools two different judges.
    model_id: str = ""

    def __post_init__(self) -> None:
        # Validate what BREAKS a gate; tolerate (loudly) what merely encodes a posture
        # the design argues against. The same split `ExtractorConfig` makes.
        #
        # An inverted band makes the post-extraction judge UNREACHABLE for every
        # candidate — a whole stage silently off, with no log at the point of use to
        # explain it. That is `DedupStage`'s inverted-threshold case exactly, and it
        # fails at construction (the composition root, at process start) for the same
        # reason.
        if self.band_low > self.band_high:
            raise JudgeConfigError(
                f"judge band_low ({self.band_low}) must be <= band_high "
                f"({self.band_high}); an inverted band is empty, which silently "
                "disables the post-extraction judge for every candidate"
            )
        # A bar outside [0,1] is not a strict or lenient setting, it is a broken one. A
        # negative bar drops on EVERY `duplicate` verdict regardless of confidence —
        # the dangerous direction — and a bar above 1.0 disables the drop the operator
        # asked for while looking like it is on.
        for name, value in (
            ("pre_drop_confidence", self.pre_drop_confidence),
            ("post_drop_confidence", self.post_drop_confidence),
        ):
            if not (0.0 <= value <= 1.0):
                raise JudgeConfigError(
                    f"judge {name} ({value}) must be within [0.0, 1.0]; a bar outside "
                    "the confidence range either drops unconditionally or never drops"
                )
        if self.post_drop_confidence > self.pre_drop_confidence:
            # NOT an error. Both bars stay reachable and meaningful, so nothing is
            # silently unreachable — this is a posture, and an operator running a
            # measured experiment should be able to express it. It is still wrong by
            # default (the pre-extraction judge sees strictly less), so it is loud.
            _logger.warning(
                "judge pre_drop_confidence (%.2f) is LOWER than post_drop_confidence "
                "(%.2f). The pre-extraction judge sees no generalization, no "
                "parameterization and no result grain, so it is the less-informed of "
                "the two and should be held to the HIGHER bar. Check the config.",
                self.pre_drop_confidence,
                self.post_drop_confidence,
            )


@dataclass(frozen=True)
class JudgeOutcomeResult:
    """What one judgement did.

    `drop=True` is the ONLY thing a caller must act on. `assessment` is carried so the caller can
    stamp it on an envelope, and `outcome` is the telemetry label distinguishing the several
    reasons a judgement did not happen — which a bare `drop=False` would flatten into one.
    """

    drop: bool = False
    assessment: CoverageAssessment | None = None
    outcome: str = "not_judged"


# Telemetry labels for `JudgeOutcomeResult.outcome`. Each is a genuinely different fact
# about the loop and the rates are read separately: `skipped_unavailable` rising is an
# infrastructure problem, `skipped_no_prior_art` rising means a young corpus,
# `skipped_below_floor` rising means the band is mistuned, and `failed` rising means the
# judge model or its prompt is broken.
OUTCOME_DROPPED = "dropped"
OUTCOME_PROCEEDED = "proceeded"
OUTCOME_SKIPPED_UNAVAILABLE = "skipped_unavailable"
OUTCOME_SKIPPED_NO_PRIOR_ART = "skipped_no_prior_art"
OUTCOME_SKIPPED_BELOW_FLOOR = "skipped_below_floor"
OUTCOME_SKIPPED_ABOVE_BAND = "skipped_above_band"
OUTCOME_FAILED = "failed"
OUTCOME_RECORD_WRITE_FAILED = "record_write_failed"


class CoverageJudge:
    """The pre- and post-extraction coverage judge. Construct once per process."""

    def __init__(
        self,
        model_client: ModelClient,
        audit: AuditStore,
        *,
        prior_art: PriorArtIndex,
        config: JudgeConfig | None = None,
        tracer: object | None = None,
        trace_verbose: bool = False,
    ) -> None:
        self._model_client = model_client
        # MUST be the same instance the consumer writes evidence snapshots with. A split
        # would put the drop records in a store nobody queries — the mitigation present
        # in code and absent in practice. Pinned at the composition root (factory §1).
        self._audit = audit
        # REQUIRED, unlike everywhere else in this loop. With no prior-art index there is
        # nothing to be covered BY, so the question the judge asks has no content; a
        # judge without one could only ever answer `new`, at the price of a model call.
        # The factory therefore builds no judge when no index is wired.
        self._prior_art = prior_art
        self._config = config or JudgeConfig()
        self._tracer = tracer
        # The D25 gate for THIS span (`observability.py::judge_span`). A ctor argument
        # next to `tracer` rather than a `JudgeConfig` field, mirroring
        # `PromotionScheduler`: it configures the telemetry seam, not the judgement, and
        # nothing about a verdict may depend on it. Sourced from
        # `LearningSettings.learning_trace_verbose` at the composition root
        # (`factory.py::_build_judge`), so this span's posture can never disagree with
        # the triage/extract spans of the same session.
        self._trace_verbose = trace_verbose

    # -- stage 1: before extraction --------------------------------------------

    async def screen_session(self, summary: SessionSummary) -> JudgeOutcomeResult:
        """Judge one KEEP-triaged session BEFORE extraction. `drop=True` ⇒ do not extract.

        The prior-art read is the SAME path slice 3a built (`prior_art_query_text` →
        `lookup_prior_art`), not a parallel one: same query construction, same fail-open posture,
        same three-way available/empty/not-searched distinction. Two readers of one lookup could
        otherwise disagree about what the corpus contains within a single session's lifetime.
        """
        lookup = await lookup_prior_art(
            self._prior_art,
            prior_art_query_text(summary),
            limit=self._config.prior_art_limit,
        )
        return await self._judge(
            stage="pre_extraction",
            summary=summary,
            lookup=lookup,
            brief=session_brief(summary),
            threshold=self._config.pre_drop_confidence,
            band_high=None,  # see JudgeConfig.band_high — no upper free pass pre-extraction
            candidate_id=None,
        )

    # -- stage 2: after extraction, in the ambiguous band only -----------------

    async def adjudicate_candidate(
        self,
        env: CandidateEnvelope,
        summary: SessionSummary,
        cards: list[PriorArtCard],
    ) -> JudgeOutcomeResult:
        """Judge one EXTRACTED candidate against the cards the dedup soft layer already retrieved.

        `drop=True` ⇒ do not keep the candidate. Takes the cards rather than searching again: the
        soft layer has just embedded the intent and unioned two sources to produce them, and it
        means the judge adjudicates EXACTLY what dedup banded. The judgement is KEYED on the
        rendered candidate brief, NOT on `env.candidate_id` — a re-extraction can emit a different
        count and order, so an id-keyed cache can hand one candidate a verdict rendered about
        another.
        """
        lookup = PriorArtLookup(
            query=_intent_of(env), cards=tuple(cards), available=True
        )
        return await self._judge(
            stage="post_extraction",
            summary=summary,
            lookup=lookup,
            brief=candidate_brief(env),
            threshold=self._config.post_drop_confidence,
            band_high=self._config.band_high,
            candidate_id=env.candidate_id,
        )

    # -- the shared body -------------------------------------------------------

    async def _judge(
        self,
        *,
        stage: JudgeStage,
        summary: SessionSummary,
        lookup: PriorArtLookup,
        brief: str,
        threshold: float,
        band_high: float | None,
        candidate_id: str | None,
    ) -> JudgeOutcomeResult:
        """The one body both stages share: gate → reuse-or-ask → record → drop.

        The order is load-bearing at every step: the free gates run BEFORE the model call, so the
        common case costs nothing; the idempotency read runs before it too, so a redelivery costs a
        `get` rather than a non-idempotent generation; the record write runs before the drop, so a
        drop can never be invisible; the drop gate is RE-APPLIED on the reuse path, so retuning a bar
        takes effect on the next delivery instead of being frozen into a stored outcome; and shadow
        mode forces the drop to False LAST, so a shadow row is byte-for-byte the row the same session
        would have produced in anger. The judgement key is derived HERE rather than by the two
        callers, so there is one rule and no way to key on something that is not content.
        """
        fingerprint = judgement_fingerprint(stage, summary.content_hash, brief)
        ref = (
            pre_extraction_ref(fingerprint)
            if stage == "pre_extraction"
            else post_extraction_ref(fingerprint)
        )
        best = _best_card(lookup.cards)
        gate = self._free_gate(lookup, best, band_high=band_high)
        if gate is not None:
            self._observe(
                stage, summary, gate, candidate_id=candidate_id, lookup=lookup,
                best_similarity=best.confidence if best is not None else 0.0,
                threshold=threshold, reused=False,
            )
            return gate

        assert best is not None  # `_free_gate` returns for an empty card list
        reused = await self._reuse(ref, fingerprint)
        assessment = reused
        if assessment is None:
            assessment = await self._ask(lookup, brief)
        if assessment is None:
            result = JudgeOutcomeResult(outcome=OUTCOME_FAILED)
            self._observe(
                stage, summary, result, candidate_id=candidate_id, lookup=lookup,
                best_similarity=best.confidence, threshold=threshold, reused=False,
            )
            return result

        assessment, allowed, origin, authorizing = _resolve_covered_by(
            assessment, lookup.cards
        )
        would_drop = _drop_allowed(
            assessment,
            allowed=allowed,
            droppable_origin=origin in DROP_ELIGIBLE_ORIGINS,
            threshold=threshold,
        )
        drop = would_drop and not self._config.shadow
        outcome = OUTCOME_DROPPED if drop else OUTCOME_PROCEEDED
        record = JudgeRecord(
            judgement_ref=ref,
            stage=stage,
            session_id=summary.session_id,
            content_hash=summary.content_hash,
            trace_id=summary.trace_id,
            assessment=assessment,
            outcome=outcome,
            threshold=threshold,
            best_similarity=best.confidence,
            authorizing_similarity=authorizing,
            cards_shown=len(lookup.cards),
            model=self._config.model_id,
            judged_at=_now(),
            candidate_id=candidate_id,
            covered_by_known=allowed,
            covered_by_origin=origin,
            fingerprint=fingerprint,
            would_drop=would_drop,
            shadow=self._config.shadow,
        )
        if not await self._persist(record, drop=drop):
            result = JudgeOutcomeResult(
                assessment=assessment, outcome=OUTCOME_RECORD_WRITE_FAILED
            )
            self._observe(
                stage, summary, result, candidate_id=candidate_id, lookup=lookup,
                best_similarity=best.confidence, threshold=threshold,
                reused=reused is not None,
            )
            return result

        if would_drop and self._config.shadow:
            _logger.warning(
                "judge: %s SHADOW MODE — session %s (candidate %s) WOULD have been "
                "dropped (verdict=%s covered_by=%s confidence=%.2f >= %.2f) and was "
                "NOT. The verdict is recorded at %s with would_drop=true; nothing is "
                "discarded while LEARNING_JUDGE_SHADOW_MODE is on.",
                stage, summary.session_id, candidate_id, assessment.verdict,
                assessment.covered_by, assessment.confidence, threshold, ref,
            )
        elif drop:
            _logger.warning(
                "judge: %s DROP for session %s (candidate %s) — verdict=%s "
                "covered_by=%s tier=%s confidence=%.2f >= %.2f. Extraction/keep is "
                "CANCELLED and the work is discarded; the durable record is at %s.",
                stage, summary.session_id, candidate_id, assessment.verdict,
                assessment.covered_by, assessment.covered_by_tier,
                assessment.confidence, threshold, ref,
            )
        result = JudgeOutcomeResult(drop=drop, assessment=assessment, outcome=outcome)
        self._observe(
            stage, summary, result, candidate_id=candidate_id, lookup=lookup,
            best_similarity=best.confidence, threshold=threshold,
            reused=reused is not None, would_drop=would_drop,
        )
        return result

    def _free_gate(
        self,
        lookup: PriorArtLookup,
        best: PriorArtCard | None,
        *,
        band_high: float | None,
    ) -> JudgeOutcomeResult | None:
        """The three answers that need no model call, or `None` to go on and ask.

        Each is a different fact and gets its own label: collapsing them would make the judge's own
        miss rate unreadable, and one of them (`unavailable`) is an outage that must never be
        mistaken for a corpus that holds nothing.
        """
        if not lookup.available:
            _logger.warning(
                "judge: prior art UNAVAILABLE — NOT judging this session. An empty "
                "block would read as 'nothing exists', and a drop taken on a failed "
                "read is exactly the invisible loss this stage is built to avoid. "
                "Extraction proceeds."
            )
            return JudgeOutcomeResult(outcome=OUTCOME_SKIPPED_UNAVAILABLE)
        if best is None:
            # Nothing exists to be covered BY. The verdict would be `new` by
            # construction, so asking for it is pure cost — and RECORDING it would be
            # worse, because a fabricated verdict is indistinguishable in the store from
            # one a judge gave, and this store is the dataset.
            return JudgeOutcomeResult(outcome=OUTCOME_SKIPPED_NO_PRIOR_ART)
        if best.confidence < self._config.band_low:
            return JudgeOutcomeResult(outcome=OUTCOME_SKIPPED_BELOW_FLOOR)
        if band_high is not None and best.confidence > band_high:
            return JudgeOutcomeResult(outcome=OUTCOME_SKIPPED_ABOVE_BAND)
        return None

    async def _reuse(self, ref: str, fingerprint: str) -> CoverageAssessment | None:
        """A previously stored assessment for this exact CONTENT, or `None`.

        Never raises: a store that cannot be read yields `None`, i.e. "ask the model again" — the
        honest interpretation, and the one that fails toward doing the work rather than discarding
        it. The fingerprint is RE-CHECKED even though it is already baked into *ref*, because a
        hand-written doc or a key-format change that outlived its data would otherwise hand this
        candidate a verdict rendered about different content, and the audit `reason` would describe
        the wrong session. DEADLINE-BOUND like the model call: a hung `get` would stall every
        KEEP-triaged session, and this read exists purely to SAVE a model call.
        """
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                record = await self._audit.read_judgement(ref)
        except TimeoutError:
            _logger.warning(
                "judge: reading the prior judgement at %s exceeded %.1fs; re-judging. "
                "A read that did not return is not evidence of a prior verdict.",
                ref,
                self._config.timeout_seconds,
            )
            return None
        except Exception:  # noqa: BLE001 - an unreadable cache must never cost a session
            _logger.warning(
                "judge: could not read the prior judgement at %s; re-judging. A failed "
                "read is not evidence of a prior verdict.",
                ref,
                exc_info=True,
            )
            return None
        if record is None:
            return None
        if record.fingerprint != fingerprint:
            _logger.warning(
                "judge: the stored judgement at %s carries fingerprint %r, not %r — "
                "treating it as no record on file and re-judging. Reusing it would "
                "apply a verdict rendered about different content.",
                ref,
                record.fingerprint,
                fingerprint,
            )
            return None
        _logger.info(
            "judge: reusing the stored assessment at %s (verdict=%s confidence=%.2f) — "
            "no model call. The drop gate is re-applied against the CURRENT thresholds.",
            ref,
            record.assessment.verdict,
            record.assessment.confidence,
        )
        return record.assessment

    async def _ask(self, lookup: PriorArtLookup, brief: str) -> CoverageAssessment | None:
        """One forced-tool model turn, or `None` on ANY failure.

        No retries, deliberately: the judge's whole justification is that it costs less than the call
        it cancels, and a retry budget spends the saving to salvage an optimization. EVERYTHING is
        inside the try, including `begin_turn_client` and `parse_assessment` — `ModelClient` is a
        Protocol, so a client that returns a foreign object or whose `begin_turn` raises would
        otherwise escape without the fail-open log and with NO `learning.judge` span, silently
        breaking the "the span is the DENOMINATOR" invariant the whole drop cross-check rests on.
        """
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    # The block is a SEPARATE user message, ahead of the brief —
                    # untrusted corpus text never goes inside the instruction message.
                    # Same placement, and the same reasoning, as the extractor's
                    # prior-art message.
                    {"role": "user", "content": render_prior_art_block(lookup)},
                    {"role": "user", "content": brief},
                ]
                client = begin_turn_client(self._model_client)
                result = await client.send_turn(messages, [build_judge_tool()])
                return parse_assessment(result)
        except TimeoutError:
            _logger.warning(
                "judge: model call exceeded %.1fs — failing OPEN, extraction proceeds",
                self._config.timeout_seconds,
            )
            return None
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            # The THREE that must still propagate. Cancellation is how a graceful
            # shutdown reaches an awaiting task, and swallowing it here would turn the
            # judge into a task that cannot be cancelled; the other two are the
            # operator asking the process to stop. Re-raised explicitly rather than
            # left to a narrower `except Exception`, because the clause below is
            # deliberately wider than that.
            raise
        except BaseException:  # noqa: BLE001 - see below; the judge may not kill the daemon
            # WIDER THAN `Exception`, and QA is the reason. `_ask` caught `Exception`,
            # and so do both call sites AND the consumer's own per-message isolation —
            # so a `BaseException` from a model client escaped `run_once`, killed the
            # whole batch, and left the session stuck at `processing`, a state a
            # redelivery can only dead-letter. The judge is an OPTIMIZATION; it must
            # not be able to destroy a session, let alone a batch of them. A provider
            # SDK raising a bare `BaseException` is a bug in that SDK, but "the library
            # is well-behaved" is not a property this component may depend on.
            _logger.warning(
                "judge: the model turn raised a NON-Exception BaseException (a broken "
                "client) — failing OPEN, extraction proceeds",
                exc_info=True,
            )
            return None

    async def _persist(self, record: JudgeRecord, *, drop: bool) -> bool:
        """Write the judgement. Returns False ONLY when a DROP must be abandoned.

        The asymmetry is the mitigation: a drop with no record is the one outcome this design
        refuses, so a write failure on the drop path turns the drop into a proceed, while on the
        PROCEED path the same failure costs one dataset row and is swallowed. DEADLINE-BOUND, and a
        timeout counts as a FAILED write — a write that did not return is one we cannot claim
        landed.
        """
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                await self._audit.record_judgement(record)
        except Exception:  # noqa: BLE001 - see the docstring for the two directions
            if drop:
                _logger.error(
                    "judge: the durable drop record at %s could NOT be written — "
                    "REFUSING to drop. A drop with no audit row is an invisible loss, "
                    "which is precisely what this record exists to prevent. Extraction "
                    "proceeds.",
                    record.judgement_ref,
                    exc_info=True,
                )
                return False
            _logger.warning(
                "judge: could not write the judgement record at %s; the verdict is "
                "lost from the coverage dataset but nothing else changes.",
                record.judgement_ref,
                exc_info=True,
            )
        return True

    def _observe(
        self,
        stage: JudgeStage,
        summary: SessionSummary,
        result: JudgeOutcomeResult,
        *,
        candidate_id: str | None,
        lookup: PriorArtLookup,
        best_similarity: float,
        threshold: float,
        reused: bool,
        would_drop: bool = False,
    ) -> None:
        """Emit the `learning.judge` span — shape-only, plus the D25-gated basis of the verdict.

        NEVER raises. Takes the whole *lookup* rather than a pre-computed count, because under
        verbose the span carries `render_prior_art_block(lookup)` — the SAME renderer, on the same
        object, that built the message the model was actually sent, which is the only way "what was
        fed in" cannot drift from what was fed in. It is rendered ONLY when verbose is on, since the
        renderer sanitizes every field of every card. The verbose attrs are ENTITY-BEARING (see
        `observability.py::judge_span`) and appear on the free-gate paths too: `reason` is empty
        there, but the prior-art block is what makes a `skipped_below_floor` readable.

        The blanket catch is not defensive habit. This runs AFTER the durable record is written and
        BEFORE `_judge` returns, so a raising exporter would leave a `dropped=true` row in the audit
        store for a session that then went on to be extracted — a permanent lie in the one dataset
        the drop mitigation depends on. Telemetry may never contradict the record.
        """
        if self._tracer is None:
            return
        assessment = result.assessment
        try:
            with judge_span(
                self._tracer,
                session_id=summary.session_id,
                stage=stage,
                outcome=result.outcome,
                candidate_id=candidate_id,
                verdict=assessment.verdict if assessment is not None else None,
                confidence=assessment.confidence if assessment is not None else 0.0,
                covered_by_tier=(
                    assessment.covered_by_tier if assessment is not None else None
                ),
                best_similarity=best_similarity,
                threshold=threshold,
                cards_shown=len(lookup.cards),
                dropped=result.drop,
                would_drop=would_drop,
                shadow=self._config.shadow,
                reused=reused,
                verbose=self._trace_verbose,
                reason=assessment.reason if assessment is not None else None,
                covered_by=assessment.covered_by if assessment is not None else None,
                prior_art=(
                    render_prior_art_block(lookup) if self._trace_verbose else None
                ),
            ):
                pass
        except Exception:  # noqa: BLE001 - see the docstring: telemetry never rewrites history
            _logger.warning(
                "judge: emitting the learning.judge span failed; the judgement itself "
                "stands and is already recorded.",
                exc_info=True,
            )


# --- pure helpers -------------------------------------------------------------------


def _intent_of(env: CandidateEnvelope) -> str:
    intent = env.payload.get("intent") if isinstance(env.payload, dict) else None
    return intent if isinstance(intent, str) else ""


def _usable_cards(cards: tuple[PriorArtCard, ...]) -> list[PriorArtCard]:
    """The cards this module can actually operate on, in input order.

    The isinstance guard is NOT enough: the dataclass says nothing about its FIELDS, and the two
    downstream operations are picky in ways the class is not. `min(..., key=(-c.confidence,
    c.id))` needs a real NUMBER — `confidence` is a computed property, so a `str` similarity
    raises on ACCESS — and a `str` id, because the tiebreak only evaluates on a confidence TIE
    and a non-str id is a latent comparison error; `{c.id: c}` in `_resolve_covered_by` needs a
    HASHABLE id. Both are reachable: the post-extraction path is handed a list assembled by a
    SECOND producer from stored JSON, and `PriorArtIndex` is a Protocol whose other
    implementations have no coercing mapper. A card that cannot be RANKED also cannot be cited
    as `covered_by`, so skipping it can never turn a proceed into a drop.
    """
    usable: list[PriorArtCard] = []
    for card in cards:
        if not isinstance(card, PriorArtCard) or not isinstance(card.id, str):
            continue
        try:
            confidence = card.confidence
        except Exception:  # noqa: BLE001 - a computed property over a mis-typed field
            continue
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            continue
        try:
            score = float(confidence)
        except (OverflowError, ValueError):
            continue
        # A cosine outside [0,1] is not a weak signal, it is a broken one — and `inf`
        # would sort above every genuine hit and clear every band. The same posture
        # `extractor/prior_art.py::_score` takes, for the same reason. NaN fails both
        # comparisons, so this rejects it without a separate isnan test.
        if not (0.0 <= score <= 1.0):
            continue
        usable.append(card)
    return usable


def _drop_rank(card: PriorArtCard) -> int:
    """How permissive a card is, LOWER being more restrictive.

    Used only to break a duplicate-id collision in `_resolve_covered_by`, where the answer must
    be the card that authorizes the LEAST. `0` cannot authorize a drop at all; `1` can. Nothing
    else is ranked, because nothing else changes what the id is allowed to do.
    """
    return (
        1
        if card.origin in DROP_ELIGIBLE_ORIGINS and card.tier in DROP_ELIGIBLE_TIERS
        else 0
    )


def _best_card(cards: tuple[PriorArtCard, ...]) -> PriorArtCard | None:
    """The highest-confidence USABLE card, or `None`.

    Sorted here rather than trusting the producer's ordering: one caller's cards come from
    `PriorArtIndex.search` (ordered by contract), the other's from a UNION of two independently
    ordered sources where the contract does not hold. The `id` tiebreak keeps the choice
    deterministic across equal confidences. Total over a bad member AND a bad FIELD.
    """
    usable = _usable_cards(cards)
    if not usable:
        return None
    return min(usable, key=lambda c: (-c.confidence, c.id))


def _resolve_covered_by(
    assessment: CoverageAssessment, cards: tuple[PriorArtCard, ...]
) -> tuple[CoverageAssessment, bool, str, float]:
    """Resolve the model's `covered_by` against the cards it was actually shown.

    Returns the assessment with `covered_by_tier` filled in from the REAL card (never from
    anything the model said), whether the id was known at all, the card's ORIGIN, and that card's
    own retrieval score. The origin is returned rather than pre-reduced to a boolean so it can go
    ON THE RECORD — it is one of the five drop conditions and the only one a reader cannot infer
    from the other stored fields. The score is returned because the band gates on the BEST card
    while the drop is authorized by whichever card the model NAMES, and those need not be the
    same card.

    The MEMBERSHIP TEST is the guard, derived from what `covered_by` is FOR: a human opens the
    audit row and looks the artifact up, so an id that is not in the corpus makes that row
    unauditable and must not be allowed to cancel work. It handles `""` and a hallucinated id
    identically, with no separate emptiness check. Duplicate ids resolve MOST-RESTRICTIVE-WINS,
    not last-wins: a positional dict comprehension would make a discard depend on the order two
    producers happened to be concatenated in.
    """
    # `_usable_cards`, not a bare isinstance: the dict HASHES the id, and a card whose
    # id is a list raises `unhashable type` right here. Using the same filter as
    # `_best_card` also means the card that authorizes a drop is always one that could
    # have been ranked.
    by_id: dict[str, PriorArtCard] = {}
    for candidate in _usable_cards(cards):
        incumbent = by_id.get(candidate.id)
        if incumbent is None or _drop_rank(candidate) < _drop_rank(incumbent):
            by_id[candidate.id] = candidate
    card = by_id.get(assessment.covered_by)
    if card is None:
        if assessment.covered_by:
            _logger.warning(
                "judge: covered_by=%r is not one of the %d artifacts the judge was "
                "shown. Recording the verdict, but it can never authorize a drop — an "
                "id nobody can look up makes the audit record unauditable.",
                assessment.covered_by,
                len(by_id),
            )
        return replace(assessment, covered_by_tier=""), False, "", 0.0
    return (
        replace(assessment, covered_by_tier=card.tier),
        True,
        card.origin,
        card.confidence,
    )


def _drop_allowed(
    assessment: CoverageAssessment,
    *,
    allowed: bool,
    droppable_origin: bool,
    threshold: float,
) -> bool:
    """The drop gate. FIVE conditions, all required.

      1. `verdict == duplicate` — `existing-plus-delta` says by construction that something is
         left to learn, so only one verdict may cancel work.
      2. `confidence >= threshold` — at or above, matching the settings' wording.
      3. the named artifact was one we showed (see `_resolve_covered_by`).
      4. its tier may authorize a drop (`DROP_ELIGIBLE_TIERS`) — never `unsourced`.
      5. its origin may authorize a drop — never an unlanded `learning_corpus` sibling.

    Deliberately a conjunction of five POSITIVE facts rather than a search for reasons to refuse:
    every path that is not all five is a proceed, including every path this function has not
    thought of.
    """
    return (
        assessment.verdict == DROPPABLE_VERDICT
        and assessment.confidence >= threshold
        and allowed
        and droppable_origin
        and assessment.covered_by_tier in DROP_ELIGIBLE_TIERS
    )


__all__ = [
    "DROP_ELIGIBLE_ORIGINS",
    "DROP_ELIGIBLE_TIERS",
    "OUTCOME_DROPPED",
    "OUTCOME_FAILED",
    "OUTCOME_PROCEEDED",
    "OUTCOME_RECORD_WRITE_FAILED",
    "OUTCOME_SKIPPED_ABOVE_BAND",
    "OUTCOME_SKIPPED_BELOW_FLOOR",
    "OUTCOME_SKIPPED_NO_PRIOR_ART",
    "OUTCOME_SKIPPED_UNAVAILABLE",
    "CoverageJudge",
    "JudgeConfig",
    "JudgeConfigError",
    "JudgeOutcomeResult",
]
