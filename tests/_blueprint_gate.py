"""Test helper for the getBlueprint-before-runBlueprint gate
(`loop/agent_loop.py::_blueprint_definition_not_read`).

The gate refuses a `runBlueprint` unless THIS turn's trail already holds a
successful `getBlueprint` for the same id. Every Layer-1 test that scripts a
`runBlueprint` directly — because it is testing the executor seam, the pause
checkpoint, the intent tag, the redaction, and not the routing decision — must
therefore say that the model expanded the blueprint first.

`expand_blueprint` writes exactly the trail entry a successful `getBlueprint` leaves
behind. It is deliberately a REAL persisted entry rather than a flag that disables
the gate: the gate reads the trail, so a test that seeds it is exercising the same
predicate production does. Nothing here weakens an assertion — the tests keep every
claim they made, with one more (true) fact about the turn established up front.

`turn_index` defaults to 0 because `AgentLoop.run` computes `turn_index = 0` for a
session with no messages, which is the case in every Layer-1 loop test. A resume
test that starts from a seeded turn passes its own.
"""

from __future__ import annotations

from data_agent.runtime.session.models import TrailEntry
from data_agent.runtime.session.store import SessionStore


async def expand_blueprint(
    store: SessionStore,
    session_id: str,
    blueprint_id: str,
    *,
    turn_index: int = 0,
    tool_call_id: str | None = None,
    ts: str = "2026-01-01T00:00:00+00:00",
) -> None:
    """Record that the model expanded *blueprint_id* with `getBlueprint` this turn."""
    await store.append_trail_entry(
        session_id,
        TrailEntry(
            turn_index=turn_index,
            tool_call_id=tool_call_id or f"gb-{blueprint_id}",
            tool_name="getBlueprint",
            args={"id": blueprint_id},
            status="ok",
            error_code=None,
            # The real tool returns the blueprint's scoped `uses` footprint; an empty
            # determined set is in scope under every scope and keeps the seed from
            # interacting with a test's own column-scope narrowing.
            provenance=frozenset(),
            result_preview=None,
            result_full_ref=None,
            ts=ts,
        ),
    )
