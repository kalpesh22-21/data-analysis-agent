"""Optional ingestion receipts queried by the runtime, outside model-facing tools."""

import asyncio
import json
import logging
from datetime import UTC, datetime

import httpx
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.auth.jwt_verify import verify_jwt_claims
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.provenance.capture import DATA_TOOLS

_logger = logging.getLogger(__name__)


class FreshnessClient:
    def __init__(self, settings: RuntimeSettings, *, transport=None):
        self.settings = settings
        self.transport = transport

    def _tables(self, tool_name, arguments, provenance):
        database = self.settings.data_as_of_clickhouse_database
        if tool_name == "sampleRows":
            tables = {(arguments.get("database"), arguments.get("table"))}
        else:
            sql = arguments.get("sql") or arguments.get("query")
            tree = sqlglot.parse_one(sql, dialect="clickhouse")
            tables = set()
            for scope in traverse_scope(tree):
                for _, source in scope.selected_sources.values():
                    if isinstance(source, exp.Table):
                        if not isinstance(source.this, exp.Identifier) or source.catalog:
                            return None
                        tables.add((source.db or database, source.name))
        for qualified, _ in provenance or ():
            db, table = qualified.rsplit(".", 1)
            tables.add((db, table))
        # Other databases (including scratch) cannot borrow a same-named product's date.
        if not tables or any(db != database or not table for db, table in tables):
            return None
        return sorted({table for _, table in tables})

    async def lookup(
        self, tool_name: str, arguments: dict, credentials: RuntimeCredentials, provenance
    ) -> str | None:
        settings = self.settings
        if (
            not settings.data_as_of_enabled
            or not settings.data_as_of_clickhouse_url
            or tool_name not in DATA_TOOLS
        ):
            return None
        try:
            async with asyncio.timeout(settings.data_as_of_timeout_seconds):
                tables = self._tables(tool_name, arguments, provenance)
                if not tables:
                    return None
                claims = await asyncio.to_thread(
                    verify_jwt_claims,
                    credentials.jwt,
                    jwks_url=settings.jwks_url,
                    issuer=settings.jwt_issuer,
                    audience=settings.jwt_audience,
                )
                pc = claims.get("proc_center")
                client = claims.get("clientcode")
                if not isinstance(pc, str) or not pc or not isinstance(client, str) or not client:
                    return None
                placeholders = ", ".join(
                    "{table_" + str(i) + ":String}" for i in range(len(tables))
                )
                # Only generated parameter names enter SQL. All caller/product values are bound.
                sql = (
                    "SELECT data_product_key, toUnixTimestamp64Milli(toDateTime64(max(last_sync_date), 3)) "
                    "FROM dbpcm_warehouse.etl_product_refresh_status FINAL "
                    "WHERE processing_center = {pc:String} AND client_code = {client:String} "
                    f"AND data_product_key IN ({placeholders}) GROUP BY data_product_key "
                    "FORMAT JSONCompactEachRow"
                )
                params = {
                    "param_pc": pc,
                    "param_client": client,
                    "readonly": "1",
                    **{f"param_table_{i}": table for i, table in enumerate(tables)},
                }
                async with httpx.AsyncClient(
                    transport=self.transport,
                    timeout=settings.data_as_of_timeout_seconds,
                    auth=(
                        settings.data_as_of_clickhouse_user,
                        settings.data_as_of_clickhouse_password,
                    ),
                ) as http:
                    response = await http.post(
                        settings.data_as_of_clickhouse_url,
                        files={
                            key: (None, value) for key, value in {"query": sql, **params}.items()
                        },
                    )
                    response.raise_for_status()
                rows = [json.loads(line) for line in response.text.splitlines() if line.strip()]
                # ClickHouse quotes Int64 JSON values by default.
                stamps = {
                    row[0]: int(row[1])
                    if isinstance(row[1], str) and row[1].lstrip("-").isdigit()
                    else row[1]
                    for row in rows
                }
                if set(stamps) != set(tables) or any(type(v) is not int for v in stamps.values()):
                    return None
                return (
                    datetime.fromtimestamp(min(stamps.values()) / 1000, UTC)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z")
                )
        except Exception:
            # Includes missing table/database, invalid JWT, unavailable server and timeout.
            # Never log credentials, SQL parameters, server error text, or response values.
            _logger.info("Optional data freshness lookup unavailable")
            return None
