"""Layer-2 integration — the prior-art mapper against WRONG-TYPED live node properties.

neo4j will store whatever is written to it. There is no schema on `:Blueprint`, no type
constraint on any of the eight properties this reader projects, and three writers that
can touch them (the canon seeder, the learning landing writer, and a human in the
browser). This codebase has already found this class four separate times — an operation
that is total on the intended type and fatal on a neighbouring one — and the builder
found a fifth in this very stage (`uses_rules` as a bare string char-exploding into
fictitious rule ids).

So the properties are attacked HERE, against a real graph, rather than against a dict in
a unit test: only a live write proves that neo4j accepts the shape at all, that the
Cypher `WHERE`/`ORDER BY` clauses treat it the way the Python mapper does, and that the
two do not disagree. The Cypher/Python agreement is the part a unit test cannot reach and
the part that matters most: `_BY_STRUCTURAL_KEY_QUERY` decides WHICH node wins a
cross-tier tie with `node.source = 'mcp'` in Cypher, while `_tier` decides whether the
winner is canon with `raw == TIER_MCP` in Python. If those two ever disagree about what
counts as `'mcp'`, a node can win the canon tie-break and then be mapped as
non-canon (a silently lost drop) or the reverse (a candidate dropped against a node that
is not canon at all).

Two things are asserted of every hostile shape:
  1. NOTHING RAISES out of `search()` or `get_by_structural_key()` — one corrupt node
     must not take out the whole read for the good neighbours next to it.
  2. NOTHING IS FABRICATED — a malformed value degrades to the empty/absent reading of
     that field, never to an invented one, and never upgrades a node's trust.

Every node is prefixed `qahostile-` and DETACH DELETEd in teardown; nothing else in the
graph is touched.

    NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j \
    NEO4J_TEST_PASSWORD=testpassword EMBEDDING_TEST_URL=http://localhost:18003/embed \
        uv run pytest tests/integration/test_learning_prior_art_hostile_properties_live.py -v
"""

from __future__ import annotations

import os

import pytest
from neo4j import AsyncGraphDatabase

from data_agent.learning.priorart import (
    TIER_LEARNING,
    TIER_MCP,
    TIER_UNSOURCED,
    PriorArtCard,
)
from data_agent.learning.priorart.neo4j_index import Neo4jPriorArtIndex
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.retrieval.corpus_loader import schema_statements

pytestmark = pytest.mark.skipif(
    not (os.environ.get("NEO4J_TEST_URI") and os.environ.get("EMBEDDING_TEST_URL")),
    reason="Requires a live neo4j + embedding API (set NEO4J_TEST_URI + EMBEDDING_TEST_URL).",
)

_MODEL = "all-mpnet-base-v2"
_PREFIX = "qahostile-"
_INTENT = "total earnings by department for a calendar year"
_KEY = "sha256:qahostile-shared-key"
_LONELY_KEY = "sha256:qahostile-lonely-key"

# Every hostile shape, one node each. The property values are chosen to be the things a
# neighbouring type actually IS in this codebase — a list where a scalar was meant (the
# YAML `- ` shape), a string where a bool was meant (an env-var round trip), a number
# where an enum was meant — not arbitrary garbage.
#
#  id suffix          source      status        verified  model      structural_key
_HOSTILE = [
    # `source` as a LIST: the shape a YAML author produces by adding a `-`. It must NOT
    # be canon, and it must not win the canon tie-break in Cypher either.
    ("source-list", ["mcp"], "validated", True, _MODEL, _KEY),
    # `source` as a number / empty string — the two other non-tier scalars.
    ("source-number", 7, "validated", True, _MODEL, None),
    ("source-empty", "", "validated", True, _MODEL, None),
    # `verified` as the STRING "true": truthy to `if node.verified`, not a verdict.
    ("verified-str", "mcp", "validated", "true", _MODEL, None),
    ("verified-list", "mcp", "validated", [True], _MODEL, None),
    # `status` as a number and as a list — the frozenset membership test that reads it
    # raises TypeError on an unhashable list if the coercion is skipped.
    ("status-number", "learning", 42, False, _MODEL, None),
    ("status-list", "learning", ["rejected"], False, _MODEL, None),
    # `structural_key` as an ARRAY — unhashable, and a dict key downstream.
    ("sk-array", "mcp", "validated", True, _MODEL, ["sha256:x"]),
    # `embedding_model` as a number — the bare `==` against the expected model.
    ("model-number", "learning", "validated", False, 3, None),
    # Everything null at once: neo4j REMOVES a property set to null, so this is a node
    # with no source, no status, no verified and no key — the real shape a `"source":
    # null` export entry used to produce.
    ("all-null", None, None, None, None, None),
]

# `*_json` property attacks, on their own axis so a JSON failure cannot be confused with
# a scalar failure.
#   id suffix           uses_rules_json                        result_grain_json
_HOSTILE_JSON = [
    ("ur-malformed", '{"not closed', '["Department"]'),
    ("ur-bare-string", '"gross_earnings"', '["Department"]'),
    ("ur-nested-list", '[["gross_earnings"]]', '["Department"]'),
    # The REAL canon shape for `bp-total-earnings-by-department` — see the
    # known-limitation test at the bottom.
    ("ur-dicts", '[{"id": "earnings_only", "binds": "earn_codes"}]', '["Department"]'),
    ("ur-mixed", '["gross_earnings", {"id": "earnings_only"}]', '["Department"]'),
    ("rg-malformed", '["ok"]', "{{{"),
    ("rg-nonstr-member", '["ok"]', '["Dept", 5]'),
    ("rg-bare-string", '["ok"]', '"department"'),
    ("rg-null-member", '["ok"]', '["Dept", null]'),
    ("rg-nested", '["ok"]', '{"columns": {"a": 1}}'),
]

_SEED = """
MERGE (b:Blueprint {id: $id})
SET b.intent = $intent, b.intent_embedding = $embedding,
    b.embedding_model = $model, b.drift_status = 'clean',
    b.source = $source, b.status = $status, b.verified = $verified,
    b.structural_key = $structural_key,
    b.uses_rules_json = $uses_rules_json,
    b.result_grain_json = $result_grain_json
"""


def _uri() -> str:
    return os.environ["NEO4J_TEST_URI"]


def _auth() -> tuple[str, str]:
    return (
        os.environ.get("NEO4J_TEST_USER", "neo4j"),
        os.environ.get("NEO4J_TEST_PASSWORD", "testpassword"),
    )


@pytest.fixture
async def hostile_graph():
    """Write every hostile node, yield (driver, embedder), then DETACH DELETE by prefix."""
    driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
    embedder = HttpEmbeddingClient(
        url=os.environ["EMBEDDING_TEST_URL"], api_key="", model=_MODEL
    )
    vector = (await embedder.embed([_INTENT]))[0]
    async with driver.session() as session:
        for statement in schema_statements(len(vector)):
            if "blueprint_structural_key" in statement:
                await session.run(statement)  # type: ignore[arg-type]
        await session.run("CALL db.awaitIndexes(60)")  # type: ignore[arg-type]
        for suffix, source, status, verified, model, key in _HOSTILE:
            await session.run(  # type: ignore[arg-type]
                _SEED,
                {
                    "id": f"{_PREFIX}{suffix}",
                    "intent": _INTENT,
                    "embedding": vector,
                    "model": model,
                    "source": source,
                    "status": status,
                    "verified": verified,
                    "structural_key": key,
                    "uses_rules_json": '["ok-rule"]',
                    "result_grain_json": '["Department"]',
                },
            )
        for suffix, uses_rules_json, result_grain_json in _HOSTILE_JSON:
            await session.run(  # type: ignore[arg-type]
                _SEED,
                {
                    "id": f"{_PREFIX}{suffix}",
                    "intent": _INTENT,
                    "embedding": vector,
                    "model": _MODEL,
                    "source": "learning",
                    "status": "validated",
                    "verified": False,
                    "structural_key": None,
                    "uses_rules_json": uses_rules_json,
                    "result_grain_json": result_grain_json,
                },
            )
        # Non-string SCALARS where strings were expected, plus NATIVE neo4j lists in the
        # `*_json` string properties (a writer that forgot to `json.dumps`).
        await session.run(  # type: ignore[arg-type]
            """MERGE (b:Blueprint {id: $id})
               SET b.intent = 12345, b.intent_embedding = $embedding,
                   b.embedding_model = ['a', 'b'], b.drift_status = 9,
                   b.source = 'mcp', b.status = 'validated', b.verified = true,
                   b.structural_key = $key,
                   b.uses_rules_json = ['native', 'list'],
                   b.result_grain_json = ['Department']""",
            {"id": f"{_PREFIX}native-lists", "embedding": vector, "key": _LONELY_KEY},
        )
        # A node with NOTHING but an id and a vector — every projected property absent.
        await session.run(  # type: ignore[arg-type]
            "MERGE (b:Blueprint {id: $id}) SET b.intent_embedding = $embedding",
            {"id": f"{_PREFIX}naked", "embedding": vector},
        )
    try:
        yield driver, embedder
    finally:
        async with driver.session() as session:
            await session.run(  # type: ignore[arg-type]
                "MATCH (b:Blueprint) WHERE b.id STARTS WITH $p DETACH DELETE b",
                {"p": _PREFIX},
            )
        await driver.close()


def _index(driver, embedder) -> Neo4jPriorArtIndex:
    return Neo4jPriorArtIndex(
        driver=driver, embedding_client=embedder, expected_model=_MODEL
    )


async def _ours(driver, embedder) -> dict[str, PriorArtCard]:
    cards = await _index(driver, embedder).search(_INTENT, limit=200)
    return {c.id: c for c in cards if c.id.startswith(_PREFIX)}


_ALL_SUFFIXES = (
    [row[0] for row in _HOSTILE]
    + [row[0] for row in _HOSTILE_JSON]
    + ["native-lists", "naked"]
)


# --- 1. nothing raises, nothing is lost ---------------------------------------


async def test_every_hostile_node_survives_the_read_as_a_well_formed_card(hostile_graph):
    """The whole-population assertion. Not "it did not crash" — every hostile node must
    still come BACK, because a reader that silently drops corrupt rows under-reports
    prior art, and under-reporting is what makes the loop re-propose work we own.

    Then every field of every card is checked against the type its downstream reader
    needs, exhaustively rather than per-case, so a new hostile shape added to the tables
    above is automatically type-checked."""
    driver, embedder = hostile_graph
    cards = await _ours(driver, embedder)

    assert set(cards) == {f"{_PREFIX}{s}" for s in _ALL_SUFFIXES}
    for card in cards.values():
        assert isinstance(card.id, str)
        assert isinstance(card.intent, str)
        assert isinstance(card.status, str)
        assert isinstance(card.drift_status, str)
        assert isinstance(card.structural_key, str)
        assert isinstance(card.embedding_model, str)
        assert isinstance(card.similarity, float)
        assert isinstance(card.model_matched, bool)
        assert card.verified is None or isinstance(card.verified, bool)
        assert card.tier in {TIER_MCP, TIER_LEARNING, TIER_UNSOURCED}
        assert isinstance(card.uses_rules, tuple)
        assert all(isinstance(r, str) for r in card.uses_rules)
        assert isinstance(card.result_grain, tuple)
        assert all(isinstance(g, str) for g in card.result_grain)


async def test_every_card_survives_the_operations_its_readers_perform(hostile_graph):
    """The guard is only correct if it is derived from what CONSUMERS DO, so this runs
    the actual operations rather than re-asserting the types: frozenset membership on
    `status` (TypeError on an unhashable list), `sorted()` + `", ".join()` on the two
    tuples (TypeError on mixed types), hashing the structural key (TypeError on a list),
    a float comparison on the score (TypeError against a str), and `is_terminal` /
    `confidence`, which every caller reads."""
    driver, embedder = hostile_graph
    cards = await _ours(driver, embedder)

    for card in cards.values():
        assert card.is_terminal in (True, False)
        assert card.is_canon in (True, False)
        assert card.confidence >= 0.0
        assert card.similarity >= 0.0
        assert isinstance(", ".join(sorted(card.uses_rules)), str)
        assert isinstance(", ".join(sorted(card.result_grain)), str)
        by_key = {card.structural_key: card.id}  # hashing an unhashable key raises
        assert by_key[card.structural_key] == card.id
        assert card.confidence >= 0.83 or card.confidence < 0.83  # a real float compare


# --- 2. nothing is fabricated, and nothing is upgraded ------------------------


@pytest.mark.parametrize(
    "suffix",
    ["source-list", "source-number", "source-empty", "all-null", "naked"],
)
async def test_a_non_string_source_is_unsourced_and_never_canon(hostile_graph, suffix):
    """A node must SAY it is canon, in a string, to be treated as canon. `['mcp']` is the
    highest-risk of these: it CONTAINS the word, so any coercion (`str(raw)`, a
    `'mcp' in raw` membership test, a JSON re-encode) turns it into canon and makes the
    node droppable-against."""
    driver, embedder = hostile_graph
    card = (await _ours(driver, embedder))[f"{_PREFIX}{suffix}"]
    assert card.tier == TIER_UNSOURCED
    assert not card.is_canon


@pytest.mark.parametrize("suffix", ["verified-str", "verified-list"])
async def test_a_non_boolean_verified_reads_as_unknown_not_as_true(hostile_graph, suffix):
    """`verified` is tri-state on purpose. A truthy string must NOT become `True` — that
    would report a human approval that never happened — and it must not become `False`
    either, which would look like a normal un-verified landing rather than the foreign
    write it is."""
    driver, embedder = hostile_graph
    assert (await _ours(driver, embedder))[f"{_PREFIX}{suffix}"].verified is None


@pytest.mark.parametrize("suffix", ["status-number", "status-list", "all-null", "naked"])
async def test_a_non_string_status_reads_as_absent_and_non_terminal(hostile_graph, suffix):
    """Prior art asks "was this positively KILLED?", so an unreadable status is NOT a
    kill — it stays visible. The `status-list` case also proves the Cypher `IN
    $terminal_statuses` tolerates a list-valued property: `['rejected']` is not the
    string `'rejected'`, so the node is included, and the Python side then reads it as
    absent rather than as the terminal status it superficially contains."""
    driver, embedder = hostile_graph
    card = (await _ours(driver, embedder))[f"{_PREFIX}{suffix}"]
    assert card.status == ""
    assert not card.is_terminal


async def test_a_bare_string_uses_rules_never_char_explodes(hostile_graph):
    """The fifth instance of the class, pinned. `tuple("gross_earnings")` is fifteen
    single-character "rule ids", each one a plausible-looking string that no rule
    registry has ever heard of, produced WITHOUT raising."""
    driver, embedder = hostile_graph
    card = (await _ours(driver, embedder))[f"{_PREFIX}ur-bare-string"]
    assert card.uses_rules == ()


@pytest.mark.parametrize(
    "suffix", ["ur-malformed", "ur-nested-list", "native-lists", "naked"]
)
async def test_an_unreadable_uses_rules_is_empty_never_invented(hostile_graph, suffix):
    driver, embedder = hostile_graph
    assert (await _ours(driver, embedder))[f"{_PREFIX}{suffix}"].uses_rules == ()


@pytest.mark.parametrize(
    "suffix",
    ["rg-malformed", "rg-nonstr-member", "rg-bare-string", "rg-null-member",
     "rg-nested", "native-lists", "naked"],
)
async def test_an_unreadable_result_grain_is_empty_never_invented(hostile_graph, suffix):
    """`rg-null-member` is the one that used to fabricate: `str(None)` produced the
    literal column `"none"`, which then collided with any real column of that name. The
    normalizer now calls the WHOLE grain unusable rather than guessing at the author's
    intent, and the mapper turns that into `()`."""
    driver, embedder = hostile_graph
    card = (await _ours(driver, embedder))[f"{_PREFIX}{suffix}"]
    assert card.result_grain == ()
    assert "none" not in card.result_grain


async def test_a_non_string_embedding_model_can_never_read_as_a_match(hostile_graph):
    """A bare `==` with no coalesce, so "the node does not say" is not parity. If it
    were, a corpus that lost its model stamps would score full-confidence cosines across
    two vector spaces — the exact failure the discount exists to prevent."""
    driver, embedder = hostile_graph
    cards = await _ours(driver, embedder)
    for suffix in ("model-number", "native-lists", "all-null", "naked"):
        card = cards[f"{_PREFIX}{suffix}"]
        assert card.embedding_model == ""
        assert card.model_matched is False
        assert card.confidence == pytest.approx(card.similarity * 0.5)


async def test_a_non_string_intent_or_drift_status_is_empty_not_a_repr(hostile_graph):
    """`str(12345)` is `"12345"` and `str(None)` is `"None"` — both of which flow into
    logs, joins and (one refactor later) a prompt, looking exactly like real content."""
    driver, embedder = hostile_graph
    card = (await _ours(driver, embedder))[f"{_PREFIX}native-lists"]
    assert card.intent == ""
    assert card.drift_status == ""


# --- 3. the Cypher and the Python must agree about `mcp` ----------------------


async def test_a_list_valued_source_cannot_win_the_canon_tie_break(hostile_graph):
    """The seam. Two nodes share `_KEY`: `source-list` (`['mcp']`) and — added here —
    a genuine learning node. The Cypher `ORDER BY CASE WHEN node.source = 'mcp'` must
    rank the list-valued node as NON-canon exactly as `_tier` does, or the lookup returns
    a node that Cypher believed was canon and Python maps as `unsourced`.

    Whichever node wins, the invariant is the same and is what the drop verdict rests on:
    a card is canon ONLY if its stored `source` is literally the string `mcp`."""
    driver, embedder = hostile_graph
    async with driver.session() as session:
        await session.run(  # type: ignore[arg-type]
            "MERGE (b:Blueprint {id: $id}) SET b.source = 'learning', "
            "b.status = 'validated', b.structural_key = $key",
            {"id": f"{_PREFIX}real-learning", "key": _KEY},
        )

    card = await _index(driver, embedder).get_by_structural_key(_KEY)

    assert card is not None
    assert not card.is_canon
    assert card.tier in {TIER_UNSOURCED, TIER_LEARNING}


async def test_the_keyed_lookup_of_a_hostile_node_is_still_a_usable_card(hostile_graph):
    """The deterministic layer is the one allowed to DROP, so its output is the highest-
    stakes card in the system. Looked up against a node whose intent, model and json
    properties are all wrong-typed, it must still be a complete, correctly-typed card —
    and its confidence must be the exact-identity 1.0, undiscounted, because the
    unreadable model stamp is irrelevant to a hash match."""
    driver, embedder = hostile_graph
    card = await _index(driver, embedder).get_by_structural_key(_LONELY_KEY)

    assert card is not None
    assert card.id == f"{_PREFIX}native-lists"
    assert card.score_basis == "structural_key"
    assert card.is_canon
    assert card.model_matched is False
    assert card.confidence == 1.0
    assert card.structural_key == _LONELY_KEY


async def test_an_array_structural_key_matches_nothing_and_reads_as_absent(hostile_graph):
    """A list-valued key is unhashable and would be a dict key downstream. It reads as
    `""` — and, critically, an empty key is a MISS rather than a query, so it can never
    match every other keyless node."""
    driver, embedder = hostile_graph
    assert (await _ours(driver, embedder))[f"{_PREFIX}sk-array"].structural_key == ""
    index = _index(driver, embedder)
    assert await index.get_by_structural_key("") is None
    assert await index.get_by_structural_key("sha256:nothing-has-this") is None


# --- 4. BOTH authored `uses_rules` shapes, against real stored properties ---------


async def test_the_object_shaped_uses_rules_the_canon_really_uses_comes_through(
    hostile_graph,
):
    """HISTORY — this was a pinned KNOWN LIMITATION; it now asserts the fix.

    A `uses_rules` entry is legitimately EITHER a bare string (a static rule) or an
    OBJECT carrying `id`/`resolve_via`/`table`/`binds` — `runtime/blueprint/rules.py::
    parse_rule` handles the object form, and `tests/fixtures/corpus/blueprints.yaml`
    authors both: 2 of the 10 canon blueprints use strings and
    `bp-total-earnings-by-department` uses an object.

    The card's mapper originally accepted ONLY the string form, so that blueprint's card
    reported `()` — no rules at all — and a MIXED list lost its string entries too,
    because one unreadable member invalidated the whole value. Safe (nothing invented,
    nothing raised) but silently wrong for anything comparing a candidate's rules against
    a canon card's, which is exactly what the judge slice will do.

    `_rule_ids` now projects each entry to a NAME, mirroring `parse_rule`'s `rule_id`
    derivation (`id`, else `binds`), and skips only the members that name nothing."""
    driver, embedder = hostile_graph
    cards = await _ours(driver, embedder)
    assert cards[f"{_PREFIX}ur-dicts"].uses_rules == ("earnings_only",)
    # A MIXED list keeps both: the readable string AND the projected object id.
    assert cards[f"{_PREFIX}ur-mixed"].uses_rules == ("gross_earnings", "earnings_only")
    # The string-only form is unchanged.
    assert cards[f"{_PREFIX}source-list"].uses_rules == ("ok-rule",)
