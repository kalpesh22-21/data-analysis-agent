"""Locate the clickhouse-api (MCP) checkout that canon-parity tests compare against.

Resolved like ``scripts/regen_*``: ``$CLICKHOUSE_API_REPO``, else ``../clickhouse-api``
beside this repo — never a developer's absolute path. A missing checkout SKIPS a local
run but FAILS under CI (``$CI``) or ``REQUIRE_MCP_REPO=1``, so a pipeline that lost
the checkout cannot pass a drift guard by skipping it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_AGENT_REPO = Path(__file__).resolve().parents[1]


def mcp_repo() -> Path:
    env = os.environ.get("CLICKHOUSE_API_REPO")
    return Path(env).expanduser() if env else _AGENT_REPO.parent / "clickhouse-api"


def canon_blueprints_dir() -> Path:
    return mcp_repo() / "app" / "corpus" / "data" / "blueprints"


def require(path: Path) -> Path:
    """Return *path*, or skip (local) / fail (CI) when the MCP checkout lacks it."""
    if path.exists():
        return path
    message = f"clickhouse-api (MCP) checkout not found at {path}; set CLICKHOUSE_API_REPO"
    if os.environ.get("CI") or os.environ.get("REQUIRE_MCP_REPO") == "1":
        pytest.fail(message)
    pytest.skip(message)
