"""Write-router routing rules (Slice 7, Contract D §4 / D58a/b/D18/D52).

ONE source of truth for two questions, so the writer's decision and the inbox's label can
never disagree: `route_candidate` returns the terminal `status` + pipeline `control` +
inbox `reason`, and `derive_inbox_reason` returns the reason for an already-`in_review`
envelope.

Routing precedence, top wins:

  1a. `global_knowledge` → ALWAYS `in_review` (human pre-gate, D58a).
  1b. `schema_edit`      → ALWAYS `in_review` (D18): reason `schema_edit` when the PR stage
      ran, else `fail_to_review`. In the correct wiring the writer never sees one at all, so
      this branch is a fail-closed DEFENSE-IN-DEPTH fallback against a stage-order violation
      (R8) — never an auto-land.
  2.  blueprint with `static_validation.outcome == "fail_to_review"` → `in_review`
      (un-rewritable is reviewed, never dropped — D52/D97).
  3.  blueprint whose `dedup.action` is anything but `insert` → `in_review`. An ALLOWLIST,
      not a denylist, so an unrecognized action becomes review noise rather than a silent
      auto-land; the two DROP verdicts should never reach the writer, and if one does a human
      sees it instead of the guarantee being trusted.
  4.  blueprint with a settled `entity_scan.result != "pass"` → ALWAYS `in_review` (100% of
      leakage near-misses, D58b).
  5.  clean blueprint, sampled → `in_review` (the D58b audit sample).
  6.  clean blueprint, not sampled → `candidate` (auto-land, retrievable after S9).
  7.  anything else → pass through unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..candidate.models import CandidateEnvelope, CandidateStatus
from ..candidate.verdicts import LeakageVerdict
from ..stage import StageControl

# The ONLY dedup action that may auto-land. An ALLOWLIST, not a denylist, and the
# inversion is load-bearing.
#
# This was `_INBOX_DEDUP_ACTIONS = {"conflict", "merge"}` — force those to review, let
# everything else through. That was safe only because the two DROP actions (`increment`,
# `redundant_with_canon`) never reach the writer: S6 stops the pipeline on both, in the
# same in-process pass. But `DedupVerdict.from_doc` rehydrates `action` with NO
# validation, so a persisted envelope, a redelivery, or an envelope written by anything
# other than today's S6 could arrive here carrying a drop action — and a denylist would
# route it as CLEAN and auto-land a blueprint the loop had decided was redundant with
# the canon. Making the invariant depend on another module's control flow is exactly the
# shape this codebase keeps getting bitten by.
#
# So: `insert` (or no verdict at all — a candidate that never ran S6) is the clean path;
# EVERY other value, recognized or not, routes to a human. An unknown action becoming
# review noise is the cheap failure; an unknown action auto-landing is not.
_AUTO_LAND_DEDUP_ACTIONS = frozenset({"insert"})


def _dedup_forces_review(env: CandidateEnvelope) -> bool:
    """True iff this candidate's dedup verdict must NOT auto-land (see the allowlist).

    `dedup is None` is clean by construction — a non-blueprint, or a blueprint that reached the
    writer without S6 adjudicating it, both of which the other routing rules already cover.
    """
    return env.dedup is not None and env.dedup.action not in _AUTO_LAND_DEDUP_ACTIONS


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
    """True iff the S5 leakage gate has NOT settled a verdict (still S3's `pending` self-check).

    A blueprint that skipped the gate must NEVER auto-land — route it to human review,
    fail-closed.
    """
    return not LeakageVerdict.is_settled(env.entity_scan)


def _schema_edit_pr_ran(env: CandidateEnvelope) -> bool:
    """True iff the S8 `schema_edit_pr` stage processed this candidate (it stamps a marker).

    A `schema_edit` reaching the writer WITHOUT it is a stage-order violation ⇒ fail-closed to
    `fail_to_review` (R8).
    """
    return isinstance(env.payload.get("schema_edit_review"), dict)


def _schema_edit_reason(env: CandidateEnvelope) -> str:
    return "schema_edit" if _schema_edit_pr_ran(env) else "fail_to_review"


def derive_inbox_reason(env: CandidateEnvelope) -> str:
    """The `InboxItem.reason` for an inbox-listable envelope.

    Precedence matches `route_candidate`; a clean blueprint in `in_review` is
    `blueprint_sampled`.
    """
    # FAIL-TO-REVIEW first, ahead of the type split, because it is the only reason here
    # that describes HOW the row got into the queue rather than what kind of thing it is:
    # the consumer persists it directly (`_persist_declined_for_review`), so it never
    # passed through `route_candidate` at all and every rule below would be answering a
    # question nobody asked of it. Keyed on the decline BLOCK, not on the status string:
    # the block is what the reviewer's task is made of, and a row carrying one is a form
    # to complete whatever else is true about it.
    if env.decline is not None:
        return "needs_parameterization"
    if env.type == "global_knowledge":
        return "knowledge_pre_gate"
    if env.type == "schema_edit":
        return _schema_edit_reason(env)
    # blueprint (or any other target that got routed to review)
    if _static_outcome(env) == "fail_to_review":
        return "fail_to_review"
    if _dedup_forces_review(env):
        return "dedup_conflict"
    if _is_leakage_near_miss(env):
        return "leakage_near_miss"
    if _entity_scan_unsettled(env):
        return "fail_to_review"
    return "blueprint_sampled"


def route_candidate(env: CandidateEnvelope, *, sampled_for_inbox: bool) -> RoutingDecision:
    """Decide the terminal status + control + inbox reason for one enriched candidate.

    Pure: reads only the envelope and the sampling coin flip. A FAIL-TO-REVIEW envelope never
    reaches here — the consumer persists it directly without running the pipeline, and the
    completion path rebuilds a CLEAN envelope before re-running the stages — so the candidate
    that arrives is an ordinary one.
    """
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
        if _dedup_forces_review(env):
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
