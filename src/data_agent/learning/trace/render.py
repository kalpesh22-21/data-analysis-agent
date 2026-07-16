"""Pure formatting of a `SessionTrace` — NO I/O, NO store access.

Two renderers:
  * `render_session_trace`      — a human-readable, sectioned, chronological report
    (the whole point: ONE view of one session's journey). ANSI color is applied
    only when *use_color* is set (the caller gates on `stdout.isatty()`); a plain
    fallback is always available.
  * `render_session_trace_json` — a stable dict → `json.dumps(indent=2)` for `--json`
    piping, serialized through the models' own `to_doc()`.

Long strings (SQL, quotes, rationale) are truncated to ~120 chars with an ellipsis.
"""

from __future__ import annotations

import json

from data_agent.learning.candidate.verdicts import LeakageVerdict

from .reconstruct import CandidateTrace, SessionTrace

_TRUNC = 120
_RULE = "─" * 60
_RESET = "\033[0m"
_CODES = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
}

# Candidate lifecycle → color bucket (verdicts.py CandidateStatus values).
_STATUS_COLOR = {
    "extracted": "yellow",
    "candidate": "cyan",
    "in_review": "yellow",
    "validated": "green",
    "quarantined": "red",
    "rejected": "red",
    "retired": "magenta",
}
# Session learning_status → color bucket (learning/models.py LearningStatus values).
_LEARNING_STATUS_COLOR = {
    "active": "dim",
    "pending": "yellow",
    "queued": "yellow",
    "processing": "cyan",
    "done": "green",
    "dead_letter": "red",
}


def _c(text: str, *styles: str, use_color: bool) -> str:
    """Wrap *text* in ANSI *styles* when *use_color*, else return it unchanged."""
    if not use_color or not styles:
        return text
    prefix = "".join(f"\033[{_CODES[s]}m" for s in styles)
    return f"{prefix}{text}{_RESET}"


def _truncate(value: object, limit: int = _TRUNC) -> str:
    """Collapse whitespace and clip *value* to *limit* chars with an ellipsis."""
    if value is None:
        return ""
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def render_session_trace(trace: SessionTrace, *, use_color: bool = True) -> str:
    """Render *trace* as a human-readable, sectioned report string."""
    lines: list[str] = []
    lines.append(
        _c("LEARNING TRACE", "bold", use_color=use_color)
        + "  "
        + _c(f"session={trace.session_id}", "bold", "cyan", use_color=use_color)
    )
    lines.append(_RULE)

    _render_session_section(lines, trace, use_color)
    lines.append("")
    _render_candidates_section(lines, trace, use_color)
    lines.append("")
    _render_lifecycle_section(lines, trace, use_color)

    if trace.errors:
        lines.append("")
        lines.append(_c("NOTES", "bold", use_color=use_color))
        for note in trace.errors:
            lines.append("  " + _c("• " + _truncate(note, 200), "dim", use_color=use_color))

    return "\n".join(lines)


def _render_session_section(lines: list[str], trace: SessionTrace, use_color: bool) -> None:
    lines.append(_c("SESSION", "bold", use_color=use_color))
    session = trace.session
    if session is None:
        lines.append(
            "  " + _c("(session not found — nothing to reconstruct)", "red", use_color=use_color)
        )
        return

    status_color = _LEARNING_STATUS_COLOR.get(session.learning_status, "yellow")
    hash_display = (
        session.learning_content_hash
        if session.learning_content_hash
        else "(none — never entered learning loop)"
    )
    lines.append(f"  created_at     : {session.created_at}")
    lines.append(f"  last_activity  : {session.last_activity}")
    lines.append(
        "  learning_status: " + _c(session.learning_status, status_color, use_color=use_color)
    )
    lines.append(f"  content_hash   : {hash_display}")

    lines.append("")
    lines.append("  " + _c("Learning inputs (from tool_trail):", "bold", use_color=use_color))
    if not session.tool_trail:
        lines.append("    " + _c("(no tool calls recorded)", "dim", use_color=use_color))
        return
    for entry in session.tool_trail:
        summary = _trail_entry_summary(entry)
        head = _c(
            f"turn {entry.turn_index} [{entry.tool_call_id}] {entry.tool_name}",
            "cyan",
            use_color=use_color,
        )
        status_tag = (
            "" if entry.status == "ok" else _c(f" ({entry.status})", "red", use_color=use_color)
        )
        lines.append(f"    {head}{status_tag}: {summary}")


def _trail_entry_summary(entry) -> str:  # noqa: ANN001 — TrailEntry (avoid the import cycle)
    """The SQL for a `runQuery`, else a compact `key=value` args summary."""
    args = entry.args or {}
    sql = args.get("sql")
    if sql:
        return _truncate(sql)
    if not args:
        return "(no args)"
    return _truncate(", ".join(f"{k}={v}" for k, v in args.items()))


def _render_candidates_section(lines: list[str], trace: SessionTrace, use_color: bool) -> None:
    lines.append(_c(f"CANDIDATES  ({len(trace.candidates)})", "bold", use_color=use_color))
    if trace.session is None:
        return
    if not trace.candidates:
        lines.append("  " + _c("(none)", "dim", use_color=use_color))
        return
    for candidate in trace.candidates:
        # M1: a single corrupt candidate must degrade to ONE line, never abort the
        # whole report — the tool is most needed precisely when a doc is malformed.
        try:
            lines.extend(_render_candidate(candidate, use_color))
        except Exception as exc:  # noqa: BLE001
            cid = getattr(candidate.envelope, "candidate_id", "?")
            lines.append(
                "  " + _c(f"(candidate {cid}: render failed: {exc})", "red", use_color=use_color)
            )


def _render_candidate(candidate: CandidateTrace, use_color: bool) -> list[str]:
    env = candidate.envelope
    out: list[str] = []
    status_color = _STATUS_COLOR.get(env.status, "yellow")
    top = (
        _c("┌─ ", "dim", use_color=use_color)
        + _c(env.candidate_id, "bold", use_color=use_color)
        + f"   type={env.type}   status="
        + _c(env.status, status_color, use_color=use_color)
    )
    out.append("  " + top)

    bar = _c("│", "dim", use_color=use_color)

    def row(text: str) -> None:
        out.append(f"  {bar}   {text}")

    row(f"created_at : {env.created_at}")
    row(f"confidence : {env.confidence}   proposed_action={env.proposed_action}")

    if env.type == "blueprint":
        payload = env.payload or {}
        row(f"intent     : {_truncate(payload.get('intent', ''))}")
        resolves = payload.get("resolves") or {}
        if resolves:
            row(f"resolves   : {_truncate(resolves)}")
        generalization = payload.get("generalization")
        if generalization:
            _render_generalization(row, generalization)

    if env.depends_on:
        row(f"depends_on : {list(env.depends_on)}")

    row(f"leakage(entity_scan): {_leakage_display(env.entity_scan)}")
    row(f"dedup      : {_dedup_display(env.dedup)}")
    row(f"drift      : {_drift_display(env.drift)}")

    if env.extractor_rationale:
        row(f"rationale  : {_truncate(env.extractor_rationale)}")

    row(f"evidence   : {len(candidate.evidence)} snapshot(s)")
    for snapshot in candidate.evidence:
        quote = _c(f'"{_truncate(snapshot.quote)}"', "dim", use_color=use_color)
        row(f"    - turn {snapshot.turn_ref} tool_call {snapshot.tool_call_ref}: {quote}")
    if not candidate.evidence and env.evidence_refs:
        row(
            "    - " + _c(f"{len(env.evidence_refs)} unresolved ref(s)", "dim", use_color=use_color)
        )

    out.append("  " + _c("└─", "dim", use_color=use_color))
    return out


def _render_generalization(row, generalization: object) -> None:
    # M1: a later stage may write `generalization`; it MUST be a dict before we
    # index it, and `uses` may be any iterable of anything — coerce defensively.
    if not isinstance(generalization, dict):
        return
    template = generalization.get("template")
    if template:
        row(f"template   : {_truncate(template)}")
    static_validation = generalization.get("static_validation")
    if isinstance(static_validation, dict) and static_validation.get("outcome") is not None:
        row(f"static_val : {static_validation.get('outcome')}")
    uses = generalization.get("uses")
    if isinstance(uses, (list, tuple, set, frozenset)) and uses:
        row(f"uses       : {sorted(map(str, uses))}")


def _leakage_display(entity_scan: dict) -> str:
    """`pending` for the S3 self-check sentinel, else the settled S5 verdict."""
    scan = entity_scan or {}
    if not LeakageVerdict.is_settled(scan):
        return "pending"
    # M1: a doc can pass `is_settled` (result:"pass") yet carry bare-string `hits`
    # that make `EntityHit.from_doc` raise — this parse runs in render, outside
    # reconstruct's try/except, so guard it here rather than kill the report.
    try:
        verdict = LeakageVerdict.from_doc(scan)
    except Exception:  # noqa: BLE001
        return "malformed"
    hit_count = len(verdict.hits)
    return f"{verdict.result} (hits={hit_count}, scanner={verdict.scanner or '—'})"


def _dedup_display(dedup) -> str:
    if dedup is None:
        return "unchecked"
    return f"{dedup.action}/{dedup.layer} matched={dedup.matched_id} sim={dedup.similarity}"


def _drift_display(drift) -> str:
    last = drift.last_drift_check_at or "—"
    failed = drift.failed_probe or "—"
    return f"{drift.status} last_check={last} failed_probe={failed}"


def _render_lifecycle_section(lines: list[str], trace: SessionTrace, use_color: bool) -> None:
    lines.append(_c("LIFECYCLE", "bold", use_color=use_color))
    if trace.session is None:
        lines.append("  " + _c("(session not found)", "dim", use_color=use_color))
        return
    status = trace.session.learning_status
    status_color = _LEARNING_STATUS_COLOR.get(status, "yellow")
    lines.append("  session:  active → … → " + _c(status, status_color, use_color=use_color))
    for candidate in trace.candidates:
        env = candidate.envelope
        cand_color = _STATUS_COLOR.get(env.status, "yellow")
        last_check = env.drift.last_drift_check_at or "—"
        # L2: a fresh candidate still at `extracted` renders just the status, not a
        # degenerate `extracted → extracted`.
        status_cell = _c(env.status, cand_color, use_color=use_color)
        transition = status_cell if env.status == "extracted" else f"extracted → {status_cell}"
        lines.append(
            f"  {env.candidate_id}:  {transition}"
            + f"   (created {env.created_at}, last drift check {last_check})"
        )


def render_session_trace_json(trace: SessionTrace) -> str:
    """Serialize *trace* to a stable, indented JSON string via the models' `to_doc()`."""
    doc = {
        "session_id": trace.session_id,
        "session": trace.session.to_doc() if trace.session is not None else None,
        "candidates": [
            {
                "envelope": candidate.envelope.to_doc(),
                "evidence": [snapshot.to_doc() for snapshot in candidate.evidence],
            }
            for candidate in trace.candidates
        ],
        "errors": list(trace.errors),
    }
    return json.dumps(doc, indent=2)
