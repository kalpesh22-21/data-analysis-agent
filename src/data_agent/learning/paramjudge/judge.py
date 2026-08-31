"""ParameterizationJudge — "are these roles right?", asked of a blueprint that passed every
deterministic check and might still be wrong.

PHASE D-1: IT OBSERVES. There is no discard, no repair, no routing change and no code path in
this file that can remove a candidate from the pipeline. That is not caution for its own sake —
see `docs/decisions/learning-blueprint-review-rework-design.md` §D.0. The documented failure
mode of this pipeline is REFUSING GOOD WORK: one session, processed three times, passing triage
and the coverage judge every time, whose SQL is the exact query that answered the user's turn,
produced nothing on all three runs. A candidate reaching this judge has already survived triage,
a coverage judge, the D97 totality walk and five static checks. Adding a sixth gate that can
discard is a bet on a base rate NOBODY HAS MEASURED, and this class exists to measure it.

So the only output is a durable row. Design §D.0 names the rollout gate explicitly: a human
reads a week of these and writes down the flag rate and the agree-with-reviewer rate. If that
number is bad, the correct outcome is to delete this package and keep the card — which the
design also says, and which is why nothing else in the plane depends on it.

EVERYTHING FAILS OPEN. An unreachable model, a timeout, a malformed response, an invented
verdict, an out-of-range confidence, a failed audit write — every one of them leaves the
candidate exactly as a deployment with no judge would. Since the judge takes no action, failing
open costs only the observation.

⚠ THE RECORD IS WRITTEN EVEN THOUGH NOTHING DEPENDS ON IT YET. In phase D-2 the write becomes a
PRECONDITION of the discard (a failed write converts the discard into a proceed), so the write
is built here with that shape already: it is attempted, its failure is logged loudly, and the
`recorded` flag comes back on the outcome. Building it as fire-and-forget now would mean
retrofitting the one guarantee D-2 rests on.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from data_agent.runtime.model.client import ModelClient, begin_turn_client
from data_agent.timeutil import now_iso as _now

from ..audit.judgement import ParamAssessment, ParamJudgeRecord, param_judgement_ref
from ..audit.store import AuditStore
from ..candidate.models import CandidateEnvelope
from .models import ParamJudgeConfig
from .prompt import SYSTEM_PROMPT, blueprint_brief
from .schema import build_param_judge_tool, parse_param_assessment

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParamJudgeOutcome:
    """What one judgement did.

    `assessment` is `None` on every fail-open path, which is also the "no opinion" signal for the
    stage. `recorded` says whether the durable row landed — meaningless to the D-1 caller, which
    proceeds either way, and load-bearing for D-2, which may not act without it.
    """

    assessment: ParamAssessment | None
    recorded: bool = False
    reused: bool = False

    @property
    def would_discard(self) -> bool:
        """WOULD phase D-2 have discarded this candidate?

        The whole measurement, computed in ONE place so the record and any future gate cannot
        drift apart. Two conditions, both from design §D.4: a non-`ok` verdict, and at least one
        Class A finding — the class that means the blueprint is WRONG rather than merely narrow.

        ⚠ The confidence bar is deliberately NOT applied. It is a phase-D-2 number that the
        design leaves unset on purpose, because it is an OUTPUT of this measurement; applying a
        made-up threshold here would filter the very rows that are supposed to determine it.
        """
        if self.assessment is None:
            return False
        return self.assessment.verdict != "ok" and self.assessment.has_class_a


@dataclass(frozen=True)
class ParameterizationJudge:
    """The D-1 judge: one forced-tool call per blueprint, one durable row, no action."""

    model_client: ModelClient
    audit_store: AuditStore
    config: ParamJudgeConfig

    async def judge(
        self, env: CandidateEnvelope, *, accepted_sql: str = ""
    ) -> ParamJudgeOutcome:
        """Judge one blueprint's parameterization. NEVER raises."""
        ref = param_judgement_ref(env.content_hash, env.candidate_id)

        # Idempotency read FIRST. A redelivered session must not pay for a second
        # non-idempotent model call, and re-judging the same content would also put two rows
        # with different verdicts into the dataset the measurement is computed over. A failed
        # READ means "could not look", which is treated as absent — judging twice is wasteful,
        # trusting a read that errored is wrong.
        existing = await self._read(ref)
        if existing is not None:
            return ParamJudgeOutcome(
                assessment=existing.assessment, recorded=True, reused=True
            )

        entries = env.payload.get("parameterization")
        entry_count = len(entries) if isinstance(entries, list) else 0
        assessment = await self._ask(env, accepted_sql=accepted_sql, entry_count=entry_count)
        if assessment is None:
            return ParamJudgeOutcome(assessment=None)

        outcome = ParamJudgeOutcome(assessment=assessment)
        recorded = await self._record(
            env, assessment, ref, would_discard=outcome.would_discard
        )
        return ParamJudgeOutcome(assessment=assessment, recorded=recorded)

    async def _read(self, ref: str) -> ParamJudgeRecord | None:
        reader = getattr(self.audit_store, "read_param_judgement", None)
        if reader is None:
            return None
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                return await reader(ref)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001 — a judge may not kill the consumer
            _logger.warning(
                "param judge: could not read the prior judgement at %s — re-judging", ref,
                exc_info=True,
            )
            return None

    async def _ask(
        self, env: CandidateEnvelope, *, accepted_sql: str, entry_count: int
    ) -> ParamAssessment | None:
        """One forced-tool model turn, or `None` on ANY failure.

        No retries, deliberately: this call produces an observation, and an observation that
        needed three attempts to parse is itself worth recording as a failure rather than
        papering over.

        EVERYTHING is inside the try, including `begin_turn_client` and the parse —
        `ModelClient` is a Protocol, so a client that returns a foreign object or whose
        `begin_turn` raises would otherwise escape the fail-open path entirely.
        """
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    # The blueprint is a SEPARATE user message, after the instructions:
                    # untrusted extracted content never goes inside the instruction message.
                    # Same placement, same reasoning, as the coverage judge's brief.
                    {
                        "role": "user",
                        "content": blueprint_brief(env.payload, accepted_sql=accepted_sql),
                    },
                ]
                client = begin_turn_client(self.model_client)
                result = await client.send_turn(messages, [build_param_judge_tool()])
                return parse_param_assessment(
                    result,
                    entry_count=entry_count,
                    findings_cap=self.config.findings_cap,
                )
        except TimeoutError:
            _logger.warning(
                "param judge: model call exceeded %.1fs — failing OPEN, no opinion recorded",
                self.config.timeout_seconds,
            )
            return None
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            # The THREE that must still propagate: cancellation is how a graceful shutdown
            # reaches an awaiting task, and the other two are the operator asking the process
            # to stop. Re-raised explicitly because the clause below is wider than `Exception`.
            raise
        except BaseException:  # noqa: BLE001 — see below
            # WIDER THAN `Exception`, for the reason `judge/judge.py` records from QA: a
            # provider SDK raising a bare `BaseException` escaped every narrower handler,
            # killed the batch, and left sessions stuck at `processing` — a state only a
            # dead-letter can clear. This judge OBSERVES; it must not be able to destroy a
            # session, let alone a batch of them.
            _logger.warning(
                "param judge: model call raised — failing OPEN, no opinion recorded",
                exc_info=True,
            )
            return None

    async def _record(
        self,
        env: CandidateEnvelope,
        assessment: ParamAssessment,
        ref: str,
        *,
        would_discard: bool,
    ) -> bool:
        """Write the durable row. Returns whether it landed; NEVER raises.

        ⚠ In phase D-1 a `False` here changes nothing — the candidate proceeds either way,
        because it was always going to. In D-2 this return value becomes the discard's
        precondition. The loud WARNING is therefore not decoration: it is the signal that the
        measurement is silently losing rows, which is the one way this phase can fail without
        anything appearing to be wrong.
        """
        writer = getattr(self.audit_store, "record_param_judgement", None)
        if writer is None:
            _logger.warning(
                "param judge: the audit store cannot record parameterization judgements — "
                "the verdict for %s is LOST (this phase produces nothing else)",
                env.candidate_id,
            )
            return False
        generalization = env.payload.get("generalization")
        template = ""
        if isinstance(generalization, dict):
            raw = generalization.get("sql_template")
            template = raw if isinstance(raw, str) else ""
        record = ParamJudgeRecord(
            judgement_ref=ref,
            candidate_id=env.candidate_id,
            session_id=env.source_session,
            content_hash=env.content_hash,
            trace_id=env.source_trace,
            assessment=assessment,
            template=template,
            model=self.config.model,
            judged_at=_now(),
            would_discard=would_discard,
            shadow=self.config.shadow,
        )
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                await writer(record)
            return True
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001 — a judge may not kill the consumer
            _logger.warning(
                "param judge: FAILED to record the judgement for %s — the verdict is LOST",
                env.candidate_id,
                exc_info=True,
            )
            return False
