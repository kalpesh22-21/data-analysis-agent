"""Deterministic pre-execution safety checks for aggregate joins."""

from __future__ import annotations

import json

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope

from data_agent.runtime.dispatch.sql_diagnostics import decode_diagnostic


def _conjuncts(node):
    """Only predicates required to be true can establish a join key."""
    if isinstance(node, exp.Paren):
        yield from _conjuncts(node.this)
    elif isinstance(node, exp.And):
        yield from _conjuncts(node.this)
        yield from _conjuncts(node.expression)
    elif node is not None:
        yield node


def _local_filters(select, alias):
    where = select.args.get("where")
    for predicate in _conjuncts(where.this if where else None):
        columns = list(predicate.find_all(exp.Column))
        if columns and {c.table for c in columns} == {alias} and not predicate.find(exp.Select):
            if all(
                isinstance(function, (exp.Lower, exp.Upper, exp.Trim, exp.Cast))
                for function in predicate.find_all(exp.Func)
            ):
                yield predicate


def _excludes_default_rows(select, alias, schema):
    # Deliberately narrow: NULL and ClickHouse's zero/empty default cannot equal
    # a nonempty string or a nonzero number. IS NOT NULL alone is insufficient.
    for predicate in _local_filters(select, alias):
        if isinstance(predicate, exp.EQ):
            for column, literal in (
                (predicate.this, predicate.expression),
                (predicate.expression, predicate.this),
            ):
                if isinstance(column, exp.Column) and isinstance(literal, exp.Literal):
                    relation = next(
                        (
                            node
                            for node in select.find_all(exp.Table)
                            if node.alias_or_name == alias
                            and node.find_ancestor(exp.Select) is select
                        ),
                        None,
                    )
                    dtype = (
                        (schema or {})
                        .get(f"{relation.db}.{relation.name}", {})
                        .get(column.name, "")
                        if relation is not None
                        else ""
                    )
                    dtype = (
                        dtype.replace("Nullable(", "").replace("LowCardinality(", "").rstrip(")")
                    )
                    if literal.is_string and literal.this and dtype == "String":
                        return True
                    if (
                        not literal.is_string
                        and float(literal.this) != 0
                        and dtype.startswith(("Int", "UInt", "Float", "Decimal"))
                    ):
                        return True
    return False


def _scope_probe(probe, scope):
    # Keep lexical nesting: flattening WITH clauses can rebind an outer CTE's
    # dependencies to a shadowing inner definition.
    first = True
    while scope:
        clause = scope.expression.args.get("with_")
        if clause:
            if not first:
                probe = exp.select("*").from_(probe.subquery("cardinality_scope"))
            probe.set("with_", clause.copy())
            first = False
        scope = scope.parent
    return probe


def _measure_columns(expression):
    """CASE/IF conditions filter values; their columns are not measured values."""
    if isinstance(expression, exp.Column):
        yield expression
    elif isinstance(expression, exp.Case):
        for branch in expression.args.get("ifs", []):
            yield from _measure_columns(branch.args.get("true"))
        yield from _measure_columns(expression.args.get("default"))
    elif isinstance(expression, exp.If):
        yield from _measure_columns(expression.args.get("true"))
        yield from _measure_columns(expression.args.get("false"))
    elif expression is not None:
        for child in expression.iter_expressions():
            yield from _measure_columns(child)


def _grouped_join_preserves_source(select: exp.Select, scope: Scope) -> bool:
    """Prove a two-relation join preserves the FROM-side grain.

    Only plain GROUP BY output keys and mandatory equality predicates establish
    this proof. Unknown shapes retain the existing live cardinality probes.
    """
    joins = select.args.get("joins", [])
    if len(joins) != 1:
        return False
    join = joins[0]
    if join.args.get("side", "").upper() not in {"", "LEFT"} or join.args.get(
        "kind", ""
    ).upper() not in {"", "INNER"}:
        return False
    source = select.args.get("from_")
    if source is None:
        return False
    left, right = source.this.alias_or_name, join.this.alias_or_name
    grouped = scope.sources.get(right)
    if not isinstance(grouped, Scope) or grouped.outer_columns:
        return False
    body = grouped.expression
    if not isinstance(body, exp.Select) or body.find(exp.Explode):
        return False
    group = body.args.get("group")
    if (
        not group
        or not group.expressions
        or any(value for key, value in group.args.items() if key != "expressions")
    ):
        return False
    # Every grouping column must survive projection unchanged under a unique name.
    outputs = [item.alias_or_name for item in body.expressions]
    group_outputs = []
    for key in group.expressions:
        if not isinstance(key, exp.Column):
            return False
        names = {
            item.alias_or_name
            for item in body.expressions
            if item.unalias() == key and outputs.count(item.alias_or_name) == 1
        }
        if not names:
            return False
        group_outputs.append(names)
    matched = set()
    if join.args.get("using"):
        matched.update(key.name for key in join.args["using"])
    else:
        pending = [join.args.get("on")]
        while pending:
            predicate = pending.pop()
            if isinstance(predicate, exp.Paren):
                pending.append(predicate.this)
            elif isinstance(predicate, exp.And):
                pending.extend((predicate.this, predicate.expression))
            elif isinstance(predicate, exp.EQ):
                a, b = predicate.this, predicate.expression
                if isinstance(a, exp.Column) and isinstance(b, exp.Column):
                    if a.table == left and b.table == right:
                        matched.add(b.name)
                    elif b.table == left and a.table == right:
                        matched.add(a.name)
    return all(names & matched for names in group_outputs)


def _validate_outer_join_counts(select: exp.Select, schema=None) -> None:
    """Reject unconditional entity counts that can include an unmatched join row.

    ClickHouse may fill unmatched non-nullable keys with their defaults, so DISTINCT
    is not a match test. Conditional aggregates remain subject to measurement review;
    this guard deliberately does not infer the truth of arbitrary predicates.
    """
    source = select.args.get("from_")
    aliases = {source.this.alias_or_name} if source else set()
    unmatched = set()
    for join in select.args.get("joins", []):
        alias = join.this.alias_or_name
        if str(join.args.get("kind", "")).upper() in {"SEMI", "ANTI"}:
            continue
        side = str(join.args.get("side", "")).upper()
        if side in {"LEFT", "FULL"}:
            unmatched.add(alias)
        if side in {"RIGHT", "FULL"}:
            unmatched.update(aliases)
        aliases.add(alias)
    unmatched = {alias for alias in unmatched if not _excludes_default_rows(select, alias, schema)}
    if not unmatched:
        return
    for aggregate in select.find_all(exp.AggFunc):
        if aggregate.find_ancestor(exp.Select) is not select:
            continue
        name = (
            aggregate.name.lower()
            if isinstance(aggregate, (exp.AnonymousAggFunc, exp.CombinedAggFunc))
            else aggregate.sql_name().lower()
        )
        if not (
            isinstance(aggregate, (exp.Count, exp.ApproxDistinct))
            or name == "countdistinct"
            or (name.startswith("uniq") and not name.endswith(("if", "state", "merge")))
        ):
            continue
        columns = list(aggregate.find_all(exp.Column))
        if not columns or any(not c.table or c.table in unmatched for c in columns):
            raise ValueError(
                "Outer-join count can include unmatched default-filled rows as employees or "
                "other entities, even with DISTINCT. Aggregate the counted source before "
                "the outer join, then fill missing counts with zero. Qualify counted columns "
                "when counting the preserved side."
            )


def cardinality_probes(sql: str, *, schema=None) -> list[str]:
    """Check relations that could multiply an aggregate's source records.

    Both join directions matter: summing right-side values requires uniqueness on
    the left. Non-equality/ambiguous joins are refused rather than declared safe.
    MIN/MAX and DISTINCT aggregates are invariant under duplicate matching rows.
    All generated reads still pass through the scoped dispatcher.
    """
    tree = sqlglot.parse_one(sql, dialect="clickhouse")
    scopes = {id(scope.expression): scope for scope in traverse_scope(tree)}
    if schema:
        # Resolve only unambiguous physical columns. Unknown derived outputs and
        # ambiguous names retain the explicit qualification diagnostic.
        for scope in scopes.values():
            for column in scope.columns:
                if column.table:
                    continue
                candidates = []
                unknown = False
                for alias, relation in scope.sources.items():
                    if isinstance(relation, exp.Table):
                        names = schema.get(f"{relation.db}.{relation.name}")
                        if names is None:
                            unknown = True
                        elif column.name in names:
                            candidates.append(alias)
                    else:
                        unknown = True
                if len(candidates) == 1 and not unknown:
                    column.set("table", exp.to_identifier(candidates[0]))
    probes = []
    for select in tree.find_all(exp.Select):
        scope = scopes.get(id(select))
        ctes = (
            {name: source.expression for name, source in scope.cte_sources.items()} if scope else {}
        )
        ranked_relations = {name for name, body in ctes.items() if body.find(exp.Window)}
        for _ in ctes:
            ranked_relations.update(
                name
                for name, body in ctes.items()
                if any(t.name in ranked_relations for t in body.find_all(exp.Table))
            )
        _validate_outer_join_counts(select, schema)
        aggregates = [
            a
            for a in select.find_all(exp.AggFunc)
            if a.find_ancestor(exp.Select) is select
            and not isinstance(a, (exp.Min, exp.Max, exp.Rank, exp.DenseRank, exp.RowNumber))
            and not isinstance(a.this, exp.Distinct)
        ]
        joins = [
            j
            for j in select.args.get("joins", [])
            if str(j.args.get("kind", "")).upper() not in {"SEMI", "ANTI"}
        ]
        if not joins:
            continue
        source = select.args.get("from_")
        relations = {source.this.alias_or_name: source.this} if source else {}
        for join in joins:
            if str(join.args.get("kind", "")).upper() in {"SEMI", "ANTI"}:
                continue
            relations[join.this.alias_or_name] = join.this
        # Joining a window-derived relation back to entities can duplicate rows
        # even without an aggregate in the outer SELECT. Check that relation's
        # matching keys, not the entity side (where salary ties are legitimate).
        required = {
            alias
            for alias, relation in relations.items()
            if (isinstance(relation, exp.Table) and relation.name in ranked_relations)
            or (isinstance(relation, exp.Subquery) and relation.find(exp.Window))
        }
        if not aggregates and not required:
            continue
        keys = {alias: [] for alias in relations}
        key_expressions = {}
        for join in joins:
            if str(join.args.get("kind", "")).upper() in {"SEMI", "ANTI"}:
                continue
            join_keys = {alias: [] for alias in relations}
            on = join.args.get("on")
            if on is not None:
                for eq in _conjuncts(on):
                    if not isinstance(eq, exp.EQ):
                        continue
                    left, right = eq.this, eq.expression
                    left_aliases = {c.table for c in left.find_all(exp.Column)}
                    right_aliases = {c.table for c in right.find_all(exp.Column)}
                    if (
                        len(left_aliases) == len(right_aliases) == 1
                        and left_aliases != right_aliases
                        and left_aliases <= keys.keys()
                        and right_aliases <= keys.keys()
                        and not eq.find(exp.Select)
                        and all(
                            isinstance(function, (exp.Lower, exp.Upper, exp.Trim, exp.Cast))
                            for function in eq.find_all(exp.Func)
                        )
                    ):
                        for expression, aliases in ((left, left_aliases), (right, right_aliases)):
                            alias = next(iter(aliases))
                            key = (
                                expression.name
                                if isinstance(expression, exp.Column)
                                else expression.sql(dialect="clickhouse")
                            )
                            key_expressions[(alias, key)] = expression.copy()
                            join_keys[alias].append(key)
                if on.find(exp.Or) and not any(join_keys.values()):
                    raise ValueError(
                        "Disjunctive aggregate joins require an explicit grain rewrite."
                    )
            elif join.args.get("using") and len(relations) == 2:
                for alias in relations:
                    join_keys[alias].extend(k.name for k in join.args["using"])
            for alias, names in join_keys.items():
                if names:
                    keys[alias].append(tuple(dict.fromkeys(names)))
        for aggregate in aggregates:
            measure = aggregate
            if isinstance(aggregate, exp.CombinedAggFunc) and aggregate.this.lower() in {
                "sumif",
                "avgif",
            }:
                measure = aggregate.expressions[0]
            columns = list(_measure_columns(measure))
            measured = {c.table for c in columns}
            if columns and ("" in measured or not measured <= relations.keys()):
                raise ValueError("Qualify measured columns so their source grain can be checked.")
            # Row counts and count predicates retain the FROM-side grain when
            # each source row matches at most one grouped lookup row. Referencing
            # a lookup value in a predicate does not measure the lookup's grain.
            row_count = isinstance(aggregate, (exp.CountIf, exp.Count))
            scope = scopes.get(id(select))
            if row_count and scope and _grouped_join_preserves_source(select, scope):
                continue
            # COUNT(*) depends on the complete join. Other measures need all
            # *other* relations to preserve their source row multiplicity.
            if (
                row_count
                and len(relations) == 2
                and joins[0].args.get("side", "").upper() in {"", "LEFT"}
            ):
                required.add(joins[0].this.alias_or_name)
                continue
            required.update(relations if not measured else set(relations) - measured)
            if len(measured) > 1:
                required.update(relations)
        # ANY limits matches on the right, but does not preserve right-side measures.
        if (
            len(joins) == 1
            and joins[0].args.get("kind", "").upper() == "ANY"
            and joins[0].args.get("side", "").upper() in {"", "LEFT", "INNER"}
        ):
            required.discard(joins[0].this.alias_or_name)
        for alias in sorted(required):
            if not keys[alias]:
                raise ValueError(
                    "Aggregate join cardinality cannot be established. Use a semijoin or aggregate each source at its intended grain."
                )
            for key_set in dict.fromkeys(keys[alias]):
                relation = relations[alias]
                body = ctes.get(relation.name) if isinstance(relation, exp.Table) else relation
                lineage = [body] if body is not None else []
                seen = set()
                for stage in lineage:
                    for table in stage.find_all(exp.Table):
                        if table.name in ctes and table.name not in seen:
                            seen.add(table.name)
                            lineage.append(ctes[table.name])
                ranks = [
                    window
                    for stage in lineage
                    for window in stage.find_all(exp.Window)
                    if isinstance(window.this, (exp.DenseRank, exp.Rank))
                ]
                if (
                    not aggregates
                    and ranks
                    and not any(
                        window.args.get("order")
                        and {c.name for c in window.args["order"].find_all(exp.Column)}
                        <= set(key_set)
                        for window in ranks
                    )
                ):
                    # A ranked employee relation joining its department directory
                    # is not a join back on salary levels. Its department need not
                    # uniquely identify employees.
                    continue
                # A data probe cannot certify future tie safety for UI re-execution.
                # Pure rank-level keys repeat for ties unless explicitly deduplicated.
                rank_aliases = {
                    a.alias
                    for stage in lineage
                    for a in stage.find_all(exp.Alias)
                    if isinstance(a.this, exp.Window)
                    and isinstance(a.this.this, (exp.DenseRank, exp.Rank))
                }
                deduplicated = any(
                    (
                        stage.args.get("distinct")
                        and stage.expressions
                        and all(isinstance(item, exp.Column) for item in stage.expressions)
                        and {item.name for item in stage.expressions} <= set(key_set) | rank_aliases
                    )
                    or (
                        stage.args.get("group")
                        and stage.args["group"].expressions
                        and all(
                            isinstance(item, exp.Column) for item in stage.args["group"].expressions
                        )
                        and {item.name for item in stage.args["group"].expressions} <= set(key_set)
                    )
                    for stage in lineage
                )
                for window in ranks:
                    rank_keys = {
                        c.name
                        for expression in (window.args.get("partition_by") or [])
                        for c in expression.find_all(exp.Column)
                    }
                    if window.args.get("order"):
                        rank_keys.update(c.name for c in window.args["order"].find_all(exp.Column))
                    if set(key_set) == rank_keys and not deduplicated:
                        raise ValueError(
                            "Rank-level joins can duplicate tied entities. Rank employee rows directly, or explicitly deduplicate salary levels before joining."
                        )
                cols = [
                    key_expressions.get((alias, k), exp.column(k, table=alias)).copy()
                    for k in key_set
                ]
                probe = exp.select(
                    exp.alias_(exp.Count(this=exp.Star()), "row_count"),
                    exp.alias_(
                        exp.Anonymous(this="uniqExact", expressions=[exp.Tuple(expressions=cols)]),
                        "distinct_count",
                    ),
                ).from_(relations[alias].copy())
                probe = probe.where(
                    exp.and_(
                        *[exp.Not(this=exp.Is(this=c.copy(), expression=exp.Null())) for c in cols]
                    )
                )
                # WHERE conjuncts local to a required relation only remove rows
                # from that relation; cross-relation and disjunctive predicates stay out.
                for predicate in _local_filters(select, alias):
                    probe = probe.where(predicate.copy())
                probe = _scope_probe(probe, scopes.get(id(select)))
                probes.append(probe.sql(dialect="clickhouse"))
    return list(dict.fromkeys(probes))


async def validate_join_cardinality(
    sql, dispatcher, credentials, *, emit_progress=True
) -> str | None:
    try:
        schema = None
        from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher

        if isinstance(dispatcher, ToolDispatcher):
            catalog = await dispatcher._resolve_catalog(credentials)
            schema = catalog.schema
        probes = cardinality_probes(sql, schema=schema)
        for statement in probes:
            result = await dispatcher.dispatch(
                "runQuery", {"sql": statement}, credentials, emit_progress=emit_progress
            )
            rows = (
                result.result_full.get("rows", []) if isinstance(result.result_full, dict) else []
            )
            if result.status != "ok":
                diagnostic = decode_diagnostic(result.denial_detail)
                return (
                    "The join cardinality probe failed; this is not proof of duplicate rows. "
                    + (
                        json.dumps(diagnostic)
                        if diagnostic
                        else str(result.error_code or "QUERY_ERROR")
                    )
                    + " Correct the underlying query or use the tested SQL procedure."
                )
            if not rows or len(rows[0]) < 2 or rows[0][0] != rows[0][1]:
                return "The aggregate join can duplicate measured records, or its cardinality could not be checked. Aggregate each source to the intended grain or use a semijoin."
    except ParseError as exc:
        location = exc.errors[0] if exc.errors else {}
        line, column = location.get("line"), location.get("col")
        position = (
            f" at line {line}, column {column}"
            if isinstance(line, int) and isinstance(column, int)
            else ""
        )
        return (
            "SQL syntax could not be parsed"
            + position
            + ". Check missing whitespace before FROM, WHERE, JOIN, and ORDER BY, and ClickHouse syntax. No join-cardinality verdict was established."
        )
    except ValueError as exc:
        if str(exc).startswith(
            (
                "Rank-level joins",
                "Qualify measured columns",
                "Disjunctive aggregate joins",
                "Aggregate join cardinality",
                "Outer-join count",
            )
        ):
            return str(exc)
        return "Aggregation safety could not be established. Use explicit join keys and aggregate each source to the intended grain."
    except Exception:
        return "Aggregation safety could not be established. Use explicit join keys and aggregate each source to the intended grain."
    return None
