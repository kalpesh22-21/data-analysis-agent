"""The minter's two forced tools, and the guard on what comes back.

⚠ **THERE ARE TWO TOOLS BECAUSE THERE ARE TWO PROVENANCE STORIES, AND ONLY ONE OF THEM MAY
WRITE SQL.**

`classify_blueprint` has NO `sql` field. When an expert submits SQL they say RAN (`sql_mode`
"exact"), that query is the accepted SQL, and the template is derived from it by the deterministic
AST rewrite. A model that could edit it would silently change what `explain_ok`,
`binds_to_subset_uses` and `read_only_select` are checking — they would stop describing a query
the warehouse answered and start describing model prose. This is `revise/schema.py`'s argument
verbatim, and it holds here for the identical reason.

`draft_blueprint` DOES write SQL, because in "pseudo"/"none" mode there is no query yet and
somebody has to. That is a genuinely weaker position, and the system does not pretend otherwise:
the draft is not trusted, it is REVIEWED. It lands as a candidate whose accepted SQL is what the
model wrote, the expert sees it on the card, can trial-run it against a real tenant, and promotion
still replays it. The honest summary is that "exact" mode mints from evidence and "pseudo" mode
mints from a proposal — which is why `sql_mode` is recorded rather than collapsed.

Not offering a single tool with an OPTIONAL `sql`: an ignored field is worse than an absent one.
A model that fills it believes it changed something it did not, and its rationale then explains an
edit that never happened.

Every other guard is derived from what downstream READS — the same discipline as the reviser:

  `entries`  walked by `to_candidate`'s D97 totality check ⇒ the SHARED `ENTRIES_SCHEMA`,
             imported rather than restated so the two callers cannot drift apart.
  `sql`      parsed by `sqlglot` and rewritten ⇒ `str`, capped, and only on the drafting tool.
  `intent`   embedded by dedup, matched by retrieval, shown on the card ⇒ `str`, one line.
"""

from __future__ import annotations

import logging
from typing import Any

from data_agent.runtime.model.client import ModelTurnResult

from ..revise.schema import (
    ENTRIES_SCHEMA,
    MAX_ENTRIES,
    coerce_entries,
    forbidden_keys_anywhere,
)
from .models import MAX_SQL_CHARS

_logger = logging.getLogger(__name__)

DRAFT_TOOL_NAME = "draft_blueprint"
CLASSIFY_TOOL_NAME = "classify_blueprint"

# The intent is a one-line statement of what the blueprint answers, not a description of the
# query. Long enough to carry a qualified metric, short enough that it cannot become the steps.
MAX_INTENT_CHARS = 400
MAX_MINT_RATIONALE_CHARS = 1_200

_INTENT = {
    "type": "string",
    "description": (
        "ONE line stating what this blueprint answers, written so a future retrieval can "
        "match a user's question against it. Name the measure and the grain: 'average annual "
        "salary by employment status for a department', not 'a query about salaries'. Fold in "
        "any assumption that CHANGES WHAT THE NUMBER MEANS (e.g. 'excluding terminated "
        "employees'); leave out how the SQL is written."
    ),
}

_RATIONALE = {
    "type": "string",
    "description": (
        "For the expert reviewing this: what you decided and why. Say which literals you made "
        "slots and which you froze, and name anything in their steps you could NOT express."
    ),
}


def build_classify_tool() -> dict[str, Any]:
    """The forced tool for `sql_mode='exact'` — classify, never rewrite.

    Note what is absent: any field in which to express SQL. The expert's query is the accepted
    SQL; all the model does is say what each literal in it MEANS.
    """
    return {
        "type": "function",
        "name": CLASSIFY_TOOL_NAME,
        "description": (
            "Classify every literal predicate of the SQL the expert submitted. Call this "
            "exactly once. Do not emit free text. You CANNOT change the SQL — only state the "
            "ROLE of literals already in it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": _INTENT,
                "entries": ENTRIES_SCHEMA,
                "rationale": _RATIONALE,
            },
            "required": ["intent", "entries", "rationale"],
        },
    }


def build_draft_tool() -> dict[str, Any]:
    """The forced tool for `sql_mode` 'pseudo'/'none' — write the query, then classify it.

    The `sql` field exists here and only here. What keeps it honest is not the schema but what
    happens next: the query is rewritten into a template, every literal in it must be accounted
    for by an entry, and the expert reviews the result before anything is promoted.
    """
    return {
        "type": "function",
        "name": DRAFT_TOOL_NAME,
        "description": (
            "Write the SQL that answers the expert's question, then classify every literal "
            "predicate in what you wrote. Call this exactly once. Do not emit free text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": _INTENT,
                "sql": {
                    "type": "string",
                    "description": (
                        "One read-only SELECT, ClickHouse dialect, reading ONLY the tables you "
                        "were shown. No DDL, no DML, no semicolon-separated statements. Write "
                        "the literals in plainly (department = '0420'); do NOT write "
                        "placeholders — the template is generated from your entries."
                    ),
                },
                "entries": ENTRIES_SCHEMA,
                "rationale": _RATIONALE,
            },
            "required": ["intent", "sql", "entries", "rationale"],
        },
    }


# The composite tools. `nodes` mirrors the steps the EXPERT declared — same count, same order —
# because the DAG shape is theirs and the model is only filling in queries. The tool cannot add,
# drop or reorder a node, which is what keeps "explicit structure" true at the response boundary
# rather than only in the prompt.
_NODE_SQL = {
    "type": "array",
    "description": (
        "One entry per step you were shown, IN THE SAME ORDER. Do not add, remove or reorder "
        "steps — the expert declared them."
    ),
    "items": {
        "type": "object",
        "properties": {
            "order": {
                "type": "integer",
                "description": "The step number you were shown, 0-based.",
            },
            "sql": {
                "type": "string",
                "description": (
                    "The read-only SELECT for this step. Reference an earlier step's result "
                    "with its `$n.name` placeholder exactly as shown."
                ),
            },
        },
        "required": ["order", "sql"],
    },
}


def build_composite_draft_tool() -> dict[str, Any]:
    """The forced tool for a composite whose SQL must be written."""
    return {
        "type": "function",
        "name": DRAFT_TOOL_NAME,
        "description": (
            "Write the SQL for each step the expert declared, then classify every literal "
            "predicate across all of them. Call this exactly once. Do not emit free text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": _INTENT,
                "nodes": _NODE_SQL,
                "entries": ENTRIES_SCHEMA,
                "rationale": _RATIONALE,
            },
            "required": ["intent", "nodes", "entries", "rationale"],
        },
    }


def coerce_node_sql(raw: Any, *, expected: int) -> list[str]:
    """The per-node SQL, indexed by the order the model was shown, or raise.

    TOTAL BY CONSTRUCTION: every declared step must come back with SQL. A model that answers for
    three of four steps would otherwise produce a DAG with a silent hole, and the failure would
    surface much later as an unrewritable template naming a node the expert cannot connect to
    the step they wrote.
    """
    if not isinstance(raw, list):
        raise MintResponseError("the response carries no per-step SQL")
    by_order: dict[int, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        order = item.get("order")
        sql = item.get("sql")
        if isinstance(order, bool) or not isinstance(order, int):
            continue
        if not isinstance(sql, str) or not sql.strip():
            continue
        if len(sql) > MAX_SQL_CHARS:
            raise MintResponseError(f"a step's SQL exceeds {MAX_SQL_CHARS} characters")
        if not 0 <= order < expected:
            # REFUSED, not dropped. An out-of-range order means the model answered about a step
            # that does not exist, so its per-step mapping is not the one it was shown — and
            # silently discarding it would leave the REAL step's SQL to be reported missing,
            # blaming the wrong thing.
            raise MintResponseError(
                f"the response carries SQL for step {order + 1}, which was not one of the "
                f"{expected} steps it was shown"
            )
        if order in by_order:
            raise MintResponseError(
                f"the response carries two queries for step {order + 1}; keeping either one "
                "silently would discard an answer the model meant"
            )
        by_order[order] = sql.strip()
    missing = [i for i in range(expected) if i not in by_order]
    if missing:
        raise MintResponseError(
            f"the response left step(s) {[i + 1 for i in missing]} without SQL; every step the "
            "expert declared must have a query"
        )
    return [by_order[i] for i in range(expected)]


class MintResponseError(ValueError):
    """The model's response cannot be used. Distinct from a bad REQUEST (400 vs 502)."""


def _one_line(text: Any, *, cap: int) -> str:
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())[:cap]


def coerce_mint_response(
    result: ModelTurnResult, *, expect_sql: bool, allow_nodes: bool = False
) -> tuple[str, str, list[dict[str, Any]], str, dict[str, Any]]:
    """`(intent, sql, entries, rationale)` from one forced call, or raise.

    `expect_sql` decides BOTH which tool name is accepted and whether a `sql` key is allowed
    through — the guard has to be enforced here rather than trusted to the schema, because a
    schema is a request and a response is data. A classify-mode reply carrying SQL is REJECTED,
    not stripped: it was written against a contract this system does not have, and silently
    dropping it would let the accompanying rationale describe an edit that never happened.
    """
    # A COMPOSITE draft answers under the drafting tool's name but carries its SQL inside
    # `nodes` rather than a top-level `sql`, so the two flags are genuinely independent: which
    # tool was called, and whether a bare `sql` field is expected.
    wanted = DRAFT_TOOL_NAME if (expect_sql or allow_nodes) else CLASSIFY_TOOL_NAME
    calls = [c for c in (result.tool_calls or []) if c.name == wanted]
    if not calls:
        raise MintResponseError(
            f"the model did not call {wanted!r} (got: "
            f"{[c.name for c in (result.tool_calls or [])] or 'no tool call'})"
        )
    if len(calls) > 1:
        _logger.info("mint: %d calls to %s, taking the first", len(calls), wanted)
    arguments = calls[0].arguments
    if not isinstance(arguments, dict):
        raise MintResponseError(f"{wanted} was called with a non-object argument")

    # THE SWEEP IS SCOPED BY MODE, because `sql` is legitimate in exactly one place: the top
    # level of a DRAFT response. Nested inside an entry it means the same thing it means to the
    # reviser — a model that thinks it is editing the template — so draft mode sweeps the
    # entries and classify mode sweeps everything.
    if allow_nodes and isinstance(arguments.get("sql"), str) and arguments["sql"].strip():
        # The module's own rule, applied evenly. A COMPOSITE draft carries its queries in
        # `nodes`; a top-level `sql` means the model wrote a whole-blueprint query that nothing
        # will ever read. Ignoring it would let the rationale describe work that was discarded —
        # exactly what `classify` mode refuses SQL for.
        raise MintResponseError(
            "the response carries a whole-blueprint `sql`, but this is a multi-step blueprint "
            "whose queries belong on its steps; that query would never be read"
        )
    trespass = (
        forbidden_keys_anywhere(arguments.get("entries"))
        if (expect_sql or allow_nodes)
        else forbidden_keys_anywhere(arguments)
    )
    if trespass:
        raise MintResponseError(
            f"the response carried {sorted(trespass)}. The SQL template is DERIVED from the "
            "accepted query by AST rewrite and is never model-authored"
            + (
                ""
                if expect_sql
                else "; this blueprint is being minted from a query the expert says already "
                "ran, so that query IS the accepted SQL and cannot be rewritten"
            )
        )

    intent = _one_line(arguments.get("intent"), cap=MAX_INTENT_CHARS)
    if not intent:
        raise MintResponseError("the response carries no intent")

    sql = ""
    if expect_sql:
        raw = arguments.get("sql")
        if not isinstance(raw, str) or not raw.strip():
            raise MintResponseError("the response carries no SQL")
        if len(raw) > MAX_SQL_CHARS:
            raise MintResponseError(f"the drafted SQL exceeds {MAX_SQL_CHARS} characters")
        sql = raw.strip()

    entries = coerce_entries(arguments.get("entries"))
    if len(entries) > MAX_ENTRIES:
        raise MintResponseError(f"more than {MAX_ENTRIES} parameterization entries")

    # THE ARGUMENTS COME BACK TOO, so no caller has to find the call again. `_ask_model` used
    # to re-derive it as "the first tool call with a dict argument", which is a DIFFERENT
    # selection rule from the by-name one above: a model emitting two dict-bearing calls could
    # have its intent and entries read from one and its per-node SQL from the other, silently
    # cross-wiring a blueprint out of two answers.
    return (
        intent,
        sql,
        entries,
        _one_line(arguments.get("rationale"), cap=MAX_MINT_RATIONALE_CHARS),
        arguments,
    )


__all__ = [
    "CLASSIFY_TOOL_NAME",
    "build_composite_draft_tool",
    "coerce_node_sql",
    "DRAFT_TOOL_NAME",
    "MintResponseError",
    "build_classify_tool",
    "build_draft_tool",
    "coerce_mint_response",
]
