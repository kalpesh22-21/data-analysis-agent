"""Shared user-facing clarification checks; never rewrite underlying slot bindings."""

from __future__ import annotations

import re
from typing import Any

from data_agent.runtime.answer_scrub import scrub_answer_prose

_CODE_ONLY = re.compile(r"^(?:[A-Z]{1,6}[-_]?\d+|\d{2,})$")


def normalize_clarification(question: Any, options: Any) -> dict[str, Any]:
    raw = str(question or "").strip()
    clean, redactions = scrub_answer_prose(raw, provenance=None)
    choices = (
        list(dict.fromkeys(v.strip() for v in options if isinstance(v, str) and v.strip()))
        if isinstance(options, list)
        else []
    )
    if any(scrub_answer_prose(choice, provenance=None)[1] for choice in choices):
        return {
            "question": "Please provide the full name or description of the person, group, or period you mean.",
            "options": None,
        }
    if len(choices) > 5:
        # Do not present an arbitrary first five: accept free text to narrow the domain.
        clean = "There are several possible matches. Please provide the full name or a more specific description to narrow the selection."
        choices = []
    elif any(_CODE_ONLY.fullmatch(v) for v in choices):
        clean = "Please provide the full name or description of the item you mean so I can identify the correct match."
        choices = []
    elif redactions or not clean:
        clean = (
            "Please clarify which person, group, or period you mean, using its name or description."
        )
    return {"question": clean, "options": choices or None}
