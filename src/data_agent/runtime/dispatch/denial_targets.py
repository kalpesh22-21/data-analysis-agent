"""Conservative column-denial targets for telemetry and retry bounding, not provenance."""

import re
from collections import Counter

import sqlglot
from sqlglot import exp

from .sql_diagnostics import decode_diagnostic

_IDENTIFIER = r"[A-Za-z_][A-Za-z_0-9]{0,127}"
_QUALIFIED = rf"{_IDENTIFIER}\.{_IDENTIFIER}"
_CODES = {"INVALID_COLUMN_REFERENCE", "COLUMN_SCOPE_VIOLATION"}


def referenced_columns(args):
    """Only unambiguous physical-table references; uncertainty never blocks a repair."""
    if "sql" not in args and "query" not in args:
        table = f"{args.get('database', '')}.{args.get('table', '')}"
        return {(table, "*")} if re.fullmatch(_QUALIFIED, table) else set()
    try:
        tree = sqlglot.parse_one(
            str(args.get("sql") or args.get("query", "")), dialect="clickhouse"
        )
        tables = {t.alias_or_name: f"{t.db}.{t.name}" for t in tree.find_all(exp.Table) if t.db}
        physical = set(tables.values())
        result = set()
        for col in tree.find_all(exp.Column):
            table = f"{col.db}.{col.table}" if col.db else tables.get(col.table)
            if not col.table and len(physical) == 1:
                table = next(iter(physical))
            if table in physical:
                result.add((table, col.name))
        if any(isinstance(s.parent, exp.Select) for s in tree.find_all(exp.Star)):
            result.update((table, "*") for table in physical)
        return result
    except Exception:
        return set()


def denied_columns(code, detail, args):
    """Read bounded, known API error formats and corroborate identifiers with the call."""
    if code not in _CODES or not detail:
        return set()
    diagnostic = decode_diagnostic(detail)
    message = (diagnostic.get("api_message", "") if diagnostic else detail)[:4000]
    targets = set()
    if code == "INVALID_COLUMN_REFERENCE":
        match = re.match(
            rf"Column '({_IDENTIFIER})' is not present in the physical catalog for "
            rf"((?:{_QUALIFIED})(?:, {_QUALIFIED})*)\. Check getTableSchema",
            message,
        )
        if match:
            targets.update((table, match[1]) for table in match[2].split(", ")[:16])
    else:
        match = re.match(
            r"(?:This query needs access to columns outside your permitted scope|"
            r"This table has columns outside your permitted scope): (.+?)\. ",
            message,
        )
        if match:
            for name in match[1].split(", ")[:16]:
                if re.fullmatch(rf"{_QUALIFIED}\.{_IDENTIFIER}", name):
                    table, column = name.rsplit(".", 1)
                    targets.add((table, column))
    referenced = referenced_columns(args)
    return {pair for pair in targets if pair in referenced or (pair[0], "*") in referenced}


def repeated_target_denial(tool_name, args, trail, turn_index, scope_hash):
    """Two denials of the same column/category stop SQL rewording; successes reset it."""
    requested = referenced_columns(args)
    counts = Counter()
    for entry in trail:
        if (
            entry.turn_index != turn_index
            or entry.tool_name != tool_name
            or (entry.model_response or {}).get("scope_hash") != scope_hash
        ):
            continue
        if entry.status == "ok":
            used = referenced_columns(entry.args)
            for key in list(counts):
                if key[1:] in used or (key[1], "*") in used:
                    del counts[key]
        elif entry.error_code in _CODES:
            for table, column in denied_columns(entry.error_code, entry.denial_detail, entry.args):
                counts[entry.error_code, table, column] += 1
    for (code, table, column), count in counts.items():
        if count >= 2 and ((table, column) in requested or (table, "*") in requested):
            return code
    return None


def record_column_denial(span, *, code, detail, args, tool_name, tool_call_id):
    """Telemetry never raises and carries only corroborated catalog identifiers."""
    try:
        if span is None or not span.is_recording():
            return
        targets = denied_columns(code, detail, args)
        if targets:
            attributes = {
                "error_code": code,
                "tool_name": tool_name,
                "columns": sorted(f"{table}.{column}" for table, column in targets)[:16],
            }
            if tool_call_id:
                attributes["tool_call_id"] = tool_call_id
            span.add_event("tool.column_denial", attributes=attributes)
    except Exception:
        pass
