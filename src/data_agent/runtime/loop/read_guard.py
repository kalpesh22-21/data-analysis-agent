"""Repeated-idempotent-read guard primitives — the neutral, dependency-free core
shared by `loop/agent_loop.py` and `context/discovery_emulation.py`.

Extracted here so `discovery_emulation` no longer reaches into `agent_loop`'s
private module namespace at runtime (which forced `agent_loop` to keep its own
reciprocal reference behind a `TYPE_CHECKING` guard — a fragile cycle where any
future runtime import of `discovery_emulation` from `agent_loop` would crash at
startup). Both modules now depend only on THIS leaf, in one direction.

`IDEMPOTENT_READ_TOOLS`: the idempotent, side-effect-free read tools whose result
depends ONLY on their arguments — an identical repeat within a turn is guaranteed
to return the same already-served data (it is in the history above). The
repeated-idempotent-read guard (generalizing D94) declines to re-dispatch such a
repeat and injects a data-free "you already have this" nudge instead.
runQuery/runBlueprint/askUser/resolveValues are deliberately EXCLUDED — a repeated
runQuery may be a distinct legitimate step and is never guarded here.

`idempotent_read_signature`: the content key that identifies an already-served
idempotent read — the tool name plus its canonicalized arguments (stable key
order, `str`-coerced for any non-JSON-native arg). Two calls with the same key
return the same data by construction, so the second is a re-fetch.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

IDEMPOTENT_READ_TOOLS = frozenset({"getTableSchema", "listTables", "listDatabases", "explainQuery"})

__all__ = ["IDEMPOTENT_READ_TOOLS", "idempotent_read_signature"]


def idempotent_read_signature(tool_name: str, arguments: Mapping[str, Any]) -> tuple[str, str]:
    """The content key that identifies an already-served idempotent read: the
    tool name plus its canonicalized arguments (stable key order, `str`-coerced
    for any non-JSON-native arg). Two calls with the same key return the same
    data by construction, so the second is a re-fetch."""
    return (tool_name, json.dumps(dict(arguments), sort_keys=True, default=str))
