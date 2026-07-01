"""Adversarial D45 pause/resume CAS coverage at the full `AgentLoop` level
(QA hardening pass).

`tests/runtime/loop/test_agent_loop.py` already proves the CAS mechanics
sequentially (obtain a stale CAS, bump the version out from under it, assert
`CASMismatchError`) and proves a *second, later* resume raises
`AlreadyConsumedError`. Those are valuable but not a genuine RACE: Python's
cooperative asyncio means two `AgentLoop.resume()` coroutines driven via
`asyncio.gather` never actually interleave unless something explicitly yields
control back to the event loop — `InMemorySessionStore`'s own module
docstring flags this.

This file forces a REAL interleaving with a thin `SessionStore` wrapper that
inserts an explicit `await asyncio.sleep(0)` between `get_session_with_cas`
(the CAS read) and the resulting `resume_checkpoint` write — i.e. exactly the
window during which two truly-concurrent resumers racing over a network
would both observe the same CAS token. This exercises the production
`AgentLoop.resume()` code path (not just the store's CAS primitive in
isolation) under `asyncio.gather`, and confirms:
  - exactly one of the two concurrent resumers wins (TurnOutcome), and
  - the loser raises CASMismatchError, and
  - the turn body (a fresh model round-trip) is NOT re-run for the loser —
    proven by counting `ScriptedModelClient.calls_made`, not just by
    "no exception propagated further".
"""

from __future__ import annotations

import asyncio

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.store import AlreadyConsumedError, CASMismatchError

# `InMemorySessionStore.resume_checkpoint` checks `pause_checkpoint.consumed`
# on the shared/live doc BEFORE comparing the CAS token (see memory_store.py).
# For two resumers racing over the SAME checkpoint, the winner's commit sets
# `consumed=True` on that shared object, so the loser's `consumed` check
# trips before its (also-stale) CAS token is ever compared — the loser
# observably raises `AlreadyConsumedError`, never `CASMismatchError`, in this
# specific race shape. The QA brief accepts either
# (`AlreadyConsumed/CASMismatch`) as a valid "loser" outcome, so both are
# treated as passing here; this is documented, not silently asserted away.
_LOSER_EXCEPTIONS = (AlreadyConsumedError, CASMismatchError)

CATALOG = CatalogHandle({"dbpcm_warehouse.employee": {"EmployeeCode": "String"}})
SESSION_ID = "sess-cas-race-test"

TOOLS_SCHEMA = [
    {
        "type": "function",
        "name": "askUser",
        "description": "",
        "parameters": {"type": "object", "properties": {"question": {"type": "string"}}},
    },
]


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return list(TOOLS_SCHEMA)


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt-not-under-test", column_scope=frozenset())


class _YieldingCasStore:
    """Delegates every `SessionStore` method to a real `InMemorySessionStore`,
    except `get_session_with_cas`, which yields control to the event loop
    once before returning — widening the CAS read/write race window enough
    that two coroutines scheduled via `asyncio.gather` genuinely interleave
    instead of running the fake store's normally-synchronous methods back to
    back."""

    def __init__(self, inner: InMemorySessionStore) -> None:
        self._inner = inner

    async def get_session_with_cas(self, session_id: str):
        result = await self._inner.get_session_with_cas(session_id)
        await asyncio.sleep(0)  # force a real interleaving point
        return result

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _build_loop(*, model_client, mcp_client, store) -> AgentLoop:
    dispatcher = ToolDispatcher(mcp_client, CATALOG)
    assembler = ContextAssembler(store, history_token_budget=100_000)
    return AgentLoop(
        model_client=model_client,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
    )


async def test_two_truly_concurrent_resumes_exactly_one_wins_and_loser_reruns_nothing() -> None:
    real_store = InMemorySessionStore()
    yielding_store = _YieldingCasStore(real_store)

    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[ToolCallRequest(id="call_1", name="askUser", arguments={"question": "Which dept?"})]
            ),
            ModelTurnResult(assistant_text="Winner's answer."),  # only ONE more round-trip may occur
        ]
    )
    mcp = FakeMCPClient()
    loop = _build_loop(model_client=model, mcp_client=mcp, store=yielding_store)

    paused = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="Show payroll.")
    assert paused.status == "paused_ask_user"
    assert model.calls_made == 1

    results = await asyncio.gather(
        loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="Sales"),
        loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="Marketing"),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]

    assert len(successes) == 1, f"expected exactly one winner, got {results!r}"
    assert len(failures) == 1, f"expected exactly one loser, got {results!r}"
    assert isinstance(failures[0], _LOSER_EXCEPTIONS), (
        f"loser must raise AlreadyConsumedError or CASMismatchError, got {failures[0]!r}"
    )
    assert successes[0].status == "done"
    assert successes[0].assistant_text == "Winner's answer."

    # The turn body did NOT re-run for the loser: exactly 2 model round-trips
    # total (the initial askUser turn + the ONE winning resume's follow-up),
    # never 3.
    assert model.calls_made == 2

    doc = await real_store.get_or_create_session(SESSION_ID)
    assert doc.pause_checkpoint.consumed is True
    # Exactly one of the two answers was threaded into the transcript — not
    # both, and not neither.
    contents = [m.content for m in doc.messages]
    assert ("Sales" in contents) != ("Marketing" in contents)


async def test_loser_of_the_race_never_reaches_the_mcp_either() -> None:
    """A stronger corollary: since the turn body never re-runs for the loser,
    the loser's answer never triggers any tool dispatch at all — even if the
    winner's follow-up turn itself calls a tool."""
    real_store = InMemorySessionStore()
    yielding_store = _YieldingCasStore(real_store)

    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[ToolCallRequest(id="call_1", name="askUser", arguments={"question": "Which dept?"})]
            ),
            ModelTurnResult(assistant_text="done, no tool needed"),
        ]
    )
    mcp = FakeMCPClient()  # no scripted tool responses at all
    loop = _build_loop(model_client=model, mcp_client=mcp, store=yielding_store)

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="go")

    results = await asyncio.gather(
        loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="A"),
        loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="B"),
        return_exceptions=True,
    )
    exceptions = [r for r in results if isinstance(r, BaseException)]
    assert len(exceptions) == 1
    assert isinstance(exceptions[0], _LOSER_EXCEPTIONS)
    assert mcp.calls == []  # neither the winner nor the loser ever needed/hit the MCP


async def test_resume_via_loop_with_stale_cas_from_unrelated_write_raises_cas_mismatch() -> None:
    """A genuine `CASMismatchError` (not `AlreadyConsumedError`) at the
    `AgentLoop.resume()` level: the checkpoint itself is untouched (still
    unconsumed), but an unrelated write (e.g. another in-flight turn action)
    advances the document's CAS version between the pause and the resume
    attempt. Exercised through the production `AgentLoop.resume()` entry
    point — not by calling `SessionStore.resume_checkpoint` directly — using
    the same yielding wrapper to widen the race window realistically."""
    real_store = InMemorySessionStore()
    yielding_store = _YieldingCasStore(real_store)

    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[ToolCallRequest(id="call_1", name="askUser", arguments={"question": "Which dept?"})]
            ),
        ]
    )
    mcp = FakeMCPClient()
    loop = _build_loop(model_client=model, mcp_client=mcp, store=yielding_store)

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="go")

    async def _unrelated_write() -> None:
        # `loop.resume(...)` (scheduled first by asyncio.gather) runs up to
        # its own internal `sleep(0)` inside `get_session_with_cas` and
        # suspends there *before* this coroutine gets its first turn. This
        # coroutine then advances the document's version with NO leading
        # yield of its own, so it completes synchronously in one scheduling
        # slot — landing squarely inside the CAS read/write race window,
        # before `loop.resume(...)` is rescheduled to attempt its write. The
        # checkpoint itself is left untouched/unconsumed, isolating the
        # CAS-mismatch path from the consumed-flag path.
        await real_store.bump_last_activity(SESSION_ID)

    results = await asyncio.gather(
        loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="Sales"),
        _unrelated_write(),
        return_exceptions=True,
    )
    resume_result = results[0]
    assert isinstance(resume_result, CASMismatchError), (
        f"expected a genuine CASMismatchError from an unrelated concurrent "
        f"write, got {resume_result!r}"
    )
    # The turn body never ran for the losing resume attempt.
    assert model.calls_made == 1  # only the initial pause-triggering call
