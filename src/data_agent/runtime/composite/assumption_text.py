"""Conservative, deterministic cleaning of business assumptions and caveats.

Recognized whole-statement paraphrases share a key. Unknown wording keeps its
literal key: similarity alone must never erase a period, exclusion or population.
"""

from __future__ import annotations

import re

_UI_INSTRUCTION = re.compile(
    r"^(?:only\s+)?(?:the\s+)?(?:(?:top|first|last)\s+)?\d+\b.*\b(?:shown|displayed|rendered)\b.*\b(?:chart|table|preview|screen)\b"
    r"|^(?:the\s+)?(?:chart|table|preview)\b.*\b(?:limited to|shows? (?:only |the )?(?:first|top)|displays? (?:only |the )?(?:first|top))\b"
    r"|^(?:please\s+)?(?:use|click|scroll|download|export|page through)\b.*\b(?:paging|pages?|csv|table|rows|export)\b"
    r"|\b(?:full|complete|remaining|all)\s+(?:result(?:s)?|rows|records|data)\b.*\b(?:available|view|access)\b.*\b(?:table|csv|paging|download)\b",
    re.IGNORECASE,
)


def business_caveat(text: str) -> str:
    """Remove UI-only sentences/clauses, retaining adjacent business qualifications."""
    parts = re.split(r"(?<=[.!?;])\s+|\n+", text.strip())
    kept = [part for part in parts if not _UI_INSTRUCTION.search(part)]
    if len(kept) == len(parts):
        return text.strip()
    return " ".join(kept).strip().rstrip(";")


def assumption_key(text: str) -> str:
    key = " ".join(text.split()).casefold().rstrip(".")
    # Only complete, unqualified headcount definitions are interchangeable.
    # 'Current' alone is not assumed to mean Active + On Leave: require the
    # explicit status definition. Do not merge record counts with people counts.
    statement = re.sub(r"\bon[- ]leave\b", "on leave", key)
    statuses = r"(?:active\s*(?:and|or|plus|\+)\s*on leave|on leave\s*(?:and|or|plus|\+)\s*active)"
    population = rf"(?:employees (?:with status|whose status is) {statuses}|(?:current employees\s*\()?{statuses}\)?(?: employees)?)"
    patterns = (
        rf"headcount (?:counts|is|means|includes) (?:distinct |unique )?{population}",
        rf"headcount (?:counts|is|means|includes) (?:distinct |unique )?employees (?:with )?(?:a )?status (?:of )?{statuses}",
    )
    if any(re.fullmatch(pattern, statement) for pattern in patterns):
        return "semantic:headcount:employees:active+on-leave"
    if re.fullmatch(
        rf"current(?: employees)? (?:means|includes|is|are) {statuses}(?: employees)?", statement
    ):
        return "semantic:population:current:active+on-leave"
    return key


def clean_caveats(raw, *, seen: set[str] | None = None) -> list[str]:
    """Clean one list with a shared document-wide seen set; preserve first wording."""
    seen = seen if seen is not None else set()
    result = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = business_caveat(item)
        key = assumption_key(text)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def displayed_qualification_keys(document) -> set[str]:
    """Whole statements only; a substring inside a negation is not disclosure."""
    texts = [document.get("takeaway", "")]
    for section in document.get("sections", []):
        texts.extend(section.get(field, "") for field in ("title", "explanation"))
        if section.get("id") != "scope":
            texts.append(section.get("presentation", {}).get("markdown", ""))
    keys = set()
    for text in texts:
        if not isinstance(text, str):
            continue
        for statement in [text, *re.split(r"(?<=[.!?;])\s+|\n+", text)]:
            key = assumption_key(statement)
            if key:
                keys.add(key)
    return keys


def clean_document_caveats(document):
    """Mutate a candidate/copy at both composition and final sealing boundaries."""
    seen = displayed_qualification_keys(document)
    sections = document.get("sections", [])
    for section in sections:
        section["caveats"] = clean_caveats(section.get("caveats", []), seen=seen)
    # The full headcount definition already states this exact population and
    # additionally preserves distinct counting. Prefer it over the standalone
    # definition regardless of section/order; never drop qualified definitions.
    if "semantic:headcount:employees:active+on-leave" in seen:
        for section in sections:
            section["caveats"] = [
                caveat
                for caveat in section["caveats"]
                if assumption_key(caveat) != "semantic:population:current:active+on-leave"
            ]
