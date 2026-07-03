"""Deterministic triage gate (D100, §3) — keep/skip, no LLM.

A pure function of the `SessionSummary`: KEEP on any positive signal (K1–K4),
else SKIP with a reason code. Runs BEFORE any extractor cost, so the ~majority of
sessions that teach nothing are dropped for zero tokens. Every decision is a pure
function of the summary → Layer-1 unit-testable with exact fixtures.

Bias is PERMISSIVE (keep on any signal): the cost of heuristics is recall risk,
mitigated by the observable skip-reason telemetry (§3.4) — an LLM refinement can
later trim false-keeps over the KEEP set without touching this contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..summary.models import SessionSummary

_DATA_TOOLS = ("runQuery", "runBlueprint")


@dataclass(frozen=True)
class TriageVerdict:
    decision: Literal["keep", "skip"]
    reason: str  # a K1..K4 slug (keep) or a skip_* reason code
    # {"blueprint","global_knowledge","user_knowledge","schema_edit"} — HINTS only;
    # S3's classifier is authoritative and may ignore them.
    target_hints: tuple[str, ...] = ()


def _has_ok_data_call(summary: SessionSummary) -> bool:
    return any(
        tc.tool_name in _DATA_TOOLS and tc.status == "ok" for tc in summary.tool_calls
    )


def _has_any_tool_call(summary: SessionSummary) -> bool:
    return len(summary.tool_calls) > 0


def _has_answered_askuser(summary: SessionSummary) -> bool:
    return any(ex.answer for ex in summary.askuser_exchanges)


def _has_corrected_blueprint(summary: SessionSummary) -> bool:
    return any(bp.outcome == "corrected" for bp in summary.blueprint_usages)


def triage(summary: SessionSummary) -> TriageVerdict:
    """Return the keep/skip verdict for *summary* (§3.2 KEEP, §3.3 SKIP)."""
    hints: list[str] = []

    # K1 — an accepted, successful query is a liftable blueprint (D34).
    k1 = summary.accepted_signal is not None and _has_ok_data_call(summary)
    # K2 — a fixed failure teaches a lesson / a `resolves` mapping.
    k2 = len(summary.failed_fixed_sql) >= 1
    # K3 — a resolved clarification is a `resolves` / user-knowledge fact.
    k3 = _has_answered_askuser(summary)
    # K4 — a misfired blueprint is a negative signal.
    k4 = _has_corrected_blueprint(summary)

    if k1:
        hints.append("blueprint")
    if k2:
        hints.extend(("global_knowledge", "blueprint"))
    if k3:
        hints.extend(("user_knowledge", "global_knowledge"))
    if k4:
        hints.append("blueprint")

    if k1 or k2 or k3 or k4:
        # First-listed keep slug is the reason; hints de-duped preserving order.
        reason = "K1" if k1 else "K2" if k2 else "K3" if k3 else "K4"
        return TriageVerdict(decision="keep", reason=reason, target_hints=_dedup(hints))

    return TriageVerdict(decision="skip", reason=_skip_reason(summary))


def _skip_reason(summary: SessionSummary) -> str:
    # §3.3 canonical skip classes. LOW-b: `skip_all_failed` must mean exactly
    # "there WERE data queries and every one failed (none fixed)" — it must NOT
    # swallow a non-data-only success (e.g. getTableSchema-only) or an
    # unanswered-askUser session, which have no failed data query at all.
    if not _has_any_tool_call(summary):
        return "skip_no_tool_calls"  # greeting-/chat-only

    data_calls = [tc for tc in summary.tool_calls if tc.tool_name in _DATA_TOOLS]
    if any(tc.status == "ok" for tc in data_calls):
        # A successful data query but no acceptance and no K2–K4 signal.
        return "skip_no_acceptance"
    if data_calls:
        # Data queries were attempted and ALL failed (K2 already false → no fix).
        return "skip_all_failed"
    # Tool calls exist but none were data queries (schema-only, unanswered
    # askUser, …) — nothing failed, nothing lifted: the honest catch-all.
    return "skip_other"


def _dedup(items: list[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return tuple(seen)
