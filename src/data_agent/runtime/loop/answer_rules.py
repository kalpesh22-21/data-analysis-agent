"""Finish-time ANSWER RULES (05 §L) — the extensible half of exit-#1 enforcement.

A rule inspects the prose a turn is about to finish with, plus what the turn actually
ran, and either matches or does not. Two facts about the shape are load-bearing:

EACH RULE CARRIES ITS OWN PRECONDITION. `turn_sql is empty` belongs to the GROUNDING
rules and must not be hoisted into a shared condition — a markdown table in an ordinary
finish is wrong whether or not a query ran, and is in fact likeliest when one did.

EACH RULE CHARGES TO AN EXISTING ALLOWANCE KIND rather than minting its own, so N rules
cost N extra round-trips per window only if they are N genuinely different complaints.
Grounding rules share `ungrounded_answer`; a form complaint charges to `answer_shape`.

`first_match` returns AT MOST ONE rule, preserving the one-refusal-per-round-trip rule
the rest of the gate chain expresses structurally.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from data_agent.runtime.session.models import FinalizationBlockKind

from .finalization import MAX_NUDGE_DRAFT_CHARS

__all__ = [
    "ANSWER_RULES",
    "ANSWER_RULE_EXHAUSTED_EVENT",
    "ANSWER_RULE_REFUSED_EVENT",
    "AnswerRule",
    "AnswerRuleContext",
    "asserts_quantity",
    "contains_sql",
    "first_match",
    "has_markdown_table",
]

# The `loop_` prefix is load-bearing, not a convention:
# `observability/tracing.py::guardrail_observer` drops every event that lacks it,
# SILENTLY. `rule` is the only attribute either event carries and is allowlisted there.
ANSWER_RULE_REFUSED_EVENT = "loop_answer_rule_refused"
ANSWER_RULE_EXHAUSTED_EVENT = "loop_answer_rule_exhausted"


@dataclass(frozen=True)
class AnswerRuleContext:
    """Everything a rule's predicate may read. `prose` is already stripped and non-empty
    (an empty finish belongs to the empty-answer gate, not here)."""

    prose: str
    turn_sql: tuple[str, ...]


@dataclass(frozen=True)
class AnswerRule:
    """One finish-time check. `name` is a runtime-authored slug and the ONLY thing either
    event puts on the span, so it must never be derived from model or user text."""

    name: str
    charges_to: FinalizationBlockKind
    applies: Callable[[AnswerRuleContext], bool]
    nudge: Callable[[str | None], str]


# --- predicates -------------------------------------------------------------

# Date-shaped runs are removed before the numeric scan: a correctly dated prose answer
# ("as of 2026-03-31") reports no figure.
_DATE_SHAPES = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}")
# THE SEPARATOR MUST SIT BETWEEN DIGITS. A looser `\d[\d,]*` swallows ordinary prose
# punctuation — "Sales 3, Eng 2" parses as one comma-separated number — and every such
# sentence would be refused as a reported figure. Comma form first so it wins the
# alternation.
_NUMERIC = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def _is_year(token: str) -> bool:
    return len(token) == 4 and token.isdigit() and 1900 <= int(token) <= 2100


def reported_figures(prose: str) -> list[str]:
    """Every measured figure the prose reports, digits only (`"9,184"` -> `"9184"`),
        first-occurrence order, deduped.

        THE SAME PREDICATE `asserts_quantity` APPLIES, extracted so the two cannot drift:
        that function is exactly "is this list non-empty", and the corroboration pass
        (09 §D.4) needs the members rather than the boolean. Both therefore inherit the
        narrowness argued at `asserts_quantity` — three digits or a thousands separator,
        bare years and date shapes excluded.

        DIGITS ONLY, because the only use is comparison against warehouse cells: `9,184` in
        prose and `9184` in a result row are the same figure, and the separator is a
        presentation choice made on one side only.
    """
    figures: list[str] = []
    for token in _NUMERIC.findall(_DATE_SHAPES.sub(" ", prose)):
        if _is_year(token):
            continue
        digits = token.replace(",", "")
        if "," in token or len(digits.replace(".", "")) >= 3:
            if digits not in figures:
                figures.append(digits)
    return figures


def asserts_quantity(prose: str) -> bool:
    """Whether the prose reports a measured figure.

    DELIBERATELY NARROW, and the asymmetry is the reason: a false negative costs nothing
    beyond the status quo, while a false positive burns a round-trip and tells a model
    that answered correctly that it fabricated. So a number qualifies only at THREE
    digits or with a thousands separator. That catches a warehouse figure (`412`,
    `9,184`) and lets through the small numbers a KNOWLEDGE-grounded answer legitimately
    carries with no query behind it ("overtime is 1.5 times base rate", "beyond 40 hours
    in a week"). Widen it on the exhausted-event rate, not before.
    """
    # DELEGATED, not re-implemented. This function is exactly "is that list non-empty",
    # and a second copy of the scan is the "two enforcers for one rule" shape doc 09 §B
    # warns about — they agree today and there is nothing structural keeping them so.
    return bool(reported_figures(prose))


# A header row (two or more pipes) immediately followed by a pipe-bearing separator rule.
# Both halves are required: prose can contain a stray `|`, and a bare `---` is a
# horizontal rule.
_MD_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")


def has_markdown_table(prose: str) -> bool:
    lines = prose.splitlines()
    return any(
        lines[i].count("|") >= 2
        and "|" in lines[i + 1]
        and _MD_SEPARATOR.match(lines[i + 1]) is not None
        for i in range(len(lines) - 1)
    )


# The prompt's rule is unconditional — "database, table and column names, DDL and SQL
# text never belong in your answer text" — and it was the last strong one at exit #1 with
# no runtime enforcement. `answer_scrub.py` is not that enforcement: it matches identifier
# SHAPES, so it redacts the names INSIDE a pasted query and leaves the keywords, and what
# reaches the user is a half-redacted statement that is neither prose nor a runnable query.
#
# THREE SHAPES, ALL HIGH-PRECISION, and the same asymmetry as `asserts_quantity` decides
# the tuning: a missed leak costs what today already costs, a false positive burns a
# round-trip telling a model that wrote a clean answer that it leaked schema. The English
# words `select`, `from` and `where` are ordinary prose ("the data comes FROM the employee
# table WHERE department is Sales"), so plain keyword membership is unusable. What is NOT
# ordinary prose is a keyword pair in CAPS, or a code fence, or a code span:
#
#   1. a fence tagged `sql`;
#   2. a fence whose first word is a statement keyword, any case;
#   3. `SELECT`…`FROM` — uppercase, which is how a model writes SQL and not how it writes
#      a sentence — or the same pair, any case, inside a single-backtick code span.
#
# ACCEPTED FALSE NEGATIVE: bare lowercase `select … from …` with no fence and no backticks.
# Catching it means matching the two commonest verbs in English answer prose. Widen on the
# exhausted-event rate, not before.
_SQL_LEAD = r"SELECT|WITH|SHOW|DESCRIBE|DESC|EXPLAIN|CREATE|INSERT|UPDATE|DELETE|ALTER|DROP"

_SQL_SHAPES = (
    re.compile(r"```[ \t]*sql\b", re.IGNORECASE),
    re.compile(r"```[^\n`]*\n\s*(?:" + _SQL_LEAD + r")\b", re.IGNORECASE),
    # The span is capped so two unrelated capitalised words far apart cannot pair up.
    re.compile(r"\bSELECT\b[\s\S]{0,400}?\bFROM\b"),
    re.compile(r"`[^`\n]*\bSELECT\b[^`\n]*\bFROM\b[^`\n]*`", re.IGNORECASE),
)


def contains_sql(prose: str) -> bool:
    """Whether the prose carries query text the user was never meant to read as the answer."""
    return any(shape.search(prose) is not None for shape in _SQL_SHAPES)


# --- nudges -----------------------------------------------------------------


def _draft_echo(draft: str | None) -> list[str]:
    """The draft carried back, marked when cut. Exit #1 persists nothing, so this echo is
    the model's only surviving copy of what it wrote."""
    if not draft or not draft.strip():
        return []
    stripped = draft.strip()
    echo = stripped[:MAX_NUDGE_DRAFT_CHARS]
    if len(echo) < len(stripped):
        echo += " …[truncated]"
    return [f"You drafted: {echo}", ""]


def _ungrounded_quantity_nudge(draft: str | None) -> str:
    lines = _draft_echo(draft)
    lines.append(
        "That answer reports a figure, but this turn executed no query — no runQuery "
        "result and no runBlueprint result stands behind it."
    )
    lines.append(
        "The turn is NOT over and every tool is still available to you. Run the query "
        "or blueprint that produces the figure, then answer from what it returns."
    )
    lines.append(
        "If you cannot run one, say so in text and say what blocked you — that is a "
        "valid, complete answer. A figure you did not measure is not."
    )
    return "\n".join(lines)


def _markdown_table_nudge(draft: str | None) -> str:
    lines = _draft_echo(draft)
    lines.append(
        "That answer contains a markdown table. Rows are never delivered as markdown — "
        "the user's interface renders its own scrollable, paginated grid, and pasted "
        "rows replace it with a truncated copy."
    )
    lines.append(
        "The turn is NOT over and answerWithTable is still available to you: call it in "
        "your NEXT response. Pass one table per part you answered — blueprint_id for a "
        "result a blueprint produced, sql otherwise — and keep your prose as the "
        "answer, with the rows taken out of it."
    )
    return "\n".join(lines)


def _sql_in_answer_nudge(draft: str | None) -> str:
    lines = _draft_echo(draft)
    lines.append(
        "That answer contains SQL. The query already reaches the user in full — the "
        "interface renders it in its own panel beside your answer, from what this turn "
        "executed and from the table you designate — so pasting it into the prose adds "
        "nothing and is the one place it must never appear."
    )
    lines.append(
        "The turn is NOT over. Re-send the SAME answer in your NEXT response with the "
        "query taken out, in the user's own business language: no SQL, no table or "
        "column names, no DDL. Describe what you measured, not how you measured it."
    )
    return "\n".join(lines)


# --- the registry -----------------------------------------------------------

# ORDER IS PRECEDENCE, because `first_match` returns one rule per round-trip: the more
# fundamental complaint comes first. A fabricated table matches every rule, and "you
# measured nothing" is the one worth spending the round on — telling it to reformat
# would be correcting the presentation of an invented answer.
#
# CONTENT BEFORE CHANNEL for the two form rules. `sql_in_answer` says text that must not
# be in an answer at all is in it; `markdown_table` says the right content went out the
# wrong door. The fix for the first is a deletion the model can always make, so the round
# spent on it almost always lands. THE HONEST COST: both charge `answer_shape`, so on a
# finish that pastes rows AND the query behind them, the second complaint meets a spent
# allowance and its half ships — a rarer shape than either alone, and the trade is a real
# one rather than a free win.
ANSWER_RULES: tuple[AnswerRule, ...] = (
    AnswerRule(
        name="ungrounded_quantity",
        charges_to="ungrounded_answer",
        applies=lambda ctx: not ctx.turn_sql and asserts_quantity(ctx.prose),
        nudge=_ungrounded_quantity_nudge,
    ),
    AnswerRule(
        name="sql_in_answer",
        # FORM again, so it shares the shape gate's grant rather than adding a kind.
        charges_to="answer_shape",
        applies=lambda ctx: contains_sql(ctx.prose),
        nudge=_sql_in_answer_nudge,
    ),
    AnswerRule(
        name="markdown_table",
        # A FORM complaint, so it shares the answer-shape gate's allowance rather than
        # adding a fourth kind — see `session/models.py::FINALIZATION_BLOCK_KINDS`.
        charges_to="answer_shape",
        applies=lambda ctx: has_markdown_table(ctx.prose),
        nudge=_markdown_table_nudge,
    ),
)


def first_match(prose: str | None, turn_sql: Sequence[str] | None) -> AnswerRule | None:
    """The first rule this finish trips, or `None`. Empty prose returns `None`: that is the
    empty-answer gate's complaint, and this must not pre-empt it."""
    text = (prose or "").strip()
    if not text:
        return None
    ctx = AnswerRuleContext(prose=text, turn_sql=tuple(turn_sql or ()))
    for rule in ANSWER_RULES:
        if rule.applies(ctx):
            return rule
    return None
