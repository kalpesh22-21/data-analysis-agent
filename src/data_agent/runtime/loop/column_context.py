"""Current-turn, scope-filtered column lookup evidence from the backend.

Only returned context values are evidence; descriptions state their interpretation
and limits. Never execute catalog SQL or turn authored example values into receipts.
"""

from data_agent.runtime.context.scope_filter import filter_trail


async def column_context_evidence(trail, *, turn_index, session_id, store, column_scope):
    receipts = []
    for entry in filter_trail(trail, column_scope):
        if (
            entry.turn_index != turn_index
            or entry.status != "ok"
            or entry.error_code
            or entry.tool_name != "getTableSchema"
        ):
            continue
        full = None
        if entry.result_full_ref:
            try:
                full = await store.read_full_result(session_id, entry.result_full_ref)
            except Exception:
                continue
        elif entry.result_preview and not entry.result_preview.truncated:
            rows = entry.result_preview.preview_rows
            if len(rows) == 1 and len(rows[0]) == 1:
                full = rows[0][0]
        if not isinstance(full, dict) or not isinstance(full.get("columns"), list):
            continue
        for column in full["columns"]:
            if not isinstance(column, dict) or not isinstance(column.get("context"), dict):
                continue
            name = column.get("name")
            targets = {t for t, c in entry.provenance or () if c == name}
            if len(targets) != 1:
                continue
            table = next(iter(targets))
            if column_scope and f"{table}.{name}" not in column_scope:
                continue
            for key, values in (column["context"] or {None: []}).items():
                if (
                    (key is not None and not isinstance(key, str))
                    or not isinstance(values, list)
                    or not all(isinstance(r, dict) for r in values)
                ):
                    continue
                receipts.append(
                    {
                        "result_id": entry.tool_call_id,
                        "table": table,
                        "column": name,
                        "context_key": key,
                        "retrieved_at": entry.ts,
                        "description": column.get("description", ""),
                        "rows": values,
                        "scope": "Caller-bound backend lookup; use the column description for population and time limits.",
                        "limitations": "Supports only the documented lookup facts, population and time scope. Code configuration does not establish historical usage, employee access, leave taken, or balances.",
                    }
                )
    return receipts


def business_values(receipts):
    # Only atomic returned cell strings, never context keys, field names or prose.
    return frozenset(
        value
        for receipt in receipts
        for row in receipt["rows"]
        for value in row.values()
        if isinstance(value, str) and value
    )
