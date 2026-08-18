"""Neo4jPriorArtIndex — the cross-tier prior-art reader over the neo4j corpora.

THE QUERIES BELOW DELIBERATELY DO NOT FILTER ON `source`. Every other neo4j read in this
codebase does, so an unfiltered read looks exactly like a forgotten gate. It is not: recall
asks "may the AGENT be shown this?", where anything outside the trusted canon must fail
closed, while prior art asks "does this ALREADY EXIST anywhere?", and a `source='learning'`
node the loop landed last week is the single most likely thing a new candidate duplicates.
Nothing read here reaches a prompt as an artifact — it is projected onto `PriorArtCard` and
used only to adjudicate novelty. IF A CARD EVER FEEDS THE AGENT'S REQUEST PATH, THIS REASONING
NO LONGER HOLDS AND THE GATE MUST COME BACK.

Two further deliberate inversions, for the same "different question" reason. NO
`embedding_model` FILTER: the hydrator preserves learning-tier nodes across a model change
without re-embedding them, and a cross-tier search that filtered would silently drop the whole
tier after a model swap — so every card reports its stored model and a `model_matched` flag,
and `confidence` discounts a mismatch. And `NOT status IN $terminal` rather than
`status = 'validated'`: recall asks "is this positively eligible?", prior art asks "was this
positively KILLED?", so an un-stamped node still counts.

`db.index.vector.queryNodes` applies `$k` INSIDE the index, before the `WHERE`, so the
terminal-status clause is a post-filter that can eat results — hence the over-fetch. Import-safe
without the neo4j SDK: the driver is injected.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from data_agent.runtime.blueprint.structural_key import normalize_structural_grain
from data_agent.runtime.retrieval.vector_index import CORPUS_INDEX_BY_KIND
from data_agent.untrusted import as_bool_or_none, as_float, as_str

from .index import PriorArtUnavailableError
from .models import (
    TERMINAL_STATUSES,
    TIER_LEARNING,
    TIER_MCP,
    TIER_UNSOURCED,
    PriorArtCard,
    PriorArtKind,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from neo4j import AsyncDriver

    from data_agent.runtime.model.embedding_client import EmbeddingClient

_logger = logging.getLogger(__name__)

# THE index-name map, imported from recall rather than mirrored. It was duplicated on
# the argument that the two readers must be free to diverge (this one may one day grow a
# corpus recall never serves) — but the names are PHYSICAL neo4j objects created once by
# the hydrator, so divergence is not a degree of freedom either reader has: a rename here
# and not there queries an index that does not exist, which neo4j answers with an error,
# i.e. a permanent `PriorArtUnavailableError` and a permanently fail-open loop. If this
# reader ever does serve a corpus recall does not, that is a new entry in the shared map
# (an index the hydrator must create), not a second map.
_KIND_INDEX = CORPUS_INDEX_BY_KIND

# How many extra rows to pull from the index per requested result. `$k` is applied
# INSIDE the vector index before the terminal-status `WHERE`, so without over-fetching a
# corpus whose nearest neighbours happen to be rejected artifacts returns short. 4x is
# arbitrary but bounded: the whole point is to survive a handful of dead neighbours, not
# to page the corpus.
_OVERFETCH = 4

# Absolute ceiling on `$k`, so a caller passing a silly `limit` cannot ask neo4j to
# materialize the whole corpus into a sort.
_MAX_K = 200

# Materialized once: a neo4j list parameter must be a `list`, and `sorted()` keeps the
# value the query plan sees stable across processes (frozenset iteration order is not).
_TERMINAL_LIST: list[str] = sorted(TERMINAL_STATUSES)

# The projection every prior-art row returns, one spelling for both corpora so ONE
# mapper serves both (`_card_from_record`). The knowledge corpus has no
# structural_key/uses_rules/result_grain, so it returns explicit nulls for them rather
# than a shorter row — a mapper that has to ask "which shape is this?" is a mapper that
# will eventually guess wrong.
_BLUEPRINT_PROJECTION = """
    node.id AS id, node.intent AS intent, node.source AS source,
    node.status AS status, node.verified AS verified,
    node.drift_status AS drift_status, node.embedding_model AS embedding_model,
    node.structural_key AS structural_key,
    node.uses_rules_json AS uses_rules_json,
    node.result_grain_json AS result_grain_json
"""
_KNOWLEDGE_PROJECTION = """
    node.id AS id, node.text AS intent, node.source AS source,
    node.status AS status, node.verified AS verified,
    node.drift_status AS drift_status, node.embedding_model AS embedding_model,
    null AS structural_key, null AS uses_rules_json, null AS result_grain_json
"""

# --- THE UNFILTERED VECTOR SEARCH (see the module docstring before editing) ---------
# NO `source` gate    — every trust tier is prior art (that is the whole point).
# NO `embedding_model` gate — a model-skewed corpus must stay visible, discounted.
# ONLY an explicitly terminal status is excluded.
_SEARCH_QUERY_TEMPLATE = """
CALL db.index.vector.queryNodes($index_name, $k, $query_vector)
YIELD node, score
WHERE NOT coalesce(node.status, '') IN $terminal_statuses
RETURN {projection}, score
ORDER BY score DESC
"""

_SEARCH_QUERY: dict[str, str] = {
    "blueprint": _SEARCH_QUERY_TEMPLATE.format(projection=_BLUEPRINT_PROJECTION),
    "knowledge": _SEARCH_QUERY_TEMPLATE.format(projection=_KNOWLEDGE_PROJECTION),
}

# --- THE DETERMINISTIC KEYED LOOKUP -------------------------------------------------
# Backed by a RANGE index (`blueprint_structural_key`, created in
# `corpus_loader.schema_statements`) so this is a `NodeIndexSeek`, not a label scan —
# verified with EXPLAIN against the live graph, because the previous slice found that
# the obvious index shape can silently lose the plan property you assumed it had.
#
# `ORDER BY (canon first), id` is SEMANTIC, not cosmetic: when both tiers carry the same
# structural key, "the MCP canon already has this" is the stronger answer and the only
# one that justifies dropping a candidate. The `id` tiebreak makes the choice
# deterministic when two nodes in the same tier collide.
#
# The `node` alias in the projection is bound by the `MATCH` below so ONE projection
# constant serves both queries.
_BY_STRUCTURAL_KEY_QUERY = f"""
MATCH (node:Blueprint)
WHERE node.structural_key = $key
  AND NOT coalesce(node.status, '') IN $terminal_statuses
RETURN {_BLUEPRINT_PROJECTION}, 1.0 AS score
ORDER BY CASE WHEN node.source = 'mcp' THEN 0 ELSE 1 END, node.id
LIMIT 1
"""


def _tier(raw: Any) -> str:
    """Map a stored `source` property onto a card tier.

    BARE equality on `'mcp'`, exactly like recall's trust gate — a node must SAY it is canon to be
    treated as canon. Any other non-empty string is the learning staging tier; anything absent,
    blank or not a string is the explicit `unsourced` tier. Both writers we control always stamp a
    non-null `source`, so an `unsourced` card means a hand edit or a foreign writer touched the
    graph, which is worth SEEING rather than absorbing into a real tier.
    """
    if raw == TIER_MCP:
        return TIER_MCP
    if isinstance(raw, str) and raw.strip():
        return TIER_LEARNING
    return TIER_UNSOURCED


def _str(raw: Any) -> str:
    """A stored property as a `str`, or `""` — never `str(raw)`, which turns a null into "None"."""
    return as_str(raw)


def _bool_or_none(raw: Any) -> bool | None:
    """A stored `verified` flag as a tri-state.

    Only a real bool is a verdict; anything else, including a string `"true"` from a foreign
    writer, means "the node does not say".
    """
    return as_bool_or_none(raw)


def _float(raw: Any) -> float:
    """The index score as a renderable float in `[0.0, 1.0]`.

    A score unusable for RANKING degrades to 0.0 — the bottom of the list — rather than crashing
    the whole search. Three ways to be unusable: non-numeric (every consumer `>=`-compares it),
    `bool` (an `int` subclass, and a `True` ranking as 1.0 is a perfect false positive), and OUT
    OF RANGE or unconvertible — the score is a cosine, so anything outside `[0, 1]` is broken
    rather than weak, `inf` would outrank every genuine hit and `float(10**400)` raises. All three
    are `untrusted.as_float`; the bounds are what make it a RANKING guard rather than a type
    check.
    """
    return as_float(raw, lo=0.0, hi=1.0)


def _rule_ids(raw: Any) -> tuple[str, ...]:
    """A JSON-decoded `uses_rules` list as a `tuple[str, ...]` of rule ids, or `()`.

    A `uses_rules` entry is legitimately EITHER shape and the canon authors both: a bare string,
    or an OBJECT carrying `id`/`binds`. Accepting only the string form made a canon blueprint's
    card claim it used NO rules — silent, and exactly wrong for a reader comparing rule sets. The
    id derivation MIRRORS `parse_rule` (`id` when a non-empty str, else `binds`) and deliberately
    does not re-implement the rest of it: the card needs a NAME, not an executable rule.

    CONTAINER vs MEMBER, treated differently on purpose. A non-list container is `()`: a bare
    string must never be ITERATED, because `tuple("abc")` manufactures plausible rule ids no
    registry has heard of, without raising. A member that names NOTHING is SKIPPED rather than
    fatal — with two legal member shapes, one authoring typo would otherwise hide every real rule,
    and skipping an unnameable member cannot FABRICATE an id. The invariant is "never invent", not
    "all or nothing". Order is preserved and duplicates collapse.
    """
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for item in raw:
        name = _rule_id(item)
        if name and name not in out:
            out.append(name)
    return tuple(out)


def _rule_id(item: Any) -> str:
    """One `uses_rules` entry's id, or `""` when it names nothing."""
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    for key in ("id", "binds"):  # `parse_rule`'s own precedence
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _decode_json(raw: Any) -> Any:
    """Decode a stored `*_json` string property, tolerating null or malformed input (→ `None`).

    A corrupt property degrades that ONE field to absent, never the row.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        _logger.warning("prior art: skipping malformed JSON node property", exc_info=True)
        return None


def _card_from_record(
    record: Mapping[str, Any],
    *,
    kind: PriorArtKind,
    expected_model: str,
    score_basis: str = "vector",
) -> PriorArtCard:
    """Map one prior-art row → `PriorArtCard`, coercing every field to what its READER needs.

    DERIVED FROM THE READERS, not from a remembered field list. Graph properties are untrusted in
    exactly the way rehydrated JSON is — neo4j stores whatever a hand edit or a foreign writer
    puts there, and Python is happy to iterate a string, hash-fail a list and `sorted()` its way
    into a TypeError. So the OPERATION forces the requirement and the field name forces nothing:
    rendering and `==` ⇒ `str`; frozenset membership (`status`) ⇒ a hashable `str`; `is True`
    (`verified`) ⇒ `bool | None`; sorts, set ops and joins (`uses_rules`, `result_grain`) ⇒
    `tuple[str, ...]`; a dict key (`structural_key`) ⇒ `str`; a float comparison (`score`) ⇒
    `float`.

    `result_grain` goes through `normalize_structural_grain`, the SAME normalizer the structural
    key hashes, so a card's grain and its key can never disagree. A NEW field on the card belongs
    in this docstring before it belongs in the code.
    """
    embedding_model = _str(record.get("embedding_model"))
    grain = normalize_structural_grain(_decode_json(record.get("result_grain_json")))
    return PriorArtCard(
        id=_str(record.get("id")),
        kind=kind,
        tier=_tier(record.get("source")),  # type: ignore[arg-type]
        status=_str(record.get("status")),
        verified=_bool_or_none(record.get("verified")),
        drift_status=_str(record.get("drift_status")),
        intent=_str(record.get("intent")),
        result_grain=tuple(grain or ()),
        uses_rules=_rule_ids(_decode_json(record.get("uses_rules_json"))),
        structural_key=_str(record.get("structural_key")),
        embedding_model=embedding_model,
        similarity=_float(record.get("score")),
        # BARE equality, no coalesce: an un-stamped `embedding_model` is NOT a match.
        # Treating "the node does not say" as parity is how a post-swap corpus would
        # keep scoring full-confidence cosines across two vector spaces.
        model_matched=bool(embedding_model) and embedding_model == expected_model,
        score_basis=score_basis,  # type: ignore[arg-type]
    )


class Neo4jPriorArtIndex:
    """Real `PriorArtIndex` — one ANN call per corpus over the neo4j vector indexes.

    Holds NO driver of its own: the driver is injected so the process opens exactly one pool.
    `expected_model` is the model the QUERY text is embedded with and MUST equal the value the
    corpus was built with — there is one source of truth for that, and configuring a second would
    produce a 100% `model_matched=False` rate that looks exactly like a real corpus skew. Unlike
    `Neo4jVectorIndex`, this RAISES `PriorArtUnavailableError` on infra failure instead of
    degrading to `[]`, because here an empty list is a factual claim the caller acts on.
    """

    def __init__(
        self,
        *,
        driver: AsyncDriver,
        embedding_client: EmbeddingClient,
        expected_model: str,
        database: str = "neo4j",
    ) -> None:
        self._driver = driver
        self._embedding_client = embedding_client
        self._expected_model = expected_model
        self._database = database

    async def search(
        self,
        text: str,
        *,
        kinds: tuple[PriorArtKind, ...] = ("blueprint",),
        limit: int = 5,
    ) -> list[PriorArtCard]:
        """See `PriorArtIndex.search`. One embed of *text*, then one
        `db.index.vector.queryNodes` per requested corpus."""
        query_text = text.strip()
        if not query_text or limit <= 0:
            # Nothing to look for — a real, answerable "no prior art", not a failure.
            return []
        known = tuple(k for k in kinds if k in _SEARCH_QUERY)
        if not known:
            return []

        try:
            vectors = await self._embedding_client.embed([query_text])
        except Exception as exc:  # noqa: BLE001 - any embed failure ⇒ we could not look
            raise PriorArtUnavailableError(
                "prior-art search could not embed the query text"
            ) from exc
        if not vectors or not vectors[0]:
            # A well-formed response carrying no vector is still "we could not look" —
            # returning [] here would assert novelty on the strength of a broken embedder.
            raise PriorArtUnavailableError("embedding endpoint returned no query vector")
        query_vector = list(vectors[0])

        k = min(max(limit, 1) * _OVERFETCH, _MAX_K)
        cards: list[PriorArtCard] = []
        for kind in known:
            try:
                records = await self._run(
                    _SEARCH_QUERY[kind],
                    {
                        "index_name": _KIND_INDEX[kind],
                        "k": k,
                        "query_vector": query_vector,
                        "terminal_statuses": _TERMINAL_LIST,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - a query failure is "could not look"
                raise PriorArtUnavailableError(
                    f"prior-art search failed against the {kind} corpus"
                ) from exc
            cards.extend(self._map_rows(records, kind=kind))

        # ROW-level malformation already degraded to a skipped row in `_map_rows`; here
        # we only re-rank. Sorting on `confidence` (not the raw cosine) is what makes the
        # model-skew discount actually affect the ORDER a consumer sees, rather than
        # being a decoration on a list still ranked by an incomparable score.
        cards.sort(key=lambda c: (-c.confidence, c.id))
        return cards[:limit]

    async def get_by_structural_key(self, key: str) -> PriorArtCard | None:
        """See `PriorArtIndex.get_by_structural_key`. A `NodeIndexSeek` on the
        `blueprint_structural_key` range index; canon wins a cross-tier tie."""
        if not key:
            return None  # `structural_key_from_templates` returns "" when it cannot mint
        try:
            records = await self._run(
                _BY_STRUCTURAL_KEY_QUERY,
                {"key": key, "terminal_statuses": _TERMINAL_LIST},
            )
        except Exception as exc:  # noqa: BLE001
            raise PriorArtUnavailableError(
                "prior-art structural-key lookup failed"
            ) from exc
        if not records:
            return None
        cards = self._map_rows(records, kind="blueprint", score_basis="structural_key")
        return cards[0] if cards else None

    def _map_rows(
        self,
        records: list[dict[str, Any]],
        *,
        kind: PriorArtKind,
        score_basis: str = "vector",
    ) -> list[PriorArtCard]:
        """Map rows → cards, skipping (never raising on) a single malformed row.

        Same posture as `Neo4jVectorIndex.recall`: a QUERY-level failure is the caller's problem, but
        one corrupt record must not discard the good neighbours next to it. `_card_from_record` is
        total over every JSON type by construction, so reaching this `except` means something
        genuinely unexpected — worth a stack trace.
        """
        cards: list[PriorArtCard] = []
        for record in records:
            try:
                cards.append(
                    _card_from_record(
                        record,
                        kind=kind,
                        expected_model=self._expected_model,
                        score_basis=score_basis,
                    )
                )
            except Exception:  # noqa: BLE001 - one bad row is skipped, not fatal
                _logger.warning(
                    "prior art: skipping malformed %s row", kind, exc_info=True
                )
        return cards

    async def _run(self, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        """The single driver-touching seam, so a Layer-1 test can drive mapping with no neo4j."""
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, parameters)  # type: ignore[arg-type]
            return await result.data()


__all__ = ["Neo4jPriorArtIndex"]
