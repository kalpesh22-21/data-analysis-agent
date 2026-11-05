"""Bind a measurement repair to the evidence actually selected for delivery.

A new execution elsewhere in the trail cannot clear an outstanding rejection of
an unchanged selected result. This is turn-local, and explicit reassessment remains
possible when the earlier judgement was wrong. No SQL heuristics or row rewriting.
"""

from dataclasses import replace

MEASUREMENT_VIOLATIONS = frozenset({"measurement_mismatch", "unrequested_employee_status_filter"})
REASSESSMENT_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "result_id": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["result_id", "reason"],
    },
    "description": (
        "Normally []. Only when retaining a result in measurement_rejections: explain why "
        "THAT selected result's executed SQL/receipt disproves the earlier rejection. "
        "Cite its exact result_id. A different, unselected corrected execution is not a reason. "
        "Otherwise select the corrected result and update every claim/selector, or reject."
    ),
}


def pending_measurement_rejections(state):
    if not state.violation:
        return ()
    return tuple(r for r in state.rejection_history
                 if isinstance(r, dict) and r.get("violation") in MEASUREMENT_VIOLATIONS
                 and r.get("repair_type") == "analysis" and r.get("result_ids"))


def selected_ids(document):
    """Read bindings, never arbitrary strings inside prose or source metadata."""
    if not isinstance(document, dict):
        return set()
    evidence = document.get("evidence", ())
    refs = {r for r in evidence if isinstance(r, str)} if isinstance(evidence, (list, tuple)) else set()
    sections = document.get("sections", ())
    if not isinstance(sections, (list, tuple)):
        return refs
    for section in sections:
        if not isinstance(section, dict):
            continue
        evidence = section.get("evidence_refs", ())
        if isinstance(evidence, (list, tuple)):
            refs.update(r for r in evidence if isinstance(r, str))
        for key in ("presentation", "supporting_table", "fallback_table"):
            view = section.get(key)
            ref = view.get("result_id") if isinstance(view, dict) else None
            if isinstance(ref, str):
                refs.add(ref)
    return refs


def unresolved_measurement(brief, document, reassessments=(), *, extra_refs=()):
    refs = selected_ids(document)
    refs.update(extra_refs)
    if document is None:
        refs.update(brief.referenced_result_ids)
        refs.update(brief.designated_tool_call_ids)
    reassessments = reassessments if isinstance(reassessments, (list, tuple)) else ()
    justified = {r["result_id"] for r in reassessments
                 if isinstance(r, dict) and isinstance(r.get("result_id"), str)
                 and isinstance(r.get("reason"), str) and r["reason"].strip()}
    for rejection in brief.measurement_rejections:
        stale = refs.intersection(rejection["result_ids"]) - justified
        if stale:
            return rejection, tuple(sorted(stale))
    return None


def guard_measurement_approval(verdict, brief):
    if not verdict.approved or not verdict.reviewed or not getattr(brief, "measurement_rejections", ()):
        return verdict
    document = verdict.corrected_document if verdict.corrected_document is not None else brief.answer_document
    extra_refs = () if verdict.corrected_document is not None else (
        *brief.referenced_result_ids, *brief.designated_tool_call_ids
    )
    unresolved = unresolved_measurement(brief, document, verdict.measurement_reassessments, extra_refs=extra_refs)
    if not unresolved:
        return verdict
    rejection, refs = unresolved
    return replace(
        verdict, approved=False, violation=rejection["violation"], result_ids=refs,
        feedback=("The answer still selects a result rejected for its measurement. "
                  "Use the corrected execution in the answer's evidence and presentation and update its claims. "
                  "A new query elsewhere does not repair the selected result. " + rejection.get("feedback", "")),
        repair_type="analysis", corrected_document=None, corrected_answer=None,
        corrected_question=None, partial_answer=None,
    )
