"""
Column-provenance extractor — D52, D62, D63, D69.

Public API
----------
    extract_column_provenance(sql, catalog_schema, *, session_id=None)
        -> frozenset[tuple[str, str]]

Returns the USES set as frozenset of (database.table, column) pairs (D69/OQ-3).

Fail behavior (D63):
    Any parse failure, qualification failure, or lambda-body-not-proved-walked
    (D69/OQ-1) raises ProvenanceExtractionError.  Callers map this to their own
    fail behavior (D57/D63 -> reject query; D44 -> drop trail entry; D48 -> skip
    hard key; D35 -> fail-to-review).

Precondition (D69/OQ-5):
    Caller must NEVER pass an EXPLAIN statement.  Enforced at the call site.

Scratch tables (D69/OQ-4):
    Tables in the `scratch` database are NOT column-scope-checked.  Their column
    references are accepted without catalog qualification.  If `session_id` is
    supplied, the scratch table name is validated against `s_<session_id>_*`; a
    mismatch raises ScratchSessionError (a subclass of ProvenanceExtractionError).
    If `session_id` is None, scratch table names are not validated (useful in unit
    tests that only test column extraction, not session isolation).

SELECT * expansion (D69/OQ-2):
    qualify_columns from sqlglot expands SELECT * into individual columns when fed
    the catalog schema.  We do NOT need separate star handling — the expansion
    happens inside the optimizer pass.  If qualify_columns cannot expand because a
    table is not in the schema, we catch the resulting unresolved references as a
    fail-closed condition.

Lambda / higher-order bodies (D69/OQ-1):
    EMPIRICAL FINDING: sqlglot 30.x DOES walk lambda argument lists (the arguments
    to arrayMap, arrayFilter, etc.) when traversing the AST with find_all(Column).
    When the lambda body itself contains a column reference (e.g.
    `arrayMap(x -> x * Amount, ...)` where Amount is a real column), sqlglot finds
    that column during AST traversal.  The lambda body `x -> expr` has `expr` as
    the `this` argument of the Lambda node, and find_all recurses into it.
    We verify this with an active check: for every Lambda node in the qualified AST,
    we walk its body for Column nodes and verify they appear in the extracted set.
    If any catalogued column appears in a lambda body but NOT in the extracted set,
    we raise ProvenanceExtractionError (fail-closed, no silent skip).

Algorithm
---------
1. Guard: empty/whitespace input -> fail-closed.
2. Parse with sqlglot ClickHouse dialect (parse_one, raises on multi-statement).
3. Reject non-SELECT/WITH statement kinds (DDL, INSERT, SHOW, Block/stacked).
4. qualify_tables: fill in database prefix for bare table references.
5. Build a mapping: alias -> (real_table_name, database), CTE names excluded.
6. qualify_columns: resolve unqualified column refs to their owning table.
   This also expands SELECT * into individual columns (D69/OQ-2).
7. Walk all Column nodes; for each, resolve its table part through the alias map.
8. Fail-closed if a column's table maps to an uncatalogued non-scratch table.
9. Lambda-body coverage check (D69/OQ-1).
10. Return frozenset.
"""

from __future__ import annotations

import re

import sqlglot
import sqlglot.expressions as exp
from sqlglot.optimizer.qualify_columns import qualify_columns
from sqlglot.optimizer.qualify_tables import qualify_tables

# ---------------------------------------------------------------------------
# Public exception hierarchy
# ---------------------------------------------------------------------------


class ProvenanceExtractionError(Exception):
    """Raised when column provenance cannot be reliably extracted.

    Consumers must treat this as fail-closed (D63):
      - D57/live runQuery -> reject query + alert
      - D44/replay trail  -> drop trail entry
      - D48/dedup         -> skip hard key, fall to soft embedding
      - D35/template      -> fail-to-review
    """


class ScratchSessionError(ProvenanceExtractionError):
    """Raised when a scratch table reference does not match the injected session_id (D64)."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCRATCH_DB = "scratch"
# Scratch table naming pattern: s_<sessionId>_<anything> (D64)
# We match this by checking the prefix `s_<session_id>_` directly rather than
# using a regex, because session_id can itself contain underscores.
# The general validity check (must look like a scratch table at all) uses this regex:
_SCRATCH_TABLE_GENERIC_RE = re.compile(r"^s_.+_.+$")

# Statement kinds that are blocked upstream (read-only gate) but which we also
# reject explicitly in case the extractor is called incorrectly (D21, D63).
_BLOCKED_STATEMENT_KINDS: tuple[type[exp.Expression], ...] = (
    exp.Create,
    exp.Drop,
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Alter,
    exp.Block,   # stacked statements (multi-statement injection)
)

# Default database name; used when qualify_tables fills in the database prefix.
_DEFAULT_DB = "dbpcm_warehouse"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_nested_schema(
    catalog_schema: dict[str, dict[str, str]],
) -> dict[str, dict[str, dict[str, str]]]:
    """Convert flat `database.table` keys to sqlglot's nested {db: {table: {col: type}}} form."""
    nested: dict[str, dict[str, dict[str, str]]] = {}
    for qualified_key, columns in catalog_schema.items():
        if "." in qualified_key:
            database, table = qualified_key.split(".", 1)
        else:
            # Bare table name without database prefix — use a placeholder.
            # This should not occur in production catalog data.
            database = "unknown"
            table = qualified_key
        nested.setdefault(database, {})[table] = columns
    return nested


def _collect_virtual_table_names(ast: exp.Expression) -> set[str]:
    """Return the set of virtual table names in the AST.

    Virtual tables are tables that are not in the catalog but represent
    intermediate query results:
      - CTE names (WITH cte_name AS (...))
      - Subquery aliases (FROM (...) AS subquery_name)

    Column references to virtual tables are skipped in the USES set because
    the real base-table columns are already extracted from within the CTE/subquery
    definitions themselves.
    """
    names: set[str] = set()
    # CTE definitions
    for cte_node in ast.find_all(exp.CTE):
        alias = cte_node.alias
        if alias:
            names.add(alias)
    # Subquery aliases in FROM clauses
    for subquery_node in ast.find_all(exp.Subquery):
        alias_node = subquery_node.args.get("alias")
        if alias_node:
            alias = alias_node.name
            if alias:
                names.add(alias)
    return names


def _build_alias_map(
    ast: exp.Expression,
    cte_names: set[str],
    catalog_schema: dict[str, dict[str, str]],
    session_id: str | None,
) -> dict[str, tuple[str, str]]:
    """Build alias -> (table_name, database) mapping for all real (non-CTE) table refs.

    After qualify_tables has run, all Table nodes should have a db attribute.
    We map both the alias (if any) and the bare table name to (table, db) so
    that column resolution can proceed.

    SIDE EFFECT on `cte_names`:
        When a Table node's name is a CTE name and the Table node itself has an alias
        (e.g. ``earn_rows AS er``), this function adds the alias to `cte_names` so that
        step 8 can recognise columns attributed to that alias as CTE projection references
        and skip them without raising.  The caller must not pass a `cte_names` set that
        it wants to remain unchanged after this call.

    Raises:
        ScratchSessionError: if a scratch table reference doesn't match session_id.
        ProvenanceExtractionError: if a referenced table is not in catalog and is
            not a scratch table (fail-closed for uncatalogued warehouse tables).
    """
    alias_map: dict[str, tuple[str, str]] = {}

    for tbl_node in ast.find_all(exp.Table):
        tbl_name = tbl_node.name or ""
        if not tbl_name:
            continue

        db_node = tbl_node.args.get("db")
        tbl_db = db_node.name if db_node else _DEFAULT_DB

        alias_node = tbl_node.args.get("alias")
        alias = alias_node.name if alias_node else None

        # Skip CTE references — they are virtual tables, not real catalog tables.
        # We also add the alias -> CTE_name mapping so that column references
        # like `er.Amount` (where `er` is an alias for CTE `earn_rows`) are
        # correctly identified as CTE-attributed and skipped in step 8.
        if tbl_name in cte_names:
            if alias:
                # Map CTE alias -> CTE name so step 8 knows it's a CTE ref
                cte_names.add(alias)
            continue
        if alias and alias in cte_names:
            continue

        # Handle scratch tables
        if tbl_db == _SCRATCH_DB:
            _validate_scratch_name(tbl_name, session_id)
            # Map alias -> (tbl_name, scratch_db)
            if alias:
                alias_map[alias] = (tbl_name, tbl_db)
            alias_map[tbl_name] = (tbl_name, tbl_db)
            continue

        # Warehouse table: validate it's in the catalog (case-sensitive, exact match).
        # ClickHouse identifiers are case-sensitive; `Payroll` and `payroll` are
        # genuinely different table names.  We never do case-insensitive fallback —
        # an uncatalogued table reference is an unverifiable query (D63, fail-closed).
        qualified_key = f"{tbl_db}.{tbl_name}"
        if qualified_key not in catalog_schema:
            raise ProvenanceExtractionError(
                f"Table '{qualified_key}' is not in the catalog schema. "
                "Identifier matching is case-sensitive and exact (ClickHouse semantics). "
                "Cannot qualify columns — fail-closed (D63)."
            )
        if alias:
            alias_map[alias] = (tbl_name, tbl_db)
        alias_map[tbl_name] = (tbl_name, tbl_db)

    return alias_map


def _validate_scratch_name(tbl_name: str, session_id: str | None) -> None:
    """Validate scratch table name against session_id pattern (D64/OQ-4).

    A valid own-session scratch table name must:
      1. Match the generic scratch pattern: s_<anything>_<anything>
      2. Start with `s_<session_id>_` (when session_id is provided)

    session_id can itself contain underscores (e.g. 'sess_abc123'), so we do
    a prefix check rather than a regex group match.
    """
    if session_id is None:
        # No session context; skip validation (useful for unit tests that don't
        # test session isolation, only column extraction)
        return

    expected_prefix = f"s_{session_id}_"
    if not tbl_name.startswith(expected_prefix) or len(tbl_name) <= len(expected_prefix):
        raise ScratchSessionError(
            f"Scratch table '{_SCRATCH_DB}.{tbl_name}' does not match "
            f"session_id '{session_id}' (expected {_SCRATCH_DB}.s_{session_id}_<file> pattern). "
            "Cross-session or malformed scratch access rejected (D64)."
        )


def _has_select_star(ast: exp.Expression) -> bool:
    """Return True if the AST contains an unresolved star in a select list.

    Two forms of star are detected after qualify_columns has run:
      - ``SELECT *``   — Star node whose parent is exp.Select
      - ``SELECT t.*`` — Star node whose parent is exp.Column (table-qualified star)

    ``COUNT(*)`` is intentionally excluded: its Star parent is an Anonymous function
    or Count node, neither of which matches the above conditions.

    After qualify_columns, any remaining Star in one of these forms means expansion
    failed (uncatalogued table or unresolvable table-qualified star) — fail-closed
    (D69/OQ-2, D63).
    """
    for star in ast.find_all(exp.Star):
        parent = star.parent
        if parent is not None and isinstance(parent, (exp.Select, exp.Column)):
            return True
    return False


def _is_output_alias_reference(col_node: exp.Column) -> bool:
    """Return True if an unresolved unqualified column is a provable SELECT-list alias ref.

    D70 fail-closed distinction (case (C) of the step-8 unqualified branch).

    After ``qualify_columns``, an unqualified column with ``col.table == ''`` and a name
    absent from the catalog is ambiguous: it is EITHER

      (a) a reference to a computed SELECT-list output alias of the enclosing query
          (e.g. ``count() AS c ... ORDER BY c`` or ``SUM(x) AS dept_earn ... GROUP BY
          dept_earn``) — a query-internal derived name, no new data access; safe to skip;
      OR
      (b) a genuinely unresolvable base-table column (e.g. a bare column absent from the
          single source table's catalog) — an unverifiable reference that MUST fail closed.

    Both leave ``col.table == ''``.  We distinguish them structurally:

      1. LOCATION.  ``qualify_columns`` auto-wraps every projection in an identity Alias
         (``NonExistentCol`` becomes ``NonExistentCol AS NonExistentCol``), so we cannot
         rely on "is there an alias with this name".  Instead we check WHERE the column
         node sits relative to its enclosing SELECT.  A column that IS a projection output
         (arg_key ``expressions``) cannot be *referencing* an alias — it is the (broken)
         output itself → not an alias reference → fail closed.  Only a column in
         GROUP BY / ORDER BY / HAVING / WHERE (any non-projection clause) may point at a
         declared SELECT-list alias.
      2. DECLARATION.  The name must match an alias explicitly declared in the enclosing
         SELECT's projection list.

    Only when BOTH hold is this a legitimate alias reference (skip).  Otherwise the caller
    fails closed.  A correlated outer-query reference that is genuinely unresolvable will
    fail this test and fail closed — the safe direction (D63: a false-reject is safe, a
    false-accept is the fail-open bug being fixed).
    """
    sel = col_node.find_ancestor(exp.Select)
    if sel is None:
        return False

    # Find the direct child of `sel` that contains this column, and its clause (arg_key).
    node: exp.Expression = col_node
    while node.parent is not None and node.parent is not sel:
        node = node.parent
    if node.parent is not sel:
        return False
    if node.arg_key == "expressions":
        # The column is itself a projection output — an unresolvable output, not a
        # reference to some other output alias.  Fail closed.
        return False

    alias_names = {
        proj.alias
        for proj in sel.expressions
        if isinstance(proj, exp.Alias) and proj.alias
    }
    return col_node.name in alias_names


def _references_only_scratch_sources(col_node: exp.Column) -> bool:
    """Return True if the column's enclosing SELECT draws ONLY from scratch tables.

    Scratch tables (D69/OQ-4) are NOT in the catalog, so ``qualify_columns`` cannot
    attribute a bare (unqualified) column to them — such a column is left with
    ``col.table == ''`` exactly like a genuinely-unresolvable warehouse column.  We
    must NOT fail closed on a legitimate scratch column reference.

    An unqualified, unresolved column is a legitimate scratch reference ONLY when every
    direct base-table source of its enclosing SELECT is a scratch table (db == ``scratch``).
    If ANY direct source is a catalogued warehouse table, ``qualify_columns`` would have
    resolved a real column against it; an unqualified column that stayed unresolved is
    then genuinely unverifiable and must fail closed (the D70 bug).  A derived/subquery
    source also yields fail-closed (conservative — D63: a false-reject is safe).

    "Direct base-table source" = a Table node whose nearest enclosing SELECT is the same
    SELECT that owns this column (tables inside nested subqueries belong to those
    subqueries, not to this SELECT).
    """
    sel = col_node.find_ancestor(exp.Select)
    if sel is None:
        return False
    direct_tables = [
        tbl for tbl in sel.find_all(exp.Table) if tbl.find_ancestor(exp.Select) is sel
    ]
    if not direct_tables:
        return False
    for tbl in direct_tables:
        db_node = tbl.args.get("db")
        tbl_db = db_node.name if db_node else ""
        if tbl_db != _SCRATCH_DB:
            return False
    return True


def _bound_lambda_param_names(lambda_node: exp.Lambda) -> set[str]:
    """Return the parameter names bound by this lambda AND all enclosing lambdas.

    A lambda body may reference a parameter bound by an outer lambda (nested
    higher-order calls), so the in-scope parameter set is the union of this lambda's
    parameters and those of every ancestor Lambda.  These names are NOT column
    references and must be excluded from the fail-closed lambda-body coverage check.
    """
    names: set[str] = set()
    node: exp.Expression | None = lambda_node
    while node is not None:
        if isinstance(node, exp.Lambda):
            for param in node.expressions:
                param_name = param.name or ""
                if param_name:
                    names.add(param_name)
        node = node.parent
    return names


def _check_lambda_body_coverage(
    ast: exp.Expression,
    uses: set[tuple[str, str]],
    catalog_schema: dict[str, dict[str, str]],
) -> None:
    """Verify lambda bodies were fully walked by sqlglot (D69/OQ-1).

    EMPIRICAL FINDINGS (sqlglot 30.x):
    - Lambda nodes have structure: Lambda(this=<body_expr>, expressions=[<param_identifiers>]).
    - Single-arg: ``x -> expr`` → one Identifier in expressions, body in `this`.
    - Multi-arg:  ``(acc, x) -> expr`` → multiple Identifiers in expressions, body in `this`.
    - sqlglot DOES recurse into lambda bodies during find_all() and qualify_columns().
    - When a catalog column appears directly in the body (e.g. ``x -> x * Amount``),
      find_all(exp.Column) on the full AST returns it, and qualify_columns resolves it.

    STRUCTURAL CHECK (not the tautological find_all approach):
    For every Lambda node in the qualified AST we assert:
      1. The body (`this` arg) is not None — a missing body is a parse gap.
      2. The parameter list (``expressions``) is non-empty — a lambda without parameters
         is structurally unsound.
    Then we walk the body directly for Column nodes whose names appear in the catalog
    and verify each is present in the extracted `uses` set.  If any is missing the
    step-8 column walk silently dropped it — fail-closed.

    Note: ``lambda_node.expressions`` are the PARAMETER identifiers (e.g. [x], [acc, x]).
    They are not column references.  We do NOT check them for catalog membership.

    FAIL-CLOSED COVERAGE (D70):
    Every Column node in a lambda body must be accounted for.  A body column is legitimate
    ONLY if it is (a) a lambda parameter bound by this or an enclosing lambda, or (b) a
    resolved column already present in the extracted USES set (a real catalog or scratch
    column that step 8 captured).  ANY other body column — a catalog column the step-8
    walk silently dropped, or an uncatalogued / mis-cased identifier — is unverifiable and
    MUST fail closed.  The previous implementation only examined columns that were in the
    catalog, so an uncatalogued lambda-body column (e.g. ``arrayMap(x -> x + BadCol, ...)``
    with ``BadCol`` absent from the catalog) was silently dropped — a D70 fail-open.

    If no Lambda nodes exist, this check is a no-op.
    """
    extracted_col_names = {col_name for _, col_name in uses}

    for lambda_node in ast.find_all(exp.Lambda):
        # Structural assertion 1: body must be present
        body = lambda_node.args.get("this")
        if body is None:
            raise ProvenanceExtractionError(
                "Lambda node has no body expression (`this` arg is None) — "
                "sqlglot failed to parse the lambda body. "
                "Cannot verify column coverage (D69/OQ-1, fail-closed)."
            )

        # Structural assertion 2: parameter list must be non-empty
        params = lambda_node.expressions  # list of Identifier nodes
        if not params:
            raise ProvenanceExtractionError(
                "Lambda node has an empty parameter list — "
                "structurally unsound lambda expression. "
                "Cannot verify column coverage (D69/OQ-1, fail-closed)."
            )

        bound_params = _bound_lambda_param_names(lambda_node)

        # Walk this lambda's own body for Column nodes.  Columns belonging to a nested
        # lambda are handled when that nested lambda is visited in its own iteration.
        for col_node in body.find_all(exp.Column):
            if col_node.find_ancestor(exp.Lambda) is not lambda_node:
                continue
            col_name = col_node.name or ""
            if not col_name:
                continue
            if col_name in bound_params:
                # A lambda-bound parameter is not a column reference.
                continue
            if col_name in extracted_col_names:
                # A resolved catalog/scratch column already captured by step 8.
                continue
            # Anything else is an unresolved, non-parameter column in a lambda body:
            # either a catalog column the step-8 walk dropped, or an uncatalogued /
            # mis-cased identifier.  Both are unverifiable — fail closed.
            raise ProvenanceExtractionError(
                f"Lambda body references column '{col_name}' which is neither a lambda "
                "parameter nor present in the extracted USES set — it is an unresolvable "
                "or silently-dropped column reference (D69/OQ-1, D70, fail-closed)."
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_column_provenance(
    sql: str,
    catalog_schema: dict[str, dict[str, str]],
    *,
    session_id: str | None = None,
) -> frozenset[tuple[str, str]]:
    """Extract the column USES set from a ClickHouse SQL query.

    Parameters
    ----------
    sql:
        A single SELECT/WITH statement.  Multi-statement input (semicolon-separated)
        raises ProvenanceExtractionError (D63: fail-closed).
    catalog_schema:
        Dict keyed at ``database.table`` granularity (D69/OQ-3), mapping to
        {column: type_string}.  Produced by catalog.loader.load_catalog_from_dir().
    session_id:
        Optional session identifier for scratch-table isolation checks (D64/OQ-4).
        When supplied, any scratch table reference whose name does not match
        ``s_<session_id>_*`` raises ScratchSessionError.

    Returns
    -------
    frozenset[tuple[str, str]]
        Set of (database.table, column) pairs representing every column referenced
        by the query (USES semantics — includes WHERE, JOIN, GROUP BY, ORDER BY,
        window partitions, CASE predicates, lambda bodies, subqueries, CTEs, etc.).

    Raises
    ------
    ProvenanceExtractionError
        On any parse failure, qualification failure, lambda-body coverage failure,
        non-SELECT/WITH input, or uncatalogued table reference.
    ScratchSessionError (subclass of ProvenanceExtractionError)
        On cross-session or malformed scratch table access.

    Precondition (D69/OQ-5)
    -----------------------
    Caller must NEVER pass an EXPLAIN statement.  EXPLAIN is handled by a separate
    code path (explainQuery tool); it never reaches runQuery or this extractor.
    If called with an EXPLAIN statement, this function raises ProvenanceExtractionError.
    """
    # ------------------------------------------------------------------
    # Step 1: Guard — empty / whitespace-only input (D63)
    # ------------------------------------------------------------------
    if not sql or not sql.strip():
        raise ProvenanceExtractionError(
            "Empty or whitespace-only SQL input — fail-closed (D63)."
        )

    # ------------------------------------------------------------------
    # Step 2: Parse with ClickHouse dialect (D62)
    #         parse_one does NOT raise on multi-statement (semicolon-separated)
    #         input — it returns an exp.Block node containing both statements.
    #         Multi-statement injection is caught in Step 3 via the _BLOCKED_STATEMENT_KINDS
    #         check which includes exp.Block.  parse_one raises ParseError only on
    #         syntactically invalid input that it cannot tokenise at all.
    # ------------------------------------------------------------------
    try:
        ast = sqlglot.parse_one(
            sql,
            dialect="clickhouse",
            error_level=sqlglot.ErrorLevel.RAISE,
        )
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError) as exc:
        raise ProvenanceExtractionError(
            f"sqlglot ClickHouse-dialect parse failed — fail-closed (D63): {exc}"
        ) from exc
    except Exception as exc:
        raise ProvenanceExtractionError(
            f"Unexpected parse error — fail-closed (D63): {exc}"
        ) from exc

    if ast is None:
        raise ProvenanceExtractionError(
            "sqlglot returned None for SQL input — fail-closed (D63)."
        )

    # ------------------------------------------------------------------
    # Step 3: Reject blocked statement kinds (D21, D63, OQ-5)
    # ------------------------------------------------------------------
    if isinstance(ast, _BLOCKED_STATEMENT_KINDS):
        raise ProvenanceExtractionError(
            f"{type(ast).__name__} is not a valid runQuery target "
            "(DDL, write, or stacked statements are blocked) — fail-closed (D63, D21)."
        )

    # EXPLAIN and SHOW fall through as exp.Command — reject both
    if isinstance(ast, exp.Command):
        raise ProvenanceExtractionError(
            "Command statement (EXPLAIN / SHOW / other) reached the provenance "
            "extractor — fail-closed (D63). EXPLAIN is a caller precondition "
            "violation (D69/OQ-5); SHOW produces no column USES set."
        )

    # Verify the root is a SELECT, WITH/CTE, or UNION
    if not isinstance(ast, (exp.Select, exp.Union, exp.With, exp.Subquery)):
        raise ProvenanceExtractionError(
            f"Unexpected top-level statement type {type(ast).__name__} "
            "(expected SELECT / WITH / UNION) — fail-closed (D63)."
        )

    # ------------------------------------------------------------------
    # Step 4: qualify_tables — fill database prefix for bare table names
    # ------------------------------------------------------------------
    # We infer the default database from the catalog_schema keys.
    # If all keys share one database prefix, that's the default.
    # Otherwise, use _DEFAULT_DB as a safe fallback.
    default_db = _infer_default_db(catalog_schema)

    try:
        ast_qt = qualify_tables(ast.copy(), db=default_db, dialect="clickhouse")
    except Exception as exc:
        raise ProvenanceExtractionError(
            f"qualify_tables failed — fail-closed (D63): {exc}"
        ) from exc

    # ------------------------------------------------------------------
    # Step 5: Collect CTE names (virtual tables; excluded from alias map)
    # ------------------------------------------------------------------
    cte_names = _collect_virtual_table_names(ast_qt)

    # ------------------------------------------------------------------
    # Step 6: Build alias -> (table, database) map; validate scratch names
    # ------------------------------------------------------------------
    try:
        alias_map = _build_alias_map(ast_qt, cte_names, catalog_schema, session_id)
    except (ScratchSessionError, ProvenanceExtractionError):
        raise
    except Exception as exc:
        raise ProvenanceExtractionError(
            f"Failed to build table alias map — fail-closed (D63): {exc}"
        ) from exc

    # ------------------------------------------------------------------
    # Step 6b: Check for table-valued functions (table functions with empty
    #           table names, e.g. generateRandom, numbers, system.query_log).
    #           These are not in the catalog and cannot have columns qualified.
    #           Fail-closed (D63).
    # ------------------------------------------------------------------
    for tbl_node in ast_qt.find_all(exp.Table):
        tbl_name = (tbl_node.name or "").strip()
        if not tbl_name:
            # Table with no name — likely a table-valued function (generateRandom, etc.)
            raise ProvenanceExtractionError(
                "Query references a table-valued function or anonymous table source. "
                "Column provenance cannot be extracted — fail-closed (D63)."
            )

    # ------------------------------------------------------------------
    # Step 7: qualify_columns — resolve unqualified column refs to their table
    #         and expand SELECT * into individual columns (D69/OQ-2)
    # ------------------------------------------------------------------
    nested_schema = _build_nested_schema(catalog_schema)

    try:
        ast_qc = qualify_columns(
            ast_qt,
            schema=nested_schema,
            dialect="clickhouse",
        )
    except Exception as exc:
        raise ProvenanceExtractionError(
            f"qualify_columns failed — fail-closed (D63): {exc}"
        ) from exc

    # After qualify_columns, any remaining SELECT-list Star node (i.e. a Star
    # directly in a Select expression list, not inside COUNT(*)) indicates that
    # SELECT * was NOT fully expanded (e.g. uncatalogued table) -> fail-closed.
    # We distinguish SELECT * stars from COUNT(*) stars by checking the parent.
    if _has_select_star(ast_qc):
        raise ProvenanceExtractionError(
            "SELECT * could not be fully expanded — at least one referenced table "
            "is not in the catalog schema. Fail-closed (D69/OQ-2, D63)."
        )

    # ------------------------------------------------------------------
    # Step 8: Walk all Column nodes and build the USES set
    # ------------------------------------------------------------------

    # Pre-compute column name sets for the empty-table-attribution branch.
    # all_catalog_column_names: exact (case-sensitive) column names in the catalog.
    # all_catalog_column_names_lower: lowercased versions — used to detect case-mismatches.
    # A column name that is absent from exact set but present in the lowercased set is a
    # case-mismatched identifier (e.g. `amount` vs `Amount`) — fail-closed.
    all_catalog_column_names: set[str] = set()
    for _cols in catalog_schema.values():
        all_catalog_column_names.update(_cols.keys())
    all_catalog_column_names_lower: set[str] = {n.lower() for n in all_catalog_column_names}

    uses: set[tuple[str, str]] = set()

    for col_node in ast_qc.find_all(exp.Column):
        col_name = col_node.name or ""
        if not col_name:
            continue

        col_table = col_node.table or ""
        col_db_node = col_node.args.get("db")
        col_db = col_db_node.name if col_db_node else ""

        if col_db and col_table:
            # Fully qualified by the parser/qualify step: db.table.column
            uses.add((f"{col_db}.{col_table}", col_name))
            continue

        if not col_table:
            # Column with no table attribution after qualify_columns.
            #
            # qualify_columns leaves col.table empty when it cannot resolve the column
            # to any table in the schema.  Three scenarios:
            #
            # (A) Catalog column name that failed resolution — case mismatch (e.g.
            #     `amount` vs `Amount`) or column not in the referenced table.
            #     The real access was NOT captured; this is an unverifiable reference.
            #     Action: raise fail-closed (D63, case-sensitivity contract).
            #
            # (B) Catalog column name that failed resolution in ORDER BY/GROUP BY
            #     because it shadows a SELECT-list alias of the same name.  The actual
            #     column access WAS already captured (via the SELECT-list reference
            #     which qualify_columns DID resolve).
            #     Action: raise — this is still a case-mismatch unless the column is
            #     already in `uses`.  If it is already in `uses`, the shadow reference
            #     adds no new data access; skip safely.
            #
            # (C) A computed alias name (e.g. `dept_earn` from `SUM(...) AS dept_earn`)
            #     referenced in ORDER BY / GROUP BY.  This name is not a real catalog
            #     column — it is a query-internal derived name.  No data access occurs.
            #     Action: skip (not a data access; the source columns were captured
            #     from within the expression that defines the alias).
            #
            # Distinguishing rule:
            #   - col_name exactly in catalog AND not yet in uses → case (A) → raise
            #   - col_name exactly in catalog AND already in uses  → case (B) → skip
            #   - col_name NOT in catalog exactly, but matches a catalog name
            #     case-insensitively                               → case-mismatch → raise
            #   - col_name not in catalog (exact or case-fold), AND it is a provable
            #     SELECT-list output alias referenced in GROUP BY / ORDER BY / HAVING
            #                                                       → case (C) → skip
            #   - col_name not in catalog (exact or case-fold), AND it is NOT such an
            #     alias reference (e.g. a bare column absent from the single source
            #     table's catalog, or a dropped lambda/projection column)
            #                                                       → case (D) → raise
            #
            # The case-insensitive match check catches identifiers like `amount` when
            # the catalog has `Amount`.  qualify_columns could not resolve `amount`
            # because ClickHouse identifier matching is case-sensitive; the reference
            # is genuinely unresolvable and must fail-closed (D63, case-sensitivity contract).
            #
            # D70 FIX: previously the final `else` unconditionally SKIPPED any name absent
            # from the catalog, treating it as a computed alias.  But a bare unresolvable
            # base-table column (absent from the single source table's catalog) is
            # indistinguishable from an alias by name alone — both leave col.table == ''.
            # That silently DROPPED real unresolvable columns (understated USES = fail-open;
            # the D44 replay filter then treats it as ⊆ any scope).  We now require a
            # PROVABLE output-alias reference (see _is_output_alias_reference); anything
            # else fails closed.
            extracted_col_names_so_far = {cn for _, cn in uses}
            if col_name in all_catalog_column_names:
                if col_name not in extracted_col_names_so_far:
                    # Exact catalog match but not yet captured — unresolved real column (case A)
                    raise ProvenanceExtractionError(
                        f"Column '{col_name}' could not be attributed to any table after "
                        "qualify_columns — it exists in the catalog but was not resolved. "
                        "Fail-closed (D63)."
                    )
                # else: case (B) — already captured via the real access; skip the alias ref
            elif col_name.lower() in all_catalog_column_names_lower:
                # Case-mismatch: `amount` vs `Amount`, `registerType` vs `RegisterType`, etc.
                # The identifier looks like a catalog column name but uses wrong case.
                # ClickHouse is case-sensitive; this reference is unresolvable. Fail-closed.
                raise ProvenanceExtractionError(
                    f"Column '{col_name}' could not be attributed to any table after "
                    "qualify_columns — a catalog column with the same name exists under "
                    "different casing (ClickHouse identifiers are case-sensitive). "
                    "Fail-closed (D63)."
                )
            elif not (
                _is_output_alias_reference(col_node)
                or _references_only_scratch_sources(col_node)
            ):
                # case (D) — genuinely unresolvable unqualified column.  It is neither a
                # catalog column, a provable SELECT-list output alias reference, nor a
                # bare column of a scratch-only source (uncatalogued by design, D69/OQ-4).
                # A silent skip here understates the USES set (fail-open, D70). Fail closed.
                raise ProvenanceExtractionError(
                    f"Column '{col_name}' could not be attributed to any table after "
                    "qualify_columns and is not a declared SELECT-list output alias "
                    "referenced in GROUP BY / ORDER BY / HAVING — it is an unresolvable "
                    "column reference (e.g. absent from the single source table's "
                    "catalog). Fail-closed (D63, D70)."
                )
            # else: case (C) — provable computed alias reference (dept_earn, total_earn,
            # c, dept, ...) referenced outside the projection list — skip.
            continue

        # col_table is set (possibly an alias); resolve via alias_map
        if col_table in alias_map:
            real_tbl, real_db = alias_map[col_table]
            uses.add((f"{real_db}.{real_tbl}", col_name))
        elif col_table in cte_names:
            # Column attributed to a CTE name — this is expected when the outer
            # SELECT references a CTE column that has been projected through.
            # The CTE's own source columns ARE already extracted from within the
            # CTE definition itself (qualify_columns visits all subexpressions).
            # These outer CTE-attributed references are safely skipped; they are
            # projection references, not new data accesses.
            pass
        else:
            # Table not in alias_map and not a CTE — truly unknown reference
            raise ProvenanceExtractionError(
                f"Column '{col_table}.{col_name}' references table '{col_table}' "
                "which is not in the catalog or scratch database — fail-closed (D63)."
            )

    # ------------------------------------------------------------------
    # Step 9: Lambda-body coverage check (D69/OQ-1)
    # ------------------------------------------------------------------
    _check_lambda_body_coverage(ast_qc, uses, catalog_schema)

    # ------------------------------------------------------------------
    # Step 10: Return the canonical frozenset
    # ------------------------------------------------------------------
    return frozenset(uses)


def _infer_default_db(catalog_schema: dict[str, dict[str, str]]) -> str:
    """Infer the default database from catalog schema keys.

    If all keys share a single database prefix, return that prefix.
    Otherwise return the module-level default.
    """
    databases: set[str] = set()
    for key in catalog_schema:
        if "." in key:
            db, _ = key.split(".", 1)
            databases.add(db)
    if len(databases) == 1:
        return next(iter(databases))
    return _DEFAULT_DB
