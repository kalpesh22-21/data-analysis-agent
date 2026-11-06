"""Execution provenance copied from successful runBlueprint results, never caller claims."""
from copy import deepcopy
from functools import lru_cache

EXECUTION_KEYS = ("blueprint_id", "sql", "terminal_sql", "scalar_bindings", "uses_rules")


def blueprint_execution_receipt(tool_name, status, full):
    if tool_name != "runBlueprint" or status != "ok" or not isinstance(full, dict):
        return None
    if not isinstance(full.get("blueprint_id"), str) or not full["blueprint_id"]:
        return None
    if not isinstance(full.get("terminal_sql"), str) or not full["terminal_sql"].strip():
        return None
    return {key: deepcopy(full[key]) for key in EXECUTION_KEYS if key in full}


@lru_cache(maxsize=512)
def _formatted_sql(sql):
    """Format each distinct SQL once in a bounded cache; retain exact SQL in storage."""
    import sqlglot
    from sqlglot.errors import SqlglotError
    try:
        return sqlglot.parse_one(sql, read="clickhouse").sql(dialect="clickhouse", pretty=True)
    except (SqlglotError, RecursionError):
        return sql


def model_execution(receipt):
    """SQL-only model projection; internal provenance remains available to runtime checks."""
    terminal = receipt.get("terminal_sql")
    queries = receipt.get("sql", [])
    if not isinstance(queries, list):
        queries = [queries] if isinstance(queries, str) else []
    queries = [q for q in queries if isinstance(q, str) and q.strip()]
    if len(queries) <= 1:
        return {"terminal_sql": _formatted_sql(terminal)} if terminal else {}
    return {"steps": [{"step": i + 1, "sql": _formatted_sql(q)} for i, q in enumerate(queries)]}


def judge_execution(receipt):
    """Judge/recovery receive runtime scalar lineage; the agent keeps its SQL-only view."""
    result = model_execution(receipt)
    if "steps" in result and receipt.get("scalar_bindings"):
        result["scalar_bindings"] = deepcopy(receipt["scalar_bindings"])
    return result
