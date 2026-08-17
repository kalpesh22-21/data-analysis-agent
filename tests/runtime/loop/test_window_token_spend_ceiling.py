"""The per-window token SPEND ceiling (`max_window_token_spend`, 2026-08-12).

THE DEFECT THESE TESTS PIN DOWN. `BudgetGuard`'s token ceiling had no setting of
its own and was fed `settings.model_context_window` (128k). The counter sums each
round-trip's `usage.total_tokens` (prompt + completion) and every request replays
the whole conversation, so the counter accumulated `Σ context_k` — quadratic in
round count — and was compared against a SINGLE-REQUEST OCCUPANCY limit. Measured
on this same rig: an ordinary turn (~2.2k-token tool results) ended at round 10
with the real context at 19% of the window; ~4.3k results ended at round 8.

The counting was never the bug; the comparison was. Two different questions were
riding on one number, and only one of them is `BudgetGuard`'s:

  * OCCUPANCY — "will the next request overflow the window?" — is `max(context_k)`
    and is enforced per-request by `fit_request_to_budget` against
    `request_token_budget`. `test_occupancy_is_protected_with_no_spend_ceiling_at_all`
    proves the guard was never what protected it.
  * SPEND — "what has this window cost?" — genuinely is `Σ(prompt + completion)`,
    and now has its own ceiling.

The model doubles here report `usage.total_tokens` the way a provider does: the
size of the request they were actually handed plus a fixed completion. Nothing
about the accumulation law is invented — the request sizes come from the shipped
assembly pipeline, and the ceiling is checked by the real `BudgetGuard` inside the
real loop, never by a hand-set counter.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import UpdateAnalysisStateTool
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.context.budget import estimate_message_tokens
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.prompts import AGENT_SYSTEM_PROMPT
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import live_analysis_state

SESSION_ID = "sess-window-token-spend"
_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})
_TOOLS = [{"type": "function", "name": "runQuery", "description": "", "parameters": {}}]

_SETTINGS = RuntimeSettings(_env_file=None)
SHIPPED_SPEND_CEILING = _SETTINGS.max_window_token_spend  # 1_000_000
SHIPPED_REQUEST_BUDGET = _SETTINGS.request_token_budget()  # 89_600
SHIPPED_ITERATIONS = _SETTINGS.max_loop_iterations  # 25
CONTEXT_WINDOW = _SETTINGS.model_context_window  # 128_000 — the OLD (wrong) ceiling

_COMPLETION_TOKENS = 400

# Per-result payload sizes, in tokens, as `estimate_message_tokens` scores the
# stored preview. Both are measured, not chosen for effect:
#   ORDINARY — fatter than anything in the live traces; 25 full rounds of it spend
#              786,345, the top of the range the ceiling must NOT bind on.
#   RUNAWAY  — the point where requests begin plateauing against the request budget
#              and the extra spend is re-sent context rather than new analysis.
_ORDINARY_CELL_MULT = 30  # ~2,165 tok/result
_RUNAWAY_CELL_MULT = 60  # ~4,265 tok/result


async def _tools_provider(_c: RuntimeCredentials) -> list[dict]:
    return list(_TOOLS)


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt.body.sig", column_scope=frozenset())


def _result(mult: int) -> dict[str, Any]:
    cell = "engineering-operations-emea-" * mult
    return {
        "columns": ["EmployeeCode", "Department"],
        "rows": [[f"E{i}", cell] for i in range(10)],
        "row_count": 10,
        "truncated": False,
    }


class _MCP:
    def __init__(self, mult: int) -> None:
        self._mult = mult

    async def call_tool(self, tool_name, args, *, jwt, session_id):
        return _result(self._mult)

    async def list_tools(self, *, jwt, session_id):
        return []


class _NeverTerminating:
    """One fresh `runQuery` per round, forever — a long multi-intent turn's shape.

    Reports the REAL assembled request size as `usage.total_tokens`, so the spend
    the guard accumulates is the spend the shipped pipeline would actually incur.
    """

    def __init__(self) -> None:
        self.requests: list[int] = []  # per-round assembled request size, in tokens
        self._n = 0

    async def send_turn(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        request_tokens = sum(estimate_message_tokens(m) for m in messages)
        self.requests.append(request_tokens)
        self._n += 1
        sql = f"SELECT EmployeeCode, Department FROM employee WHERE Department='D{self._n}'"
        return ModelTurnResult(
            tool_calls=[ToolCallRequest(id=f"c{self._n}", name="runQuery", arguments={"sql": sql})],
            usage={"total_tokens": request_tokens + _COMPLETION_TOKENS},
        )

    @property
    def spend(self) -> int:
        return sum(r + _COMPLETION_TOKENS for r in self.requests)

    def begin_turn(self) -> _NeverTerminating:
        return self


class _FixedSpendModel:
    """Reports a FIXED `total_tokens` per round so the trip round is exact
    arithmetic — for boundary and fresh-window assertions where the point is the
    guard's accounting, not the payload."""

    def __init__(self, per_round_tokens: int) -> None:
        self.per_round_tokens = per_round_tokens
        self.calls = 0

    async def send_turn(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        self.calls += 1
        return ModelTurnResult(
            tool_calls=[
                ToolCallRequest(
                    id=f"c{self.calls}",
                    name="runQuery",
                    arguments={"sql": f"SELECT {self.calls}"},
                )
            ],
            usage={"total_tokens": self.per_round_tokens},
        )

    def begin_turn(self) -> _FixedSpendModel:
        return self


def _build(
    model: Any,
    *,
    mult: int = _ORDINARY_CELL_MULT,
    max_token_spend: int | None,
    max_loop_iterations: int = SHIPPED_ITERATIONS,
    max_budget_windows: int = 1,
    request_token_budget: int | None = SHIPPED_REQUEST_BUDGET,
    store: InMemorySessionStore | None = None,
    runtime_tools: dict[str, Any] | None = None,
) -> tuple[AgentLoop, InMemorySessionStore]:
    store = store or InMemorySessionStore()
    # No trail compaction pre-shrinks the request (Phase 1 bypasses it): the point is
    # what the loop SPENDS replaying a full trail, and what the request fit does
    # about occupancy.
    assembler = ContextAssembler(
        store,
        base_system_prompt=AGENT_SYSTEM_PROMPT,
    )
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(_MCP(mult), CATALOG),
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=max_loop_iterations,
        max_wall_clock_seconds=10_000,  # never the binding cap in these tests
        max_budget_windows=max_budget_windows,
        max_token_spend=max_token_spend,
        request_token_budget=request_token_budget,
        runtime_tools=runtime_tools or {},
    )
    return loop, store


# --- the fix: an ordinary window runs its full round budget ------------------


async def test_an_ordinary_window_runs_every_round_under_the_shipped_spend_ceiling() -> None:
    """The regression the fix exists for.

    With ~2.2k-token tool results — fatter than the live traces — a window must be
    ended by the ITERATION cap it was deliberately raised to, not by the token
    counter. Under the old wiring this exact run stopped at round 10 with the real
    context at 19% of the window.
    """
    model = _NeverTerminating()
    loop, _store = _build(model, max_token_spend=SHIPPED_SPEND_CEILING)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="how many?")

    assert outcome.status == "stopped_hard_ceiling"  # max_budget_windows=1
    assert len(model.requests) == SHIPPED_ITERATIONS, (
        f"the window ended after {len(model.requests)} rounds, not the {SHIPPED_ITERATIONS} "
        "the iteration cap allows — something other than iterations ended it"
    )
    # It ended on ROUNDS with spend to spare (measured: 786,345 of 1,000,000).
    assert model.spend < SHIPPED_SPEND_CEILING
    # ...and it spent FAR more than the old ceiling, which is precisely why the old
    # comparison ended the window at round 10: a spend sum is not an occupancy
    # reading, and here it is >6x the context window while the context is <50% of it.
    assert model.spend > CONTEXT_WINDOW
    assert max(model.requests) < CONTEXT_WINDOW


# --- the ceiling still bites, and where it is claimed to ---------------------


async def test_the_spend_ceiling_stops_a_runaway_window_at_the_measured_round() -> None:
    """`max_window_token_spend` is a real ceiling, not a formality.

    At ~4.3k-token results the requests plateau against `request_token_budget` and
    the extra spend is re-sent context, not new analysis. Measured: that window
    pauses at round 21 of a possible 25. Asserted as a range, so an assembly change
    that shifts the request size by a few hundred tokens does not fail the suite
    while a change that moves the ceiling's bite by rounds does.
    """
    model = _NeverTerminating()
    loop, _store = _build(
        model,
        mult=_RUNAWAY_CELL_MULT,
        max_token_spend=SHIPPED_SPEND_CEILING,
        max_budget_windows=3,
    )

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="how many?")

    # A budget-cap PAUSE, not a hard stop: the user is asked, and may continue.
    assert outcome.status == "paused_budget_cap"
    assert outcome.pending_question is not None
    # SPEND ended it — not iterations, not the wall clock.
    rounds = len(model.requests)
    assert rounds < SHIPPED_ITERATIONS
    assert model.spend >= SHIPPED_SPEND_CEILING
    assert 19 <= rounds <= 23, f"the ceiling bit at round {rounds}, not the measured ~21"


async def test_the_spend_ceiling_trips_at_the_exact_round_not_before_or_after() -> None:
    """Boundary, through the real guard: 4 x 250,000 exactly reaches 1,000,000, so
    the window must end on the 4th round-trip — never the 3rd (early) nor the 5th
    (late). A cap of 1 round could not distinguish an off-by-one from correct."""
    model = _FixedSpendModel(per_round_tokens=SHIPPED_SPEND_CEILING // 4)
    loop, _store = _build(model, max_token_spend=SHIPPED_SPEND_CEILING, max_loop_iterations=99)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="how many?")

    assert outcome.status == "stopped_hard_ceiling"  # max_budget_windows=1
    assert model.calls == 4


async def test_one_token_under_the_ceiling_does_not_trip_it() -> None:
    """The other side of the boundary: the same four rounds, one token cheaper each,
    must NOT end the window — proving the trip above was the ceiling and not an
    unrelated cap."""
    model = _FixedSpendModel(per_round_tokens=(SHIPPED_SPEND_CEILING // 4) - 1)
    loop, _store = _build(model, max_token_spend=SHIPPED_SPEND_CEILING, max_loop_iterations=4)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="how many?")

    assert outcome.status == "stopped_hard_ceiling"  # ended by ITERATIONS at 4
    assert model.calls == 4


# --- occupancy is a different concern, protected elsewhere ------------------


async def test_occupancy_is_protected_with_no_spend_ceiling_at_all() -> None:
    """The load-bearing half of the two-concerns split: turn the spend ceiling OFF
    entirely and drive the fattest payload for a full 25 rounds — every single
    request handed to `send_turn` must still fit `request_token_budget`.

    If this passes with `max_token_spend=None`, the spend counter was never what
    protected occupancy, and giving it a spend-shaped ceiling cannot have weakened
    occupancy. `fit_request_to_budget` is what holds the line.
    """
    model = _NeverTerminating()
    loop, _store = _build(model, mult=150, max_token_spend=None)  # ~10.5k tok/result

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="how many?")

    assert outcome.status == "stopped_hard_ceiling"
    assert len(model.requests) == SHIPPED_ITERATIONS
    assert max(model.requests) <= SHIPPED_REQUEST_BUDGET, (
        f"a request reached {max(model.requests)} tokens, over the "
        f"{SHIPPED_REQUEST_BUDGET} occupancy budget"
    )
    # The request really was pressed against the budget (so the assertion above is
    # not vacuous), while cumulative spend ran to many times the context window.
    assert max(model.requests) > 0.9 * SHIPPED_REQUEST_BUDGET
    assert model.spend > 10 * CONTEXT_WINDOW


# --- the pause path: continue grants a FRESH window -------------------------


async def test_a_spend_cap_pause_grants_a_fresh_spend_window_on_continue() -> None:
    """D55 end to end, driven by SPEND rather than iterations: each window trips at
    the same round because the new window's guard starts at zero spend — a
    "continue" grants a fresh window, never more budget on the spent one."""
    per_window_rounds = 3
    # +1 so three rounds EXCEED the ceiling rather than landing a token short of it.
    model = _FixedSpendModel(per_round_tokens=SHIPPED_SPEND_CEILING // per_window_rounds + 1)
    loop, store = _build(
        model,
        max_token_spend=SHIPPED_SPEND_CEILING,
        max_loop_iterations=99,
        max_budget_windows=3,
    )

    statuses: list[str] = []
    calls_per_window: list[int] = []
    seen = 0
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="how many?")
    statuses.append(outcome.status)
    calls_per_window.append(model.calls - seen)
    seen = model.calls
    while outcome.status == "paused_budget_cap":
        outcome = await loop.resume(
            session_id=SESSION_ID, credentials=_creds(), answer="continue"
        )
        statuses.append(outcome.status)
        calls_per_window.append(model.calls - seen)
        seen = model.calls

    assert statuses == ["paused_budget_cap", "paused_budget_cap", "stopped_hard_ceiling"]
    # Every window spent the SAME amount before capping — no carryover (D55).
    assert calls_per_window == [per_window_rounds] * 3
    # The turn as a whole therefore spends up to max_budget_windows x the ceiling.
    assert model.calls * model.per_round_tokens >= 3 * SHIPPED_SPEND_CEILING
    doc = await store.get_or_create_session(SESSION_ID)
    assert doc.pause_checkpoint is None or doc.pause_checkpoint.consumed


async def test_a_spend_triggered_hard_ceiling_still_force_blocks_pending_intents() -> None:
    """05 §F is unchanged when the ceiling that fires is SPEND.

    `stopped_hard_ceiling` is terminal however it was reached, so every surviving
    `pending` intent must be recorded `BUDGET_EXHAUSTED` — the reason code is right
    here precisely because capacity IS the cause.
    """
    store = InMemorySessionStore()
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[
                    ToolCallRequest(
                        id="s1",
                        name="updateAnalysisState",
                        arguments={"intents": [{"description": "headcount by department"}]},
                    )
                ],
                usage={"total_tokens": SHIPPED_SPEND_CEILING},  # one round exhausts it
            )
        ]
    )
    events: list[tuple[str, dict[str, Any]]] = []
    assembler = ContextAssembler(store)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG),
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=99,  # NOT the cap that fires
        max_wall_clock_seconds=10_000,  # NOT the cap that fires
        max_budget_windows=1,
        max_token_spend=SHIPPED_SPEND_CEILING,
        observer=lambda name, payload: events.append((name, payload)),
        runtime_tools={
            "updateAnalysisState": UpdateAnalysisStateTool(
                session_store=store,
                observer=lambda name, payload: events.append((name, payload)),
            )
        },
    )

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="one thing")

    assert outcome.status == "stopped_hard_ceiling"
    doc = await store.get_or_create_session(SESSION_ID)
    state = live_analysis_state(doc, 0)
    assert [(i.status, i.reason_code) for i in state.intents] == [("blocked", "BUDGET_EXHAUSTED")]
    assert ("loop_intent_force_blocked", {"intent_id": "i1", "reason_code": "BUDGET_EXHAUSTED"}) in [
        (name, payload) for name, payload in events
    ]
