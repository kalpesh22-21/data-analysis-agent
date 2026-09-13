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
    has_alternative_evidence: bool = False
    question: str | None = None
    declined_clarification: bool = False


@dataclass(frozen=True)
class AnswerRule:
    """One finish-time check. `name` is a runtime-authored slug and the ONLY thing either
    event puts on the span, so it must never be derived from model or user text."""

    name: str
    charges_to: FinalizationBlockKind
    applies: Callable[[AnswerRuleContext], bool]
    nudge: Callable[[str | None], str]
    refusal: str | None = None


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
    narrowness argued at `asserts_quantity` — two digits or a thousands separator,
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
        if "," in token or len(digits.split(".")[0]) >= 2 or len(digits.replace(".", "")) >= 3:
            if digits not in figures:
                figures.append(digits)
    return figures


def asserts_quantity(prose: str) -> bool:
    """Whether the prose reports a measured figure.

    DELIBERATELY NARROW, and the asymmetry is the reason: a false negative costs nothing
    beyond the status quo, while a false positive burns a round-trip and tells a model
    that answered correctly that it fabricated. So a number qualifies only at TWO
    digits or with a thousands separator. That catches a warehouse figure (`412`,
    `9,184`) and lets through the small numbers a KNOWLEDGE-grounded answer legitimately
    carries with no query behind it ("overtime is 1.5 times base rate"). Widen it on the exhausted-event rate, not before.
    """
    # DELEGATED, not re-implemented. This function is exactly "is that list non-empty",
    # and a second copy of the scan is the "two enforcers for one rule" shape doc 09 §B
    # warns about — they agree today and there is nothing structural keeping them so.
    return bool(reported_figures(prose))


# A header row (two or more pipes) immediately followed by a pipe-bearing separator rule.
# Both halves are required: prose can contain a stray `|`, and a bare `---` is a
# horizontal rule.
_MD_SEPARATOR = re.compile(r"^\s*\|?[\s:|=-]*[-=][\s:|=-]*\|?\s*$")


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
# FIVE SHAPE FAMILIES, ALL HIGH-PRECISION, and the same asymmetry as `asserts_quantity`
# decides the tuning: a missed leak costs what today already costs, a false positive burns
# a round-trip telling a model that wrote a clean answer that it leaked schema. The English
# words `select`, `from` and `where` are ordinary prose ("the data comes FROM the employee
# table WHERE department is Sales"), so plain keyword membership is unusable. What is NOT
# ordinary prose:
#
#   1. a fence tagged `sql`;
#   2. a fence whose statement leads with a keyword, any case — whether the keyword
#      SHARES the fence's opening line (```SELECT ...) or heads the next one;
#   3. a backtick code span whose content leads with a statement keyword
#      (`select * from employees`, `drop table employees`), or that carries
#      SELECT … FROM anywhere inside it (`sql: select * from t`);
#   4. `SELECT`…`FROM` in CAPS — which is how a model writes SQL and not how it writes a
#      sentence — or the same pair, any case, where FROM names its OBJECT AS A BARE
#      (optionally qualified) IDENTIFIER and the statement carries a marker:
#        - a LONE-star select list (`select * from employees`, `count(*)`). Markdown
#          emphasis is never a lone star: `**bold**` and `*word*` glue each star to
#          another `*` or a letter, which the lookarounds exclude. The lone star is a
#          marker no English sentence carries, so this one needs no FROM-object gate;
#        - a clause keyword after the FROM-object — `GROUP BY`, `ORDER BY`, `LIMIT n`,
#          or `WHERE` followed by a comparison (`status = 'Active'`). `WHERE` stays
#          gated on the operator: the ungated word is English ("from those where
#          eligible");
#        - a statement-terminating `;` glued to the FROM-object (`from employees;`)
#          and NOT followed by a word-led continuation — where a pasted statement
#          ends and an English sentence ("select from staff; contractors are
#          excluded.") carries on with another word;
#   5. a bare NON-SELECT statement in CAPS with its structural keyword, in or out of a
#      fence: UPDATE … SET, INSERT INTO, DELETE FROM, DROP/CREATE/ALTER TABLE|VIEW|INDEX,
#      SHOW TABLES|DATABASES|COLUMNS, DESCRIBE/DESC <name>, EXPLAIN <statement>.
#
# THE FROM-OBJECT GATE IS THE ANY-CASE BOUNDARY, and it is drawn in the open. Real pasted
# SQL names its object — `from employees`, `from db.schema.table` — where English names a
# noun phrase with an article or determiner: "from the menu", "from each group", "from the
# eligible list, order by seniority". A small closed set of determiners in the FROM slot
# (the/a/an/each/every/some/this/that/…) therefore opts a pairing OUT of shape 4's
# any-case markers, and the `;` shape additionally declines a word-led continuation.
# WITHOUT the gate these shapes refused measured English — "We select from each group by
# hand.", "You can select from the menu, limit 3 per person.", "Select one from the plans
# where coverage > 80%." — each a round-trip spent correcting a clean answer. THE RESIDUE,
# said plainly: a BARE-noun object still matches, so "Select one from plans where coverage
# > 80%." — English, article dropped — keeps refusing; that is the accepted cost of
# catching `from employees where department = 'Sales'`, which reads identically until the
# object is named, and a zero-article mass noun is why "from staff; …" needed the
# continuation guard rather than the determiner set.
#
# ACCEPTED FALSE NEGATIVES, as deliberate as the ones this list retired: a bare lowercase
# `select … from …` carrying none of shape 4's markers (catching it means matching the two
# commonest verbs in English answer prose), an any-case pairing whose FROM names a
# determiner-led or zero-article noun phrase in prose order even when a statement was
# meant ("select from the employees;"), the destructive statements of shape 5 in lower
# case (their lead words are ordinary vocabulary — "drop by", "a regular update"), and
# WITH-led CTEs whose outer SELECT … FROM is more than the caps-shaped gap away. Widen on
# the exhausted-event rate, not before.
_SQL_LEAD = r"SELECT|WITH|SHOW|DESCRIBE|DESC|EXPLAIN|CREATE|INSERT|UPDATE|DELETE|ALTER|DROP"

# The one shared gate of the ANY-CASE shapes (see the comment above): FROM followed by a
# bare, optionally qualified identifier that is NOT an English determiner. Pasted SQL
# names its object; prose names a noun phrase with an article. The determiner set is
# closed on purpose — each addition is a real table name that stops being caught.
_SQL_FROM_OBJECT = (
    r"\bFROM\s+(?!(?:the|a|an|each|every|any|some|one|ones|all|both|either|neither|"
    r"this|that|these|those|my|your|his|her|its|our|their|another|several)\b)"
    r"[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*"
)

_SQL_SHAPES = (
    re.compile(r"```[ \t]*sql\b", re.IGNORECASE),
    # The keyword may SHARE the fence's opening line or head the next one.
    re.compile(r"```[^\n`]*\n\s*(?:" + _SQL_LEAD + r")\b", re.IGNORECASE),
    re.compile(r"```[ \t]*(?:" + _SQL_LEAD + r")\b", re.IGNORECASE),
    # A backtick span whose content leads with a statement keyword (single or double
    # backticks; the span itself stays on one line, which is how inline code is written).
    re.compile(r"`{1,2}[ \t]*(?:" + _SQL_LEAD + r")\b[^`\n]*`{1,2}", re.IGNORECASE),
    # … and the older, wider span shape: SELECT…FROM ANYWHERE inside one code span, any
    # case. Kept beside the lead-keyword one — a span like `sql: select * from t` does
    # not lead with the keyword and is still a pasted statement.
    re.compile(r"`[^`\n]*\bSELECT\b[^`\n]*\bFROM\b[^`\n]*`", re.IGNORECASE),
    # CAPS pair. The span is capped so two unrelated capitalised words far apart cannot
    # pair up.
    re.compile(r"\bSELECT\b[\s\S]{0,400}?\bFROM\b"),
    # Any-case pair, markers only: a lone-star select list between SELECT and FROM. The
    # star alone is the marker no English sentence carries, so this one keeps a bare
    # `\bFROM\b` — no FROM-object gate.
    re.compile(
        r"\bSELECT\b[\s\S]{0,200}?(?<![\w*])\*(?![\w*])[\s\S]{0,200}?\bFROM\b",
        re.IGNORECASE,
    ),
    # … a clause keyword after the FROM-object (WHERE only with a comparison in reach).
    # The object must be a bare (optionally qualified) identifier — see _SQL_FROM_OBJECT;
    # "from each group by hand" and "from the menu, limit 3" are prose, `from employees
    # order by name` is not.
    re.compile(
        r"\bSELECT\b[\s\S]{0,400}?"
        + _SQL_FROM_OBJECT
        + r"[\s\S]{0,200}?(?:\bWHERE\b[^\n]{0,80}[<>=!]|\bGROUP\s+BY\b|\bORDER\s+BY\b"
        r"|\bLIMIT\s*\d)",
        re.IGNORECASE,
    ),
    # … or a statement-terminating `;` glued to the FROM-object — same object gate, and
    # one more besides: no WORD-LED continuation may follow the `;`. A pasted statement
    # ends at the semicolon; an English sentence carries on with another word ("…from
    # staff; contractors are excluded."). The guard reads [a-z] but the pattern is
    # IGNORECASE, so "word-led" means ANY leading letter — "…employees; Managers are
    # excluded." is spared together with the lowercase kind, and that generosity is
    # deliberate: testing true lowercase would reintroduce a proper-noun false
    # positive. Accepted FN: a bare two-statement paste whose second statement starts
    # lowercase ("…employees; select …") loses the first statement to the guard.
    re.compile(
        r"\bSELECT\b[\s\S]{0,300}?" + _SQL_FROM_OBJECT + r";(?!\s+[a-z])",
        re.IGNORECASE,
    ),
    # Bare non-SELECT statements, CAPS only — see the accepted-false-negatives note above.
    re.compile(r"\bUPDATE\b[\s\S]{0,120}?\bSET\b"),
    re.compile(r"\bINSERT\s+INTO\b"),
    re.compile(r"\bDELETE\s+FROM\b"),
    re.compile(r"\b(?:DROP|CREATE|ALTER)\s+(?:TABLE|VIEW|INDEX)\b"),
    re.compile(r"\bSHOW\s+(?:TABLES|DATABASES|COLUMNS)\b"),
    re.compile(r"\b(?:DESCRIBE|DESC)\s+[A-Za-z_]"),
    re.compile(r"\bEXPLAIN\s+(?:SELECT|WITH|INSERT|UPDATE|DELETE)\b"),
)


def contains_sql(prose: str) -> bool:
    """Whether the prose carries query text the user was never meant to read as the answer."""
    return any(shape.search(prose) is not None for shape in _SQL_SHAPES)


# A PRODUCTION VERB in reach of a creative-ARTIFACT noun, inside one clause. Without
# the verb, "the attrition number is a joke — what is it really?" trips on `joke` and
# a legitimate data question is refused for a metaphor; without the window, an
# artifact recalled three clauses later pairs with an unrelated "write".
_OFF_TOPIC_ASK = re.compile(
    r"\b(?:write|writes|writing|compose|composes|composing|draft|drafts|drafting"
    r"|creates?|creating|generates?|generating|makes?|making|produces?|producing"
    r"|crafts?|crafting|sing|sing me|recites?|invents?|inventing|come up with"
    r"|give me|gives me|tell me|share)\b[^.\n?]{0,40}"
    r"\b(?:haikus?|poems?|poetry|sonnets?|limericks?|odes?|ballads?|songs?|lyrics?"
    r"|jingles?|raps?|jokes?|puns?|riddles?|lullab(?:y|ies)|nursery[ -]?rhymes?"
    r"|tongue[ -]?twisters?|pick[- ]?up[ -]?lines?|horoscopes?)\b",
    re.IGNORECASE,
)

# The escape route is the DECLINE ITSELF: an answer that IS a decline must not be
# refused as off-topic content. First-person hedge shapes a decline is actually
# written in, and nothing vaguer.
_SCOPE_DECLINE = re.compile(
    r"\b(?:i (?:can'?t|cannot|couldn'?t|won'?t|don'?t have|am (?:not able|unable)|'?m (?:not able|unable))\b"
    r"|(?:isn'?t|is not|that'?s not|that is not) something i\b"
    r"|outside (?:my|the|this) (?:scope|remit)\b)",
    re.IGNORECASE,
)


def is_off_topic_request(question: str) -> bool:
    """Whether the user asked for a creative artifact this assistant does not produce.
    Blank input is False: a rule that cannot see its subject stays silent."""
    return bool(question) and _OFF_TOPIC_ASK.search(question) is not None


def is_scope_decline(prose: str) -> bool:
    """Whether the drafted finish is itself a decline — the rule refuses off-topic
    CONTENT, never the refusal it asked for."""
    return _SCOPE_DECLINE.search(prose.replace("’", "'")) is not None


SCOPE_REFUSAL_TEXT = "I can't create that kind of content. I can help with HR and payroll data or product-usage questions instead."
_SCHEMA_TOKEN = re.compile(
    r"(?<![\w@.])[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)+(?![\w@.])"
)
_PUBLIC_TEXT = re.compile(
    r"https?://\S+|[\w.+-]+@[\w.-]+|\b[\w.-]+\.(?:csv|tsv|xlsx|json|parquet)\b", re.I
)


def contains_schema_reference(prose: str) -> bool:
    # URLs, email and data-file names are legitimate product/reference text.
    text = _PUBLIC_TEXT.sub(" ", prose)
    return bool(_SCHEMA_TOKEN.search(text) or re.search(r"`[A-Za-z][A-Za-z0-9_]*`", text))


def _scope_nudge(draft):
    return "The turn is NOT over. This request asks for creative content outside this assistant's scope. Decline through answerWithText and offer HR/payroll analysis or product help. Do not repeat the content or run SQL to justify it."


def _schema_nudge(draft):
    return "The turn is NOT over. Remove internal database/table/column identifiers from the answer. Use business language and submit the corrected answer through answerWithText or answerWithTable."


def _no_evidence_nudge(draft):
    return "The turn is NOT over. Ground the answer in current tool evidence, or finish through answerWithText with an honest explanation of what cannot be answered."


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
        "That answer reports a figure, but this turn has no supporting evidence — no "
        "runQuery result, runBlueprint result, or complete Help Center document stands "
        "behind it."
    )
    lines.append(
        "The turn is NOT over and every tool is still available to you. Run the query or "
        "blueprint that produces the figure, or fetch the complete relevant article with "
        "getHelpCenterDocument, then answer only from what it returns."
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
        name="out_of_scope_request",
        charges_to="ungrounded_answer",
        applies=lambda ctx: (
            not ctx.turn_sql
            and not ctx.has_alternative_evidence
            and bool(ctx.question)
            and is_off_topic_request(ctx.question)
            and not is_scope_decline(ctx.prose)
        ),
        nudge=_scope_nudge,
        refusal=SCOPE_REFUSAL_TEXT,
    ),
    AnswerRule(
        name="ungrounded_quantity",
        charges_to="ungrounded_answer",
        applies=lambda ctx: (
            not ctx.turn_sql and not ctx.has_alternative_evidence and asserts_quantity(ctx.prose)
        ),
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
        name="schema_in_answer",
        charges_to="answer_shape",
        applies=lambda ctx: contains_schema_reference(ctx.prose),
        nudge=_schema_nudge,
    ),
    AnswerRule(
        name="markdown_table",
        # A FORM complaint, so it shares the answer-shape gate's allowance rather than
        # adding a fourth kind — see `session/models.py::FINALIZATION_BLOCK_KINDS`.
        charges_to="answer_shape",
        applies=lambda ctx: has_markdown_table(ctx.prose),
        nudge=_markdown_table_nudge,
    ),
    AnswerRule(
        name="no_evidence",
        charges_to="ungrounded_answer",
        applies=lambda ctx: (
            not ctx.turn_sql
            and not ctx.has_alternative_evidence
            and not ctx.declined_clarification
            and not is_scope_decline(ctx.prose)
        ),
        nudge=_no_evidence_nudge,
    ),
)


def first_match(
    prose: str | None,
    turn_sql: Sequence[str] | None,
    question: str | None = None,
    *,
    declined_clarification: bool = False,
    has_alternative_evidence: bool = False,
) -> AnswerRule | None:
    """The first rule this finish trips, or `None`. Empty prose returns `None`: that is the
    empty-answer gate's complaint, and this must not pre-empt it."""
    text = (prose or "").strip()
    if not text:
        return None
    ctx = AnswerRuleContext(
        prose=text,
        turn_sql=tuple(turn_sql or ()),
        has_alternative_evidence=has_alternative_evidence,
        question=question,
        declined_clarification=declined_clarification,
    )
    for rule in ANSWER_RULES:
        if rule.applies(ctx):
            return rule
    return None
