"""Row-free checks that SQL literal lists came from authorized value resolution.

These receipts establish code membership, not semantic correctness or completeness
of the chosen population. They do not change execution provenance or answer evidence.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from data_agent.runtime.context.scope_filter import filter_trail


def _in_lists(sql, table, column, provenance):
    """Return literal sets for directly bound IN predicates; uncertain bindings are unknown."""
    lists = []
    try:
        tree = sqlglot.parse_one(sql, dialect="clickhouse")
        for scope in traverse_scope(tree):
            sources = scope.selected_sources
            for predicate in scope.find_all(exp.In):
                left = predicate.this
                if not isinstance(left, exp.Column) or left.name != column:
                    continue
                source = (
                    sources.get(left.table)
                    if left.table
                    else (next(iter(sources.values())) if len(sources) == 1 else None)
                )
                physical = source[1] if source else None
                if not isinstance(physical, exp.Table):
                    lists.append(None)
                    continue
                if physical.db:
                    target = f"{physical.db}.{physical.name}"
                else:
                    candidates = {
                        t
                        for t, c in provenance or ()
                        if c == column and t.rsplit(".", 1)[-1] == physical.name
                    }
                    target = next(iter(candidates)) if len(candidates) == 1 else None
                if target is None:
                    lists.append(None)
                elif target == table:
                    values = predicate.expressions
                    lists.append(
                        {value.this for value in values}
                        if values
                        and all(
                            isinstance(value, exp.Literal) and value.is_string for value in values
                        )
                        else None
                    )
    except (sqlglot.errors.SqlglotError, ValueError, TypeError):
        return [None]
    return lists or [None]


async def resolution_receipts(*, trail, results, turn_index, session_id, store, column_scope):
    """Read persisted resolution results locally; never include their rows or issue queries."""
    current = [
        entry
        for entry in filter_trail(trail, column_scope)
        if entry.turn_index == turn_index and entry.status == "ok"
    ]
    positions = {entry.tool_call_id: i for i, entry in enumerate(current)}
    executions = {result.get("tool_call_id"): result.get("execution", {}) for result in results}
    receipts = []
    for entry in current:
        if entry.tool_name != "resolveValues":
            continue
        column = entry.args.get("column")
        requested_table = entry.args.get("table")
        concept = entry.args.get("concept")
        if not all(isinstance(value, str) for value in (column, requested_table, concept)):
            continue
        targets = {
            table
            for table, col in entry.provenance or ()
            if col == column and requested_table in {table, table.rsplit(".", 1)[-1]}
        }
        if len(targets) != 1:
            continue
        table = next(iter(targets))
        full = None
        if entry.result_full_ref:
            try:
                full = await store.read_full_result(session_id, entry.result_full_ref)
            except Exception:
                pass  # Missing stored evidence is unknown, never a failed resolution.
        elif entry.result_preview and not entry.result_preview.truncated:
            rows = entry.result_preview.preview_rows
            if len(rows) == 1 and len(rows[0]) == 1:
                full = rows[0][0]
        values = full.get("values") if isinstance(full, dict) else None
        valid = isinstance(values, list) and all(
            isinstance(value, dict) and isinstance(value.get("value"), str) for value in values
        )
        resolved = {value["value"] for value in values} if valid else None
        degraded = full.get("degraded") if isinstance(full, dict) else None
        ranking = full.get("ranking") if isinstance(full, dict) else None
        receipt = {
            "tool_call_id": entry.tool_call_id,
            "concept": concept,
            "table": table,
            "column": column,
            "matched_count": len(resolved) if resolved is not None else None,
            "receipt_available": valid,
            "degraded": degraded if isinstance(degraded, bool) else None,
            "ranking": ranking if ranking in ("freq_only", "semantic+freq") else None,
            "checks": [],
        }
        for query in current:
            execution = executions.get(query.tool_call_id)
            if not execution or positions[query.tool_call_id] <= positions[entry.tool_call_id]:
                continue
            sqls = execution.get("sql", [])
            sqls = [sqls] if isinstance(sqls, str) else sqls
            if not isinstance(sqls, list):
                continue
            for index, sql in enumerate(sqls):
                if not isinstance(sql, str):
                    continue
                for predicate_index, literals in enumerate(
                    _in_lists(sql, table, column, query.provenance)
                ):
                    receipt["checks"].append(
                        {
                            "result_id": query.tool_call_id,
                            "statement_index": index,
                            "predicate_index": predicate_index,
                            "in_list_subset_of_resolved_values": (
                                literals <= resolved
                                if literals is not None and resolved is not None
                                else None
                            ),
                        }
                    )
        receipts.append(receipt)
    return receipts
