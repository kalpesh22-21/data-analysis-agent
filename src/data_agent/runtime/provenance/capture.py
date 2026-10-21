"""Read API-validated access dependencies; never reparse executed SQL locally."""

import re
from datetime import UTC, datetime
from typing import Any

from data_agent.runtime.mcp.client import MCPToolError

DATA_TOOLS = frozenset({"runQuery", "sampleRows", "explainQuery"})
_NO_PROVENANCE_TOOLS = frozenset(
    {"listDatabases", "listTables", "getTableSchema", "searchBlueprints", "searchKnowledge"}
)


def capture_provenance(tool_name: str, result: Any) -> frozenset[tuple[str, str]] | None:
    """Validate the versioned provenance receipt on the same API response as the data.

    Missing/malformed metadata is an API contract failure, not a successful result
    with unknown provenance. Never infer dependencies from returned column labels.
    """
    has_context = tool_name == "getTableSchema" and isinstance(result, dict) and any(
        isinstance(c, dict) and "context" in c for c in result.get("columns", ())
    )
    if tool_name in DATA_TOOLS or has_context:
        receipt = result.get("context_provenance" if has_context else "provenance") if isinstance(result, dict) else None
        if (
            not isinstance(receipt, dict)
            or type(receipt.get("version")) is not int
            or receipt["version"] != 1
        ):
            raise MCPToolError(
                "API_PROVENANCE_INVALID",
                "The data API response is missing supported provenance metadata (version 1). "
                "This is an API contract failure, not a SQL error or an empty result. "
                "Do not rewrite or repeat the query to fix it; the API deployment must provide provenance.",
            )
        columns = receipt.get("columns")
        if not isinstance(columns, list) or any(
            not isinstance(pair, list)
            or len(pair) != 2
            or any(not isinstance(value, str) or not value.strip() for value in pair)
            or "." not in pair[0]
            for pair in columns
        ):
            raise MCPToolError(
                "API_PROVENANCE_INVALID",
                "The data API returned malformed column provenance. This is an API contract "
                "failure; do not retry identical SQL or treat this as missing data.",
            )
        return frozenset(tuple(pair) for pair in columns)
    return frozenset() if tool_name in _NO_PROVENANCE_TOOLS else None


def capture_data_as_of(tool_name: str, result: Any) -> str | None:
    """Optional ingestion timestamp; accept only timezone-aware ISO datetimes.

    Normalize offsets to UTC so union ordering is chronological. Missing or invalid
    metadata never prevents a query from succeeding and never becomes a guessed time.
    """
    value = result.get("data_as_of") if isinstance(result, dict) else None
    if tool_name not in DATA_TOOLS or not isinstance(value, str):
        return None
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)",
        value,
    ):
        return None
    try:
        parsed = datetime.fromisoformat(value).astimezone(UTC)
        return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def union_data_as_of(values: list[str | None]) -> str | None:
    """Oldest known ingestion time, only when every contributing read is known."""
    normalized = [capture_data_as_of("runQuery", {"data_as_of": v}) for v in values]
    return min(normalized) if normalized and all(normalized) else None
