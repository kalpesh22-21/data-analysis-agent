"""Repeated-idempotent-read guard primitives — the neutral, dependency-free core
shared by `loop/agent_loop.py` and `context/discovery_emulation.py`.

Extracted here so `discovery_emulation` no longer reaches into `agent_loop`'s
private module namespace at runtime (which forced `agent_loop` to keep its own
reciprocal reference behind a `TYPE_CHECKING` guard — a fragile cycle where any
future runtime import of `discovery_emulation` from `agent_loop` would crash at
startup). Both modules now depend only on THIS leaf, in one direction.

`IDEMPOTENT_READ_TOOLS`: the idempotent, side-effect-free read tools whose result
depends ONLY on their arguments — so an identical repeat within a turn is guaranteed
to return the same data the first call already fetched. The
repeated-idempotent-read guard (generalizing D94) declines to re-dispatch such a
repeat and injects a data-free "you already have this" nudge instead.
runQuery/runBlueprint/askUser/resolveValues are deliberately EXCLUDED — a repeated
runQuery may be a distinct legitimate step and is never guarded here.

`getBlueprint` is INCLUDED (Release 1). It is a keyed fetch of one stored blueprint
definition by id, so it satisfies this module's own criterion — result depends only
on the arguments — exactly as well as `getTableSchema` does. It was added when the
always-`getBlueprint`-before-`runBlueprint` rule made it the most repeated read of a
blueprint turn: every run now requires one, the gate is turn-scoped so each turn
re-fetches, and a run refused for an unrelated reason (a missing slot) invites a
defensive re-fetch of a definition the model already has.

⚠ THE GUARD'S CONTRACT IS NOT "an identical repeat is always deduped". It is:

    an identical repeat is deduped **unless the first result is no longer
    readable**, in which case it is re-dispatched (capped).

The distinction is the whole safety property. The guard fires off `seen_read_calls`,
which is seeded from the PERSISTED TRAIL — and the trail is not the RENDERED window.
`context/budget.py::fit_request_to_budget` pins only the K most recent current-turn
tool pairs and drops older ones under budget pressure, and `context/assembly.py`
replaces a stranded entry with a data-free sentinel. Under the old unconditional
contract the model was then told "you already have this" about something it could
not read, with no move that recovered it — and the base prompt's own escape ("fetch
it again only if you can no longer read it") was unfollowable, because the guard
deduped that re-fetch too.

`loop/agent_loop.py` therefore exempts a repeat whose SERVING RESULT is absent from
the post-fit canonical messages (sentinel renderings excluded — a message bearing
the id is not the same as a readable result). Uniform across every tool in this set:
the predicate is a property of the context, not of the tool. Bounded by
`_MAX_TRIMMED_READ_REFETCHES` per signature per budget window, because a read that
is fetched → trimmed → re-fetched → trimmed is in aggregate the very waste the guard
exists to prevent.

`idempotent_read_signature`: the content key that identifies an already-served
idempotent read — the tool name plus its canonicalized arguments (stable key
order, `str`-coerced for any non-JSON-native arg). Two calls with the same key
return the same data by construction, so the second is a re-fetch.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

IDEMPOTENT_READ_TOOLS = frozenset(
    {"getTableSchema", "listTables", "listDatabases", "explainQuery", "getBlueprint"}
)

__all__ = ["IDEMPOTENT_READ_TOOLS", "idempotent_read_signature"]


def idempotent_read_signature(tool_name: str, arguments: Mapping[str, Any]) -> tuple[str, str]:
    """The content key that identifies an already-served idempotent read: the
    tool name plus its canonicalized arguments (stable key order, `str`-coerced
    for any non-JSON-native arg). Two calls with the same key return the same
    data by construction, so the second is a re-fetch."""
    return (tool_name, json.dumps(dict(arguments), sort_keys=True, default=str))
