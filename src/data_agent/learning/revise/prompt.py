"""The reviser's system prompt and the completion brief.

ONE brief, not two. Design §C.2.1 warned against presenting "one engine, two callers" as reuse
before the second caller exists: the UI path (a `needs_parameterization` form, no template,
incomplete entries) and the phase-D-2 judge path (a complete blueprint with a template and a
critique) are disjoint populations with different inputs. Only the first is built, so only
`completion_brief` exists here. When D-2 lands it adds `critique_brief` beside this one; if
either ever needs to branch on its caller, that is the signal to split the module rather than
defend the shared name.

The prompt's job is mostly to say what CANNOT be done, because the failure this path has
actually exhibited is a model repeatedly offering fixes the validator cannot accept — six times
across three runs on the same two predicates, per `learning-declined-candidate-review.md`. Both
of those predicates had exactly one legal classification, and nothing in the hint text the model
was shown said so.
"""

from __future__ import annotations

from typing import Any

SYSTEM_PROMPT = """\
You are helping a human reviewer finish the PARAMETERIZATION of a SQL blueprint.

A blueprint is a SQL query that really ran and really answered a user's question, generalized \
into a template with named holes. Every literal predicate in that query must be classified \
with exactly one role:

  slot   — the caller fills it in. It was THIS SESSION'S QUESTION (a department, a year).
           Needs: name, type, binds_to (the database.table.column it constrains).
  inline — it stays frozen in the template. It is part of WHAT THE BLUEPRINT MEANS.
           Needs: why — what the value MEANS, e.g. "defines the metric earnings".
  rule   — a catalog rule already declares this exact predicate. Needs: rule_id.

A validator has REJECTED this blueprint because one or more predicates have no entry, or have \
one it cannot accept. You will be shown its complaint verbatim. Your job is to propose the \
entries that satisfy it.

YOU CANNOT CHANGE THE SQL. The template is regenerated from your entries by a deterministic \
rewrite of the original query. You have no way to add, remove or edit a predicate, and no \
field in which to write SQL. If a predicate looks wrong, classify it honestly anyway.

THE THREE CASES THAT REPEATEDLY GO WRONG — read these before proposing anything:

1. A predicate on a DERIVED VALUE (an alias computed in the query itself, like a ratio's own \
denominator guard `total_earnings != '0'`). No catalog rule can ever declare it and no slot \
can bind it, because there is no base column behind it. The ONLY legal role is inline, with a \
why that says what it guards.

2. A predicate spanning TWO catalog rules (`register_type IN ('DDUCT','EARN')` where one rule \
means 'EARN' and another means 'DDUCT'). Rule correspondence compares WHOLE member sets, so no \
single rule_id can ever validate against the merged set. Do not cite either rule. Inline it \
with a why that says it spans both, or make it a slot.

3. A value that DEFINES THE METRIC the intent names. Freeze it (inline). If you make it a slot, \
a caller fills it with something else and gets a different measure entirely, while the intent \
still claims the original one. When unsure whether something is the question or the meaning, \
prefer inline.

TWO TRANSCRIPTION RULES that decide whether your entry matches the predicate at all:

  * `locator.value` is the BARE literal, WITHOUT SQL quotes. For `employee_status != 'N'` the
    value is `N`, not `'N'`. A quoted value matches no predicate and the form comes back
    declined naming the predicate you just classified.
  * OMIT the fields your role does not use. role='inline' has NO slot and NO rule_id; role='rule'
    has NO slot. Do not send them as empty objects or empty strings — an empty `slot` reads as a
    slot declaration and fails validation.

Prefer replace=false: ADD the missing entries and leave correct existing ones alone. Use \
replace=true when an existing entry is itself wrong — including when you are CHANGING the role \
of a predicate that already has an entry, because appending cannot change one.

Call the tool exactly once. Do not emit free text."""


def _entry_line(index: int, entry: dict[str, Any]) -> str:
    """One existing parameterization entry, rendered flat and numbered."""
    locator = entry.get("locator") if isinstance(entry.get("locator"), dict) else {}
    where = ".".join(
        str(p) for p in (locator.get("table"), locator.get("column")) if p
    ) or "(no locator)"
    parts = [f"[{index}] {where} = {locator.get('value')!r} -> role={entry.get('role')}"]
    slot = entry.get("slot") if isinstance(entry.get("slot"), dict) else None
    if slot:
        parts.append(
            f"slot(name={slot.get('name')!r}, binds_to={slot.get('binds_to')!r})"
        )
    if entry.get("rule_id"):
        parts.append(f"rule_id={entry.get('rule_id')!r}")
    if entry.get("why"):
        parts.append(f"why={entry.get('why')!r}")
    return "  " + "  ".join(parts)


def completion_brief(
    payload: dict[str, Any],
    *,
    accepted_sql: str,
    decline_reason: str,
    decline_detail: str,
    feedback: str,
    known_rules: tuple[str, ...] = (),
    catalog_columns: tuple[str, ...] = (),
) -> str:
    """Everything the reviser needs to propose entries for one declined blueprint.

    ⚠ Takes the RAW payload and the accepted SQL WITH ITS LITERALS. A model asked to re-role
    `department = '0420'` while being shown `department = '[redacted]'` has nothing to reason
    about. This is not a new trust boundary — the S3 extractor is handed the same SQL from the
    same session — but it IS why the engine runs server-side in the inbox service and takes
    nothing from the browser except the feedback string.

    `known_rules` and `catalog_columns` are the GROUNDING: without them a model invents a
    plausible `rule_id` or a `binds_to` for a column that does not exist, and the re-validation
    rejects the proposal for a reason the reviewer then has to decode. Both are shown as closed
    lists precisely so "I was not offered one" is a reachable conclusion.
    """
    entries = payload.get("parameterization")
    entries = entries if isinstance(entries, list) else []

    lines = [
        "INTENT (what this blueprint is meant to answer):",
        f"  {payload.get('intent') or '(none stated)'}",
        "",
        "THE ACCEPTED SQL (the query that ran; every literal in it needs a role):",
        f"  {accepted_sql or '(not available)'}",
        "",
        "WHAT THE VALIDATOR SAID:",
        f"  reason: {decline_reason or '(none)'}",
        f"  detail: {decline_detail or '(none)'}",
        "",
        "ENTRIES ALREADY CLASSIFIED (leave these alone unless one is wrong):",
    ]
    rendered = [_entry_line(i, e) for i, e in enumerate(entries) if isinstance(e, dict)]
    lines.extend(rendered or ["  (none)"])

    if known_rules:
        lines += [
            "",
            "CATALOG RULES YOU MAY CITE (a rule_id not in this list is not a rule):",
            *(f"  - {rule}" for rule in known_rules),
        ]
    else:
        lines += [
            "",
            "CATALOG RULES YOU MAY CITE: none are available for this blueprint, so role='rule'",
            "is not an option here.",
        ]
    if catalog_columns:
        lines += [
            "",
            "COLUMNS A SLOT MAY BIND TO (binds_to must be one of these, verbatim):",
            *(f"  - {column}" for column in catalog_columns),
        ]

    lines += [
        "",
        # LAST, and separately labelled. This is the one part of the brief that came from a
        # browser; keeping it visibly a quoted instruction rather than blending it into the
        # system prompt is the same posture the judge's prior-art block has.
        "THE REVIEWER'S INSTRUCTION TO YOU:",
        f"  {feedback or '(none given — propose the entries the validator is asking for)'}",
    ]
    return "\n".join(lines)
