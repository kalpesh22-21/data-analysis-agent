"""Read API-validated access dependencies; never reparse executed SQL locally."""

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
    if tool_name in DATA_TOOLS:
        receipt = result.get("provenance") if isinstance(result, dict) else None
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
