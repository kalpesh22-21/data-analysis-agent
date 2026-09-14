"""Bounded repair context; raw engine messages never enter model history."""

from __future__ import annotations

import json
import re

import sqlglot

PREFIX = "sql_diagnostic:"
FUNCTIONS = {
    name.lower(): name
    for name in (
        "countIf",
        "sumIf",
        "avgIf",
        "uniqExact",
        "uniqExactIf",
        "toStartOfMonth",
        "addMonths",
        "dateDiff",
        "formatDateTime",
        "quantileExactLow",
        "quantileExactHigh",
    )
}
MESSAGES = {
    "UNKNOWN_FUNCTION": "Use a supported ClickHouse function with its exact spelling.",
    "SYNTAX_ERROR": "Correct the SQL syntax using the ClickHouse dialect.",
    "UNKNOWN_IDENTIFIER": "Check referenced columns and aliases against the scoped schema.",
    "ILLEGAL_AGGREGATION": "Separate aggregation and window calculations into query stages.",
    "NOT_AN_AGGREGATE": "Group non-aggregate expressions or move the calculation to an outer query.",
    "TYPE_MISMATCH": "Check operand types and use an explicit compatible cast.",
    "NO_COMMON_TYPE": "Use compatible types in conditional branches and unions.",
    "ILLEGAL_TYPE_OF_ARGUMENT": "Check the function's argument types against the scoped schema.",
}


def sql_diagnostic(message: str, sql: str) -> dict:
    code = next((c for c in MESSAGES if re.search(r"\b" + c + r"\b", message)), "QUERY_ERROR")
    result = {
        "engine_code": code,
        "message": MESSAGES.get(
            code, "Check the SQL against the scoped schema and ClickHouse procedure."
        ),
        "retryable": True,
    }
    match = re.search(r"Function with name '([A-Za-z_][A-Za-z_0-9]{0,63})' does not exist", message)
    if code == "UNKNOWN_FUNCTION" and match:
        name = match[1]
        # Only expose known built-ins actually present as a call in submitted SQL.
        if name.lower() in FUNCTIONS and re.search(r"\b" + re.escape(name) + r"\s*\(", sql):
            result.update(function=name, suggested_function=FUNCTIONS[name.lower()])
    return result


def encode_diagnostic(message: str, sql: str) -> str:
    return PREFIX + json.dumps(sql_diagnostic(message, sql))


def decode_diagnostic(detail: str | None) -> dict | None:
    if not detail or not detail.startswith(PREFIX):
        return None
    try:
        value = json.loads(detail[len(PREFIX) :])
        return value if isinstance(value, dict) else None
    except ValueError:
        return None


def sql_signature(sql: str) -> str:
    try:
        tree = sqlglot.parse_one(sql, dialect="clickhouse")
        return tree.sql(dialect="clickhouse", comments=False)
    except Exception:
        return sql.strip()


def repeated_sql_failure(
    sql: str, trail, turn_index: int, *, scope_hash: str | None = None
) -> bool:
    """Two identical failures stop a third execution; successful or changed SQL can proceed."""
    signature = sql_signature(sql)
    matches = [
        e
        for e in trail
        if e.turn_index == turn_index
        and (scope_hash is None or (e.model_response or {}).get("scope_hash") == scope_hash)
        and e.tool_name == "runQuery"
        and sql_signature(str(e.args.get("sql", ""))) == signature
    ]
    if any(e.status == "ok" for e in matches):
        return False
    errors = [
        (
            e.error_code,
            json.dumps(decode_diagnostic(e.denial_detail), sort_keys=True)
            if decode_diagnostic(e.denial_detail)
            else e.denial_detail or "",
        )
        for e in matches
        if e.error_code in {"CLICKHOUSE_QUERY_ERROR", "DISALLOWED_KEYWORD", "AGGREGATION_RISK"}
    ]
    return any(errors.count(signature) >= 2 for signature in set(errors))
