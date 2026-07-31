#!/usr/bin/env python
"""Regenerate the frozen catalog-export fixture from the MCP's YAML source.

Wave 0 of "MCP as the single source of truth for the semantic catalog": this
DEV-ONLY tool imports the shared serializer from the sibling ``clickhouse-api``
(MCP) repo and writes its output to ``tests/fixtures/catalog_export.json``. That
fixture is the frozen export CONTRACT that later waves build to (the HTTP
``/catalog/export`` endpoint on the MCP side, and the agent-side runtime loaders
that will rebuild ``CatalogHandle`` / ``SemanticCatalogHandle`` / description-col
linkage from the export instead of re-parsing ``databaseSchemaDocs/``).

This script is NOT run in CI and is NOT imported by the agent at runtime. It
depends on a local clone of the MCP repo being present as a sibling directory
(or pointed at explicitly). If that repo is absent it fails loudly rather than
silently producing a stale or partial fixture.

Usage
-----
    python scripts/regen_catalog_fixture.py

Locating the MCP repo (first match wins):
    1. ``$CLICKHOUSE_API_REPO`` env var (absolute path to the clickhouse-api repo)
    2. sibling of this agent repo: ``../clickhouse-api``

Then the generated JSON is written to ``tests/fixtures/catalog_export.json``
(override with ``$CATALOG_FIXTURE_OUT``). Commit the result.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

# This file lives at <agent-repo>/scripts/regen_catalog_fixture.py
_AGENT_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_MCP_REPO = _AGENT_REPO.parent / "clickhouse-api"
_DEFAULT_FIXTURE_OUT = _AGENT_REPO / "tests" / "fixtures" / "catalog_export.json"


def _resolve_mcp_repo() -> Path:
    """Return the path to the sibling MCP (clickhouse-api) repo, or fail clearly."""
    env = os.environ.get("CLICKHOUSE_API_REPO")
    candidate = Path(env).expanduser() if env else _DEFAULT_MCP_REPO

    if not candidate.is_dir():
        raise SystemExit(
            "ERROR: cannot locate the clickhouse-api (MCP) repo.\n"
            f"  Looked for: {candidate}\n"
            "  This DEV-only script needs a local clone of the MCP repo to import\n"
            "  its shared catalog serializer. Set CLICKHOUSE_API_REPO to the repo\n"
            "  path, or clone it as a sibling directory of this agent repo."
        )

    serializer = candidate / "app" / "semantic_catalog" / "export.py"
    if not serializer.is_file():
        raise SystemExit(
            "ERROR: found a clickhouse-api directory but it has no catalog serializer.\n"
            f"  Expected: {serializer}\n"
            "  The MCP repo may be on an older commit than Wave 0. Update it so that\n"
            "  app/semantic_catalog/export.py exists, then re-run."
        )
    return candidate


def main() -> None:
    mcp_repo = _resolve_mcp_repo()

    # Import the shared serializer from the sibling repo. Prepend so the MCP's
    # own `app` package wins over any same-named module in the agent repo.
    sys.path.insert(0, str(mcp_repo))
    try:
        export = importlib.import_module("app.semantic_catalog.export")
    except Exception as exc:  # noqa: BLE001 - surface any import failure clearly
        raise SystemExit(
            f"ERROR: failed to import the MCP catalog serializer from {mcp_repo}:\n  {exc}"
        ) from exc

    payload = export.build_catalog_export()

    out_path = Path(
        os.environ.get("CATALOG_FIXTURE_OUT", str(_DEFAULT_FIXTURE_OUT))
    ).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Faithful, diff-friendly mirror: preserve authored key order (the loader's
    # parse order is deterministic), pretty-print, keep a trailing newline.
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    out_path.write_text(text, encoding="utf-8")

    table_count = len(payload.get("catalog", {}))
    print(
        f"Wrote {out_path}\n"
        f"  source MCP repo : {mcp_repo}\n"
        f"  catalog_sha     : {payload.get('catalog_sha')}\n"
        f"  tables          : {table_count}"
    )


if __name__ == "__main__":
    main()
