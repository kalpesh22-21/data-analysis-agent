"""WriterStage — the terminal write-router `CandidateStage` (Slice 7).

Runs LAST (generalize → leakage → dedup → **writer**). It converts the upstream
verdicts into the consumer-owned `status` transition (extracted → candidate |
in_review) and the pipeline `control` the consumer honors:

  * `continue`    — auto-land: the enriched envelope is persisted at
                    `status=candidate` (a clean, unsampled blueprint).
  * `route_inbox` — persist at `status=in_review`; the review inbox is the
                    projection over these rows (Contract D §4).

The blueprint inbox SAMPLE (D58b, `blueprint_inbox_sample_rate = 0.10` provisional)
is an injected decider so tests are deterministic and no global RNG/settings is
read here. Leakage near-misses are NEVER sampled out — `route_candidate` forces
them to the inbox before the sample is consulted.

This stage does NOT own `user_knowledge` (S8 auto-commits + drops it upstream) and
never mutates another stage's verdict field — it only advances `status` (the D102
additivity rule: `status` is a consumer-pipeline-owned field).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import replace

from ..candidate.models import CandidateEnvelope
from ..stage import StageContext, StageResult
from .routing import route_candidate

_DEFAULT_BLUEPRINT_INBOX_SAMPLE_RATE = 0.10


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
