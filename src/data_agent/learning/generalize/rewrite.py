"""AST rewrite: accepted SQL + S3 role classification → a `{slot}` `sql_template`.

Deterministic, sqlglot-only (D35 — never re-emit SQL from an LLM). For each S3
`parameterization` entry we locate the literal predicate in the parsed AST and:

  * role=slot  → replace the literal with a `{slot}` placeholder;
  * role=inline→ leave the literal in place (structural / metric-defining);
  * role=rule  → drop the predicate from the template and record the rule id in
                 `uses_rules` (the rule re-applies at runtime; D97 §7).

The literal is rendered with the ClickHouse dialect (preserving `sum`/`toYear`
casing) which emits a placeholder as the canonical token `{slot: }`; we then map
`{slot: }` → `{slot}` so the runtime-executable template carries the BRACE authoring
form `runtime/blueprint/template.py` expects (a promoted blueprint is runtime-
executable with zero placeholder translation). This two-step keeps identifier casing
exact while producing the `{slot}` surface the fixtures pin.

Un-rewritable / unparseable SQL raises `RewriteError` — the caller maps it to
`fail_to_review` (D52/D97), never a guessed template.
"""

from __future__ import annotations

from typing import Any

import sqlglot
import sqlglot.expressions as exp

# Comparison / membership predicates whose literal operand a slot can parameterize.
_COMPARISONS: tuple[type[exp.Expression], ...] = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Like,
    exp.ILike,
    exp.In,
)


class RewriteError(Exception):
    """The accepted SQL could not be parsed or a declared slot/rule literal could
    not be located — the caller routes to `fail_to_review` (D52/D97)."""


def parse_accepted_sql(accepted_sql: str) -> exp.Expression:
    """Parse a single accepted SQL statement (ClickHouse dialect), fail-loud."""
    if not accepted_sql or not accepted_sql.strip():
        raise RewriteError("accepted SQL is empty — cannot rewrite (fail-to-review).")
    try:
        ast = sqlglot.parse_one(
            accepted_sql, dialect="clickhouse", error_level=sqlglot.ErrorLevel.RAISE
        )
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError) as exc:
        raise RewriteError(f"unparseable accepted SQL: {exc}") from exc
    except Exception as exc:  # pragma: no cover — defensive
        raise RewriteError(f"unexpected parse error: {exc}") from exc
    if ast is None:
        raise RewriteError("accepted SQL parsed to None — cannot rewrite.")
    return ast


def _find_literal(
    ast: exp.Expression, column: str, value: str
) -> exp.Literal | None:
    """Find the (currently in-tree) literal for `column <op> value`.

    A comparison qualifies when it references `column` (directly or wrapped in a
    function such as `toYear(pay_period)`) and carries a literal whose text equals
    `value`. Replaced/dropped nodes leave the tree, so a repeat scan naturally
    advances to the next occurrence — no external cursor needed.
    """
    for cmp in ast.find_all(*_COMPARISONS):
        if column not in {col.name for col in cmp.find_all(exp.Column)}:
            continue
        for lit in cmp.find_all(exp.Literal):
            if str(lit.this) == value:
                return lit
    return None


def _drop_predicate(literal: exp.Literal) -> None:
    """Remove the comparison enclosing `literal` from the WHERE tree (role=rule)."""
    cmp: exp.Expression | None = literal
    while cmp is not None and not isinstance(cmp, _COMPARISONS):
        cmp = cmp.parent
    if cmp is None:
        return
    parent = cmp.parent
    if isinstance(parent, exp.And):
        keep = parent.left if parent.right is cmp else parent.right
        parent.replace(keep)
    else:
        cmp.pop()


def rewrite_sql_to_template(
    accepted_sql: str,
    parameterization: list[dict[str, Any]],
    *,
    strict: bool = True,
) -> str:
    """Rewrite `accepted_sql` into a `{slot}` template per the S3 role plan.

    `strict=True` (single blueprint): every role=slot/role=rule literal MUST be
    found — a miss raises `RewriteError`. `strict=False` (a composite node whose SQL
    references only a subset of the top-level params): a param whose literal is
    absent from THIS node's SQL is simply skipped.

    `uses_rules` is NOT derived here (it is a plan-level fact aggregated once by the
    builder); this function only drops rule predicates from the template text.
    """
    ast = parse_accepted_sql(accepted_sql)
    slot_names: list[str] = []

    for param in parameterization:
        role = param.get("role")
        locator = param.get("locator") or {}
        column = locator.get("column")
        value = locator.get("value")
        if role == "inline" or column is None or value is None:
            continue

        literal = _find_literal(ast, column, str(value))
        if literal is None:
            if strict:
                raise RewriteError(
                    f"role={role} literal for {column}={value!r} not found in the "
                    "accepted SQL — cannot rewrite (fail-to-review)."
                )
            continue

        if role == "slot":
            slot = param.get("slot") or {}
            name = slot.get("name")
            # A truthy NON-STRING name passed this check and then died on the
            # `"{" + name` render below with a TypeError — this module's contract is
            # that an un-rewritable plan raises `RewriteError` (the only exception its
            # callers catch), so the type belongs in the same guard as the emptiness.
            # The builder's `_plan_params_ok` gates this too; this keeps the contract
            # true for any other caller.
            if not isinstance(name, str) or not name:
                raise RewriteError("role=slot param is missing a string slot.name.")
            literal.replace(exp.Placeholder(this=name))
            slot_names.append(name)
        elif role == "rule":
            _drop_predicate(literal)

    rendered = ast.sql(dialect="clickhouse")
    for name in slot_names:
        rendered = rendered.replace("{" + name + ": }", "{" + name + "}")
    return rendered
