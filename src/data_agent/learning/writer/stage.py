"""WriterStage — the terminal write-router `CandidateStage` (Slice 7).

Runs LAST, converting the upstream verdicts into the consumer-owned `status` transition and
the pipeline `control`: `continue` parks an explicitly unsampled blueprint at `status=candidate`,
while `route_inbox` persists at `status=in_review`. Production defaults to a 100% human-review
sample; the injected decider remains so tests and deliberate deployments are deterministic. Leakage
near-misses are NEVER sampled out, because `route_candidate` forces them to the inbox before
the sample is consulted. This stage does not own `user_knowledge` and never mutates another
stage's verdict field — it only advances `status`.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import replace

from ..candidate.models import CandidateEnvelope
from ..stage import StageContext, StageResult
from .routing import route_candidate

# Production is human-review-only: every mined blueprint is visible to a reviewer. The sampler
# seam remains injectable for tests and deployments that deliberately ration review volume.
_DEFAULT_BLUEPRINT_INBOX_SAMPLE_RATE = 1.0


def _default_sampler(rate: float) -> Callable[[CandidateEnvelope], bool]:
    def _decide(_env: CandidateEnvelope) -> bool:
        return random.random() < rate

    return _decide


class WriterStage:
    """The S7 writer stage. `stage_id == "writer"` (the frozen pipeline slot)."""

    stage_id = "writer"

    def __init__(
        self,
        *,
        blueprint_inbox_sample_rate: float = _DEFAULT_BLUEPRINT_INBOX_SAMPLE_RATE,
        sampler: Callable[[CandidateEnvelope], bool] | None = None,
    ) -> None:
        self._rate = blueprint_inbox_sample_rate
        # `sampler` is a deterministic injection point for tests; the default draws
        # a fresh coin per candidate at the configured rate.
        self._sampler = sampler or _default_sampler(blueprint_inbox_sample_rate)

    async def process(self, env: CandidateEnvelope, ctx: StageContext) -> StageResult:
        # The sampling coin is only consulted for a clean auto-landing blueprint;
        # `route_candidate` checks the inbox-forcing conditions first, so drawing it
        # eagerly here is harmless and keeps the call site simple.
        sampled = self._sampler(env)
        decision = route_candidate(env, sampled_for_inbox=sampled)
        new_env = replace(env, status=decision.status)
        return StageResult(new_env, decision.control)
