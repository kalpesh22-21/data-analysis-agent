"""Literal-predicate enumeration for the D97 totality check.

The extractor's `parameterization` must have EXACTLY ONE `ParamPlan` per literal predicate
of the accepted SQL — a predicate with no plan is a silently dropped filter. This enumerates
every literal COMPARISON predicate (`=`, `!=`, `<`, `<=`, `>`, `>=`, `IN`, `BETWEEN`,
`LIKE`/`ILIKE`) ANYWHERE in the statement: WHERE, JOIN-`ON`, HAVING and sub-WHEREs at every
nesting level, because the walk is over the whole AST. "Literal" means one side resolves to
a single column (through function wrappers) and the other to a constant. OVER-enumeration is
the SAFE direction (§2.3) — a conservative decline, never a silent drop. Un-parseable ⇒
`None`.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
import sqlglot.expressions as exp


@dataclass(frozen=True)
class LiteralPredicate:
    table: str  # the column's qualifier (alias or db.table), or "" if unqualified
    column: str  # the (unqualified) column the literal constrains
    value: str  # the literal value as text (list/range members joined by ",")
    # How the column is compared to the value — "=", "!=", "<", "<=", ">", ">=",
    # "LIKE", "ILIKE", "IN", "BETWEEN" — MIRRORED when the literal was on the left, so
    # the operator always reads column-first (`5 > age` records "<").
    #
    # The totality COVERAGE test does not use it (a plan entry accounts for a predicate
    # whichever way it compares, and it was right not to). It exists because the
    # totality DECLINE now names catalog rules that match an uncovered predicate, and
    # `column`+`value` alone cannot tell `status = 'X'` from `status != 'X'` — offering
    # the rule for the first when the SQL says the second would invert a filter, which
    # is the D56 wrong-answer class the whole no-drop invariant exists to prevent.
    operator: str


# Binary comparison expressions whose (column, constant) form is a literal
# predicate. `In`/`Between` are handled separately (n-ary / range shapes). The value is
# the operator recorded on `LiteralPredicate`, and its MIRROR — what the comparison
# means read from the column's side when the literal sits on the left.
_BINARY_CMP: dict[type[exp.Expression], tuple[str, str]] = {
    exp.EQ: ("=", "="),
    exp.NEQ: ("!=", "!="),
    exp.GT: (">", "<"),
    exp.LT: ("<", ">"),
    exp.GTE: (">=", "<="),
    exp.LTE: ("<=", ">="),
    exp.Like: ("LIKE", "LIKE"),
    exp.ILike: ("ILIKE", "ILIKE"),
}


def _single_column(node: exp.Expression | None) -> exp.Column | None:
    """The single `Column` a node references, seen THROUGH function wrappers
    (`toYear(pay_period)` → `pay_period`). None if it references zero or >1 column."""
    if node is None:
        return None
    columns = list(node.find_all(exp.Column))
    return columns[0] if len(columns) == 1 else None


def _constant_text(node: exp.Expression | None) -> str | None:
    """The literal text of a CONSTANT side — a side with NO column and ≥1 literal,
    seen through a function wrapper (`toDate('2025-01-01')` → `2025-01-01`). None
    if the side references any column or carries no literal/boolean."""
    if node is None:
        return None
    if list(node.find_all(exp.Column)):
        return None
    literal = next(iter(node.find_all(exp.Literal)), None)
    if literal is not None:
        return literal.name
    boolean = next(iter(node.find_all(exp.Boolean)), None)
    if boolean is not None:
        return "TRUE" if boolean.this else "FALSE"
    return None


def _table_of(column: exp.Column) -> str:
    return column.table or ""


def _binary_predicate(node: exp.Expression) -> LiteralPredicate | None:
    forward, mirrored = _BINARY_CMP[type(node)]
    # Literal may be on EITHER side.
    operator = forward
    col = _single_column(node.this)
    const = _constant_text(node.expression)
    if col is None or const is None:
        col = _single_column(node.expression)
        const = _constant_text(node.this)
        operator = mirrored
    if col is not None and const is not None:
        return LiteralPredicate(
            table=_table_of(col), column=col.name, value=const, operator=operator
        )
    return None


def _in_predicate(node: exp.In) -> LiteralPredicate | None:
    col = _single_column(node.this)
    if col is None:
        return None
    literals = [
        text for member in node.expressions if (text := _constant_text(member)) is not None
    ]
    if not literals:  # `IN (SELECT …)` — no literal members
        return None
    return LiteralPredicate(
        table=_table_of(col), column=col.name, value=",".join(literals), operator="IN"
    )


def _between_predicate(node: exp.Between) -> LiteralPredicate | None:
    col = _single_column(node.this)
    if col is None:
        return None
    low = _constant_text(node.args.get("low"))
    high = _constant_text(node.args.get("high"))
    if low is None and high is None:
        return None
    return LiteralPredicate(
        table=_table_of(col), column=col.name, value=f"{low},{high}", operator="BETWEEN"
    )


def _predicate_of(node: exp.Expression) -> LiteralPredicate | None:
    if isinstance(node, exp.In):
        return _in_predicate(node)
    if isinstance(node, exp.Between):
        return _between_predicate(node)
    if type(node) in _BINARY_CMP:
        return _binary_predicate(node)
    return None


def literal_predicates(sql: str) -> list[LiteralPredicate] | None:
    """Enumerate every literal comparison predicate in *sql*, in deterministic AST walk order.

    `None` if *sql* cannot be parsed (→ fail-to-review, D52).
    """
    try:
        ast = sqlglot.parse_one(sql, dialect="clickhouse")
    except Exception:  # noqa: BLE001 - un-parseable SQL → caller fails to review
        return None
    if ast is None:
        return None

    predicates: list[LiteralPredicate] = []
    # Single DFS over the WHOLE statement (WHERE + JOIN-ON + HAVING + nested
    # sub-SELECTs) — deterministic order, all nesting levels.
    for node in ast.walk():
        pred = _predicate_of(node)
        if pred is not None:
            predicates.append(pred)
    return predicates


def sole_literal_predicate(fragment: str) -> LiteralPredicate | None:
    """The one literal predicate *fragment* consists of ENTIRELY, or `None`.

    Written for the catalog's `rules[*].predicate` and deliberately the STRICTEST reading of
    "the rule IS this predicate": the parse ROOT must itself be the comparison.
    `literal_predicates` walks, so it would report the `!=` inside `a != 'N' OR a IS NULL` — a
    rule that CONTAINS the predicate rather than one that IS it, and offering that would hand a
    model a rule strictly different from the filter its query ran. Prose, a placeholder-bearing
    predicate and a null-check all return `None`.
    """
    try:
        root = sqlglot.parse_one(fragment, dialect="clickhouse")
    except Exception:  # noqa: BLE001 - prose or a placeholder: not a comparison
        return None
    if root is None:
        return None
    return _predicate_of(root)
