"""Deterministic pre-execution safety checks for aggregate joins."""

from __future__ import annotations

import json

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from data_agent.runtime.dispatch.sql_diagnostics import decode_diagnostic


def _validate_outer_join_counts(select: exp.Select) -> None:
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


def cardinality_probes(sql: str) -> list[str]:
    """Check relations that could multiply an aggregate's source records.

    Both join directions matter: summing right-side values requires uniqueness on
    the left. Non-equality/ambiguous joins are refused rather than declared safe.
    MIN/MAX and DISTINCT aggregates are invariant under duplicate matching rows.
    All generated reads still pass through the scoped dispatcher.
    """
    tree = sqlglot.parse_one(sql, dialect="clickhouse")
    probes = []
    ctes = {cte.alias_or_name: cte.this for cte in tree.find_all(exp.CTE)}
    ranked_relations = {name for name, body in ctes.items() if body.find(exp.Window)}
    for _ in ctes:
        ranked_relations.update(
            name
            for name, body in ctes.items()
            if any(t.name in ranked_relations for t in body.find_all(exp.Table))
        )
    for select in tree.find_all(exp.Select):
        _validate_outer_join_counts(select)
        aggregates = [
            a
            for item in select.expressions
            for a in item.find_all(exp.AggFunc)
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
        for join in joins:
            if str(join.args.get("kind", "")).upper() in {"SEMI", "ANTI"}:
                continue
            join_keys = {alias: [] for alias in relations}
            on = join.args.get("on")
            if on is not None:
                if on.find(exp.Or):
                    raise ValueError(
                        "Disjunctive aggregate joins require an explicit grain rewrite."
                    )
                for eq in on.find_all(exp.EQ):
                    left, right = eq.this, eq.expression
                    if (
                        isinstance(left, exp.Column)
                        and isinstance(right, exp.Column)
                        and left.table != right.table
                        and left.table in keys
                        and right.table in keys
                    ):
                        join_keys[left.table].append(left.name)
                        join_keys[right.table].append(right.name)
            elif join.args.get("using") and len(relations) == 2:
                for alias in relations:
                    join_keys[alias].extend(k.name for k in join.args["using"])
            for alias, names in join_keys.items():
                if names:
                    keys[alias].append(tuple(dict.fromkeys(names)))
        for aggregate in aggregates:
            columns = list(aggregate.find_all(exp.Column))
            measured = {c.table for c in columns}
            if columns and ("" in measured or not measured <= relations.keys()):
                raise ValueError("Qualify measured columns so their source grain can be checked.")
            # COUNT(*) depends on the complete join. Other measures need all
            # *other* relations to preserve their source row multiplicity.
            required.update(relations if not measured else set(relations) - measured)
            if len(measured) > 1:
                required.update(relations)
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
                cols = [exp.column(k, table=alias) for k in key_set]
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
                if tree.args.get("with_"):
                    probe.set("with_", tree.args["with_"].copy())
                probes.append(probe.sql(dialect="clickhouse"))
    return list(dict.fromkeys(probes))


async def validate_join_cardinality(sql, dispatcher, credentials) -> str | None:
    try:
        probes = cardinality_probes(sql)
        for statement in probes:
            result = await dispatcher.dispatch("runQuery", {"sql": statement}, credentials)
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
