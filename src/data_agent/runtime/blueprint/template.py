"""blueprint/template.py — F1 sqlglot-AST typed-literal slot binding (§3.3).

`runQuery` has no bound-parameter surface (F1). This module realizes the D10
injection boundary the same way `resolveValues`' `sql_builder` does — by building
the value into the parsed AST as a TYPED sqlglot literal, never by string
interpolation. The `04-blueprints.md` "`{department:String}` server-side param"
language is realized as a typed AST literal; the security property (no
interpolation) is identical, the mechanism differs (F1).

Authoring shape: a `{slot_name}` token in the SQL template marks a bind site.
`{name}` is reserved for slots — a template must not use `{...}` for anything
else. Each `{name}` is rewritten to a sqlglot colon-placeholder (`:name`), the
template is parsed under the ClickHouse dialect (D62), and every placeholder node
is REPLACED with a typed literal built from the resolved binding:

  - str            → `exp.Literal.string(...)`   (ClickHouse-escaped: `'` → `''`)
  - bool           → `exp.Boolean(...)`
  - int/float      → `exp.Literal.number(...)`
  - list/tuple     → `exp.Tuple(...)` of per-element literals  (for `IN {codes}`)

Fail-closed (§3.3): a template that does not parse, a `{slot}` with no binding, a
binding for a slot the template never references, or an unbindable value type all
raise `TemplateBindError` — the executor never emits half-bound SQL.
"""

from __future__ import annotations

import math
import re
from typing import Any

import sqlglot
from sqlglot import exp

# A slot bind-site token: `{name}` where name is an identifier. Deliberately
# strict — a `{` followed by anything non-identifier is NOT treated as a slot
# (so a stray brace fails the parse loudly rather than binding silently).
_SLOT_TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Non-SELECT statement node kinds that must NEVER appear in a blueprint template
# (a blueprint is a READ-ONLY query). `exp.Block` is the multi-statement wrapper
# (`SELECT 1; DROP TABLE payroll` parses to a Block) — rejecting it closes the
# statement-injection hole where a trailing DDL contributes no columns and so
# "waves through" the footprint check (review FIX 2).
_FORBIDDEN_STATEMENTS: tuple[type[exp.Expression], ...] = (
    exp.Block,
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Command,
    exp.Set,
    exp.Use,
    exp.TruncateTable,
    exp.Merge,
)
# The set-operation roots whose arms are all SELECTs (a UNION/INTERSECT/EXCEPT of
# reads is still read-only). The root of a valid template is one of these or Select.
_READ_ONLY_ROOTS: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,
    exp.Intersect,
    exp.Except,
)


class TemplateBindError(Exception):
    """A slot template could not be safely bound (fail-closed, §3.3)."""


def assert_read_only_select(tree: exp.Expression) -> None:
    """Assert *tree* is a single READ-ONLY SELECT (or a set-op of SELECTs) — no
    DDL/DML, no multi-statement block (review FIX 2). Raises `TemplateBindError`.

    Closes the statement-kind hole: a template like `SELECT 1; DROP TABLE payroll`
    parses to a multi-statement `Block` whose `DROP` contributes no columns, so the
    footprint check would wave it through. A blueprint is a read; reject anything
    else at LOAD time (fail-closed)."""
    forbidden = next(
        (n for n in tree.walk() if isinstance(n, _FORBIDDEN_STATEMENTS)), None
    )
    if forbidden is not None:
        raise TemplateBindError(
            f"template contains a non-read-only or multi-statement construct "
            f"({type(forbidden).__name__}); a blueprint must be a single read-only SELECT"
        )
    if not isinstance(tree, _READ_ONLY_ROOTS):
        raise TemplateBindError(
            f"template root must be a SELECT (or a set-operation of SELECTs), "
            f"got {type(tree).__name__}"
        )


def contains_star(tree: exp.Expression) -> bool:
    """True iff the parsed template contains a `*` star anywhere (top-level OR in a
    subquery) — a star names ZERO columns, so it reads EVERY column while showing an
    empty footprint (review FIX 1a). The loader rejects it; a blueprint must name
    its columns."""
    return any(isinstance(node, exp.Star) for node in tree.walk())


def referenced_slots(sql_template: str) -> set[str]:
    """The set of `{slot}` names a template references (order-independent)."""
    return set(_SLOT_TOKEN.findall(sql_template))


def parse_template(sql_template: str) -> exp.Expression:
    """Parse a slot template's AST (with `{slot}` → placeholder) under the
    ClickHouse dialect (D62), WITHOUT binding — used by the corpus loader's
    write-time validation (§1.2) to prove a template parses and to read its
    column footprint. Raises `TemplateBindError` on a parse failure."""
    placeholder_sql = _SLOT_TOKEN.sub(lambda m: f":{m.group(1)}", sql_template)
    try:
        tree = sqlglot.parse_one(placeholder_sql, dialect="clickhouse")
    except Exception as exc:  # noqa: BLE001 - any parse failure is fail-closed
        raise TemplateBindError(f"template does not parse under ClickHouse dialect: {exc}") from exc
    if tree is None:
        raise TemplateBindError("template parsed to an empty AST")
    return tree


def _to_literal(name: str, value: Any) -> exp.Expression:
    """Build a TYPED sqlglot literal for *value* — the F1 injection boundary.

    The value is untrusted (came from the model/user) but becomes a typed literal
    node, so `.sql()` renders it ClickHouse-escaped, never concatenated (D10).
    """
    # `bool` before `int` (bool is an int subclass) so True/False bind as booleans.
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, float) and not math.isfinite(value):
        # inf / -inf / nan render as `inf`/`nan` identifiers under ClickHouse — NOT
        # a safe numeric literal (review FIX 4b). Fail-closed rather than emit them.
        raise TemplateBindError(f"slot {name!r} bound to a non-finite float {value!r}")
    if isinstance(value, (int, float)):
        return exp.Literal.number(value)
    if isinstance(value, str):
        return exp.Literal.string(value)
    if isinstance(value, (list, tuple)):
        if not value:
            raise TemplateBindError(f"slot {name!r} bound to an empty list — cannot form an IN set")
        return exp.Tuple(expressions=[_to_literal(name, element) for element in value])
    raise TemplateBindError(
        f"slot {name!r} bound to an unbindable value of type {type(value).__name__}"
    )


def bind_template(sql_template: str, bindings: dict[str, Any]) -> str:
    """Return *sql_template* with every `{slot}` replaced by a typed AST literal.

    Fail-closed: raises `TemplateBindError` on a parse failure, an unbound slot
    (`{slot}` present, no binding), an EXTRA binding (a key the template never
    references), or an unbindable value. The slot value NEVER touches SQL as a
    string — it is a typed literal in the regenerated AST (D10/F1).
    """
    referenced = referenced_slots(sql_template)
    provided = set(bindings)

    missing = referenced - provided
    if missing:
        raise TemplateBindError(
            f"template references slot(s) with no binding: {sorted(missing)}"
        )
    extra = provided - referenced
    if extra:
        # Fail-closed on an extra binding — a value the template does not use is a
        # contract mismatch (a stale template or a mis-filled slot set), never
        # silently dropped.
        raise TemplateBindError(
            f"binding(s) for slot(s) the template does not reference: {sorted(extra)}"
        )

    # Rewrite `{name}` → `:name` so sqlglot parses each bind site as a
    # placeholder node (`{name}` alone parses as a ClickHouse map literal — unusable).
    tree = parse_template(sql_template)

    def _replace(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Placeholder):
            name = node.args.get("this")
            if not isinstance(name, str) or name not in bindings:
                # Defensive: a placeholder the substitution produced but with no
                # binding (should be impossible given the checks above).
                raise TemplateBindError(f"unbound placeholder {name!r} in template")
            return _to_literal(name, bindings[name])
        return node

    bound = tree.transform(_replace)
    return bound.sql(dialect="clickhouse")


__all__ = [
    "TemplateBindError",
    "assert_read_only_select",
    "bind_template",
    "contains_star",
    "parse_template",
    "referenced_slots",
]
