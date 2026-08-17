"""Shared MCP tool-spec fixture helpers — the FAITHFUL tool catalogue.

The sibling of `tests/_catalog_fixture.py`, and for the same reason. That module
froze the MCP's `/catalog/export` so tests see the real warehouse SHAPE; this one
freezes the MCP's `tools/list` so tests see the real TOOL CONTRACT — names,
descriptions and, above all, `input_schema`.

WHY IT EXISTS. A2 (`tests/eval/test_routing_live.py`) hands its tool catalogue to
a LIVE model through the real `mcp/tool_schema.py::translate_tool_spec`, which
copies `description` and `input_schema` verbatim into the function declaration.
Its double used to advertise `description=""` and `{"type": "object",
"properties": {}}` for all six tools, so the model was told `runQuery()` takes no
`sql` and `getTableSchema()` takes no table. Every case that needed ad-hoc SQL or
a real schema read was unwinnable for a harness reason.

ANTI-DRIFT. The payload lives in `tests/fixtures/mcp_tool_specs.json`, generated
from a local clone of the MCP repo (the regen recipe is in the fixture's own
`_regenerate` key — the same DEV-only, sibling-repo idiom as
`scripts/regen_catalog_fixture.py`). It is NEVER fetched at test time: A2 must
run without a live MCP.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from data_agent.runtime.mcp.client import MCPToolSpec

TOOL_SPECS_PATH = Path(__file__).parent / "fixtures" / "mcp_tool_specs.json"

# The six tools `clickhouse-api` advertises. Named here so a test can assert the
# fixture still covers exactly them — a tool silently dropped from the fixture is
# a tool the model can no longer call, which looks like a routing failure.
MCP_TOOL_NAMES: tuple[str, ...] = (
    "listDatabases",
    "listTables",
    "getTableSchema",
    "sampleRows",
    "runQuery",
    "explainQuery",
)


def load_tool_specs_export() -> dict[str, Any]:
    """Return the parsed `{"tools": [...], "_source": ..., ...}` fixture dict."""
    with TOOL_SPECS_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def fixture_tool_specs() -> list[MCPToolSpec]:
    """The six real MCP tools as `MCPToolSpec`s, in the MCP's own order."""
    return [
        MCPToolSpec(
            name=tool["name"],
            description=tool["description"],
            input_schema=dict(tool["input_schema"]),
        )
        for tool in load_tool_specs_export()["tools"]
    ]
