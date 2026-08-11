"""The OPTIONAL `searchCorpus` tool and the turn budget around it (plan §3a).

One session can yield several candidates of different types — a blueprint and a
knowledge note on unrelated topics — and a single pre-fetch keyed on the session intent
cannot cover the second. The tool is what makes the second candidate checkable.

Adding it changes the extractor's CONTROL FLOW, which until now was one forced tool call
with retry-on-malformed. That is the risk this file is mostly about:

  * a model that never calls the tool must behave EXACTLY as before (turn count, retry
    budget, and the parsed result);
  * a model that calls it forever must not be able to spin — the budget is per CALL, not
    per turn, and the tool stops being OFFERED once it is spent;
  * a search turn must not eat a MALFORMED-response retry, because the retry budget is
    about bad output, not about a tool call the extractor itself offered;
  * every tool call in a turn must get a reply, including one to a tool that does not
    exist, or the next request is malformed at the transport level.

Slug: PA-searchcorpus-tool.
"""

from __future__ import annotations

import json

from data_agent.learning.extractor.prior_art import (
    DEFAULT_KINDS,
    KNOWN_KINDS,
    parse_search_corpus_args,
)
from data_agent.learning.extractor.schema import (
    SEARCH_CORPUS_TOOL_NAME,
    SEARCH_KIND_ENUM,
    build_search_corpus_tool,
)
from data_agent.learning.priorart import InMemoryPriorArtIndex
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest

from .helpers import (
    KEEP_VERDICT,
    blueprint_raw,
    make_extractor,
    make_summary,
    prior_art_card,
    scripted_turn,
    search_turn,
)

# --- the schema -----------------------------------------------------------------


def test_the_tool_declares_the_canonical_flat_shape():
    tool = build_search_corpus_tool()
    assert tool["type"] == "function"
    assert tool["name"] == SEARCH_CORPUS_TOOL_NAME
    assert tool["parameters"]["required"] == ["query"]


def test_the_kind_enum_matches_the_runtime_closed_set():
    """One closed set, two spellings (a JSON-Schema list and the tuple passed to the
    port). Pinned equal so an added corpus cannot be offered to the model without being
    accepted by `parse_search_corpus_args`, or vice versa."""
    assert set(SEARCH_KIND_ENUM) == set(KNOWN_KINDS)
    kinds = build_search_corpus_tool()["parameters"]["properties"]["kinds"]
    assert kinds["items"]["enum"] == SEARCH_KIND_ENUM


def test_the_tool_tells_the_model_it_gets_cards_not_payloads():
    """A model told it can "search the corpus" will ask for SQL. The card-only rule is
    the whole safety story of the port, so it is stated in the description the model
    actually reads, not only in a docstring it does not."""
    desc = build_search_corpus_tool()["description"].lower()
    assert "cards only" in desc
    assert "never sql" in desc


def test_no_limit_parameter_is_exposed():
    """The one argument with no upside: the caller knows how many cards fit in the
    prompt and the model does not, and every untrusted number is another clamp to get
    right."""
    assert "limit" not in build_search_corpus_tool()["parameters"]["properties"]


# --- the argument guard, derived from what `search` DOES with each value ---------


def test_a_missing_or_blank_query_is_rejected():
    """There is no safe default for WHAT to look for; a blank query embedded and
    searched would rank the whole corpus against nothing."""
    assert parse_search_corpus_args({}) is None
    assert parse_search_corpus_args({"query": "   "}) is None
    assert parse_search_corpus_args({"query": None}) is None


def test_a_non_string_query_is_rejected_rather_than_coerced():
    """`Neo4jPriorArtIndex.search` calls `text.strip()` and the fake calls
    `unicodedata.normalize(...)`; both raise on a non-str. `str()`-coercing would send
    the literal `['a']` to the embedding endpoint instead."""
    assert parse_search_corpus_args({"query": ["headcount"]}) is None
    assert parse_search_corpus_args({"query": 5}) is None
    assert parse_search_corpus_args("just a string") is None


def test_a_bare_string_kinds_does_not_char_explode_into_nothing():
    """THE quiet one. `tuple(k for k in kinds if k in _SEARCH_QUERY)` iterates a bare
    `str` CHAR-WISE, matches nothing, and returns `[]` — a false "nothing exists" with
    no error at all. It must fall back to searching everything, never to searching
    nothing."""
    query, kinds = parse_search_corpus_args({"query": "q", "kinds": "blueprint"})
    assert query == "q"
    assert kinds == DEFAULT_KINDS


def test_an_unhashable_kind_member_cannot_reach_the_membership_test():
    """`k in _SEARCH_QUERY` HASHES k, so a list member raises TypeError straight out of
    the port — the same class as the `node_kind: ["query"]` crash in `_validate_roles`."""
    _query, kinds = parse_search_corpus_args({"query": "q", "kinds": [["blueprint"], 5, None]})
    assert kinds == DEFAULT_KINDS


def test_an_unknown_kind_is_dropped_and_the_known_ones_kept():
    _query, kinds = parse_search_corpus_args(
        {"query": "q", "kinds": ["knowledge", "recipes"]}
    )
    assert kinds == ("knowledge",)


def test_a_non_list_kinds_falls_back_to_everything():
    _query, kinds = parse_search_corpus_args({"query": "q", "kinds": {"a": 1}})
    assert kinds == DEFAULT_KINDS


def test_the_query_is_flattened_and_capped():
    """The query goes to an embedding endpoint and its echo goes into the prompt block;
    a multi-line value would forge a line boundary there."""
    query, _kinds = parse_search_corpus_args({"query": "a\nb\r\nc"})
    assert query == "a b c"


# --- the loop -------------------------------------------------------------------


async def test_a_search_turn_is_served_and_the_model_then_emits():
    index = InMemoryPriorArtIndex(
        [prior_art_card("kn-overtime-policy", kind="knowledge", intent="overtime policy")]
    )
    extractor = make_extractor(
        [search_turn("overtime policy", kinds=["knowledge"]), scripted_turn([blueprint_raw()])],
        prior_art=index,
    )
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert len(result.candidates) == 1
    # pre-fetch + the model's own search
    assert [c[1] for c in index.search_calls] == [("blueprint", "knowledge"), ("knowledge",)]
    # The second request carries the assistant echo + the tool result.
    second = extractor._model_client.calls[1].messages
    assert second[-2]["role"] == "assistant"
    assert second[-1]["role"] == "tool"
    assert "kn-overtime-policy" in second[-1]["content"]


async def test_the_tool_is_not_offered_when_no_index_is_wired():
    """A tool the process cannot service is a trap: the model spends a turn on it and
    gets an apology, and the turn budget that should have produced candidates is gone."""
    extractor = make_extractor([scripted_turn([blueprint_raw()])])
    await extractor.extract(make_summary(), KEEP_VERDICT)
    assert {t["name"] for t in extractor._model_client.calls[0].tools} == {"emit_candidates"}


async def test_the_tool_is_offered_when_an_index_is_wired():
    extractor = make_extractor(
        [scripted_turn([blueprint_raw()])], prior_art=InMemoryPriorArtIndex([])
    )
    await extractor.extract(make_summary(), KEEP_VERDICT)
    assert {t["name"] for t in extractor._model_client.calls[0].tools} == {
        "emit_candidates", SEARCH_CORPUS_TOOL_NAME
    }


async def test_a_model_that_never_searches_behaves_exactly_as_before():
    """The compatibility guarantee. One turn, one parse, the same result."""
    extractor = make_extractor(
        [scripted_turn([blueprint_raw()])], prior_art=InMemoryPriorArtIndex([])
    )
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert len(result.candidates) == 1
    assert extractor._model_client.calls_made == 1


async def test_the_budget_caps_the_number_of_calls_and_then_withdraws_the_tool():
    """Bounded control flow. After the third served call the tool is no longer OFFERED,
    so the model is forced back to emitting rather than left able to spin."""
    extractor = make_extractor(
        [search_turn(call_id=f"c{i}") for i in range(3)] + [scripted_turn([blueprint_raw()])],
        prior_art=InMemoryPriorArtIndex([]),
        max_search_calls=3,
    )
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert len(result.candidates) == 1
    offered = [{t["name"] for t in call.tools} for call in extractor._model_client.calls]
    assert offered[:3] == [{"emit_candidates", SEARCH_CORPUS_TOOL_NAME}] * 3
    assert offered[3] == {"emit_candidates"}


async def test_the_budget_counts_calls_not_turns():
    """A model can request several searches in ONE turn, and each is an embed plus an
    ANN query per corpus — counting turns would let a single turn multiply the cost."""
    index = InMemoryPriorArtIndex([])
    turn = ModelTurnResult(
        tool_calls=[
            ToolCallRequest(id=f"c{i}", name=SEARCH_CORPUS_TOOL_NAME, arguments={"query": f"q{i}"})
            for i in range(5)
        ]
    )
    extractor = make_extractor(
        [turn, scripted_turn([])], prior_art=index, max_search_calls=3
    )
    await extractor.extract(make_summary(), KEEP_VERDICT)
    # 1 pre-fetch + exactly 3 served searches; the other two were refused.
    assert len(index.search_calls) == 4
    replies = [m for m in extractor._model_client.calls[1].messages if m.get("role") == "tool"]
    assert len(replies) == 5  # every call answered, none silently dropped
    assert sum("budget for this session is exhausted" in m["content"] for m in replies) == 2


async def test_a_search_turn_does_not_consume_a_malformed_retry():
    """The retry budget is about BAD OUTPUT. Spending it on a tool call the extractor
    itself offered would make the retry contract depend on how chatty the model is."""
    extractor = make_extractor(
        [
            search_turn(call_id="c1"),
            ModelTurnResult(assistant_text="thinking out loud", tool_calls=[]),
            scripted_turn([blueprint_raw()]),
        ],
        prior_art=InMemoryPriorArtIndex([]),
        max_retries=1,  # 1 initial + 1 retry; the search turn must not count
    )
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert len(result.candidates) == 1
    assert extractor._model_client.calls_made == 3


async def test_an_unknown_tool_call_is_answered_rather_than_left_dangling():
    """A provider rejects a follow-up whose history leaves a tool call unanswered, so a
    hallucinated sibling tool would wedge the conversation on the NEXT turn — an
    infrastructure error dressed up as a model error."""
    turn = ModelTurnResult(
        tool_calls=[
            ToolCallRequest(id="c1", name=SEARCH_CORPUS_TOOL_NAME, arguments={"query": "q"}),
            ToolCallRequest(id="c2", name="deleteEverything", arguments={}),
        ]
    )
    extractor = make_extractor(
        [turn, scripted_turn([])], prior_art=InMemoryPriorArtIndex([])
    )
    await extractor.extract(make_summary(), KEEP_VERDICT)
    followup = extractor._model_client.calls[1].messages
    replies = {m["tool_call_id"]: m["content"] for m in followup if m.get("role") == "tool"}
    assert set(replies) == {"c1", "c2"}
    assert "no tool named" in replies["c2"]
    echoed = next(m for m in followup if m.get("role") == "assistant")
    assert [c["id"] for c in echoed["tool_calls"]] == ["c1", "c2"]
    assert json.loads(echoed["tool_calls"][0]["function"]["arguments"]) == {"query": "q"}


async def test_an_emit_alongside_a_search_wins():
    """The model has answered; serving the search would discard that answer to ask a
    question it has already stopped needing."""
    turn = ModelTurnResult(
        tool_calls=[
            ToolCallRequest(id="c1", name=SEARCH_CORPUS_TOOL_NAME, arguments={"query": "q"}),
            ToolCallRequest(id="c2", name="emit_candidates", arguments={"candidates": [blueprint_raw()]}),
        ]
    )
    index = InMemoryPriorArtIndex([])
    extractor = make_extractor([turn], prior_art=index)
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert len(result.candidates) == 1
    assert extractor._model_client.calls_made == 1
    assert len(index.search_calls) == 1  # the pre-fetch only


async def test_a_malformed_search_argument_is_reported_to_the_model_not_swallowed():
    """An empty result is indistinguishable from "nothing exists" — the conflation the
    whole slice is about. A rejected argument shape must say so."""
    turn = ModelTurnResult(
        tool_calls=[ToolCallRequest(id="c1", name=SEARCH_CORPUS_TOOL_NAME, arguments={"kinds": []})]
    )
    extractor = make_extractor(
        [turn, scripted_turn([])], prior_art=InMemoryPriorArtIndex([])
    )
    await extractor.extract(make_summary(), KEEP_VERDICT)
    reply = next(
        m for m in extractor._model_client.calls[1].messages if m.get("role") == "tool"
    )
    assert "Nothing was searched" in reply["content"]


async def test_a_search_against_a_down_index_says_could_not_look():
    turn = ModelTurnResult(
        tool_calls=[ToolCallRequest(id="c1", name=SEARCH_CORPUS_TOOL_NAME, arguments={"query": "q"})]
    )
    extractor = make_extractor(
        [turn, scripted_turn([blueprint_raw()])],
        prior_art=InMemoryPriorArtIndex([], fail=True),
    )
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert len(result.candidates) == 1  # fail-open: the extraction still lands
    reply = next(
        m for m in extractor._model_client.calls[1].messages if m.get("role") == "tool"
    )
    assert "COULD NOT LOOK" in reply["content"]
