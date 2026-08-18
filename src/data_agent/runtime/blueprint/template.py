"""blueprint/template.py — sqlglot-AST typed-literal slot binding.

`runQuery` has no bound-parameter surface, so this module realizes the D10 injection boundary
the same way `resolveValues`' `sql_builder` does: by building the value into the parsed AST as
a TYPED sqlglot literal, never by string interpolation.

Authoring shape: a `{slot_name}` token in the SQL template marks a bind site, and `{...}` is
reserved for slots. Each `{name}` is rewritten to a sqlglot colon-placeholder (`:name`), the
template is parsed under the ClickHouse dialect (D62), and every placeholder node is REPLACED
with a typed literal built from the resolved binding:

  - str            -> `exp.Literal.string(...)`   (ClickHouse-escaped: `'` -> `''`)
  - bool           -> `exp.Boolean(...)`
  - int/float      -> `exp.Literal.number(...)`
  - list/tuple     -> `exp.Tuple(...)` of per-element literals  (for `IN {codes}`)

Fail-closed: a template that does not parse, a `{slot}` with no binding, a binding for a slot
the template never references, or an unbindable value type all raise `TemplateBindError` —
the executor never emits half-bound SQL.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

import sqlglot
from sqlglot import exp

# A slot bind-site token: `{name}` where name is an identifier. Deliberately
# strict — a `{` followed by anything non-identifier is NOT treated as a slot
# (so a stray brace fails the parse loudly rather than binding silently).
_SLOT_TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
# Public alias: the S4 generalize canonicalizer reuses this exact bind-site regex
# (not a reimplementation) to translate `{slot}` → `:slot` before AST-normalizing.
SLOT_TOKEN = _SLOT_TOKEN

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
    """Assert *tree* is a single READ-ONLY SELECT (or a set-op of SELECTs) — no DDL/DML, no
        multi-statement block. Raises `TemplateBindError`.

        Closes the statement-kind hole: `SELECT 1; DROP TABLE payroll` parses to a multi-statement
        block whose `DROP` contributes no columns, so the footprint check would wave it through.
    """
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
    """True iff the parsed template contains a `*` star anywhere, top-level OR in a subquery. A
        star names ZERO columns, so it reads EVERY column while showing an empty footprint; the
        loader rejects it, and a blueprint must name its columns.
    """
    return any(isinstance(node, exp.Star) for node in tree.walk())


def referenced_slots(sql_template: str) -> set[str]:
    """The set of `{slot}` names a template references (order-independent)."""
    return set(_SLOT_TOKEN.findall(sql_template))


def parse_template(sql_template: str) -> exp.Expression:
    """Parse a slot template's AST (with `{slot}` rewritten to a placeholder) under the
        ClickHouse dialect (D62), WITHOUT binding — used by the corpus loader's write-time
        validation to prove a template parses and to read its column footprint. Raises
        `TemplateBindError` on a parse failure.
    """
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


# The reserved database a table-consume placeholder lives under in a consumer
# template (`FROM scratch.<placeholder> …`). The loader treats `scratch.*` sources
# as session-gated (D69/OQ-4 — not required in `uses`); the executor rewrites the
# placeholder table to the runtime-controlled materialized scratch table.
_SCRATCH_DB = "scratch"


def _rewrite_scratch_tables(
    tree: exp.Expression, table_bindings: dict[str, str]
) -> exp.Expression:
    """Rewrite every `scratch.<placeholder>` table token to its materialized name.

        *table_bindings* maps a placeholder table name as it appears in the consumer's FROM/JOIN
        to the BARE runtime-controlled scratch table the producing node materialized to. The
        replacement is purely STRUCTURAL — a new `exp.Identifier` the runtime built, never model
        text and never a result cell — so no injection surface is opened (D10). The `scratch`
        database and any table alias are preserved, so the consumer's aliased column references
        stay valid.

        Fail-closed WITHIN its binding map, which is narrower than it sounds: a
        `scratch.<placeholder>` reference absent from a NON-EMPTY *table_bindings* raises here,
        and that is the only case this function sees, because `bind_template` calls it under
        `if table_bindings:`. A template whose scratch sources are ENTIRELY unbacked has an empty
        binding map, skips this rewrite, and is emitted with `scratch.<placeholder>` intact —
        failing at the warehouse or the MCP's scratch ownership check rather than here.

        Measured, not inferred. Do not restore the claim that "the executor never emits a query
        pointing at a non-existent scratch table" without also moving the guard out from behind
        the `if`; closing the hole properly belongs at LOAD time — every `scratch.*` source must
        be a consumed placeholder, the converse of the corpus loader's gate (h).
    """
    def _replace(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table):
            db_node = node.args.get("db")
            db = db_node.name if db_node is not None else ""
            if db == _SCRATCH_DB:
                placeholder = node.name
                if placeholder not in table_bindings:
                    raise TemplateBindError(
                        f"scratch table placeholder {placeholder!r} has no materialized "
                        "binding — the upstream table intermediate was not produced"
                    )
                new_table = node.copy()
                new_table.set("this", exp.to_identifier(table_bindings[placeholder]))
                return new_table
        return node

    return tree.transform(_replace)


# The boolean CONNECTORS / clause holders that bound a single predicate. When an
# OPTIONAL slot is omitted, its `optional_pattern` REPLACES the whole boolean
# predicate that contains the slot's `{token}` (Slice C) — NOT the token's value.
# Walking up from the placeholder, the enclosing predicate is the highest ancestor
# whose parent is one of these connectors (or a WHERE/PREWHERE/HAVING/QUALIFY
# clause): e.g. in `A AND col = {slot}` the enclosing predicate is `col = {slot}`
# (its parent is `And`), so only that arm is replaced (`A AND TRUE`), never the
# whole `And`. `Not` is a boundary too, but replacing a predicate ANYWHERE under a
# `Not` would invert the filter's polarity — that is fail-closed
# (`_enclosing_predicate` raises), never silently emitted.
_PREDICATE_BOUNDARY: tuple[type[exp.Expression], ...] = (
    exp.And,
    exp.Or,
    exp.Not,
    exp.Where,
    exp.PreWhere,
    exp.Having,
    exp.Qualify,
)

# The filtering CLAUSES a predicate lives under. Walking up from the placeholder to
# one of these bounds the "is this predicate negated?" scan — a `Not` ANYWHERE on
# that path (not merely the immediate parent) means replacing the predicate would
# invert polarity, so it is fail-closed.
_CLAUSE_BOUNDARY: tuple[type[exp.Expression], ...] = (
    exp.Where,
    exp.PreWhere,
    exp.Having,
    exp.Qualify,
)


def _is_boolean_condition(node: exp.Expression | None) -> bool:
    """True iff *node* is a BOOLEAN-valued condition fit to be a whole predicate —
    a predicate (`=`/`IN`/`LIKE`/`IS`/…), a `TRUE`/`FALSE`, a connector
    (`AND`/`OR`/`NOT`), or a parenthesized one. A bare literal (`1`), a column, or a
    pure-arithmetic expression (`1 + 1`) is NOT a boolean predicate — replacing a
    filter with one is a silent semantics change, so it is rejected (fail-closed)."""
    if not isinstance(node, exp.Condition):
        return False
    if isinstance(node, (exp.Literal, exp.Column)):
        return False  # a bare value, not a boolean test
    if isinstance(node, exp.Paren):
        return _is_boolean_condition(node.this)
    # An arithmetic/bitwise/concat `Binary` (Add/Sub/Mul/DPipe/…) is a value, not a
    # predicate; comparisons (`EQ`/`GT`/…) are `Predicate`, boolean glue is `Connector`.
    if isinstance(node, exp.Binary) and not isinstance(node, (exp.Predicate, exp.Connector)):
        return False
    return True


def _enclosing_predicate(placeholder: exp.Placeholder) -> exp.Expression:
    """The boolean predicate subtree that CONTAINS *placeholder* — the highest ancestor reached
        before a boolean connector (`AND`/`OR`/`NOT`) or a `WHERE`/`PREWHERE`/`HAVING`/`QUALIFY`
        clause. For `col = {slot}` and `col IN {slot}` that is the `EQ`/`In` node; when the slot
        is one arm of an `AND`/`OR`, only that arm is returned.

        Fail-closed: a predicate anywhere under a `Not` — directly, or nested — raises
        `TemplateBindError`, because replacing an arm under negation inverts polarity
        (`NOT (region = {region})` -> `NOT TRUE` = no rows).
    """
    name = placeholder.args.get("this")
    # Polarity scan: ANY `Not` between the placeholder and its clause boundary means
    # the predicate is negated — fail-closed, not just the immediate-parent case.
    ancestor = placeholder.parent
    while ancestor is not None and not isinstance(ancestor, _CLAUSE_BOUNDARY):
        if isinstance(ancestor, exp.Not):
            raise TemplateBindError(
                f"optional_pattern for slot {name!r} sits under a NOT — applying it "
                "would invert the filter's polarity; fail closed"
            )
        ancestor = ancestor.parent
    node: exp.Expression = placeholder
    parent = node.parent
    while parent is not None and not isinstance(parent, _PREDICATE_BOUNDARY):
        node = parent
        parent = node.parent
    return node


def _parse_optional_pattern(name: str, pattern: str) -> exp.Expression:
    """Parse an `optional_pattern` fragment to a boolean CONDITION node (fail-closed).

        The fragment REPLACES a whole predicate, so it must itself be a self-contained boolean
        condition (`TRUE`, `col IN (...)`, `a > 0 AND b < 5`). A parse failure, a non-condition
        (a bare `SELECT`, a lone literal, pure arithmetic), or a fragment carrying ANY
        `{token}`/placeholder — a self-referential or foreign-slot pattern that could re-inject or
        loop — all raise `TemplateBindError`, so the executor degrades to the raw loop rather than
        emitting silently wrong SQL.
    """
    try:
        parsed = sqlglot.parse_one(pattern, dialect="clickhouse")
    except Exception as exc:  # noqa: BLE001 - any parse failure is fail-closed
        raise TemplateBindError(
            f"optional_pattern for slot {name!r} does not parse under ClickHouse "
            f"dialect: {exc}"
        ) from exc
    if not _is_boolean_condition(parsed):
        raise TemplateBindError(
            f"optional_pattern for slot {name!r} must be a boolean SQL condition "
            f"(e.g. 'TRUE'), got {type(parsed).__name__ if parsed else 'empty'}"
        )
    # A pattern must be SELF-CONTAINED: no bind site inside it, in EITHER surface
    # form — a colon `:placeholder` (which would re-inject every re-walk and spin the
    # apply loop forever) OR a curly `{token}` (which is NOT substituted in a pattern,
    # so it silently parses to a `map()` literal — an author's `region IN {allowed}`
    # would become garbage SQL). Reject both at parse (a poisoned/legacy record and an
    # authoring mistake are both in the threat model).
    if parsed.find(exp.Placeholder) is not None or referenced_slots(pattern):
        raise TemplateBindError(
            f"optional_pattern for slot {name!r} must not contain a slot token or "
            "placeholder — it must be a self-contained boolean fragment"
        )
    return parsed


def validate_optional_pattern(name: str, pattern: str) -> None:
    """Write-time (corpus-load) validation of an authored `optional_pattern` — the SAME gate the
        runtime applies, so a malformed, placeholder-bearing or non-Condition pattern fails LOUD
        at load instead of burning a fast-path attempt on every hit.
    """
    _parse_optional_pattern(name, pattern)


def _apply_optional_patterns(
    tree: exp.Expression, optional_patterns: Mapping[str, str]
) -> exp.Expression:
    """Replace, for every omitted OPTIONAL slot in *optional_patterns*, the whole boolean
        predicate containing its `:name` placeholder with the slot's parsed `optional_pattern`
        condition.

        Every occurrence of the placeholder is handled; replacing the enclosing predicate removes
        it, so the downstream value-binding step never sees it. A pattern whose placeholder does
        NOT appear in the tree is inert and is never parsed, so a malformed pattern for a slot
        this template does not reference cannot fail this bind.

        Fail-closed on every ambiguous shape: a malformed or self-referential pattern whose token
        IS present, a placeholder not inside a boolean predicate, a predicate under a `NOT`, or a
        predicate SHARING its subtree with any OTHER bind site — where replacing the whole
        predicate would silently DROP that sibling.
    """
    # Only patterns whose `:name` placeholder actually appears are applied (and so
    # only they are parsed) — a pattern for an unreferenced token is a no-op.
    present = {
        name
        for placeholder in tree.find_all(exp.Placeholder)
        if isinstance((name := placeholder.args.get("this")), str) and name in optional_patterns
    }
    if not present:
        return tree
    parsed_patterns = {
        name: _parse_optional_pattern(name, optional_patterns[name]) for name in present
    }
    # Re-walk after each replacement: replacing a predicate mutates the tree, so a
    # fresh `find_all` avoids acting on a detached node. Each replacement strictly
    # REMOVES one applicable placeholder (patterns are placeholder-free), so the
    # loop terminates — but hard-cap at the initial applicable count as a
    # belt-and-braces guard against an unforeseen re-insertion (fail-closed).
    max_iterations = sum(
        1
        for placeholder in tree.find_all(exp.Placeholder)
        if placeholder.args.get("this") in parsed_patterns
    )
    for _ in range(max_iterations):
        target: exp.Placeholder | None = None
        for placeholder in tree.find_all(exp.Placeholder):
            name = placeholder.args.get("this")
            if isinstance(name, str) and name in parsed_patterns:
                target = placeholder
                break
        if target is None:
            return tree
        name = target.args["this"]
        predicate = _enclosing_predicate(target)
        if predicate is tree or not isinstance(predicate, exp.Condition):
            # The placeholder is not inside a boolean predicate (e.g. it sits in a
            # projection) — an optional_pattern cannot meaningfully replace it.
            raise TemplateBindError(
                f"optional_pattern for slot {name!r} could not locate an enclosing "
                "boolean predicate to replace"
            )
        # The predicate must contain NO bind site other than this token, or
        # replacing the whole predicate would silently drop it (a provided value in
        # `x BETWEEN {lo} AND {hi}`, or a second omitted token). ANY non-target
        # placeholder counts — NAMED (`:other`) OR anonymous (a bare `?`, whose
        # `this` is None) — else `x BETWEEN ? AND {lo}` would drop the `?`. Fail closed.
        foreign = sorted(
            {
                f"{{{other}}}" if isinstance((other := ph.args.get("this")), str) else "?"
                for ph in predicate.find_all(exp.Placeholder)
                if ph.args.get("this") != name
            }
        )
        if foreign:
            raise TemplateBindError(
                f"optional_pattern for slot {name!r} shares its predicate with other "
                f"bind site(s) {foreign}; replacing it would drop them — fail closed"
            )
        predicate.replace(parsed_patterns[name].copy())
    if any(
        placeholder.args.get("this") in parsed_patterns
        for placeholder in tree.find_all(exp.Placeholder)
    ):
        # Cap hit with an applicable placeholder still present — an unexpected
        # re-insertion. Never emit half-applied SQL.
        raise TemplateBindError(
            "optional_pattern application did not converge — fail closed"
        )
    return tree


def bind_template(
    sql_template: str,
    bindings: dict[str, Any],
    *,
    table_bindings: dict[str, str] | None = None,
    optional_patterns: Mapping[str, str] | None = None,
) -> str:
    """Return *sql_template* with every `{slot}` replaced by a typed AST literal.

        Fail-closed: raises `TemplateBindError` on a parse failure, an unbound slot, an EXTRA
        binding (a key the template never references), or an unbindable value. The slot value
        NEVER touches SQL as a string — it is a typed literal in the regenerated AST (D10).

        *table_bindings* maps a `scratch.<placeholder>` table token to the BARE materialized
        scratch table name, applied as an AST identifier rewrite BEFORE the literal binding. Table
        placeholders are NOT `{slot}` tokens, so they are invisible to the missing/extra slot
        checks and the two mechanisms compose cleanly.

        *optional_patterns* maps an OMITTED optional slot's `{token}` — a scalar slot's `{name}`,
        or each of a `period_range`'s `{name}_start`/`{name}_end` — to its `optional_pattern`, a
        boolean SQL fragment that REPLACES the whole predicate containing that token (so
        `WHERE region = {region}` with pattern `TRUE` becomes `WHERE TRUE`). Such a token is
        SATISFIED by its pattern, so it is excluded from the missing/extra checks and removed
        before the value-binding step. Fail-closed on any ambiguous shape.
    """
    referenced = referenced_slots(sql_template)
    provided = set(bindings)
    pattern_names = set(optional_patterns or ())

    # An omitted optional slot with an `optional_pattern` is fulfilled by the
    # pattern, not by a value — exclude it from `missing` (it has no binding by
    # design) and it can never be `extra` (it is not in `bindings`).
    missing = referenced - provided - pattern_names
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

    # Rewrite table-consume placeholders (scratch.<placeholder> → the materialized
    # scratch table) FIRST — a structural identifier swap, injection-safe.
    # NOTE: this `if` is also what scopes `_rewrite_scratch_tables`' unbound-placeholder
    # guard. With no table-consumes the map is empty, the rewrite is skipped, and a
    # template reading `scratch.*` anyway is emitted verbatim (measured). Documented on
    # that function; the real fix is a load-time gate, not widening this condition.
    if table_bindings:
        tree = _rewrite_scratch_tables(tree, table_bindings)

    # Slice C: apply each omitted optional slot's `optional_pattern` — replacing the
    # enclosing predicate removes that `:name` placeholder BEFORE literal binding.
    if optional_patterns:
        tree = _apply_optional_patterns(tree, optional_patterns)

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
    "SLOT_TOKEN",
    "TemplateBindError",
    "assert_read_only_select",
    "bind_template",
    "contains_star",
    "parse_template",
    "referenced_slots",
    "validate_optional_pattern",
]
