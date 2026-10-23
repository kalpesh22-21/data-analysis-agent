"""Structural guards for exact, judge-approved prose replacements.

The judge assesses meaning and evidence; these checks prevent a correction from
introducing numeric tokens or bypassing the normal prose gates. Never rewrite the
replacement after approval: invalid corrections go to terminal salvage.
"""

import re
from collections import Counter

from data_agent.runtime.answer_scrub import scrub_answer_prose
from data_agent.runtime.composite.answer_with_table import clean_answer_text

from .answer_rules import first_match


def validate_correction(
    verdict,
    *,
    original,
    provenance,
    turn_sql,
    assumptions,
    question,
    has_evidence,
    declined_clarification,
    business_values=frozenset(),
    has_tables=False,
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
        or len(corrected) > 20000
        or any(ord(c) < 32 and c not in "\n\t\r" for c in corrected)
    ):
        return feedback
    # Keep every distinct date, ID, signed quantity and percentage; repetitions may shrink.
    numeric = r"[-+]?\d+(?:[.,:/-]\d+)*(?:%)?"
    if Counter(re.findall(numeric, corrected)) - Counter(re.findall(numeric, original)) or set(
        re.findall(numeric, corrected)
    ) != set(re.findall(numeric, original)):
        return feedback
    if clean_answer_text(corrected) != corrected:
        return feedback
    scrubbed, redactions = scrub_answer_prose(
        corrected, provenance=provenance, business_values=business_values
    )
    if redactions or scrubbed != corrected:
        return feedback
    rule = first_match(
        corrected + "\n" + "\n".join(assumptions),
        turn_sql,
        question,
        has_alternative_evidence=has_evidence,
        declined_clarification=declined_clarification,
        business_values=business_values,
        has_tables=has_tables,
    )
    # Same exception as the proposal path: the judge assesses evidence-free conversation.
    # Verbosity is a judge signal, never a reason to discard an approved answer.
    if rule and rule.name not in {"no_evidence", "verbose_answer"}:
        return feedback
    return ""
