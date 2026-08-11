"""The extractor's search loop, driven by a HOSTILE model. Everything here passes.

`test_search_corpus_tool.py` drives the loop with a cooperative model that searches a
bounded number of times and then emits. This file drives it with one that does not
cooperate at all, because plan §3a turned a straight-line "one forced tool call, retry
on malformed" into a `while` loop, and the only thing standing between that and an
unbounded spend is an argument about which of two counters strictly decreases on every
iteration:

    while attempts_left > 0:
        if searches_left > 0 and _is_search_only(result):
            searches_left -= served          # served >= 1 whenever this branch is taken
            continue
        attempts_left -= 1

`served >= 1` holds because `_is_search_only` is only true when the turn contains at
least one `searchCorpus` call, and the first such call always has `served (0) < budget`
given the branch's own `searches_left > 0` guard. That is a two-step argument across
three functions, so it is checked here empirically instead: every test asserts a
concrete finite turn count, and the parametrized matrix asserts the GLOBAL bound
`max_search_calls + max_retries + 1` over every hostile script shape at once.

The scripts are deliberately longer than any bound the loop could have (20 turns against
a budget of 3+3): `ScriptedModelClient` raises `AssertionError` when the loop asks for a
turn it did not script, so "the loop wanted more turns than the bound" fails loudly
rather than hanging, and a genuine non-termination shows up as a test timeout rather
than a wedged suite.

Slug: PA-search-loop-termination.
"""

from __future__ import annotations

import pytest

from data_agent.learning.extractor.schema import (
    EXTRACTOR_TOOL_NAME,
    SEARCH_CORPUS_TOOL_NAME,
    SchemaMismatchError,
)
from data_agent.learning.priorart import InMemoryPriorArtIndex
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest

from .helpers import KEEP_VERDICT, blueprint_raw, make_extractor, make_summary

_BUDGET = 3
_RETRIES = 2
# 1 initial parse attempt + `_RETRIES` retries + `_BUDGET` search turns. Every hostile
# script must terminate inside this, whatever it interleaves.
_MAX_TURNS = _BUDGET + _RETRIES + 1

# Far more turns than any bound, so exhausting the script means the loop over-ran.
_OVERLONG = 20


def _search(cid="c", **args):
    return ToolCallRequest(
        id=cid, name=SEARCH_CORPUS_TOOL_NAME, arguments=args or {"query": "q"}
    )


def _emit(cid="e", candidates=None):
    return ToolCallRequest(
        id=cid, name=EXTRACTOR_TOOL_NAME,
        arguments={"candidates": candidates if candidates is not None else []},
    )


def _turn(*calls, text=None):
    return ModelTurnResult(assistant_text=text, tool_calls=list(calls))


async def _run(script, *, budget=_BUDGET, retries=_RETRIES, index=None):
    """Drive one hostile script to completion. Returns `(extractor, index, raised)`."""
    index = InMemoryPriorArtIndex([]) if index is None else index
    extractor = make_extractor(
        script, prior_art=index, max_search_calls=budget, max_retries=retries
    )
    raised: Exception | None = None
    try:
        await extractor.extract(make_summary(), KEEP_VERDICT)
    except SchemaMismatchError as exc:  # the documented terminal failure
        raised = exc
    return extractor, index, raised


# --- the global bound, over every hostile shape ----------------------------------


_HOSTILE_SCRIPTS = {
    "searches_forever": [_turn(_search(f"s{i}")) for i in range(_OVERLONG)],
    "wrong_typed_args_forever": [
        _turn(ToolCallRequest(id=f"s{i}", name=SEARCH_CORPUS_TOOL_NAME,
                              arguments=["not", "a", "dict"]))
        for i in range(_OVERLONG)
    ],
    "blank_query_forever": [
        _turn(_search(f"s{i}", query="   ")) for i in range(_OVERLONG)
    ],
    "unknown_tool_forever": [
        _turn(ToolCallRequest(id=f"x{i}", name="deleteEverything", arguments={}))
        for i in range(_OVERLONG)
    ],
    "free_text_forever": [_turn(text="thinking") for _ in range(_OVERLONG)],
    "no_tool_calls_at_all": [_turn() for _ in range(_OVERLONG)],
    "interleaved_search_and_malformed": [
        t for i in range(_OVERLONG) for t in (_turn(_search(f"s{i}")), _turn(text="hm"))
    ],
    "search_then_unknown_tool": [
        t for i in range(_OVERLONG)
        for t in (_turn(_search(f"s{i}")),
                  _turn(ToolCallRequest(id=f"x{i}", name="nope", arguments={})))
    ],
    "budget_burned_in_one_turn_then_searches": (
        [_turn(*[_search(f"m{i}") for i in range(50)])]
        + [_turn(_search(f"s{i}")) for i in range(_OVERLONG)]
    ),
    "searches_after_withdrawal": (
        [_turn(_search(f"a{i}")) for i in range(_BUDGET)]
        + [_turn(_search(f"b{i}")) for i in range(_OVERLONG)]
    ),
}


@pytest.mark.parametrize("name", sorted(_HOSTILE_SCRIPTS))
async def test_every_hostile_script_terminates_inside_the_global_turn_bound(name):
    """The one property the `while` has to have. A model that never emits must cost a
    bounded number of provider round-trips whatever it does instead."""
    extractor, _index, raised = await _run(list(_HOSTILE_SCRIPTS[name]))
    assert isinstance(raised, SchemaMismatchError), (
        f"{name} should end in the documented terminal failure, got {raised!r}"
    )
    assert extractor._model_client.calls_made <= _MAX_TURNS, name


@pytest.mark.parametrize("name", sorted(_HOSTILE_SCRIPTS))
async def test_no_hostile_script_can_exceed_the_search_call_budget(name):
    """The COST property, which the turn bound does not imply: one turn may carry fifty
    calls, and each is an embed plus an ANN query per corpus. `+ 1` is the mandatory
    pre-fetch."""
    _extractor, index, _raised = await _run(list(_HOSTILE_SCRIPTS[name]))
    assert len(index.search_calls) <= _BUDGET + 1, name


# --- the individual behaviours the bound is made of -------------------------------


async def test_a_rejected_argument_shape_still_spends_budget():
    """LOAD-BEARING for termination, and non-obvious enough to pin: a `searchCorpus`
    call whose arguments do not parse never reaches the index, but it DOES decrement the
    budget. If it did not, a model emitting malformed arguments forever would take the
    search branch forever — the branch that does not consume a parse attempt."""
    script = [
        _turn(ToolCallRequest(id=f"s{i}", name=SEARCH_CORPUS_TOOL_NAME, arguments=None))
        for i in range(_OVERLONG)
    ]
    extractor, index, raised = await _run(script)
    assert isinstance(raised, SchemaMismatchError)
    assert len(index.search_calls) == 1  # the pre-fetch only; none of the 3 reached it
    assert extractor._model_client.calls_made == _MAX_TURNS


async def test_the_budget_counts_calls_and_refuses_the_overflow_in_the_same_turn():
    """Per CALL, not per turn — and the overflow is REFUSED rather than dropped, because
    a dropped call reads to the model as an empty corpus."""
    script = [_turn(*[_search(f"c{i}") for i in range(50)]), _turn(_emit())]
    extractor, index, raised = await _run(script)
    assert raised is None
    assert len(index.search_calls) == _BUDGET + 1
    replies = [
        m for m in extractor._model_client.calls[1].messages if m.get("role") == "tool"
    ]
    assert len(replies) == 50  # every call answered — an unanswered one wedges the next turn
    assert sum("budget for this session is exhausted" in m["content"] for m in replies) == 47


async def test_the_tool_is_withdrawn_the_turn_after_the_budget_is_spent():
    """Whether the budget is spent over three turns or inside one, the NEXT offer must
    drop the tool — being told "no" while still being shown the tool is how a model
    spins."""
    for script in (
        [_turn(_search(f"c{i}")) for i in range(_BUDGET)] + [_turn(_emit())],
        [_turn(*[_search(f"c{i}") for i in range(_BUDGET)]), _turn(_emit())],
    ):
        extractor, _index, raised = await _run(list(script))
        assert raised is None
        offered = [{t["name"] for t in c.tools} for c in extractor._model_client.calls]
        assert offered[-1] == {"emit_candidates"}
        assert all(SEARCH_CORPUS_TOOL_NAME in o for o in offered[:-1])


async def test_a_search_turn_never_consumes_a_parse_attempt():
    """Stated as an equality rather than a "still succeeds": with the budget fully spent
    on searches, the model must still get its full `max_retries + 1` attempts to emit."""
    script = (
        [_turn(_search(f"s{i}")) for i in range(_BUDGET)]
        + [_turn(text="malformed") for _ in range(_RETRIES)]
        + [_turn(_emit([blueprint_raw()]))]
    )
    extractor, _index, raised = await _run(script)
    assert raised is None
    assert extractor._model_client.calls_made == _BUDGET + _RETRIES + 1


async def test_a_search_alongside_an_emit_does_not_cost_a_turn_or_a_search():
    """The emit wins: the model has answered, and serving the search would discard that
    answer to ask a question it has stopped needing."""
    extractor, index, raised = await _run(
        [_turn(_search("c1"), _emit("c2", [blueprint_raw()]))]
    )
    assert raised is None
    assert extractor._model_client.calls_made == 1
    assert len(index.search_calls) == 1  # the pre-fetch only


async def test_an_unknown_tool_alone_is_a_parse_failure_not_a_served_turn():
    """A hallucinated tool with no `searchCorpus` beside it is NOT the search branch —
    it is a malformed response, and it costs a retry. Pinned because the "every tool
    call gets a reply" rule reads as universal and is in fact scoped to search-bearing
    turns; on this path the loop never echoes the assistant message at all, so nothing
    is left dangling in the history either."""
    script = [_turn(ToolCallRequest(id="x", name="nope", arguments={}))] * _OVERLONG
    extractor, _index, raised = await _run(script)
    assert isinstance(raised, SchemaMismatchError)
    assert extractor._model_client.calls_made == _RETRIES + 1
    followup = extractor._model_client.calls[1].messages
    assert not any(m.get("role") in ("assistant", "tool") for m in followup)
    assert followup[-1]["role"] == "user"


async def test_an_unknown_tool_beside_a_search_is_answered_so_the_history_stays_valid():
    """The other half: once the turn IS served, every call in it needs a reply or the
    provider rejects the follow-up whose history leaves one dangling."""
    script = [
        _turn(_search("c1"), ToolCallRequest(id="c2", name="nope", arguments={})),
        _turn(_emit()),
    ]
    extractor, _index, raised = await _run(script)
    assert raised is None
    replies = {
        m["tool_call_id"]: m["content"]
        for m in extractor._model_client.calls[1].messages
        if m.get("role") == "tool"
    }
    assert set(replies) == {"c1", "c2"}
    assert "no tool named" in replies["c2"]


@pytest.mark.parametrize("budget", [0, -1])
async def test_a_non_positive_budget_never_offers_the_tool_and_still_terminates(budget):
    """A configuration edge, not a model one: `max_search_calls <= 0` must behave like
    "no tool", including for a model that calls it anyway."""
    extractor, index, raised = await _run(
        [_turn(_search(f"c{i}")) for i in range(_OVERLONG)], budget=budget
    )
    assert isinstance(raised, SchemaMismatchError)
    assert extractor._model_client.calls_made == _RETRIES + 1
    assert len(index.search_calls) == 1  # the pre-fetch, nothing else
    assert all(
        SEARCH_CORPUS_TOOL_NAME not in {t["name"] for t in c.tools}
        for c in extractor._model_client.calls
    )


async def test_an_unwired_extractor_never_takes_the_search_branch():
    """With no index the branch is unreachable by construction (`searches_left = 0`), so
    a model calling the tool it was never offered is just a malformed response."""
    extractor = make_extractor(
        [_turn(_search(f"c{i}")) for i in range(_OVERLONG)], max_retries=_RETRIES
    )
    with pytest.raises(SchemaMismatchError):
        await extractor.extract(make_summary(), KEEP_VERDICT)
    assert extractor._model_client.calls_made == _RETRIES + 1
