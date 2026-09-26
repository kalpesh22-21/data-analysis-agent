"""Structural guards for exact, judge-approved prose replacements.

The judge assesses meaning and evidence; these checks prevent a correction from
changing numeric tokens or bypassing the normal prose gates. Never rewrite the
replacement after approval: reject it for agent repair if it needs transformation.
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
    # Keep dates, IDs, signed quantities, percentages and their multiplicities intact.
    numeric = r"[-+]?\d+(?:[.,:/-]\d+)*(?:%)?"
    if Counter(re.findall(numeric, original)) != Counter(re.findall(numeric, corrected)):
        return feedback
    if clean_answer_text(corrected) != corrected:
        return feedback
    scrubbed, redactions = scrub_answer_prose(corrected, provenance=provenance)
    if redactions or scrubbed != corrected:
        return feedback
    rule = first_match(
        corrected + "\n" + "\n".join(assumptions),
        turn_sql,
        question,
        has_alternative_evidence=has_evidence,
        declined_clarification=declined_clarification,
    )
    # Same exception as the proposal path: the judge assesses evidence-free conversation.
    if rule and rule.name != "no_evidence":
        return feedback
    return ""
