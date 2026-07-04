"""Write-router routing rules (Slice 7, Contract D §4 / D58a/b/D18/D52).

ONE source of truth for two questions, so the writer's decision and the inbox's
label can never disagree:

  * `route_candidate(env, sampled_for_inbox)` — the terminal `status` + pipeline
    `control` + (if inbox-bound) the `InboxItem.reason`.
  * `derive_inbox_reason(env)` — the `InboxItem.reason` for an already-`in_review`
    envelope (the inbox projection calls this; it needs no sampling flag because a
    clean blueprint only reaches `in_review` via the sampled path).

**Frozen stage order (D-frozen §7.1).** The target-specific writer stages run BEFORE
this terminal writer and stop (persist/drop) their own type:
`generalize → leakage → dedup → schema_edit_pr → user_commit → writer`. The
`schema_edit_pr` stage handles every `schema_edit`, stamps a `schema_edit_review`
marker on the payload, and returns `route_inbox` (stopping the pipeline before the
writer). So in the correct wiring the writer NEVER sees a `schema_edit`; its
`schema_edit` branch is a fail-closed DEFENSE-IN-DEPTH fallback (R8): a `schema_edit`
that reaches the writer WITHOUT the PR-stage marker means the PR bot was bypassed
(stage-order violation) — it is routed to review flagged `fail_to_review`, NEVER
auto-landed.

Routing (precedence, top wins):

  1a. `global_knowledge`  → ALWAYS `in_review` (human pre-gate; never auto-retrievable
      — D58a). Reason `knowledge_pre_gate`.
  1b. `schema_edit`       → ALWAYS `in_review` (human pre-gate; D18). Reason
      `schema_edit` when the PR bot ran (`schema_edit_review` marker present),
      else `fail_to_review` (R8 — the PR stage was bypassed). Never auto-landed.
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


def _entity_scan_unsettled(env: CandidateEnvelope) -> bool:
    """True iff the S5 leakage gate has NOT settled a verdict (still S3's `pending`
    self-check). A blueprint that skipped the gate must NEVER auto-land (S4 fail-open
    fix / §5 doc amendment) — route it to human review, fail-closed."""
    return not LeakageVerdict.is_settled(env.entity_scan)


def _schema_edit_pr_ran(env: CandidateEnvelope) -> bool:
    """True iff the S8 `schema_edit_pr` stage processed this candidate (it stamps a
    `schema_edit_review` marker on the payload; R8). A `schema_edit` reaching the
    writer WITHOUT it is a stage-order violation → fail-closed to `fail_to_review`."""
    return isinstance(env.payload.get("schema_edit_review"), dict)


def _schema_edit_reason(env: CandidateEnvelope) -> str:
    return "schema_edit" if _schema_edit_pr_ran(env) else "fail_to_review"


def derive_inbox_reason(env: CandidateEnvelope) -> str:
    """The `InboxItem.reason` for an `in_review` envelope. Precedence matches
    `route_candidate`; a clean blueprint in `in_review` is `blueprint_sampled`."""
    if env.type == "global_knowledge":
        return "knowledge_pre_gate"
    if env.type == "schema_edit":
        return _schema_edit_reason(env)
    # blueprint (or any other target that got routed to review)
    if _static_outcome(env) == "fail_to_review":
        return "fail_to_review"
    if env.dedup is not None and env.dedup.action in _INBOX_DEDUP_ACTIONS:
        return "dedup_conflict"
    if _is_leakage_near_miss(env):
        return "leakage_near_miss"
    if _entity_scan_unsettled(env):
        return "fail_to_review"
    return "blueprint_sampled"


def route_candidate(env: CandidateEnvelope, *, sampled_for_inbox: bool) -> RoutingDecision:
    """Decide the terminal status + control + inbox reason for one enriched
    candidate. Pure: reads only the envelope + the sampling coin flip."""
    if env.type in ("global_knowledge", "schema_edit"):
        # Both are human pre-gated → ALWAYS in_review, NEVER auto-landed. A
        # `schema_edit` without the PR-stage marker (R8) still fail-closes to review
        # (reason `fail_to_review` via `derive_inbox_reason`); it can never auto-land.
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
        if _entity_scan_unsettled(env):
            # The leakage gate never settled a verdict (S5 skipped) → fail-closed to
            # human review; a blueprint must never auto-land on an unsettled scan (S4).
            return RoutingDecision(
                CandidateStatus.IN_REVIEW, "route_inbox", "fail_to_review"
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
