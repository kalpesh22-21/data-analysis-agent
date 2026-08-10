"""Load-bearing regression (2026-08): the FULL assembled request handed to
`send_turn` is bounded by a TOTAL-request token budget, and the base prompt is
NEVER front-truncated out of the model window — even after many iterations that
pile MULTIPLE ~30k-token tool results into the request so the RAW request exceeds
the model context window (the production trace: 274k tokens vs a 128k window that
let the endpoint drop `messages[0]`).

Two complementary scenarios, both asserting on EVERY main-loop `send_turn` payload
that (1) messages[0] is the base prompt (system), byte-identical AND the sole
system message; (2) total estimated tokens <= request_token_budget; (3) the
current user question is present; (4) no orphaned tool / assistant-tool_calls
(pairing intact); (5) the OLDEST content is trimmed while recent content survives:

  * `test_..._runaway_single_turn` — a SINGLE non-terminating turn that piles many
    fat results across iterations/windows (cap -> resume -> hard ceiling). Because
    the interleaved layout puts the current turn's tool pairs in the tail (their
    `ts` follows the question), this is the case the naive "pin the whole tail" fit
    would leave UN-trimmable — reproducing the front-truncation bug. The tiered fit
    must instead pin only the most-recent K current-turn pairs and drop the OLDER
    ones, so the request stays bounded.
  * `test_..._every_send_turn` (multi-turn `_PerTurnModel`) — several polite turns,
    each one fat query + answer, so PRIOR turns pile up as the droppable middle and
    the fit trims whole OLDEST turns while pinning base + current question + this
    turn's own tool pair.
"""

from __future__ import annotations

import copy
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.context.budget import estimate_message_tokens
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.prompts import AGENT_SYSTEM_PROMPT
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})
SESSION_ID = "sess-total-request-budget"
SECRET_JWT = "jwt.body.sig"
_TOOLS = [{"type": "function", "name": "runQuery", "description": "", "parameters": {}}]

# A fat runQuery result: 20 wide rows so a single tool result is ~30k tokens —
# the "giant single tool result" the bug report measured, in the trail verbatim.
_WIDE_CELL = "lorem-ipsum-dolor-sit-amet-consectetur-" * 160  # ~6.2k chars
_REQUEST_TOKEN_BUDGET = 60_000
_ITERS_PER_WINDOW = 8
_MAX_WINDOWS = 3


def _fat_result() -> dict[str, Any]:
    return {
        "columns": ["EmployeeCode", "Department"],
        "rows": [[f"E{i}", _WIDE_CELL] for i in range(20)],
        "row_count": 20,
        "truncated": False,
    }


async def _tools_provider(_c: RuntimeCredentials) -> list[dict]:
    return list(_TOOLS)


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=SECRET_JWT, column_scope=frozenset())


class _PerTurnModel:
    """One fat, DISTINCT `runQuery` then a final answer, per external turn. Records
    every payload tagged with its turn label so the deepest (last-turn) call — with
    the largest accumulated PRIOR-turn history — can be inspected.

    In the interleaved layout the droppable middle is the PRIOR turns (each turn's
    user question + tool pair + assistant answer, contiguous by the merge sort), so
    the total-request budget bounds the request by dropping whole oldest turns while
    the base prompt, the current question, and the current turn's own tool pair are
    pinned (design's fit invariant: "base + current question + current-turn tools
    survive"). This mirrors the real 80-turn production trace far more directly than
    a single runaway turn (which the new layout pins in the tail)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []
        self.turn_label = "t0"
        self._did_query_this_turn = False
        self._n = 0

    def new_turn(self, label: str) -> None:
        self.turn_label = label
        self._did_query_this_turn = False

    async def send_turn(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        self.calls.append((self.turn_label, copy.deepcopy(messages)))
        if not self._did_query_this_turn:
            self._did_query_this_turn = True
            self._n += 1
            sql = f"SELECT EmployeeCode, Department FROM employee WHERE Department='D{self._n}'"
            return ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id=f"c{self._n}", name="runQuery", arguments={"sql": sql})
                ],
                usage={"total_tokens": 5000},
            )
        return ModelTurnResult(
            assistant_text=f"[{self.turn_label}] the answer", usage={"total_tokens": 5000}
        )

    def begin_turn(self) -> _PerTurnModel:
        return self


class _NeverTerminatingModel:
    """Records every `messages` payload and ALWAYS issues a fresh, DISTINCT runQuery
    within ONE external turn, so the turn runs to the hard ceiling and piles many
    fat results into the CURRENT turn's tool pairs (all `turn_index` 0). In the
    interleaved layout these pairs land in the tail (their `ts` follows the
    question) — the exact case the tiered fit must keep bounded by trimming the
    OLDER current-turn pairs while pinning the most-recent K."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self._n = 0

    async def send_turn(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        self.calls.append(copy.deepcopy(messages))
        self._n += 1
        sql = f"SELECT EmployeeCode, Department FROM employee WHERE Department='D{self._n}'"
        return ModelTurnResult(
            tool_calls=[ToolCallRequest(id=f"c{self._n}", name="runQuery", arguments={"sql": sql})],
            usage={"total_tokens": 5000},
        )

    def begin_turn(self) -> _NeverTerminatingModel:
        return self


# A budget large enough to pin base + question + K(=3, the default) fat current-turn
# pairs (~31k each) — so "total <= budget" holds while the OLDER current-turn pairs
# are still trimmed. (The 60k multi-turn budget above cannot hold 3 pinned pairs.)
_RUNAWAY_REQUEST_TOKEN_BUDGET = 110_000
_RUNAWAY_PINNED_K = 3


class _FatMCP:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call_tool(self, tool_name, args, *, jwt, session_id):
        self.calls.append(tool_name)
        return _fat_result()

    async def list_tools(self, *, jwt, session_id):
        return []


def _assert_pairing_intact(messages: list[dict[str, Any]]) -> None:
    open_ids: set[str] = set()
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                open_ids.add(tc["id"])
        elif m["role"] == "tool":
            assert m["tool_call_id"] in open_ids, (
                f"orphan tool message {m.get('tool_call_id')} without a preceding tool_calls"
            )
            open_ids.discard(m["tool_call_id"])
    assert not open_ids, f"assistant tool_calls left dangling without a tool result: {open_ids}"


def _tool_pair_sqls(messages: list[dict[str, Any]]) -> list[str]:
    """The SQL of every surviving assistant runQuery tool_call, in order."""
    sqls: list[str] = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                sqls.append(tc["function"]["arguments"])
    return sqls


async def test_total_request_budget_pins_base_prompt_and_bounds_every_send_turn() -> None:
    model = _PerTurnModel()
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(_FatMCP(), CATALOG)
    # history_token_budget HIGH so trail compaction does not pre-shrink the trail —
    # we are exercising the SEPARATE total-request fit, which must bound the whole
    # request even when the trail replays verbatim and grows unbounded.
    assembler = ContextAssembler(
        store,
        history_token_budget=10_000_000,
        base_system_prompt=AGENT_SYSTEM_PROMPT,
    )
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=_ITERS_PER_WINDOW,
        max_wall_clock_seconds=999,
        max_budget_windows=_MAX_WINDOWS,
        request_token_budget=_REQUEST_TOKEN_BUDGET,
    )

    # Drive several external turns. Each turn issues ONE fat (~30k-token) runQuery
    # then answers, so PRIOR turns pile up in history: after N turns the raw request
    # (base + N fat tool pairs + N question/answer exchanges + the current question)
    # dwarfs the budget on every later turn — the multi-turn analogue of the 80-turn
    # production trace. In the interleaved layout those prior turns ARE the droppable
    # middle (each turn's user question -> tool pair -> assistant answer is contiguous
    # by the merge sort), so the fit must trim whole OLDEST turns while pinning the
    # base prompt, THIS turn's current question, and this turn's own tool pair.
    n_turns = 5
    questions: dict[str, str] = {}
    for t in range(1, n_turns + 1):
        label = f"t{t}"
        question = f"question {label}?"
        questions[label] = question
        model.new_turn(label)
        outcome = await loop.run(
            session_id=SESSION_ID, credentials=_creds(), user_message=question
        )
        assert outcome.status == "done"

    # Two send_turn calls per external turn (one fat query, one answer).
    assert len(model.calls) == 2 * n_turns

    # A single fat result is genuinely ~30k tokens — so a raw untrimmed request
    # with several of them dwarfs the budget (the scenario the fix must contain).
    assert estimate_message_tokens(
        {"role": "tool", "tool_call_id": "x", "content": str(_fat_result())}
    ) > 25_000

    for i, (label, msgs) in enumerate(model.calls):
        # (1) base prompt byte-identical at index 0 — NEVER dropped/front-truncated.
        assert msgs[0]["role"] == "system", f"call[{i}] messages[0] is not system"
        assert msgs[0]["content"] == AGENT_SYSTEM_PROMPT, (
            f"call[{i}] dropped/altered the base prompt at messages[0]"
        )
        # ...and it is the SOLE system message after interleave + discovery + fit.
        assert sum(1 for m in msgs if m["role"] == "system") == 1, (
            f"call[{i}] emitted more than the sole base-prompt system message"
        )
        # (2) total request within the budget.
        total = sum(estimate_message_tokens(m) for m in msgs)
        assert total <= _REQUEST_TOKEN_BUDGET, (
            f"call[{i}] total {total} exceeds budget {_REQUEST_TOKEN_BUDGET}"
        )
        # (3) THIS turn's current question survives (pinned tail).
        assert any(
            m["role"] == "user" and m.get("content") == questions[label] for m in msgs
        ), f"call[{i}] ({label}) dropped its current user question"
        # (4) pairing intact — no orphan tool / dangling tool_calls.
        _assert_pairing_intact(msgs)

    # (5) The DEEPEST call (the LAST turn's answer round-trip, with the largest
    # accumulated prior history) proves OLDEST-first turn trimming: the FIRST turn's
    # fat result is gone, while THIS turn's own tool pair — pinned in the tail after
    # the current question — survives (design fit invariant: base + current question
    # + current-turn tools survive; oldest turn dropped first).
    deepest_label, deepest = model.calls[-1]
    assert deepest_label == f"t{n_turns}"
    sqls = _tool_pair_sqls(deepest)
    assert sqls, "the deepest call kept no tool pairs at all"
    # The very first turn's result (Department='D1') was dropped as oldest...
    assert not any("'D1'" in s for s in sqls), "oldest turn's result was NOT trimmed"
    # ...and this turn's own (most recent) result survived (pinned current-turn tool).
    assert any(f"'D{n_turns}'" in s for s in sqls), (
        f"the current turn's result (D{n_turns}) did not survive the trim"
    )
    # Far fewer pairs survived than were produced — the request really was trimmed.
    assert len(sqls) < n_turns


async def test_total_request_budget_bounds_a_runaway_single_turn() -> None:
    """The BLOCKER regression: a SINGLE non-terminating turn piles many fat
    (~30k-token) tool results into the CURRENT turn's tool pairs. In the interleaved
    layout those pairs land in the tail (their `ts` follows the question); a naive
    "pin the whole tail" fit would leave them UN-trimmable → the request exceeds the
    budget then the model window → the exact front-truncation bug. The tiered fit
    must pin only the most-recent K current-turn pairs and DROP the older ones, so
    the request stays bounded on EVERY send_turn while the model runs to the hard
    ceiling."""
    model = _NeverTerminatingModel()
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(_FatMCP(), CATALOG)
    assembler = ContextAssembler(
        store,
        history_token_budget=10_000_000,  # HIGH: exercise the total-request fit, not compaction.
        base_system_prompt=AGENT_SYSTEM_PROMPT,
    )
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=_ITERS_PER_WINDOW,
        max_wall_clock_seconds=999,
        max_budget_windows=_MAX_WINDOWS,
        request_token_budget=_RUNAWAY_REQUEST_TOKEN_BUDGET,
        request_budget_pinned_recent_tool_pairs=_RUNAWAY_PINNED_K,
    )

    statuses: list[str] = []
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="how many?")
    statuses.append(outcome.status)
    while outcome.status == "paused_budget_cap":
        outcome = await loop.resume(session_id=SESSION_ID, credentials=_creds(), answer="continue")
        statuses.append(outcome.status)

    # The turn really ran deep: cap -> resume -> ... -> hard ceiling.
    assert statuses[-1] == "stopped_hard_ceiling"
    assert len(model.calls) == _ITERS_PER_WINDOW * _MAX_WINDOWS

    # A single fat result is genuinely ~30k tokens — several dwarf the budget.
    assert estimate_message_tokens(
        {"role": "tool", "tool_call_id": "x", "content": str(_fat_result())}
    ) > 25_000

    for i, msgs in enumerate(model.calls):
        # (1) base prompt byte-identical at index 0, and the SOLE system message.
        assert msgs[0]["role"] == "system", f"call[{i}] messages[0] is not system"
        assert msgs[0]["content"] == AGENT_SYSTEM_PROMPT, (
            f"call[{i}] dropped/altered the base prompt at messages[0]"
        )
        assert sum(1 for m in msgs if m["role"] == "system") == 1, (
            f"call[{i}] emitted more than the sole base-prompt system message"
        )
        # (2) EVERY send_turn is within the budget — the un-trimmable-tail bug is gone.
        total = sum(estimate_message_tokens(m) for m in msgs)
        assert total <= _RUNAWAY_REQUEST_TOKEN_BUDGET, (
            f"call[{i}] total {total} exceeds budget {_RUNAWAY_REQUEST_TOKEN_BUDGET}"
        )
        # (3) the current question survives.
        assert any(
            m["role"] == "user" and m.get("content") == "how many?" for m in msgs
        ), f"call[{i}] dropped the current user question"
        # (4) pairing intact — no orphan tool / dangling tool_calls.
        _assert_pairing_intact(msgs)

    # (5) The DEEPEST call proves the tiered current-turn dropping: the OLDEST
    # current-turn pair (D1) is trimmed, while the most-recent K (=3) survive.
    deepest = model.calls[-1]
    sqls = _tool_pair_sqls(deepest)
    produced = model.calls[-1] and (len(model.calls) - 1)  # D1..D{produced} are in the deepest payload
    assert not any("'D1'" in s for s in sqls), "oldest current-turn result was NOT trimmed"
    # The three most-recent current-turn results survived (pinned recent-K).
    for k in range(produced - _RUNAWAY_PINNED_K + 1, produced + 1):
        assert any(f"'D{k}'" in s for s in sqls), f"recent current-turn result D{k} was trimmed"
    # Exactly K current-turn pairs survive under this tight budget — the older ones
    # (tier 2) were dropped so the request fit.
    assert len(sqls) == _RUNAWAY_PINNED_K
    assert len(sqls) < produced  # far fewer than were produced — really trimmed.
