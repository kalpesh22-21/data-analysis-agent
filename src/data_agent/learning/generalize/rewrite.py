"""AST rewrite: accepted SQL + S3 role classification → a `{slot}` `sql_template`.

Deterministic, sqlglot-only (D35 — never re-emit SQL from an LLM). role=slot replaces the
literal with a `{slot}` placeholder; role=inline and role=rule both KEEP the literal in
place. **role=rule keeps the predicate verbatim and only records the rule id — the runtime
never re-applies a deleted predicate** (see docs/cleanup/WORKLOG.md #18).

The guards check the OUTPUT rather than a list of shapes somebody has to keep complete:
`_check_inline_literals` before any mutation, `_recheck_inline_literals` after it, and
`_check_rewritten` (the template re-parses, and no function call lost an argument).

`RewriteError` is the ONLY exception this module raises for a bad plan or an awkward SQL
shape; both call sites catch exactly that and stamp `fail_to_review/unrewritable_sql`. The
reason tags are frozen, so WHY a rewrite refused belongs in the exception MESSAGE.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import sqlglot
import sqlglot.expressions as exp

from ...runtime.blueprint.template import SLOT_TOKEN
from ..extractor.sql_predicates import literal_predicates

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

# Operator nodes excluded from the ARITY census: their "arity" is grammar, not a call
# signature (`_function_shapes`). Only the three `Connector` classes are actually both
# `Func` and `Binary`, so `Binary` is the live entry here; `Unary` and `Paren` are
# belt-and-braces (no `Unary`/`Paren` subclass is a `Func` in the pinned version) and
# are kept so the census stays right if that changes.
_OPERATOR_NODES: tuple[type[exp.Expression], ...] = (exp.Binary, exp.Unary, exp.Paren)


class RewriteError(Exception):
    """The accepted SQL could not be parsed, or a declared slot/rule literal could not be located.

    The caller routes to `fail_to_review` (D52/D97).
    """


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

    A comparison qualifies when it references `column` — directly or wrapped in a function such
    as `toYear(pay_period)` — and carries a literal whose text equals `value`. Replaced or
    dropped nodes leave the tree, so a repeat scan naturally advances to the next occurrence.
    """
    for cmp in ast.find_all(*_COMPARISONS):
        if column not in {col.name for col in cmp.find_all(exp.Column)}:
            continue
        for lit in cmp.find_all(exp.Literal):
            if str(lit.this) == value:
                return lit
    return None


def _func_name(node: exp.Expression) -> str:
    """The function's name as authored (`sumIf`, `toYear`) or its SQL name (`SUM`).

    Unrecognized functions carry their spelling as a STRING in `this` (the `Anonymous` family);
    recognized ones answer `sql_name()`. Both are stable across the rewrite.
    """
    this = node.args.get("this")
    if isinstance(this, str) and this:
        return this
    sql_name = getattr(node, "sql_name", None)
    if callable(sql_name):
        try:
            return str(sql_name())
        except Exception:  # pragma: no cover — defensive
            pass
    return type(node).__name__.upper()


def _function_shapes(ast: exp.Expression) -> Counter[tuple[str, int]]:
    """`(function name, argument count)` → occurrences, over the whole tree.

    The ARGUMENT COUNT is the point: `sumIf(amount, register_type = 'EARN')` with its condition
    deleted is `sumIf(amount)` — still parses, still runs on some engines, and computes a
    DIFFERENT number under the same alias. Nothing downstream of S4 looks at arity.
    """
    shapes: Counter[tuple[str, int]] = Counter()
    for func in ast.find_all(exp.Func):
        if isinstance(func, _OPERATOR_NODES):
            continue
        arity = 0
        for arg in func.args.values():
            if isinstance(arg, list):
                arity += sum(1 for item in arg if isinstance(item, exp.Expression))
            elif isinstance(arg, exp.Expression):
                arity += 1
        shapes[(_func_name(func).lower(), arity)] += 1
    return shapes


def _check_rewritten(
    rendered: str, before: Counter[tuple[str, int]], after: exp.Expression
) -> None:
    """The output post-condition: the template PARSES, and no call lost an argument.

    The re-parse rewrites `{slot}` → `:slot` first, exactly as `structural_key` and the runtime
    binder do, because a brace placeholder is not SQL and would fail the parse it is supposed to
    prove. The arity check flags a shape the INPUT did not have that many times, not one the
    output no longer has: dropping a whole predicate legally removes a `toYear` call, while
    `sumIf/2 → sumIf/1` legally removes nothing and is the bug.
    """
    colon_form = SLOT_TOKEN.sub(lambda m: f":{m.group(1)}", rendered)
    try:
        reparsed = sqlglot.parse_one(
            colon_form, dialect="clickhouse", error_level=sqlglot.ErrorLevel.RAISE
        )
    except Exception as exc:
        raise RewriteError(
            f"the rewritten template does not parse ({exc}) — the rewrite produced "
            f"invalid SQL: {rendered!r} (fail-to-review)."
        ) from exc
    if reparsed is None:  # pragma: no cover — defensive
        raise RewriteError("the rewritten template parsed to None (fail-to-review).")
    grown = [
        shape for shape, count in _function_shapes(after).items() if count > before[shape]
    ]
    if grown:
        name, arity = sorted(grown)[0]
        raise RewriteError(
            f"the rewrite changed a function call to {name}/{arity} — an argument was "
            "deleted, so the template computes something the accepted SQL did not "
            "(fail-to-review)."
        )


def _inline_literal_present(
    ast: exp.Expression, accepted_sql: str, column: str, value: str
) -> bool:
    """Is an `inline` entry's literal actually in the accepted SQL?

    Present under EITHER authority, because the two enumerate the same predicates at different
    granularity and an inline entry may legitimately be written against either: this module's
    `_find_literal` sees INDIVIDUAL literals (each `IN` member separately), while the S3
    totality enumerator joins list/range members with commas and reads shapes `_find_literal`
    cannot (`BETWEEN`, boolean constants). Being present in one is enough — the hole this closes
    is a HALLUCINATED literal, one that appears in neither.
    """
    if _located_inline_literal(ast, column, value):
        return True
    predicates = literal_predicates(accepted_sql) or ()
    return any(
        predicate.column.lower() == column.lower() and predicate.value == value
        for predicate in predicates
    )


def _located_inline_literal(ast: exp.Expression, column: str, value: str) -> bool:
    """Is the inline literal findable IN THE TREE?

    The whole value, or every comma-separated member of it (an `IN` list's members arrive
    joined). The tree-only half of `_inline_literal_present`, split out because it is the half
    that can be re-asked AFTER the rewrite.
    """
    if _find_literal(ast, column, value) is not None:
        return True
    members = [member for member in value.split(",") if member]
    return len(members) > 1 and all(
        _find_literal(ast, column, member) is not None for member in members
    )


def _recheck_inline_literals(
    ast: exp.Expression, entries: list[tuple[str, str]]
) -> None:
    """POST-CONDITION: every inline literal that was in the tree is STILL in the tree.

    The pre-pass proves the plan is honest about the accepted SQL; this proves the REWRITE was
    honest about the plan. It passes trivially today and is KEPT deliberately, because the
    invariant it states — no edit here removes a literal another entry vouched for — is what a
    future role, optimizer pass or re-introduced drop has to get past (see
    docs/cleanup/WORKLOG.md #18). Tree granularity only: an entry whose presence rested on the
    S3 enumerator was never visible to `_find_literal`, so only entries the pre-pass located in
    the tree are re-checked.
    """
    for column, value in entries:
        if not _located_inline_literal(ast, column, value):
            raise RewriteError(
                f"the rewrite removed the role=inline literal {column}={value!r} that "
                "the plan declared it would keep — an edit took more than its own "
                "predicate (fail-to-review)."
            )


def _check_inline_literals(
    ast: exp.Expression, accepted_sql: str, parameterization: list[dict[str, Any]]
) -> list[tuple[str, str]]:
    """Strict-mode only: every `role=inline` locator must name a literal of THIS SQL.

    S3's totality gate counts ANY entry as covering a predicate, and the rewrite leaves inline
    literals alone — so an inline entry for a predicate the SQL does not contain used to sail
    through as `outcome: ok`, shipping a plan that claims a filter the template has not got.
    Runs BEFORE the rewrite loop touches the tree, so an earlier entry's edit cannot make a
    later one look absent, and RETURNS the entries it found there for the recheck.
    """
    recheck: list[tuple[str, str]] = []
    for param in parameterization:
        if param.get("role") != "inline":
            continue
        locator = param.get("locator") or {}
        column = locator.get("column")
        value = locator.get("value")
        if column is None or value is None:
            continue
        text = str(value)
        if _located_inline_literal(ast, column, text):
            recheck.append((column, text))
            continue
        if not _inline_literal_present(ast, accepted_sql, column, text):
            raise RewriteError(
                f"role=inline literal for {column}={text!r} is not in the accepted "
                "SQL — the plan declares a filter the template does not carry "
                "(fail-to-review)."
            )
    return recheck


def rewrite_sql_to_template(
    accepted_sql: str,
    parameterization: list[dict[str, Any]],
    *,
    strict: bool = True,
) -> str:
    """Rewrite `accepted_sql` into a `{slot}` template per the S3 role plan.

    `strict=True` (single blueprint): every role=slot/role=rule literal MUST be found — the plan
    is a claim about THIS SQL, and an entry naming a predicate not in it is wrong whether or not
    the rewrite would have edited it — and every role=inline literal must be present before the
    rewrite and STILL present after. `strict=False` (a composite node whose SQL references only
    a subset of the top-level params): a param whose literal is absent from THIS node's SQL is
    skipped, inline entries included. `uses_rules` is NOT derived here (a plan-level fact the
    builder aggregates); role=rule leaves the predicate in the template verbatim.
    """
    ast = parse_accepted_sql(accepted_sql)
    before = _function_shapes(ast)
    inline_recheck = (
        _check_inline_literals(ast, accepted_sql, parameterization) if strict else []
    )
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
        # role == "rule": KEEP THE PREDICATE, ANNOTATE THE BLUEPRINT. The literal was
        # LOCATED above (a miss is still a strict `RewriteError` — a plan naming a
        # predicate that is not there is not a plan for this SQL), and that is the whole
        # of the rewrite's job for this role: the template keeps the accepted SQL's
        # filter verbatim, and the builder records the catalog rule id in `uses_rules`.
        #
        # THE TEMPLATE AND THE ANNOTATION SAY THE SAME THING, which is what makes the
        # role safe. A learned `uses_rules` entry is a bare catalog id STRING, and
        # `runtime/blueprint/rules.py::parse_rule` returns None for anything that is not
        # a dict carrying `resolve_via: resolveValues(col, 'concept')` + `table` — so
        # every entry S4 can produce is STATIC at the runtime's parser, and
        # `executor._expand_rules` `continue`s past it before any resolve hook is
        # called. The annotation is provenance for a reviewer and for D48 keying; it
        # changes no SQL.
        #
        # IF THAT EVER CHANGES — if `uses_rules` is enriched into rule DICTS at landing
        # — read `_expand_rules` before doing it. Binding is tolerant of a rule whose
        # placeholder the template does not carry (the single-node path adds it only
        # `if binds_name in referenced`, and `_node_bindings` iterates the template's
        # refs), so a kept-predicate template would not fail to bind. RESOLUTION is not
        # tolerant: it runs per rule before binding, and with no hook wired it returns
        # UNSUPPORTED (the blueprint falls back to the raw loop) while a
        # degrade/empty/denied resolve is an `ExecFailed` — a filter the template does
        # not even need would then be able to stop the query.

    _recheck_inline_literals(ast, inline_recheck)
    try:
        rendered = ast.sql(dialect="clickhouse")
    except Exception as exc:
        # The renderer is not total over an EDITED tree: an `OR` with one arm removed
        # raised a bare `KeyError` here, past every `except RewriteError` in the plane.
        raise RewriteError(
            f"the rewritten AST could not be rendered as SQL ({exc!r}) — the drop left a "
            "shape sqlglot cannot emit (fail-to-review)."
        ) from exc
    for name in slot_names:
        rendered = rendered.replace("{" + name + ": }", "{" + name + "}")
    _check_rewritten(rendered, before, ast)
    return rendered
