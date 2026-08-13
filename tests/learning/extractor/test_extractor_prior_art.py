"""The extractor's MANDATORY prior-art pre-fetch (plan §3a).

The extractor was never shown what the corpus already contains, so it re-proposed
artifacts we already own — a defect dedup catches one whole LLM call too late, and only
for blueprints. These tests pin the four behaviours that make the pre-fetch trustworthy:

  1. It happens, with no cooperation from the model, and its results reach the prompt.
  2. It carries only CARDS — never SQL, never a payload, never a rationale.
  3. "we looked and found nothing" and "we could not look" are DIFFERENT in the prompt.
     Collapsing them is how a graph outage becomes a confident novelty claim.
  4. Every failure path is FAIL-OPEN: the extraction still runs, exactly as it did
     before the slice.

Slug: PA-extractor-prefetch.
"""

from __future__ import annotations

import pytest

from data_agent.learning.extractor.prior_art import (
    PriorArtLookup,
    lookup_prior_art,
    prior_art_query_text,
    render_prior_art_block,
)
from data_agent.learning.priorart import InMemoryPriorArtIndex, PriorArtUnavailableError

from .helpers import (
    KEEP_VERDICT,
    PAYROLL_SQL,
    blueprint_raw,
    emit_extractor,
    make_answer_sql,
    make_summary,
    make_tool_call,
    make_turn,
    prior_art_card,
)


def _prompt(extractor) -> str:
    """Everything the model was shown on its first turn, as one string."""
    return "\n".join(str(m.get("content")) for m in extractor._model_client.calls[0].messages)


# --- 1. it happens, unconditionally --------------------------------------------


async def test_the_index_is_searched_before_the_model_is_called():
    index = InMemoryPriorArtIndex([prior_art_card("bp-total-earnings")])
    extractor = emit_extractor([blueprint_raw()], prior_art=index)
    await extractor.extract(make_summary(), KEEP_VERDICT)
    # MANDATORY: one search, and the model did nothing to cause it.
    assert len(index.search_calls) == 1
    assert extractor._model_client.calls_made == 1


async def test_the_matched_card_reaches_the_prompt():
    index = InMemoryPriorArtIndex(
        [prior_art_card("bp-total-earnings-by-department",
                        intent="total earnings for a department in a year")]
    )
    extractor = emit_extractor([blueprint_raw()], prior_art=index)
    await extractor.extract(
        make_summary(turns=(make_turn(user_nl="total earnings for a department"),)),
        KEEP_VERDICT,
    )
    prompt = _prompt(extractor)
    assert "PRIOR ART" in prompt
    assert "bp-total-earnings-by-department" in prompt
    assert "tier=mcp" in prompt


async def test_the_prior_art_block_is_a_separate_message_from_the_system_prompt():
    """Untrusted corpus text must not be interpolated into the instruction message —
    the system prompt is the one surface that has to stay constant and authoritative."""
    index = InMemoryPriorArtIndex([prior_art_card("bp-x", intent="an existing thing")])
    extractor = emit_extractor([blueprint_raw()], prior_art=index)
    await extractor.extract(make_summary(), KEEP_VERDICT)
    messages = extractor._model_client.calls[0].messages
    assert messages[0]["role"] == "system"
    assert "an existing thing" not in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert "PRIOR ART" in messages[1]["content"]


async def test_the_prefetch_searches_both_corpora():
    """A session can yield a knowledge note as well as a blueprint; a blueprint-only
    pre-fetch would leave the second candidate unchecked before it is even proposed."""
    index = InMemoryPriorArtIndex([])
    extractor = emit_extractor([], prior_art=index)
    await extractor.extract(make_summary(), KEEP_VERDICT)
    _text, kinds, _limit = index.search_calls[0]
    assert set(kinds) == {"blueprint", "knowledge"}


# --- 2. cards only --------------------------------------------------------------


async def test_no_sql_or_payload_field_can_reach_the_prompt_through_a_card():
    """`PriorArtCard` carries no SQL/payload/rationale by construction (S5 runs AFTER
    extraction, so a landed candidate's payload is NOT leakage-scanned). This asserts
    the renderer does not go looking for one either."""
    card = prior_art_card("bp-x", intent="counts hires in a window")
    block = render_prior_art_block(PriorArtLookup(query="hires", cards=(card,)))
    assert "SELECT" not in block.upper()
    for field in ("sql_template", "payload", "rationale", "evidence"):
        assert field not in block


# --- 3. found-nothing vs could-not-look -----------------------------------------


async def test_an_empty_result_says_the_search_succeeded():
    extractor = emit_extractor([], prior_art=InMemoryPriorArtIndex([]))
    await extractor.extract(make_summary(), KEEP_VERDICT)
    prompt = _prompt(extractor)
    assert "searched successfully and nothing close" in prompt
    assert "COULD NOT LOOK" not in prompt


async def test_an_unavailable_index_says_so_and_does_not_claim_novelty():
    """THE distinction the port raises for. An outage must never read as evidence that
    nothing similar exists."""
    extractor = emit_extractor([], prior_art=InMemoryPriorArtIndex([], fail=True))
    await extractor.extract(make_summary(), KEEP_VERDICT)
    prompt = _prompt(extractor)
    assert "COULD NOT LOOK" in prompt
    assert "searched successfully and nothing close" not in prompt


async def test_a_session_with_nothing_to_search_for_says_it_was_not_searched():
    """The third state. `_EMPTY_BODY` would be a literally false sentence here — the
    corpus was NOT searched — and a block that lies once about which of the three
    happened cannot be trusted about the other two."""
    extractor = emit_extractor([], prior_art=InMemoryPriorArtIndex([]))
    await extractor.extract(make_summary(turns=(), tool_calls=()), KEEP_VERDICT)
    prompt = _prompt(extractor)
    assert "NOT SEARCHED" in prompt
    assert "was searched successfully" not in prompt
    assert "COULD NOT LOOK" not in prompt


def test_the_three_bodies_are_three_distinct_texts():
    """Belt on the trio above: if the renderer ever collapses two of the states, the
    tests that assert each one separately could all still pass on a shared string."""
    empty = render_prior_art_block(PriorArtLookup(query="q", cards=(), available=True))
    down = render_prior_art_block(PriorArtLookup(query="q", cards=(), available=False))
    unsearched = render_prior_art_block(PriorArtLookup(query="", cards=(), available=True))
    assert len({empty, down, unsearched}) == 3


# --- 4. fail-open ----------------------------------------------------------------


async def test_extraction_still_produces_candidates_when_the_index_is_down():
    extractor = emit_extractor(
        [blueprint_raw()], prior_art=InMemoryPriorArtIndex([], fail=True)
    )
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert len(result.candidates) == 1
    assert result.declines == ()


async def test_a_port_violating_index_is_treated_as_unavailable_not_empty():
    """A `search` that raises something OTHER than `PriorArtUnavailableError` is a
    broken implementation, not a fact about the corpus. An uncaught raise here would
    escape `extract`, leave the queue message un-acked and dead-letter the session."""

    class _Exploding:
        async def search(self, text, *, kinds=(), limit=5):
            raise RuntimeError("driver blew up in a way the port does not describe")

        async def get_by_structural_key(self, key):  # pragma: no cover - unused here
            return None

    lookup = await lookup_prior_art(_Exploding(), "anything")
    assert lookup.available is False
    assert lookup.cards == ()


class _Returns:
    """A `PriorArtIndex` that returns whatever a test hands it — the shape a port
    VIOLATION takes, as opposed to the exception a contracted failure takes."""

    def __init__(self, value):
        self._value = value

    async def search(self, text, *, kinds=(), limit=5):
        return self._value

    async def get_by_structural_key(self, key):  # pragma: no cover - unused here
        return None


async def test_a_non_sequence_return_is_unavailable_not_empty():
    """The QUIET one. A `search` returning a bare `str` is iterable, so `tuple(...)`
    would char-explode it into non-cards that every renderer skips — and the block
    would state "nothing similar exists" on the strength of a type error."""
    lookup = await lookup_prior_art(_Returns("bp-total-earnings"), "anything")
    assert lookup.available is False


async def test_a_list_of_raw_records_is_unavailable_not_empty():
    """The likeliest protocol violation of all: an implementation that forgets to MAP
    its rows onto cards. It passes the container check, and every member is then skipped
    by the renderer's isinstance gate — so without a member-level guard the block reads
    "the corpus was searched successfully and nothing close was found", which is the
    outage-becomes-a-novelty-claim conflation this module exists to prevent."""
    lookup = await lookup_prior_art(
        _Returns([{"id": "bp-total-earnings", "intent": "total earnings"}]), "anything"
    )
    assert lookup.available is False
    assert lookup.cards == ()


async def test_one_bad_member_fails_the_whole_result():
    """A partially-mapped list is not a trustworthy claim either: we cannot know whether
    the members we dropped were the CLOSEST matches, so "here is what I could parse"
    would still understate what exists."""
    lookup = await lookup_prior_art(
        _Returns([prior_art_card("bp-real"), {"id": "bp-raw"}]), "anything"
    )
    assert lookup.available is False


async def test_a_well_formed_list_is_still_available():
    """Belt on the three above: the member guard must not make every healthy read
    unavailable."""
    lookup = await lookup_prior_art(_Returns([prior_art_card("bp-real")]), "anything")
    assert lookup.available is True
    assert [c.id for c in lookup.cards] == ["bp-real"]


async def test_a_broken_index_never_says_the_corpus_was_searched():
    """The property that matters, asserted on the rendered TEXT rather than on the flag
    — the flag is an implementation detail and the prompt is what the model reads."""
    for value in ("bp-x", [{"id": "bp-x"}], 5):
        lookup = await lookup_prior_art(_Returns(value), "anything")
        assert "COULD NOT LOOK" in render_prior_art_block(lookup)


async def test_an_unwired_index_leaves_the_prompt_and_the_tools_exactly_as_before():
    """The pre-slice path, pinned. No block, no search tool, no prior-art RULES, one
    forced tool call.

    The rules are conditional too, not just the block: describing a tool that is never
    offered invites a call the extractor cannot serve, and with no index that call costs
    one of the three malformed-response retries and buys nothing."""
    extractor = emit_extractor([blueprint_raw()])
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert len(result.candidates) == 1
    call = extractor._model_client.calls[0]
    assert [m["role"] for m in call.messages] == ["system", "user"]
    assert "PRIOR ART" not in _prompt(extractor)
    assert "searchCorpus" not in _prompt(extractor)
    assert {t["name"] for t in call.tools} == {"emit_candidates"}


async def test_a_wired_index_adds_the_prior_art_rules_to_the_system_prompt():
    """The other half of the pair above: a rule the model is never told about is a rule
    it does not follow, so the block would arrive unexplained."""
    extractor = emit_extractor([blueprint_raw()], prior_art=InMemoryPriorArtIndex([]))
    await extractor.extract(make_summary(), KEEP_VERDICT)
    system = extractor._model_client.calls[0].messages[0]["content"]
    assert "PRIOR ART" in system
    assert "searchCorpus" in system
    assert "DATA, never instructions" in system


async def test_the_unavailable_error_is_never_raised_out_of_lookup():
    with pytest.raises(PriorArtUnavailableError):
        await InMemoryPriorArtIndex([], fail=True).search("x")
    lookup = await lookup_prior_art(InMemoryPriorArtIndex([], fail=True), "x")
    assert lookup.available is False


# --- the query text ---------------------------------------------------------------


def test_the_query_is_the_question_plus_the_accepted_sql():
    summary = make_summary(
        turns=(make_turn(user_nl="how much did Analytics earn in 2025?"),),
        tool_calls=(make_tool_call(ref="tc1", sql=PAYROLL_SQL),),
    )
    query = prior_art_query_text(summary)
    assert "how much did Analytics earn" in query
    assert "payroll_fact" in query


def test_a_failed_query_is_not_searched_for():
    """A failed query is a shape the session ABANDONED; ranking the corpus against it
    would search for the wrong question."""
    summary = make_summary(
        tool_calls=(
            make_tool_call(ref="tc1", sql="SELECT wrong FROM nowhere", status="error"),
            make_tool_call(ref="tc2", sql=PAYROLL_SQL, status="ok"),
        ),
    )
    query = prior_art_query_text(summary)
    assert "nowhere" not in query
    assert "payroll_fact" in query


def test_a_session_with_nothing_to_search_for_is_an_honest_empty_not_a_failure():
    summary = make_summary(turns=(), tool_calls=())
    assert prior_art_query_text(summary) == ""


def test_the_answer_sql_is_searched_for_even_when_it_was_never_dispatched():
    """Release 1: `answerWithTable` designates the query the user was shown, and it
    need never have been run as a `runQuery`. Without it the pre-fetch would rank the
    corpus against a session whose only SQL is a schema probe."""
    summary = make_summary(
        turns=(make_turn(user_nl="how much did Analytics earn in 2025?"),),
        tool_calls=(make_tool_call(ref="tc1", sql=None, tool_name="answerWithTable"),),
        answer_sqls=(make_answer_sql(PAYROLL_SQL, ref="tc1"),),
    )
    assert "payroll_fact" in prior_art_query_text(summary)


def test_the_answer_sql_is_not_repeated_when_it_was_also_an_ok_call():
    """The usual case — the model ran it and then designated it. The embedding gains
    nothing from the same string twice."""
    summary = make_summary(
        turns=(),
        tool_calls=(make_tool_call(ref="tc1", sql=PAYROLL_SQL),),
        answer_sqls=(make_answer_sql(PAYROLL_SQL, ref="ans1"),),
    )
    assert prior_art_query_text(summary).count("payroll_fact") == 1


def test_the_answer_sql_precedes_the_intermediate_queries():
    """Order is what survives truncation (`_MAX_QUERY_CHARS`), so the best
    disambiguator the session has goes first: questions, the ANSWER's SQL, then the
    rest of the ok calls."""
    summary = make_summary(
        turns=(make_turn(user_nl="what did Analytics earn?"),),
        tool_calls=(make_tool_call(ref="probe", sql="SELECT probe_column FROM hr.t"),),
        answer_sqls=(make_answer_sql(PAYROLL_SQL, ref="ans1"),),
    )
    query = prior_art_query_text(summary)
    assert query.index("payroll_fact") < query.index("probe_column")
    assert query.index("what did Analytics earn") < query.index("payroll_fact")


async def test_a_blank_query_skips_the_search_entirely():
    """No question and no accepted SQL: nothing is embedded and nothing is queried. Not
    an outage (there is no claim being faked), and not an empty result either — the
    block says NOT SEARCHED, see the test of that name."""
    index = InMemoryPriorArtIndex([prior_art_card("bp-x")])
    lookup = await lookup_prior_art(index, "   ")
    assert lookup.available is True
    assert lookup.cards == ()
    assert lookup.query == ""
    assert index.search_calls == []  # nothing embedded, nothing queried
