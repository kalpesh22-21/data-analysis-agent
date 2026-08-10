"""Neo4jPriorArtIndex — the cross-tier reader, driven through its `_run` seam.

No live neo4j: `_run` is the single driver-touching method (mirroring
`Neo4jVectorIndex._run`), so mapping, ranking, over-fetch, the query SHAPE and the
degrade paths are all Layer-1 testable. The live proofs are in
`tests/integration/test_learning_prior_art_live.py`.

Slugs:
  * PA-neo4j-no-trust-gate       — the queries deliberately drop recall's `source`
                                   filter, and every tier comes back.
  * PA-neo4j-no-model-filter     — recall's `embedding_model` parity filter is dropped;
                                   skew is reported and discounted, never hidden.
  * PA-neo4j-terminal-excluded   — only an EXPLICITLY terminal status is dropped.
  * PA-neo4j-untrusted-props     — every graph property type maps without raising.
  * PA-neo4j-unavailable-raises  — infra failure raises, never `[]`.
"""

from __future__ import annotations

import pytest

from data_agent.learning.priorart import (
    TIER_LEARNING,
    TIER_MCP,
    TIER_UNSOURCED,
    PriorArtUnavailableError,
)
from data_agent.learning.priorart.neo4j_index import (
    _BY_STRUCTURAL_KEY_QUERY,
    _OVERFETCH,
    _SEARCH_QUERY,
    Neo4jPriorArtIndex,
)
from data_agent.runtime.model.embedding_client import EmbeddingError

_MODEL = "all-mpnet-base-v2"


class _Embedder:
    def __init__(self, *, fail: bool = False, vectors=None) -> None:
        self._fail = fail
        self._vectors = vectors
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self._fail:
            raise EmbeddingError("embedding endpoint down")
        if self._vectors is not None:
            return self._vectors
        return [[0.1, 0.2, 0.3] for _ in texts]


def _index(rows=None, *, embedder=None, raises=None) -> Neo4jPriorArtIndex:
    index = Neo4jPriorArtIndex(
        driver=object(),  # never touched — `_run` is replaced below
        embedding_client=embedder or _Embedder(),
        expected_model=_MODEL,
    )
    index.runs = []  # type: ignore[attr-defined]

    async def _run(query, parameters):
        index.runs.append((query, parameters))  # type: ignore[attr-defined]
        if raises is not None:
            raise raises
        return list(rows or [])

    index._run = _run  # type: ignore[assignment]
    return index


def _row(**overrides) -> dict:
    base = {
        "id": "bp-total-earnings-by-department",
        "intent": "total earnings by department for a year",
        "source": "mcp",
        "status": "validated",
        "verified": True,
        "drift_status": "clean",
        "embedding_model": _MODEL,
        "structural_key": "sha256:abc",
        "uses_rules_json": '["rule-overtime-multiplier"]',
        "result_grain_json": '{"columns": ["Department"], "verifiable": true}',
        "score": 0.9,
    }
    base.update(overrides)
    return base


# --- PA-neo4j-no-trust-gate ---------------------------------------------------


def _where_clause(query: str) -> str:
    return query.split("WHERE", 1)[1].split("RETURN", 1)[0]


def test_no_query_filters_on_source():
    """THE deliberate inversion. Every other neo4j read in this codebase filters to
    `source = 'mcp'` and fails closed; these must not, or the loop goes back to being
    blind to the landed learning tier and to the canon it is supposed to detect.

    Asserted on the WHERE clause specifically — the structural-key query legitimately
    mentions `source` in its ORDER BY (canon wins a tie), and conflating "ranks by" with
    "filters on" is how a well-meaning `AND node.source = 'mcp'` would slip in as a
    consistency fix. It would be silent: the queries would still work, just see less."""
    for query in (*_SEARCH_QUERY.values(), _BY_STRUCTURAL_KEY_QUERY):
        assert "source" not in _where_clause(query)


def test_the_queries_carry_no_embedding_model_filter():
    """Recall's `WHERE node.embedding_model = $expected_model` must NOT appear here: the
    hydrator leaves `source='learning'` nodes un-re-embedded across a model swap, so this
    filter would silently delete the whole learning tier from prior art. Skew is instead
    reported per card and discounted."""
    for query in (*_SEARCH_QUERY.values(), _BY_STRUCTURAL_KEY_QUERY):
        assert "embedding_model = $expected_model" not in query


async def test_every_tier_comes_back_including_a_sourceless_node():
    """A hand-edited/foreign-written node with no `source` is INVISIBLE to recall (bare
    equality, fail-closed) but visible here. It is surfaced with an explicit tier rather
    than dropped — prior art wants to know the artifact exists — and it is never canon,
    so it can never trigger the drop verdict."""
    index = _index(
        [
            _row(id="bp-canon", source="mcp"),
            _row(id="bp-learning", source="learning", verified=None),
            _row(id="bp-nosource", source=None, verified=None),
        ]
    )
    cards = {c.id: c for c in await index.search("total earnings", limit=5)}
    assert cards["bp-canon"].tier == TIER_MCP
    assert cards["bp-learning"].tier == TIER_LEARNING
    assert cards["bp-nosource"].tier == TIER_UNSOURCED
    assert not cards["bp-nosource"].is_canon
    # `verified` stays tri-state — never coerced into a verdict the node did not give.
    assert cards["bp-learning"].verified is None
    assert cards["bp-canon"].verified is True


async def test_a_blank_source_is_unsourced_not_a_learning_node():
    index = _index([_row(source="   ")])
    (card,) = await index.search("q")
    assert card.tier == TIER_UNSOURCED


# --- PA-neo4j-no-model-filter -------------------------------------------------


async def test_a_model_mismatch_is_flagged_and_discounted_not_dropped():
    index = _index(
        [
            _row(id="bp-current", embedding_model=_MODEL, score=0.80),
            _row(id="bp-stale", embedding_model="text-embedding-3-small", score=0.99),
        ]
    )
    cards = await index.search("q", limit=5)
    by_id = {c.id: c for c in cards}
    assert by_id["bp-stale"].model_matched is False
    assert by_id["bp-current"].model_matched is True
    # The RAW cosine is preserved (an operator can see the skew) ...
    assert by_id["bp-stale"].similarity == 0.99
    # ... but the discount actually reorders the list, which is the point: sorting on
    # the raw score would have put a meaningless cross-space 0.99 first.
    assert [c.id for c in cards] == ["bp-current", "bp-stale"]


async def test_an_unstamped_embedding_model_is_not_treated_as_parity():
    """BARE equality, no coalesce. Treating "the node does not say" as a match is how a
    post-swap corpus would keep scoring full-confidence cosines across two spaces."""
    index = _index([_row(embedding_model=None)])
    (card,) = await index.search("q")
    assert card.model_matched is False
    assert card.embedding_model == ""


# --- PA-neo4j-terminal-excluded -----------------------------------------------


async def test_the_terminal_filter_is_sent_as_a_parameter_and_only_kills_explicit_states():
    """The polarity is the OPPOSITE of recall's eligibility filter, deliberately: recall
    asks "is this positively eligible?" and excludes an un-stamped node; prior art asks
    "was this positively KILLED?" so an un-stamped node still counts as prior art. Under-
    including here means re-proposing something we already have an opinion about."""
    index = _index([_row(status=None)])
    (_query, params) = (await index.search("q"), index.runs[0])[1]  # type: ignore[attr-defined]
    assert params["terminal_statuses"] == ["rejected", "retired"]
    assert "NOT coalesce(node.status, '') IN $terminal_statuses" in _SEARCH_QUERY["blueprint"]
    # A statusless node maps cleanly and is NOT terminal.
    (card,) = await index.search("q")
    assert card.status == ""
    assert not card.is_terminal


# --- over-fetch ---------------------------------------------------------------


async def test_k_is_overfetched_because_the_index_applies_it_before_the_where():
    """`db.index.vector.queryNodes` applies `$k` INSIDE the index, before the
    terminal-status `WHERE` runs — so without over-fetching, a corpus whose nearest
    neighbours happen to be rejected artifacts returns short."""
    index = _index()
    await index.search("q", limit=5)
    (_query, params) = index.runs[0]  # type: ignore[attr-defined]
    assert params["k"] == 5 * _OVERFETCH
    assert params["index_name"] == "blueprint_intent_vec"


async def test_results_are_truncated_to_the_requested_limit_after_mapping():
    index = _index([_row(id=f"bp-{i}", score=0.9 - i / 100) for i in range(10)])
    assert len(await index.search("q", limit=3)) == 3


async def test_the_prior_art_index_names_match_recall():
    """The two readers keep their OWN index-name map (they must be free to diverge), so
    a rename in one and not the other would silently make prior art query a
    nonexistent index — which neo4j answers with an error, i.e. a permanent
    `PriorArtUnavailableError` and a permanently fail-open loop."""
    from data_agent.learning.priorart.neo4j_index import _KIND_INDEX
    from data_agent.runtime.retrieval.vector_index import _CORPUS_INDEX

    assert _KIND_INDEX == _CORPUS_INDEX


# --- PA-neo4j-untrusted-props -------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        # A bare string where a list is expected: `tuple("abc")` would char-explode into
        # three fictitious rule ids.
        {"uses_rules_json": '"rule-a"'},
        # Mixed member types: `sorted()` over them raises, and `", ".join` raises on int.
        {"uses_rules_json": "[1, \"rule-a\"]"},
        {"uses_rules_json": "{}"},
        {"uses_rules_json": "not json at all"},
        {"uses_rules_json": None},
        # Grain containers the normalizer treats differently (unusable ⇒ no grain).
        {"result_grain_json": "[null]"},
        {"result_grain_json": '"department"'},
        {"result_grain_json": "123"},
        {"result_grain_json": None},
        # Unhashable / wrong-typed scalars — `status` reaches a frozenset membership
        # test, `structural_key` reaches a dict key, `score` reaches a float compare.
        {"status": ["rejected"]},
        {"status": {"a": 1}},
        {"structural_key": ["sha256:abc"]},
        {"score": "0.9"},
        {"score": None},
        {"score": True},
        {"id": 42},
        {"intent": None},
        {"drift_status": 7},
        {"verified": "true"},
        {"embedding_model": ["x"]},
    ],
)
async def test_every_malformed_graph_property_maps_without_raising(overrides):
    """DERIVED FROM THE READERS, not from a field list — see `_card_from_record`'s table.
    Graph properties are untrusted in exactly the way rehydrated JSON is, and the
    previous slice found four crash sites where an operation total on the INTENDED type
    was fatal on a neighbouring one. So every field is fuzzed against the operation it
    actually feeds, and the resulting card must be safe for all of them."""
    index = _index([_row(**overrides)])
    cards = await index.search("q")
    assert len(cards) == 1
    card = cards[0]
    # The operations downstream code actually performs, run for real.
    assert isinstance(card.id, str)
    assert card.status in {"rejected", "retired"} or not card.is_terminal  # frozenset hash
    assert ", ".join(card.uses_rules) is not None  # join needs str members
    assert sorted(card.result_grain) is not None  # sort needs comparable members
    assert {card.structural_key: 1}  # must be hashable
    assert card.confidence >= 0.0  # float comparison
    assert card.similarity >= 0.0
    assert card.verified in (True, False, None)


async def test_a_true_score_does_not_masquerade_as_a_perfect_match():
    """`bool` is an `int` subclass. A `score: True` coerced through `float()` becomes 1.0
    — a perfect false positive on the strongest possible evidence — so it is excluded
    explicitly rather than left to `isinstance(raw, (int, float))`."""
    index = _index([_row(score=True)])
    (card,) = await index.search("q")
    assert card.similarity == 0.0


# --- uses_rules: BOTH authored shapes ----------------------------------------


async def test_the_object_shaped_uses_rules_the_canon_actually_stores_comes_through():
    """HISTORY. This mapper accepted only `list[str]`, so
    `bp-total-earnings-by-department` — which stores
    `[{"id": "earnings_only", "binds": "earn_codes"}]` — produced a card claiming it used
    NO rules. Silent, safe (nothing invented, nothing raised), and exactly wrong for a
    reader comparing a candidate's rules against a canon card's.

    A `uses_rules` entry is legitimately EITHER a bare string or an object
    (`runtime/blueprint/rules.py::parse_rule`), and the canon authors both."""
    index = _index([_row(uses_rules_json='[{"id": "earnings_only", "binds": "earn_codes"}]')])
    (card,) = await index.search("q")
    assert card.uses_rules == ("earnings_only",)


async def test_a_mixed_list_keeps_its_string_entries_too():
    """The old all-or-nothing rule lost the perfectly-readable string entry alongside the
    object it could not parse. With two legal member shapes, one authoring typo must not
    hide every real rule on the node."""
    index = _index([_row(uses_rules_json='["gross_earnings", {"id": "earnings_only"}]')])
    (card,) = await index.search("q")
    assert card.uses_rules == ("gross_earnings", "earnings_only")


async def test_the_id_derivation_mirrors_parse_rule_falling_back_to_binds():
    """`parse_rule` derives `rule_id` as `id` when it is a non-empty str, else `binds`.
    Mirrored, not re-implemented: the card needs a NAME, not an executable rule, and
    duplicating the `resolve_via` regex here is the mirror-drift failure mode."""
    index = _index(
        [_row(uses_rules_json='[{"binds": "earn_codes", "table": "t"}, {"id": "", "binds": "b2"}]')]
    )
    (card,) = await index.search("q")
    assert card.uses_rules == ("earn_codes", "b2")


@pytest.mark.parametrize(
    "raw",
    [
        '[{"resolve_via": "x", "table": "t"}]',  # an object naming nothing
        "[[\"gross_earnings\"]]",  # a nested list names nothing
        "[null, 5, true]",
        "[]",
    ],
)
async def test_a_member_that_names_nothing_is_skipped_never_invented(raw):
    index = _index([_row(uses_rules_json=raw)])
    (card,) = await index.search("q")
    assert card.uses_rules == ()


async def test_an_unnameable_member_does_not_invalidate_its_readable_siblings():
    index = _index([_row(uses_rules_json='[{"table": "t"}, "gross_earnings", null]')])
    (card,) = await index.search("q")
    assert card.uses_rules == ("gross_earnings",)


async def test_duplicate_rule_ids_collapse_preserving_order():
    """Order-preserving and de-duplicated, so the tuple is join-able, sort-able and
    set-comparable without a caller normalizing it first."""
    index = _index(
        [_row(uses_rules_json='["b", "a", {"id": "b"}, {"binds": "a"}]')]
    )
    (card,) = await index.search("q")
    assert card.uses_rules == ("b", "a")


async def test_a_bare_string_container_still_never_char_explodes():
    """The container rule is UNCHANGED and must stay: `tuple("gross_earnings")` is fifteen
    single-character rule ids, produced without raising. Member-level leniency does not
    extend to the container."""
    index = _index([_row(uses_rules_json='"gross_earnings"')])
    (card,) = await index.search("q")
    assert card.uses_rules == ()


async def test_the_grain_uses_the_same_normalizer_the_structural_key_hashes():
    """Canon writes `result_grain: [Department]` while its SQL selects `AS department`.
    The key's normalizer case-folds; reusing it here means a card's grain and its key can
    never disagree about what the grain IS."""
    index = _index([_row(result_grain_json='["Department", "department"]')])
    (card,) = await index.search("q")
    assert card.result_grain == ("department",)


async def test_one_malformed_row_does_not_discard_its_good_neighbours():
    index = _index([_row(id="bp-good"), "not-a-mapping"])  # type: ignore[list-item]
    cards = await index.search("q", limit=5)
    assert [c.id for c in cards] == ["bp-good"]


# --- PA-neo4j-unavailable-raises ----------------------------------------------


async def test_an_embed_failure_raises_rather_than_asserting_novelty():
    index = _index([], embedder=_Embedder(fail=True))
    with pytest.raises(PriorArtUnavailableError):
        await index.search("q")


async def test_an_empty_vector_from_a_well_formed_response_still_raises():
    """A 200 carrying `[[]]` is a broken embedder, not an answer. Returning `[]` here
    would assert novelty on the strength of a malfunction."""
    index = _index([], embedder=_Embedder(vectors=[[]]))
    with pytest.raises(PriorArtUnavailableError):
        await index.search("q")


async def test_a_query_failure_raises():
    index = _index(raises=RuntimeError("neo4j unreachable"))
    with pytest.raises(PriorArtUnavailableError):
        await index.search("q")
    index2 = _index(raises=RuntimeError("neo4j unreachable"))
    with pytest.raises(PriorArtUnavailableError):
        await index2.get_by_structural_key("sha256:k")


async def test_empty_query_text_is_an_answer_not_a_failure():
    """Nothing to look for is a real, answerable "no prior art" — and it must not cost an
    embed call."""
    embedder = _Embedder()
    index = _index([_row()], embedder=embedder)
    assert await index.search("   ") == []
    assert await index.search("q", limit=0) == []
    assert embedder.calls == []


async def test_an_unknown_kind_is_ignored_rather_than_queried():
    index = _index([_row()])
    assert await index.search("q", kinds=("nonsense",)) == []  # type: ignore[arg-type]
    assert index.runs == []  # type: ignore[attr-defined]


# --- structural-key lookup ----------------------------------------------------


async def test_structural_key_lookup_is_an_exact_undiscounted_identity():
    index = _index([_row(embedding_model="a-different-model", score=1.0)])
    card = await index.get_by_structural_key("sha256:abc")
    assert card is not None
    assert card.score_basis == "structural_key"
    # A model mismatch on an EXACT key identity must not discount it — no vector was
    # involved in producing this match at all.
    assert card.model_matched is False
    assert card.confidence == 1.0


async def test_structural_key_lookup_orders_canon_first_in_cypher():
    """The canon-wins rule lives in the query (`ORDER BY ... LIMIT 1`), not in Python, so
    it survives a caller that only reads the first row."""
    assert "CASE WHEN node.source = 'mcp' THEN 0 ELSE 1 END" in _BY_STRUCTURAL_KEY_QUERY
    assert "LIMIT 1" in _BY_STRUCTURAL_KEY_QUERY


async def test_an_empty_structural_key_never_reaches_the_database():
    index = _index([_row()])
    assert await index.get_by_structural_key("") is None
    assert index.runs == []  # type: ignore[attr-defined]


async def test_a_structural_key_miss_is_none():
    index = _index([])
    assert await index.get_by_structural_key("sha256:nope") is None
