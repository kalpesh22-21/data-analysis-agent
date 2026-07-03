"""The `CandidateStage` pipeline seam (Wave-0 contract freeze, D102 §7.1).

The ONE frozen injection point the write-router stages (S4 generalize → S5 leakage
→ S6 dedup → S7 writer) plug into. The consumer runs an ordered, injected
`tuple[CandidateStage, ...]` over each freshly-extracted (`status=extracted`)
envelope. Each builder implements their stage in their OWN module exposing a
`CandidateStage`; wiring is a one-line registration at the composition root
(`scripts/run_learning_consumer.py`), so no two builders co-edit the consumer.

**Empty tuple ⇒ behaviorally identical current behavior** (no extra puts; additive
keys only — the stub fallback, mirroring the S3 extractor DI). No concrete stage
lives here — this is the contract only.

The S9 promotion scheduler is NOT a stage (§7.2): it is the separate cron-scanned
process reading `learning_candidates` by `status`; it shares the envelope contract
but not this seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from .candidate.models import CandidateEnvelope
from .summary.models import SessionSummary
from .triage import TriageVerdict

# The stage's control signal, honored by the consumer after each stage runs:
#   continue    — advance to the next stage in the pipeline
#   route_inbox — stop this candidate's pipeline; persist the enriched envelope
#                 (a writer stage will have set status=in_review) for the review inbox
#   drop        — stop this candidate's pipeline; do NOT persist the enriched envelope
#                 (e.g. S8 user_knowledge auto-commit writes elsewhere then drops)
#   halt        — stop the WHOLE extraction pipeline; the current enriched envelope
#                 IS persisted; the remaining candidates are skipped
StageControl = Literal["continue", "route_inbox", "drop", "halt"]


@dataclass(frozen=True)
class StageContext:
    """Read-only context a stage may consult. Carries the session summary + triage
    verdict the envelope was extracted from; stages remain pure w.r.t. the
    request-path session (D72) — they read this, they do not mutate the session."""

    summary: SessionSummary
    verdict: TriageVerdict


@dataclass(frozen=True)
class StageResult:
    """A stage's output: a NEW frozen envelope with this stage's field filled, plus
    a control signal. `control` defaults to `continue` (the common case)."""

    envelope: CandidateEnvelope
    control: StageControl = "continue"


class CandidateStage(Protocol):
    """One write-router stage. Implemented in the stage's own module; registered in
    the consumer's `stages` tuple at the composition root."""

    stage_id: str  # "generalize" | "leakage" | "dedup" | "writer"

    async def process(
        self, env: CandidateEnvelope, ctx: StageContext
    ) -> StageResult: ...
