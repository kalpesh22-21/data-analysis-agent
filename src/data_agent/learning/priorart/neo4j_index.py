"""Neo4jPriorArtIndex — the cross-tier prior-art reader over the neo4j corpora.

# READ THIS BEFORE TOUCHING THE CYPHER BELOW

**The queries in this module deliberately DO NOT filter on `source`.** Every other
neo4j read in this codebase does — `vector_index._BLUEPRINT_RECALL_QUERY`,
`_KNOWLEDGE_RECALL_QUERY` and `_GET_BLUEPRINT_QUERY` all carry a BARE
`source = 'mcp'` equality that fails closed, and `corpus_loader`'s GC and parity
probes are scoped the same way. An unfiltered read looks exactly like someone forgot
the gate. It is not forgotten. It is the point of this module:

  * Recall asks "may the AGENT be shown this?" — and the answer must be no for
    anything outside the trusted, human-reviewed MCP canon. Fail closed.
  * Prior art asks "does this ALREADY EXIST anywhere?" — and a `source='learning'`
    node that the loop landed last week is the single most likely thing a new
    candidate duplicates. Filtering it out would reintroduce the exact blindness this
    port exists to remove.

Nothing read here reaches a prompt or an answer as an artifact: it is projected onto
`PriorArtCard` (cards only, never payloads — see `models.py`) and used to adjudicate
whether a candidate is new. **If you ever make a card feed the agent's request path,
this reasoning no longer holds and the gate has to come back.**

Two other deliberate inversions of recall's behaviour, for the same "different
question" reason:

  * **No `embedding_model = $expected_model` filter.** This is not an oversight either;
    it is a correctness hole being made VISIBLE. The hydrator preserves
    `source='learning'` nodes across an embedding-model change WITHOUT re-embedding
    them — its parity accounting (`corpus_loader._EXISTING_MODELS`) is scoped to
    `source='mcp'`. Recall never notices because it filters on the model. A cross-tier
    search cannot filter (it would silently drop the whole learning tier after a model
    swap), so instead every card reports its stored `embedding_model` and a
    `model_matched` flag, and `PriorArtCard.confidence` discounts a mismatch rather
    than trusting a cosine computed between two different vector spaces.

  * **`NOT coalesce(status,'') IN $terminal`, not `coalesce(status,'validated') =
    'validated'`.** Recall asks "is this positively eligible?"; prior art asks "was
    this positively KILLED?". So only an EXPLICIT `rejected`/`retired` is excluded, and
    a `candidate`/`in_review`/un-stamped node still counts as prior art. Over-including
    here costs a human a glance; under-including re-proposes work we already have an
    opinion about.

**`db.index.vector.queryNodes` applies `$k` INSIDE the index, before the `WHERE`
runs.** The terminal-status clause is therefore a post-filter that can eat results, so
the reader over-fetches (`_OVERFETCH`) and truncates after mapping.

Import-safe without the neo4j SDK: the driver is injected, and the only neo4j import is
under `TYPE_CHECKING`.
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

    BARE equality on `'mcp'`, exactly like recall's trust gate — a node must SAY it is
    canon to be treated as canon. Everything else that is a non-empty string is the
    learning staging tier; anything absent, blank, or not a string at all is the
    explicit `unsourced` tier.

    Both writers we control always stamp a non-null `source` (`corpus_loader` from
    `BlueprintSeed.source`, which now refuses to be overridden by a null export value;
    the learning landing writer hardcodes `"learning"`). So an `unsourced` card means a
    hand edit or a foreign writer touched the graph, which is worth SEEING rather than
    silently absorbing into one of the real tiers."""
    if raw == TIER_MCP:
        return TIER_MCP
    if isinstance(raw, str) and raw.strip():
        return TIER_LEARNING
    return TIER_UNSOURCED


def _str(raw: Any) -> str:
    """A stored property as a `str`, or `""` when it is anything else
    (`untrusted.as_str` — never `str(raw)`, which would turn a null into `"None"`)."""
    return as_str(raw)


def _bool_or_none(raw: Any) -> bool | None:
    """A stored `verified` flag as a tri-state. Only a real bool is a verdict; anything
    else — including a string `"true"` from a foreign writer — is "the node does not
    say" (see `PriorArtCard.verified`)."""
    return as_bool_or_none(raw)


def _float(raw: Any) -> float:
    """The index score as a renderable float in `[0.0, 1.0]`.

    A score that is unusable for RANKING degrades to 0.0 — the bottom of the list —
    rather than crashing the whole search. Three ways to be unusable, and the third was
    missing until a prompt started rendering these:

      * non-numeric — every consumer `>=`-compares it against a threshold, which raises
        on a `str`/`None`;
      * `bool` — an `int` subclass, and a `True` silently ranking as 1.0 is a perfect
        false positive;
      * OUT OF RANGE or unconvertible. The score is a cosine, so anything outside
        `[0, 1]` is a broken signal rather than a weak one. `float(10**400)` raises
        `OverflowError`; `inf` outranks every genuine hit and would let a hand-edited
        node top the list; `nan` fails both comparisons and is rejected by the same
        test. Only a hand edit or a foreign writer can put such a value on a node,
        which is exactly the population the `unsourced` tier exists to flag.

    All three are `untrusted.as_float`; the `[0, 1]` bounds are what makes them a
    RANKING guard rather than a type check.
    """
    return as_float(raw, lo=0.0, hi=1.0)


def _rule_ids(raw: Any) -> tuple[str, ...]:
    """A JSON-decoded `uses_rules` list as a `tuple[str, ...]` of rule ids, or `()`.

    **A `uses_rules` entry is legitimately EITHER shape**, and the canon authors both:
    a bare string (a static rule the SQL already inlines) or an OBJECT carrying
    `id`/`resolve_via`/`table`/`binds` (`runtime/blueprint/rules.py::parse_rule`).
    2 of the 10 canon blueprints use strings; `bp-total-earnings-by-department` stores
    `[{"id": "earnings_only", "binds": "earn_codes"}]`. An earlier cut of this mapper
    accepted only the string form, so that blueprint's card claimed it used NO rules —
    silent, and exactly wrong for a reader comparing a candidate's rules against a canon
    card's.

    The id derivation MIRRORS `parse_rule`: `id` when it is a non-empty `str`, else
    `binds`. It deliberately does NOT re-implement the rest of `parse_rule` (the
    `resolve_via` regex, the `predicate` single-token fallback) — the card needs a NAME
    for display and comparison, not an executable rule, and duplicating that parser here
    is the mirror-drift failure this codebase keeps hitting.

    **Container vs. member, treated differently on purpose.** A non-list container is
    `()`: a bare string in particular must never be ITERATED, because `tuple("abc")` is
    `('a','b','c')` — the char-explosion class, which manufactures plausible-looking rule
    ids that no registry has heard of, without raising.

    A member that names NOTHING (a nested list, a null, a number, an object with no
    usable `id`/`binds`) is SKIPPED, not fatal. That reverses the earlier all-or-nothing
    rule, deliberately: with two legal member shapes, one authoring typo would otherwise
    hide every real rule on the node, and — unlike the char-explosion case — skipping an
    unnameable member cannot FABRICATE an id. The invariant that matters is "never
    invent", not "all or nothing".

    Order is preserved and duplicates are collapsed, so the result is join-able,
    sort-able and set-comparable by every downstream reader (see `_card_from_record`).
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
    """Decode a stored `*_json` string property, tolerating null/malformed (→ `None`).
    Mirrors `vector_index._decode_json`: a corrupt property degrades that ONE field to
    absent, never the row."""
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
    """Map one prior-art row → `PriorArtCard`, coercing every field to the type its
    DOWNSTREAM READER needs.

    DERIVED FROM THE READERS, not from a remembered field list. Graph properties are
    untrusted input in exactly the way rehydrated JSON is — neo4j will store whatever a
    hand edit or a foreign writer puts there, and Python is happy to iterate a string,
    hash-fail a list, and `sorted()` its way into a TypeError. So the table below is the
    operation each field is subjected to, which is what forces the requirement; the
    field NAME forces nothing:

      field            reader / operation                              ⇒ requirement
      id               `DedupVerdict.matched_id` → `to_doc()` → JSON,
                       and `f"...{card.id}"` in logs                   ⇒ str
      intent           prompt/log rendering, `intent[:120]` slicing    ⇒ str
      source           `_tier` → `== 'mcp'` (total on any type, but a
                       non-str must not be read as a tier name)        ⇒ str, else unsourced
      status           `status in TERMINAL_STATUSES` — frozenset
                       membership, TypeError on an unhashable list     ⇒ str
      verified         rendered / `is True` — never arithmetic         ⇒ bool | None
      drift_status     `==` and rendering                              ⇒ str
      uses_rules       `sorted(...)`, set ops, `", ".join(...)` —
                       mixed types break `<`, non-str breaks join      ⇒ tuple[str, ...]
                       (BOTH authored shapes projected to an id —
                        see `_rule_ids`)
      result_grain     same joins/sorts as uses_rules                  ⇒ tuple[str, ...]
      structural_key   dict key + `==` — must be hashable              ⇒ str
      embedding_model  `== expected_model`                             ⇒ str
      score            `>= merge_threshold` float comparison —
                       TypeError against a str/None                    ⇒ float

    `result_grain` goes through `normalize_structural_grain`, the SAME normalizer the
    structural key hashes, so a card's grain and its key can never disagree about what
    the grain is; a grain that normalizer calls unusable (`None`) becomes `()`.

    A NEW field on the card belongs in this table before it belongs in the code.
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

    Holds NO driver of its own: the driver is injected so the process opens exactly one
    pool (the consumer entrypoint creates and closes it), mirroring how
    `build_promotion_write_plane` takes a driver rather than a URL.

    `expected_model` is the model the QUERY text is embedded with, and it must be the
    same value the corpus was built with. There is one source of truth for that —
    `RuntimeSettings.embedding_model`, which is what `build_hydrator` passes as both
    the embedding client's `model` and the vector index's `expected_model` — and the
    entrypoint threads the same value here. Configuring a second one would produce a
    100% `model_matched=False` rate that looks exactly like a real corpus skew.

    Unlike `Neo4jVectorIndex`, this class RAISES `PriorArtUnavailableError` on infra failure
    instead of degrading to `[]`. See `index.py`'s module docstring: here an empty list
    is a factual claim ("nothing like this exists") that the caller acts on.
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

        Same posture as `Neo4jVectorIndex.recall`: a QUERY-level failure is the caller's
        problem, but one corrupt record must not discard the good neighbours next to it.
        `_card_from_record` is total over every JSON type by construction, so reaching
        this `except` means something genuinely unexpected — worth a stack trace."""
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
        """The single driver-touching seam (mirrors `Neo4jVectorIndex._run`), so a
        Layer-1 test can drive mapping/ranking/degrade with no live neo4j."""
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, parameters)  # type: ignore[arg-type]
            return await result.data()


__all__ = ["Neo4jPriorArtIndex"]
