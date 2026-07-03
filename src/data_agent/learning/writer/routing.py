"""Write-router routing rules (Slice 7, Contract D §4 / D58a/b/D18/D52).

ONE source of truth for two questions, so the writer's decision and the inbox's
label can never disagree:

  * `route_candidate(env, sampled_for_inbox)` — the terminal `status` + pipeline
    `control` + (if inbox-bound) the `InboxItem.reason`.
  * `derive_inbox_reason(env)` — the `InboxItem.reason` for an already-`in_review`
    envelope (the inbox projection calls this; it needs no sampling flag because a
    clean blueprint only reaches `in_review` via the sampled path).

Routing (precedence, top wins):

  1. `global_knowledge` / `schema_edit`  → ALWAYS `in_review` (human pre-gate; never
     auto-retrievable — D58a/D18). Reasons `knowledge_pre_gate` / `schema_edit`.
  2. blueprint, `static_validation.outcome == "fail_to_review"` → `in_review`,
     reason `fail_to_review` (un-rewritable, reviewed not dropped — D52/D97).
  3. blueprint, `dedup.action ∈ {conflict, merge}` (a SOFT-layer near-miss) →
     `in_review`, reason `dedup_conflict` ("soft conflict/variant" — never
     auto-append, §3). (A hard-key `increment` was already dropped upstream.)
  4. blueprint, settled `entity_scan.result != "pass"` (a leakage near-miss) →
     ALWAYS `in_review`, reason `leakage_near_miss` (100% of near-misses, D58b).
  5. clean blueprint, sampled (`blueprint_inbox_sample_rate`) → `in_review`, reason
     `blueprint_sampled` (D58b audit sample).
  6. clean blueprint, not sampled → `candidate` (auto-land, retrievable after S9).
  7. anything else (`user_knowledge`, unknown) → pass through unchanged — S8's
     auto-commit stage handles `user_knowledge` and drops it before the writer.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..candidate.models import CandidateEnvelope, CandidateStatus
from ..candidate.verdicts import LeakageVerdict
from ..stage import StageControl

# The soft-layer dedup actions that force a candidate into the inbox (never
# auto-appended, §3). `increment` never reaches the writer (dropped at S6);
# `insert` is the clean auto-land path.
_INBOX_DEDUP_ACTIONS = frozenset({"conflict", "merge"})


@dataclass(frozen=True)
class RoutingDecision:
    """The writer's verdict for one candidate."""

    status: str  # the terminal status the writer stamps (candidate | in_review | unchanged)
    control: StageControl  # "route_inbox" (persist to inbox) | "continue" (auto-land) | ...
    reason: str | None  # the InboxItem.reason, or None when auto-landing


def _static_outcome(env: CandidateEnvelope) -> str | None:
    gen = env.payload.get("generalization")
    if not isinstance(gen, dict):
        return None
    sv = gen.get("static_validation")
    if not isinstance(sv, dict):
        return None
    return sv.get("outcome")


def _is_leakage_near_miss(env: CandidateEnvelope) -> bool:
    scan = env.entity_scan
    return LeakageVerdict.is_settled(scan) and scan.get("result") != "pass"


def derive_inbox_reason(env: CandidateEnvelope) -> str:
    """The `InboxItem.reason` for an `in_review` envelope. Precedence matches
    `route_candidate`; a clean blueprint in `in_review` is `blueprint_sampled`."""
    if env.type == "global_knowledge":
        return "knowledge_pre_gate"
    if env.type == "schema_edit":
        return "schema_edit"
    # blueprint (or any other target that got routed to review)
    if _static_outcome(env) == "fail_to_review":
        return "fail_to_review"
    if env.dedup is not None and env.dedup.action in _INBOX_DEDUP_ACTIONS:
        return "dedup_conflict"
    if _is_leakage_near_miss(env):
        return "leakage_near_miss"
    return "blueprint_sampled"


def route_candidate(env: CandidateEnvelope, *, sampled_for_inbox: bool) -> RoutingDecision:
    """Decide the terminal status + control + inbox reason for one enriched
    candidate. Pure: reads only the envelope + the sampling coin flip."""
    if env.type in ("global_knowledge", "schema_edit"):
        return RoutingDecision(
            CandidateStatus.IN_REVIEW, "route_inbox", derive_inbox_reason(env)
        )

    if env.type == "blueprint":
        if _static_outcome(env) == "fail_to_review":
            return RoutingDecision(CandidateStatus.IN_REVIEW, "route_inbox", "fail_to_review")
        if env.dedup is not None and env.dedup.action in _INBOX_DEDUP_ACTIONS:
            return RoutingDecision(CandidateStatus.IN_REVIEW, "route_inbox", "dedup_conflict")
        if _is_leakage_near_miss(env):
            return RoutingDecision(
                CandidateStatus.IN_REVIEW, "route_inbox", "leakage_near_miss"
            )
        if sampled_for_inbox:
            return RoutingDecision(
                CandidateStatus.IN_REVIEW, "route_inbox", "blueprint_sampled"
            )
        # Clean, unsampled blueprint → auto-land as a retrievable-after-S9 candidate.
        return RoutingDecision(CandidateStatus.CANDIDATE, "continue", None)

    # user_knowledge / unknown: S8 owns user_knowledge (auto-commit + drop) upstream;
    # the writer leaves anything it does not own untouched.
    return RoutingDecision(env.status, "continue", None)
