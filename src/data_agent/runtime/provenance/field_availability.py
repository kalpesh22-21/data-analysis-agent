"""Exact-name absence receipts from complete, authorized schema checks."""


def field_availability(entry):
    if entry.tool_name != "getTableSchema" or entry.status != "ok" or entry.error_code:
        return None
    preview = entry.result_preview
    rows = preview.preview_rows if preview else []
    if len(rows) != 1 or len(rows[0]) != 1 or not isinstance(rows[0][0], dict):
        return None
    result = rows[0][0]
    # Schema preview budgeting can trim column documentation, but preserves
    # table-level receipts computed against the full authorized schema.
    if result.get("column_check") != {
        "coverage": "complete_authorized_table_schema", "matching": "exact_names",
    }:
        return None
    missing = result.get("columns_not_found")
    requested = entry.args.get("columns")
    if (not isinstance(missing, list) or not missing or len(missing) > 64
            or not isinstance(requested, list)
            or any(not isinstance(n, str) or not n or n not in requested for n in missing)):
        return None
    database, table = entry.args.get("database"), entry.args.get("table")
    if not isinstance(database, str) or not isinstance(table, str):
        return None
    # A name the schema answered with similarly named columns is not shown to be
    # unavailable: `city` may be `primary_city_municipality`. It is once every candidate
    # was itself requested in the same check, so the model saw each and still lacks it.
    similar = result.get("similar_columns")
    if isinstance(similar, dict):
        missing = [name for name in missing if not unchecked_similar(similar.get(name), requested)]
        if not missing:
            return None
    return {
        "table": f"{database}.{table}",
        "unavailable_fields": list(dict.fromkeys(missing)),
        "coverage": "complete_authorized_table_schema",
        "matching": "exact_names",
    }


def unchecked_similar(candidates, requested):
    """Similar columns the same schema check did not request by exact name."""
    if not isinstance(candidates, list):
        return []
    return [name for name in candidates if name not in requested]
