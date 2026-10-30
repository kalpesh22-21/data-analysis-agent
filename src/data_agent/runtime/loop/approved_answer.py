"""One delivery contract for ordinary, corrected, and recovered answers.

The judge owns meaning. This receipt binds the rendered payload to the successful
receipts it reviewed and the current access scope. Delivery checks identity only;
it cannot turn an approval into a new semantic verdict.
"""

from .proposal import evidence_kind, fingerprint


def answer_version(text, accum, trail, turn_index, scope_hash, evidence_ids=None):
    receipts = {
        entry.tool_call_id: fingerprint(entry.to_doc())
        for entry in trail
        if entry.turn_index == turn_index
        and evidence_kind(entry)
        and (evidence_ids is None or entry.tool_call_id in evidence_ids)
    }
    return fingerprint(
        {
            "answer": text or "",
            "answer_document": accum.answer_document,
            "tables": [table.to_doc() for table in accum.answer_tables],
            "cards": accum.capability_cards,
            "assumptions": accum.assumptions,
            "extra_evidence": accum.extra_evidence,
            "scope": scope_hash,
            "receipts": receipts,
        }
    )


def approve_answer(state, text, accum, trail, turn_index):
    if accum.answer_document is not None:
        accum.answer_document = {**accum.answer_document, "takeaway": text or ""}
    state.approved_evidence = tuple(
        entry.tool_call_id
        for entry in trail
        if entry.turn_index == turn_index and evidence_kind(entry)
    )
    state.delivery_version = answer_version(text, accum, trail, turn_index, state.scope_hash)
    state.delivery_status = "approved"
    state.delivery_reason = ""
    state.violation = ""


def normalize_approved_text(text, *, provenance, business_values=frozenset(), has_tables=False):
    """Normalize presentation before binding approval; never discard supported content."""
    from data_agent.runtime.answer_scrub import scrub_answer_prose

    from .answer_rules import strip_markdown_tables

    if not isinstance(text, str):
        return text
    text = "".join(c for c in text if ord(c) >= 32 or c in "\n\r\t").strip()
    if not text:
        return text
    text = strip_markdown_tables(text)
    if not text and has_tables:
        text = "The results are shown in the table."
    return scrub_answer_prose(text, provenance=provenance, business_values=business_values)[0]


def validate_correction(
    verdict,
):
    corrected = verdict.corrected_answer
    feedback = (
        "The proposed wording correction could not be safely applied. Repair the answer "
        "using the existing evidence; preserve supported numbers and selected components."
    )
    if (
        not verdict.approved
        or not verdict.reviewed
        or verdict.repair_type != "prose"
        or verdict.violation
        or verdict.feedback
        or not isinstance(corrected, str)
        or not corrected.strip()
    ):
        return feedback
    return ""
