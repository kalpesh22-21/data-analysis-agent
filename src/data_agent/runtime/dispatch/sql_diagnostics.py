"""Bounded API/SQL failure details and repair hints for same-turn model recovery."""

from __future__ import annotations

import json
import re

import sqlglot

from .denial_mapping import (
    AGGREGATION_QUERY_CODES,
    MODEL_QUERY_ERROR_CODES,
    MODEL_SERVICE_ERROR_CODES,
)

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
    try:
        payload = json.loads(message)
    except (ValueError, TypeError):
        payload = None
    if (isinstance(payload, dict) and payload.get("version") == 1
            and payload.get("code") in MODEL_QUERY_ERROR_CODES | MODEL_SERVICE_ERROR_CODES
            and payload.get("input_dialect") == "clickhouse"):
        # Closed fields; never copy backend metadata or arbitrary server fields.
        result = {key: payload[key][:3000] for key in
                  ("code", "message", "input_dialect", "guidance", "retry", "candidate_expression")
                  if isinstance(payload.get(key), str)}
        result["retryable"] = payload.get("retry") in {"after_query_change", "after_scope_or_capacity_change"}
        return result
    return _legacy_sql_diagnostic(message, sql)


def _legacy_sql_diagnostic(message: str, sql: str) -> dict:
    """Compatibility with older API deployments; new errors bypass regex diagnosis."""
    code = next((c for c in MESSAGES if re.search(r"\b" + c + r"\b", message)), "QUERY_ERROR")
    result = {
        "engine_code": code,
        "api_message": message[:4000],
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


def encode_column_reference_diagnostic(message: str, sql: str) -> str:
    """Expose only bounded identifiers also present in the submitted query."""
    from .denial_mapping import classify_denial

    result = {
        "engine_code": "INVALID_COLUMN_REFERENCE",
        "api_message": message[:4000],
        "message": classify_denial("INVALID_COLUMN_REFERENCE").user_message,
        "retryable": True,
    }
    match = re.match(
        r"Column '([A-Za-z_][A-Za-z_0-9]{0,127})' is not present in the physical catalog for "
        r"([A-Za-z_0-9., ]{1,2048})\. Check getTableSchema",
        message[:4096],
    )
    if match:
        try:
            tree = sqlglot.parse_one(sql, dialect="clickhouse")
            columns = {col.name for col in tree.find_all(sqlglot.exp.Column)}
            tables = {f"{table.db}.{table.name}" for table in tree.find_all(sqlglot.exp.Table)}
            if match[1] in columns:
                result["column"] = match[1]
                result["tables"] = [t for t in match[2].split(", ") if t in tables][:16]
        except Exception:
            pass  # The generic repair guidance remains usable for unparseable SQL.
    return PREFIX + json.dumps(result)


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


def aggregation_signature(sql: str) -> str:
    """Ignore cosmetic aliases/order only. Preserve filters, keys and measures.

    CTE/derived-column alias changes remain conservative: without full lineage
    resolution they must not accidentally merge different analytical queries.
    """
    try:
        from sqlglot.optimizer.scope import traverse_scope
        tree = sqlglot.parse_one(sql, dialect="clickhouse")
        for scope in traverse_scope(tree):
            aliases = {}
            for index, (name, source) in enumerate(scope.sources.items()):
                if isinstance(source, sqlglot.exp.Table):
                    aliases[name] = f"_guard_{index}"
                    source.set("alias", sqlglot.exp.TableAlias(this=sqlglot.exp.to_identifier(aliases[name])))
            for column in scope.columns:
                if column.table in aliases:
                    column.set("table", sqlglot.exp.to_identifier(aliases[column.table]))
            if isinstance(scope.expression, sqlglot.exp.Select) and scope is not None:
                # Only remove output labels on the root projection, where they
                # cannot rename a column consumed by another SQL scope.
                if scope.expression is tree:
                    tree.set("expressions", [x.unalias().copy() for x in tree.expressions])
                    order = tree.args.get("order")
                    if order is not None and not order.find(sqlglot.exp.AggFunc):
                        tree.set("order", None)
        return tree.sql(dialect="clickhouse", comments=False)
    except Exception:
        return sql_signature(sql)


def repeated_sql_failure(
    sql: str, trail, turn_index: int, *, scope_hash: str | None = None
) -> bool:
    """Two identical failures stop a third execution; successful or changed SQL can proceed."""
    signature = sql_signature(sql)
    guard_signature = aggregation_signature(sql)
    guard_matches = [e for e in trail
                     if e.turn_index == turn_index and e.tool_name == "runQuery"
                     and (scope_hash is None or (e.model_response or {}).get("scope_hash") == scope_hash)
                     and e.error_code in AGGREGATION_QUERY_CODES | {"AGGREGATION_RISK", "AGGREGATION_CHECK_UNAVAILABLE"}
                     and aggregation_signature(str(e.args.get("sql", ""))) == guard_signature]
    if any(e.error_code == "AGGREGATION_CHECK_UNAVAILABLE" for e in guard_matches):
        return True
    if any(sum(e.error_code == code for e in guard_matches) >= 2
           for code in AGGREGATION_QUERY_CODES | {"AGGREGATION_RISK"}):
        return True
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
        if e.error_code
        in MODEL_QUERY_ERROR_CODES | AGGREGATION_QUERY_CODES | {
            "CLICKHOUSE_QUERY_ERROR",
            "INVALID_COLUMN_REFERENCE",
            "DISALLOWED_KEYWORD",
            "AGGREGATION_RISK",
        }
    ]
    return any(errors.count(signature) >= 2 for signature in set(errors))
