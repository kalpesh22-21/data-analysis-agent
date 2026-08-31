"""`ParameterizationJudgeStage` — the D-1 observer in the write-router pipeline.

PLACEMENT: after `GeneralizeStage`, before `LeakageGateStage` (design §D.1).

  * AFTER generalize, because every finding it can make is about the REWRITTEN TEMPLATE and its
    relationship to the intent. The S3 plan does not contain that artifact; it is produced by
    the AST rewrite one stage earlier than this.
  * BEFORE leakage, because the phase-D-2 loop mutates the payload, and settling an entity scan
    and then changing what it was settled about is the exact bug `inbox/completion.py`
    `_still_declined` had to add a re-settle to fix. D-1 mutates nothing, but the stage's
    POSITION is the thing D-2 must not have to move.

`control` is ALWAYS `"continue"`. There is no branch in this file that returns anything else,
and `tests/learning/paramjudge/` asserts that over the whole verdict space rather than over the
shadow flag — design §D.0 says "do not implement a discard path in D-1", and that instruction is
only real if a test can fail when someone does.

WHAT IT SKIPS, and why each skip is not merely an optimization:

  * non-blueprints — there is no parameterization to judge;
  * a `fail_to_review` generalization — the candidate ALREADY has a deterministic complaint with
    a reason tag the writer routes on. Paying a model to add a second opinion about a candidate
    that is already going to a human risks the two disagreeing about why it is there. The
    judge's whole population is candidates that passed every mechanical check and might still be
    wrong;
  * a blueprint with no parameterization entries at all — nothing to have an opinion about.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Any

from ..candidate.models import CandidateEnvelope
from ..observability import param_judge_span
from ..stage import StageContext, StageResult
from ..summary.refs import sql_by_ref
from .judge import ParameterizationJudge

_logger = logging.getLogger(__name__)


def _static_outcome(env: CandidateEnvelope) -> str | None:
    """The S4 static-validation outcome, read defensively (mirrors `writer/routing.py`)."""
    generalization = env.payload.get("generalization")
    if not isinstance(generalization, dict):
        return None
    validation = generalization.get("static_validation")
    if not isinstance(validation, dict):
        return None
    outcome = validation.get("outcome")
    return outcome if isinstance(outcome, str) else None


def _accepted_sql(env: CandidateEnvelope, ctx: StageContext) -> str:
    """The query this blueprint was generalized from, or `""`.

    Best-effort by design. The SQL lets the judge check the one ceiling it cannot check
    otherwise — whether a literal it wants re-roled is even in the original query — so its
    absence weakens the judgement but must never skip it: a summary is an in-process value and
    a re-queued candidate can legitimately arrive without one.

    "Latest wins" across refs, matching the builder's rule, and deliberately NOT the S4 stage's
    `_collapse_designations` safety analysis. That function REFUSES when it cannot prove one
    designation subsumes the others, because it feeds a rewrite that would silently drop a
    constraint. Nothing is rewritten here; the SQL is context for a model. Borrowing a refusal
    built for a writer would make this judge skip candidates for a reason that does not apply
    to it.
    """
    summary = getattr(ctx, "summary", None)
    if summary is None:
        return ""
    try:
        by_ref = sql_by_ref(summary)
    except Exception:  # pragma: no cover — defensive; a brief is never worth an exception
        return ""
    refs = env.payload.get("source_tool_call_refs")
    if not isinstance(refs, list):
        return ""
    for ref in reversed(refs):
        sqls = by_ref.get(ref) if isinstance(ref, str) else None
        if sqls:
            return sqls[-1]
    return ""


@dataclass(frozen=True)
class ParameterizationJudgeStage:
    """Observe, record, stamp. Never route, never drop, never repair."""

    judge: ParameterizationJudge
    # The tracer, injected like every other stage's. `None` = no tracing (the `nullcontext`
    # below), so the stage is unchanged in a deployment with no OTLP endpoint.
    tracer: object | None = None
    # The D25 gate. Read at the composition root, never defaulted here, so a session's spans
    # cannot end up half entity-bearing.
    trace_verbose: bool = False
    stage_id: str = "param_judge"

    def _span(self, env: CandidateEnvelope, outcome: str, **attrs: Any) -> Any:
        """One `learning.param_judge` span, or a no-op when no tracer is wired.

        Emitted on EVERY path including the skips, because the span is the DENOMINATOR of the
        phase-D-1 flag rate: the audit store holds only verdicts a judge actually gave, so the
        reasons a candidate was never judged live here and nowhere else.
        """
        if self.tracer is None:
            return nullcontext()
        return param_judge_span(
            self.tracer,  # type: ignore[arg-type]
            candidate_id=env.candidate_id,
            session_id=env.source_session,
            outcome=outcome,
            shadow=True,
            model=self.judge.config.model,
            verbose=self.trace_verbose,
            **attrs,
        )

    async def process(
        self, env: CandidateEnvelope, ctx: StageContext
    ) -> StageResult:
        if env.type != "blueprint":
            with self._span(env, "skipped_not_blueprint"):
                return StageResult(envelope=env, control="continue")
        if _static_outcome(env) != "ok":
            with self._span(env, "skipped_failed_validation"):
                return StageResult(envelope=env, control="continue")
        entries = env.payload.get("parameterization")
        if not isinstance(entries, list) or not entries:
            with self._span(env, "skipped_no_entries"):
                return StageResult(envelope=env, control="continue")

        outcome = await self.judge.judge(env, accepted_sql=_accepted_sql(env, ctx))
        if outcome.assessment is None:
            # Fail-open: indistinguishable from a deployment with no judge wired.
            with self._span(env, "failed"):
                return StageResult(envelope=env, control="continue")
        assessment = outcome.assessment

        if outcome.would_discard:
            # LOUD, and this is the whole compensating control of the phase. In D-2 this line
            # accompanies a destroyed candidate; here it accompanies one that survives, which
            # is exactly the population a human needs to go and look at while deciding whether
            # D-2 should ever ship.
            _logger.warning(
                "param judge: %s WOULD BE DISCARDED under the phase-D-2 rules "
                "(verdict=%s confidence=%.2f) — shadow mode, it proceeds unchanged: %s",
                env.candidate_id,
                outcome.assessment.verdict,
                outcome.assessment.confidence,
                outcome.assessment.feedback,
            )

        # The stamp is ADDITIVE and inert: it changes no routing rule, no static check and no
        # dedup key. It exists so the reviewer card can show what the judge said about the very
        # candidate in front of the reviewer — which is how the agree/disagree half of the
        # measurement gets collected without asking anybody to do extra work.
        generalization = env.payload.get("generalization")
        template = (
            generalization.get("sql_template")
            if isinstance(generalization, dict)
            else None
        )
        with self._span(
            env,
            "reused" if outcome.reused else "judged",
            verdict=assessment.verdict,
            confidence=assessment.confidence,
            findings=len(assessment.findings),
            class_a_findings=sum(1 for f in assessment.findings if f.finding_class == "A"),
            would_discard=outcome.would_discard,
            recorded=outcome.recorded,
            reused=outcome.reused,
            feedback=assessment.feedback or None,
            template=template if isinstance(template, str) else None,
        ):
            return StageResult(
                envelope=replace(env, param_judge=assessment), control="continue"
            )
