"""The parameterization judge's system prompt and its brief.

Two things this prompt spends most of its words on, both because the naive version of this
judge is actively harmful:

  1. **What it may NOT ask for.** The rewrite can only punch a hole where a literal already
     exists — a slot for a predicate the accepted SQL never had would require generating SQL,
     and the template would stop being the query that ran. A judge that does not know this
     produces feedback nobody can act on.
  2. **Which direction is dangerous.** Over-inlining makes a blueprint NARROW; over-slotting
     makes it WRONG. The prompt says so explicitly, because "add more slots for flexibility" is
     the intuitive reading of the task and it is the reading that manufactures wrong answers.

It also names the two checks that are already deterministic (`frozen_date_literal`, the D97
totality walk) and tells the model not to re-report them: a second enforcer for a rule a regex
already enforces produces disagreement, not coverage.
"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = """\
You review the PARAMETERIZATION of a SQL blueprint that has been generalized from a query \
that really ran and really answered a user's question.

A blueprint is a SQL template with named holes. Every literal in the original query was given \
one of three roles:
  - slot   — it becomes a hole the caller fills in. It was THIS SESSION'S QUESTION.
  - inline — it stays frozen in the template. It is part of WHAT THE BLUEPRINT MEANS.
  - rule   — a catalog rule accounts for it.

Your job is to decide whether those roles are right.

THE ASYMMETRY THAT MATTERS MOST
A literal that should have been a slot but was frozen makes the blueprint NARROW: it still \
answers its own question correctly, it just answers fewer questions. That is a minor problem.

A literal that DEFINES THE METRIC but was made a slot makes the blueprint WRONG: a caller \
fills it with something else, gets a different measure entirely, and the blueprint's stated \
intent still claims the original one. Nothing downstream catches this.

So: be strict about slots that should be frozen. Be relaxed about frozen values that could \
have been slots. When you are unsure which way an entry goes, prefer leaving it frozen.

WHAT YOU MAY ASK FOR
Only a change of ROLE on a literal that is already in the template. You may NOT ask for a slot \
on a column the query never filtered on — there is no literal there to replace, and inventing \
one would mean rewriting the SQL, which is not permitted. If a blueprint is too narrow because \
the original query simply did not filter on something, that is not a defect you can report.

WHAT IS ALREADY CHECKED — DO NOT REPORT IT
  - Frozen absolute dates in the template. A deterministic check already rejects those.
  - Whether every literal has a role at all. A deterministic totality walk already enforces it.
  - Whether the blueprint duplicates one already in the corpus. A different judge does that.
Reporting these wastes the finding and contradicts a check that has already run.

WHAT TO LOOK FOR, in the order you should weigh it
  1. A slot whose value defines the metric the intent names. Test it by asking: if a caller \
puts a different value in this hole, is the answer still a correct answer to the intent as \
written? If not, this is the serious case.
  2. An intent that describes something the template does not compute.
  3. A slot with a name or type that would be unusable to whoever recalls this later.
  4. A frozen value that could reasonably have been a slot. Report it, but weakly.

Call the tool exactly once. Do not emit free text."""


def _entry_line(index: int, entry: dict[str, Any]) -> str:
    """One parameterization entry, rendered flat.

    Flat and NUMBERED because `entry_index` on a finding points back into this list — the model
    can only cite a position it was shown, so the position must be unmissable.
    """
    locator = entry.get("locator") if isinstance(entry.get("locator"), dict) else {}
    table = locator.get("table") or ""
    column = locator.get("column") or ""
    value = locator.get("value")
    where = ".".join(p for p in (table, column) if p) or "(no locator)"
    role = entry.get("role") or "(no role)"
    parts = [f"[{index}] {where} = {value!r} -> role={role}"]
    slot = entry.get("slot") if isinstance(entry.get("slot"), dict) else None
    if slot:
        parts.append(
            f"slot(name={slot.get('name')!r}, type={slot.get('type')!r}, "
            f"binds_to={slot.get('binds_to')!r}, "
            f"required={slot.get('required')})"
        )
    if entry.get("rule_id"):
        parts.append(f"rule_id={entry.get('rule_id')!r}")
    why = entry.get("why")
    parts.append(f"why={why!r}" if why else "why=NONE GIVEN")
    return "  " + "  ".join(parts)


def blueprint_brief(payload: dict[str, Any], *, accepted_sql: str = "") -> str:
    """The judge's view of one blueprint.

    ⚠ Takes the RAW payload, not the redacted reviewer view, and the accepted SQL with its
    literals intact. That is not a new trust boundary — the S3 extractor is handed the same SQL
    from the same session — but it IS the reason this runs server-side in the learning plane and
    never anywhere a browser can reach.

    The accepted SQL is included because ceiling 1 (see the system prompt) is only checkable
    against it: whether a proposed slot has a literal to replace is a question about the original
    query, not about the template.
    """
    generalization = (
        payload.get("generalization") if isinstance(payload.get("generalization"), dict) else {}
    )
    entries = payload.get("parameterization")
    entries = entries if isinstance(entries, list) else []
    result_signature = payload.get("result_signature")

    lines = [
        "INTENT (what this blueprint claims to answer):",
        f"  {payload.get('intent') or '(none stated)'}",
        "",
        "TEMPLATE (the generalized SQL; {name} are the slots):",
        f"  {generalization.get('sql_template') or '(none)'}",
        "",
        "PARAMETERIZATION (index -> role):",
    ]
    rendered = [_entry_line(i, e) for i, e in enumerate(entries) if isinstance(e, dict)]
    lines.extend(rendered or ["  (empty)"])

    if accepted_sql:
        lines += [
            "",
            "THE QUERY THIS WAS GENERALIZED FROM (a slot can only replace a literal that",
            "appears here):",
            f"  {accepted_sql}",
        ]
    if isinstance(result_signature, dict):
        lines += ["", "RESULT SIGNATURE:", f"  {json.dumps(result_signature, sort_keys=True)}"]
    if payload.get("notes"):
        lines += ["", "NOTES:", f"  {payload.get('notes')}"]
    return "\n".join(lines)
