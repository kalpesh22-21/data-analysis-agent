"""Turn-local circuit breaker for API-confirmed unavailable catalog sources."""
from contextvars import ContextVar
from functools import wraps
import json
import re

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from data_agent.runtime.mcp.client import MCPToolError

_STATE = ContextVar("unavailable_sources", default=None)
_SOURCE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*")


def source_receipt(detail):
    try:
        payload = json.loads((detail or "").removeprefix("sql_diagnostic:"))
    except (ValueError, TypeError):
        return None
    if isinstance(payload, dict) and payload.get("code") == "SOURCE_UNAVAILABLE" and isinstance(payload.get("cause"), dict):
        payload = payload["cause"]
    if (isinstance(payload, dict) and payload.get("code") == "SOURCE_UNAVAILABLE"
            and isinstance(payload.get("source"), str)
            and _SOURCE.fullmatch(payload["source"])):
        return payload["source"]
    return None


def source_availability_boundary(fn):
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        token = _STATE.set(set())
        try:
            return await fn(*args, **kwargs)
        finally:
            _STATE.reset(token)
    return wrapped


def restore_sources(doc, turn_index, scope_hash):
    state = _STATE.get()
    if state is None:
        return
    for entry in doc.tool_trail:
        if (entry.turn_index == turn_index and entry.error_code == "SOURCE_UNAVAILABLE"
                and (entry.model_response or {}).get("scope_hash") == scope_hash):
            source = source_receipt(entry.denial_detail)
            if source:
                state.add(source)


def remember_source(detail):
    source = source_receipt(detail)
    state = _STATE.get()
    if source and state is not None:
        state.add(source)


def check_sources(tool_name, args):
    state = _STATE.get()
    if not state or tool_name not in {"runQuery", "explainQuery"}:
        return
    try:
        tree = sqlglot.parse_one(args.get("sql") or args.get("query", ""), read="clickhouse")
        tables = [source for scope in traverse_scope(tree) for source in scope.sources.values()
                  if isinstance(source, exp.Table)]
        blocked = next((known for known in sorted(state) for table in tables
                        if known == f"{table.db}.{table.name}"
                        or (not table.db and known.rsplit(".", 1)[-1] == table.name)), None)
    except Exception:
        return  # Normal SQL validation still applies.
    if blocked:
        raise MCPToolError("SOURCE_UNAVAILABLE", json.dumps({
            "version": 1, "code": "SOURCE_UNAVAILABLE", "source": blocked,
            "input_dialect": "clickhouse", "retry": "after_source_recovery",
            "message": "A required data source is unavailable; this query was not executed.",
            "guidance": "Do not retry this source during this turn. Use independent sources or deliver verified portions with a limitation.",
        }))
