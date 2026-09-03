"""The minter's system prompts and its drafting brief.

TWO system prompts, because the two modes are genuinely different jobs and the shared 80% is
not worth collapsing them over: `CLASSIFY_SYSTEM_PROMPT` describes a query that already exists
and must be explained, `DRAFT_SYSTEM_PROMPT` describes one that must be written. Conflating them
would mean a single prompt that says "do not write SQL (unless you are writing SQL)", which is
exactly the kind of conditional instruction models drop.

Both inherit the reviser's hard-won content. The three role-classification traps in
`revise/prompt.py` were learned from six repeated failures on live candidates, and a minter that
did not repeat them would rediscover them. They are stated once here, in `_ROLES`.

⚠ THE DATE RULE IS IMPORTED, not restated. Every prompt in this system that lets a model WRITE
SQL owes it the same warning — a run date frozen into the body makes the blueprint answer a
different question every day it ages, and `check_no_frozen_date_literal` refuses it downstream.
The minting prompts had no date guidance at all until §C.5 went looking for the gap. It lives in
`learning/prompts/sql_rules.py` rather than in either package: minting importing it from revising
would read as one peer depending on the other, when what they actually share is a CHECK.
"""

from __future__ import annotations

from ..prompts.sql_rules import DATE_RULE
from .models import MintRequest

_ROLES = """\
Every literal predicate in the query must be classified with exactly one role:

  slot   — the CALLER fills it in. It is the part of the question that varies (a department, a
           year, a status being asked about). Needs: name, type, binds_to (the
           database.table.column it constrains).
  inline — it stays FROZEN in the template. It is part of WHAT THE BLUEPRINT MEANS.
           Needs: why — what the value MEANS, e.g. "defines the metric earnings".
  rule   — a catalog rule already declares this exact predicate. Needs: rule_id.

THE THREE CASES THAT REPEATEDLY GO WRONG — read these before classifying anything:

1. A predicate on a DERIVED VALUE (an alias computed in the query itself, like a ratio's own \
denominator guard `total_earnings != 0`). No catalog rule can declare it and no slot can bind \
it, because there is no base column behind it. The ONLY legal role is inline, with a why that \
says what it guards.

2. A predicate spanning TWO catalog rules (`register_type IN ('DDUCT','EARN')` where one rule \
means 'EARN' and another means 'DDUCT'). Rule correspondence compares WHOLE member sets, so no \
single rule_id can validate against the merged set. Do not cite either rule. Inline it with a \
why that says it spans both, or make it a slot.

3. A value that DEFINES THE METRIC the intent names. Freeze it (inline). If you make it a slot, \
a caller fills it with something else and gets a different measure entirely while the intent \
still claims the original one. When unsure whether something is the question or the meaning, \
prefer inline.

TWO TRANSCRIPTION RULES that decide whether your entry matches the predicate at all:

  * `locator.value` is the BARE literal, WITHOUT SQL quotes. For `employee_status != 'N'` the
    value is `N`, not `'N'`.
  * OMIT the fields your role does not use. role='inline' has NO slot and NO rule_id;
    role='rule' has NO slot. Do not send them as empty objects or empty strings — an empty
    `slot` reads as a slot declaration and fails validation.

EVERY literal predicate needs an entry. A validator walks the query and rejects the blueprint if \
even one is unaccounted for, naming it. Do not skip a predicate because it looks obvious."""

CLASSIFY_SYSTEM_PROMPT = f"""\
You are helping a data expert turn a SQL query they have ALREADY WRITTEN AND RUN into a reusable
blueprint.

A blueprint is that query generalized into a template with named holes, so it can answer the same
QUESTION later with different values.

YOU CANNOT CHANGE THE SQL, and you have no field in which to write any. The expert vouches for
this query. The template is regenerated from your entries by a deterministic rewrite of it, so
your entire job is to say what each literal MEANS. If a predicate looks wrong to you, classify it
honestly anyway and say so in your rationale — the expert reads that before promoting.

{_ROLES}

Call the tool exactly once. Do not emit free text."""

DRAFT_SYSTEM_PROMPT = f"""\
You are helping a data expert turn a question they know how to answer into a reusable blueprint.

They have given you the question, the steps they would take, the assumptions that apply, and the
tables you may read. They may also have given you a SKETCH of the SQL — pseudo-code, or a query
that is roughly right. Your job is to write the REAL query and then classify every literal in it.

WRITE THE SQL FIRST, then classify what you wrote. Constraints on the query:

  * ONE read-only SELECT, ClickHouse dialect. No DDL, no DML, no multiple statements.
  * Read ONLY the tables you were shown, and ONLY columns that exist on them. A column that is
    not on the list does not exist; if you cannot answer without one, say so in your rationale
    and write the closest query you can.
  * Write literals in PLAINLY — `department = '0420'`, not `department = {{department}}`. The
    template is generated from your entries, not from placeholders you type. A query containing
    braces will fail to parse.
  * Follow the expert's steps. Where a step cannot be expressed against these tables, say which
    one in your rationale rather than quietly dropping it.

{DATE_RULE}

{_ROLES}

Call the tool exactly once. Do not emit free text."""


COMPOSITE_SYSTEM_PROMPT = f"""\
You are helping a data expert turn a MULTI-STEP question into a reusable composite blueprint.

They have declared the STEPS themselves — how many there are, what each one answers, and which
earlier steps each one needs. THAT STRUCTURE IS FIXED. You cannot add a step, remove one,
reorder them, or change which step feeds which. Write the SQL for each step exactly as declared,
and if a step cannot be expressed against these tables, say which one in your rationale rather
than folding it into another.

PASSING RESULTS BETWEEN STEPS. There are TWO kinds, and each step tells you which it produces:

  ONE VALUE (a total, an average). The step that needs it writes that result's NAME IN BRACES
  where the value belongs — `{{dept_total}}`. Not `$0.dept_total`, which is the DAG's own wiring
  notation and is not valid SQL, and not the value itself.

  A TABLE (a whole result set — per-employee rows, say). It is materialized under the `scratch`
  database, so the step that needs it reads `scratch.<name>` AS AN ORDINARY TABLE in FROM or
  JOIN — `FROM scratch.emp_earnings AS x`. There is no brace token for a table.

Each step below is shown exactly what to write for every result it needs.

You MUST actually reference what a step declares it needs — the brace token, or the
`scratch.<name>` table. A step that says it needs an earlier result and then never uses it is
silently wrong: the DAG will still pass that value in, and the query will quietly ignore it and
answer something else.

Never re-derive an earlier step's value by repeating its query inside a later one; that is what
the DAG exists to avoid.

Each step is ONE read-only SELECT, ClickHouse dialect, reading only the tables you were shown.

{DATE_RULE}

{_ROLES}

The entries are ONE FLAT LIST covering the literals of ALL steps together, not per step.

Call the tool exactly once. Do not emit free text."""


def _bullets(label: str, items: tuple[str, ...], *, empty: str) -> list[str]:
    if not items:
        return [label, f"  {empty}"]
    return [label, *(f"  {i}. {item}" for i, item in enumerate(items, 1))]


def mint_brief(
    request: MintRequest,
    *,
    known_rules: tuple[str, ...] = (),
    catalog_columns: tuple[str, ...] = (),
    catalog_schema: dict[str, dict[str, str]] | None = None,
) -> str:
    """Everything the model needs to draft or classify one blueprint.

    `catalog_columns` is the GROUNDING and is why `tables` is a required field on the request:
    without a closed column list a model invents a plausible column, the rewrite's
    `binds_to_subset_uses` check rejects it, and the expert is handed a validator complaint about
    a column they never chose. Shown as a closed list precisely so "there is no column for this"
    is a reachable conclusion the model can state in its rationale.

    THE EXPERT'S TEXT IS LABELLED AS THEIRS AND COMES LAST. It is the one part of this brief that
    was typed by a human into a browser, and keeping it visibly a quoted submission rather than
    blending it into the system prompt is the same posture the reviser's feedback block has.
    """
    lines = [
        "THE TABLES YOU MAY READ:",
        *(f"  - {table}" for table in request.tables),
    ]
    selected_schema = {table: (catalog_schema or {}).get(table, {}) for table in request.tables}
    if any(selected_schema.values()):
        lines += ["", "THE SCHEMA FOR THOSE TABLES (this is a closed allowlist):"]
        for table, columns in selected_schema.items():
            lines.append(f"  {table}:")
            lines.extend(f"    - {name} ({kind})" for name, kind in columns.items())
    elif catalog_columns:
        lines += [
            "",
            "THE COLUMNS THOSE TABLES HAVE (a column not listed here does not exist, and a "
            "slot's binds_to must be one of these verbatim):",
            *(f"  - {column}" for column in catalog_columns),
        ]
    else:
        lines += [
            "",
            "⚠ No column list is available for those tables, so you are working unverified. "
            "Use only columns the expert names or that appear in the SQL they submitted.",
        ]

    if known_rules:
        lines += [
            "",
            "CATALOG RULES YOU MAY CITE (a rule_id not in this list is not a rule):",
            *(f"  - {rule}" for rule in known_rules),
        ]
    else:
        lines += [
            "",
            "CATALOG RULES YOU MAY CITE: none are available here, so role='rule' is not an "
            "option for this blueprint.",
        ]

    lines += [
        "",
        "=" * 72,
        "THE EXPERT'S SUBMISSION",
        "=" * 72,
        "",
        "THE QUESTION:",
        f"  {request.question}",
        "",
    ]
    lines += _bullets(
        "THE STEPS THEY WOULD TAKE TO ANSWER IT:",
        request.steps,
        empty="(none given — infer them from the question)",
    )
    lines += [""]
    lines += _bullets(
        "THE ASSUMPTIONS THAT APPLY (fold any that CHANGE WHAT THE NUMBER MEANS into the "
        "intent, and enforce the rest in the query):",
        request.assumptions,
        empty="(none given)",
    )

    if request.is_composite:
        lines += ["", "=" * 72, "THE STEPS THEY DECLARED — write SQL for each, in this order:"]
        for order, node in enumerate(request.nodes):
            lines += ["", f"STEP {order} — {node.step_intent}"]
            kind = "a table" if node.output_kind == "table" else "one value"
            lines.append(f"  it produces {kind}, named: {node.output_for(order)}")
            if node.feeds_from:
                # SPELLED PER EDGE, because the two kinds are written differently and a model
                # told only "it needs step 1" has to guess which. The guard downstream checks
                # for exactly the form named here.
                refs = []
                for edge in node.feeds_from:
                    upstream = request.nodes[edge]
                    name = upstream.output_for(edge)
                    refs.append(
                        f"the table `scratch.{name}` (read it in FROM or JOIN)"
                        if upstream.output_kind == "table"
                        else "the token {" + name + "}"
                    )
                lines.append("  its SQL MUST reference:")
                lines.extend(f"    - {ref}" for ref in refs)
            else:
                lines.append("  it needs nothing from earlier steps")
            if node.sql.strip():
                label = (
                    "  the SQL they submitted for it (it ran; classify, do not rewrite):"
                    if request.sql_is_authoritative
                    else "  their sketch of it (correct and complete it):"
                )
                lines.append(label)
                lines += [f"    {line}" for line in node.sql.splitlines()]

    if request.sql.strip():
        header = (
            "THE SQL THEY SUBMITTED — this query RAN and they vouch for it. It is the accepted "
            "SQL. Classify its literals; do not rewrite it:"
            if request.sql_is_authoritative
            else "A SKETCH OF THE SQL (pseudo-code or roughly-right SQL — correct it, complete "
            "it, and make it run):"
        )
        lines += ["", header, *(f"  {line}" for line in request.sql.splitlines())]
    return "\n".join(lines)


__all__ = ["CLASSIFY_SYSTEM_PROMPT", "COMPOSITE_SYSTEM_PROMPT", "DRAFT_SYSTEM_PROMPT", "mint_brief"]
