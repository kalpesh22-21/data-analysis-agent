"""Literal-predicate enumeration for the D97 totality check.

The extractor's `parameterization` must have EXACTLY ONE `ParamPlan` per literal
predicate of the accepted SQL (D97 §4.1) — a predicate with no plan is a silently
dropped filter (the D56 wrong-answer class). This module deterministically
enumerates EVERY literal comparison predicate so the validator can assert
coverage per-locator.

Scope (reconciled §4.1 ↔ D97 "every literal predicate"): all literal COMPARISON
predicates — `=`, `!=`, `<`, `<=`, `>`, `>=`, `IN`, `BETWEEN`, `LIKE`/`ILIKE` —
found ANYWHERE in the statement: WHERE, JOIN-`ON`, HAVING, and derived-table/CTE
sub-WHEREs at every nesting level (the walk is over the whole AST, not a single
WHERE subtree — the previous WHERE-only walk silently missed JOIN-`ON` literals
and >1-WHERE queries). A predicate is "literal" iff ONE side resolves to a single
column (seen through a function wrapper, e.g. `toYear(col)`) and the OTHER side is
a constant — INCLUDING a function-wrapped literal (`toDate('2025-01-01')`) — on
EITHER side.

Over-enumeration is the SAFE direction (D97 §2.3): a predicate this module returns
that the plan does not cover → a conservative decline (fail-to-review), never a
silent drop. Subquery-internal predicates are therefore enumerated too — a known
conservative-decline class, not special-cased away. Un-parseable SQL → `None`
(the validator routes it to fail-to-review, D52).
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


# Binary comparison expressions whose (column, constant) form is a literal
# predicate. `In`/`Between` are handled separately (n-ary / range shapes).
_BINARY_CMP: tuple[type[exp.Expression], ...] = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.LT,
    exp.GTE,
    exp.LTE,
    exp.Like,
    exp.ILike,
)


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
    # Literal may be on EITHER side.
    col = _single_column(node.this)
    const = _constant_text(node.expression)
    if col is None or const is None:
        col = _single_column(node.expression)
        const = _constant_text(node.this)
    if col is not None and const is not None:
        return LiteralPredicate(table=_table_of(col), column=col.name, value=const)
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
    return LiteralPredicate(table=_table_of(col), column=col.name, value=",".join(literals))


def _between_predicate(node: exp.Between) -> LiteralPredicate | None:
    col = _single_column(node.this)
    if col is None:
        return None
    low = _constant_text(node.args.get("low"))
    high = _constant_text(node.args.get("high"))
    if low is None and high is None:
        return None
    return LiteralPredicate(table=_table_of(col), column=col.name, value=f"{low},{high}")


def literal_predicates(sql: str) -> list[LiteralPredicate] | None:
    """Enumerate every literal comparison predicate in *sql* (deterministic AST
    walk order), or `None` if *sql* cannot be parsed (→ fail-to-review, D52)."""
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
        pred: LiteralPredicate | None = None
        if isinstance(node, exp.In):
            pred = _in_predicate(node)
        elif isinstance(node, exp.Between):
            pred = _between_predicate(node)
        elif isinstance(node, _BINARY_CMP):
            pred = _binary_predicate(node)
        if pred is not None:
            predicates.append(pred)
    return predicates
