"""The knowledge reviser's system prompt and its brief (design §C.1).

A SIBLING of `prompt.py`, not a branch of it. The two briefs share no input: one is a declined
blueprint with an accepted SQL, a validator complaint and a catalog to ground against, and this
one is a five-field fact with an entity scanner's findings. `prompt.py`'s own docstring records
the rule this follows — "one engine, two callers" is not reuse until the second caller exists,
and here the second caller is a different engine.

WHAT THE PROMPT IS MOSTLY ABOUT, and it is not the same thing `prompt.py` is mostly about. The
blueprint prompt spends its length on what CANNOT be done, because that model kept proposing
fixes the validator could not accept. This one spends its length on WHAT AN ENTITY IS, because
that is the whole job: the reviewer clicks the assistant precisely when the scanner flagged a
name, a code or a date, and the model has to remove it while keeping the fact TRUE. The failure
mode to steer away from is not "an illegal edit" — the tool has no illegal field — it is a
model that deletes the entity and leaves a sentence that no longer says anything, or that
paraphrases the entity ("a certain senior manager") and thinks it has removed it.

⚠ THE BRIEF SHOWS THE SPANS. The scanner's hits are rendered as `field (kind): span`, entity
value included, because a model asked to remove `E10842` while being shown `[redacted]` has
nothing to work with. This is not a new trust boundary — the S3 extractor was handed the same
session — and it is why the reviser runs SERVER-SIDE in the inbox service and takes nothing
from the browser but the feedback string. The proposal that comes back is scanned again before
it is allowed near a response body (`knowledge.py`), which is the boundary that actually holds.
"""

from __future__ import annotations

import json
from typing import Any

_HEAD = """\
You are helping a human reviewer correct a piece of GLOBAL KNOWLEDGE before it is stored.

Global knowledge is a single fact that is true for EVERYONE in the organisation. It is stored \
in a shared index with no per-user scoping and recalled into other people's conversations, so \
it must be true independently of the session that produced it, and it must name nobody.

A fact has exactly five fields and no others:

  statement       — the fact, as ONE self-contained sentence. This IS the stored knowledge.
  knowledge_type  — a short label for the kind of fact ("business_rule", "definition").
  structured      — flat key/value STRINGS of supporting detail. No nesting.
  related_terms   — other words this fact should be recalled by.
  scope           — what the fact is about, in a few words. It titles the stored chunk."""

# ⚠ THE RULE THE WHOLE PATH EXISTS FOR, and the one a model reliably half-applies: it removes
# the literal string and leaves a description that identifies the same person, or it removes the
# entity and leaves a sentence that no longer states anything. Both are spelled out with an
# example, because "be entity-free" alone produced exactly those two outcomes.
ENTITY_RULE = """\
THE ONE RULE: NAME NOBODY AND NOTHING SPECIFIC.

An entity is anything that identifies one person, one organisational unit, one customer or one \
moment: a name, an employee code, a department or cost-centre code, an email address, a \
customer id, a specific date. An entity scanner reads every one of the five fields, and a fact \
that still carries one cannot be stored — the reviewer will only be able to reject it.

TWO WAYS THIS GOES WRONG, and both look like success:

1. PARAPHRASING IS NOT REMOVING. "the employee in department 0420" and "a senior manager in \
the finance department" identify the same person to anyone who works there. Remove the \
identification; do not rewrite it in words.

2. AN EMPTY FACT IS WORSE THAN NO FACT. If everything that made the sentence worth storing was \
the specific case, say so in your rationale and leave `statement` as the closest TRUE general \
version — not a sentence with a hole in it. "Overtime is paid at 1.5x the base rate for hours \
past 40 in a week" is a fact. "Overtime is paid at a multiplier" is not.

GENERALISE, DO NOT DELETE. The usual right move is to lift the specific case into the rule it \
is an instance of: `employee E10842 accrues 1.5 days of leave per month` becomes `leave accrues \
at 1.5 days per month for full-time staff`. A number, a rate, a rule name and a column name are \
NOT entities and should stay."""

_TAIL = """\
CHANGE AS LITTLE AS YOU CAN. The reviewer asked for a specific correction; a rewrite of fields \
they did not mention makes them re-adjudicate the whole fact. If a field is already fine, \
return it unchanged.

RETURN THE COMPLETE FACT. Your proposal REPLACES all five fields — anything you leave out is \
stored empty, not carried over from what is there now.

Call the tool exactly once. Do not emit free text."""


SYSTEM_PROMPT = f"{_HEAD}\n\n{ENTITY_RULE}\n\n{_TAIL}"


def system_prompt() -> str:
    """The knowledge reviser's system prompt.

    A FUNCTION rather than a bare constant, mirroring `prompt.py::system_prompt`, so a future
    mode switch (an `allow_*` opt-in, a second caller) lands in one place instead of at every
    read site — which is the split that let the blueprint prompt offer a field while telling the
    model it had none.
    """
    return SYSTEM_PROMPT


def _field_line(name: str, value: Any) -> str:
    """One current field, rendered flat.

    `structured` and `related_terms` go through `json.dumps` rather than `repr`: the model reads
    this as data and a Python repr (single quotes, `None`) is a dialect it has to translate,
    which is how a `None` came back as the four-character string "None" in an adjacent path.
    """
    if value is None or value == "" or value == [] or value == {}:
        return f"  {name}: (not set)"
    if isinstance(value, (list, dict)):
        return f"  {name}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}"
    return f"  {name}: {value}"


def knowledge_brief(
    payload: dict[str, Any],
    *,
    surfaces: tuple[str, ...],
    hits: tuple[tuple[str, str, str], ...] = (),
    scan_result: str = "",
    feedback: str = "",
) -> str:
    """Everything the knowledge reviser needs to correct one fact.

    *hits* is `(field, kind, span)` from the SETTLED scan — see the module docstring for why the
    span is shown. *surfaces* is passed in rather than imported so the brief and the tool cannot
    list different fields: they come from one tuple at the caller.

    An EMPTY *hits* with a non-`pass` *scan_result* is a real and awkward state (a scanner that
    asserted a leak it could not localise), and it is reported as such rather than silently
    rendering an empty section — a model told "there are findings" and shown none will invent
    one to remove.
    """
    lines = [
        "THE FACT AS IT STANDS (these five fields are all there is):",
        *(_field_line(name, payload.get(name)) for name in surfaces),
        "",
    ]
    if hits:
        lines += [
            "WHAT THE ENTITY SCANNER FOUND (field, what it thinks it is, and the exact text):",
            *(f"  {field} ({kind}): {span}" for field, kind, span in hits),
            "",
            "Every one of those has to be gone from your proposal, including from any field "
            "you did not otherwise change.",
            "",
        ]
    elif scan_result and scan_result != "pass":
        lines += [
            f"THE ENTITY SCAN SETTLED '{scan_result}' BUT LOCALIZED NOTHING. There is no span "
            "to remove, so do not invent one — re-read the five fields yourself and say in "
            "your rationale whether you think anything in them identifies a person or a unit.",
            "",
        ]
    else:
        lines += [
            "THE ENTITY SCAN FOUND NOTHING. The reviewer's instruction is the whole task; "
            "keep the fact entity-free as you change it.",
            "",
        ]
    lines += [
        # LAST, and separately labelled — the same placement, and the same reasoning, the
        # blueprint brief gives: this is the one part that came from a browser, and it stays
        # visibly a quoted instruction rather than being blended into the system prompt.
        "THE REVIEWER'S INSTRUCTION TO YOU:",
        f"  {feedback or '(none given — remove any entity and leave the fact true)'}",
    ]
    return "\n".join(lines)


__all__ = ["ENTITY_RULE", "SYSTEM_PROMPT", "knowledge_brief", "system_prompt"]
