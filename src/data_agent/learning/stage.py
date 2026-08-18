"""The `CandidateStage` pipeline seam (Wave-0 contract freeze, D102 §7.1).

The consumer runs an ordered, injected `tuple[CandidateStage, ...]` (empty ⇒ no-op) over
each `status=extracted` envelope. FROZEN ORDER: `generalize (S4) → leakage (S5) → dedup
(S6) → schema_edit_pr (S8) → user_commit (S8) → writer (S7)`. The target-specific stages
handle-then-stop their own candidate type, so a `schema_edit`/`user_knowledge` reaching the
terminal `writer` is a stage-order violation the writer fail-closes (R8).
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
    """Read-only context a stage may consult: the session summary + triage verdict.

    Stages read this; they never mutate the request-path session (D72).
    """

    summary: SessionSummary
    verdict: TriageVerdict


@dataclass(frozen=True)
class StageResult:
    """A stage's output: a NEW frozen envelope plus a control signal (`continue` by default)."""

    envelope: CandidateEnvelope
    control: StageControl = "continue"


class CandidateStage(Protocol):
    """One write-router stage, registered in the consumer's `stages` tuple at composition."""

    stage_id: str  # "generalize" | "leakage" | "dedup" | "writer"

    async def process(
        self, env: CandidateEnvelope, ctx: StageContext
    ) -> StageResult: ...


@dataclass(frozen=True)
class PipelineOutcome:
    """What running the pipeline over ONE envelope decided.

    The enriched envelope, whether the caller must persist it, and the last stage's control.
    """

    envelope: CandidateEnvelope
    persist: bool
    control: StageControl


async def run_pipeline(
    stages: tuple[CandidateStage, ...], env: CandidateEnvelope, ctx: StageContext
) -> PipelineOutcome:
    """Run *stages* in order over *env* and report what to do with the result.

    The control semantics live HERE so the two callers — the consumer, and the inbox completion
    path re-running a human-filled candidate — cannot drift on what `drop` means. An UNKNOWN
    control string RAISES rather than defaulting to a route nobody chose. Deliberately does NOT
    persist: the store belongs to the caller.
    """
    persist = True
    control: StageControl = "continue"
    for stage in stages:
        outcome = await stage.process(env, ctx)
        env = outcome.envelope
        control = outcome.control
        if control == "continue":
            continue
        if control in ("route_inbox", "halt"):
            persist = True
        elif control == "drop":
            # The stage committed the candidate elsewhere (or discarded it); the
            # enriched envelope must NOT be persisted here.
            persist = False
        else:
            raise ValueError(
                f"stage {getattr(stage, 'stage_id', stage)!r} returned an "
                f"unknown control {control!r} (expected one of continue, "
                f"route_inbox, drop, halt)"
            )
        break
    return PipelineOutcome(envelope=env, persist=persist, control=control)
