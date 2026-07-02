"""VectorIndex — the recall seam (design §2/§3.2).

The vector store is settled as neo4j-native (D60); this module defines the
`VectorIndex` Protocol the pipeline depends on, an in-memory `FakeVectorIndex`
for Layer-1 and the Layer-2 embed→rerank end-to-end tests, and — Slice 2
(`neo4j-corpus-design.md`) — the real `Neo4jVectorIndex` that recalls both
corpora from a neo4j native vector index.

`recall` is order-preserving by descending similarity and sets each returned
`Candidate.score` to its recall similarity, so the "no reranker" degrade path
(design §2) keeps a meaningful recall-order score without re-computing anything.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol

from opentelemetry import trace

from data_agent.runtime.composite.ranking import cosine

from .models import BlueprintDetail, Candidate

if TYPE_CHECKING:
    from collections.abc import Mapping

    from neo4j import AsyncDriver
    from opentelemetry.trace import Tracer

_logger = logging.getLogger(__name__)


class VectorIndex(Protocol):
    """The recall seam. Phase-1 real impl is `Neo4jVectorIndex` (Slice 2)."""

    async def recall(
        self, *, query_vector: list[float], kind: str, k: int
    ) -> list[Candidate]:
        """Return up to *k* nearest `Candidate`s of *kind*, most-similar first.

        The real (neo4j) impl reads the `embedding_model` stamped on stored
        vectors and returns `[]` (with a warn span attr) on a model-id mismatch
        rather than garbage neighbours (design §2). An unavailable/empty index
        returns `[]` — a per-corpus degrade that never fails the turn.
        """
        ...

    async def get_blueprint(self, blueprint_id: str) -> BlueprintDetail | None:
        """Keyed fetch of one blueprint's stored projection by id (read-tools
        §1.2 / §4). NOT a vector op — a single keyed read over the same store —
        but colocated here so `getBlueprint` needs no parallel store abstraction.

        `None` on a genuine miss AND on any store failure (the real impl never
        raises — driver/query error degrades to `None`, D86), which the tool
        renders identically as `{found: false}` (the §3 non-oracle).
        """
        ...


class FakeVectorIndex:
    """Layer-1 `VectorIndex` double — an in-memory corpus ranked by cosine.

    Seeded per test with `(Candidate, vector)` pairs. `recall` filters by
    `kind`, ranks by `cosine(query_vector, vector)` descending, and returns the
    top-*k* with `Candidate.score` set to the similarity. Deterministic; no
    network. A `fail=True` flag makes `recall` return `[]` for every corpus, to
    exercise the pipeline's "index unavailable/empty" degrade (design §2).
    """

    def __init__(
        self,
        entries: list[tuple[Candidate, list[float]]] | None = None,
        *,
        fail: bool = False,
        details: dict[str, BlueprintDetail] | None = None,
    ) -> None:
        self._entries: list[tuple[Candidate, list[float]]] = list(entries or [])
        self._fail = fail
        # Keyed store for `get_blueprint` (read-tools §4) — seeded independently
        # of the recall `entries` since a `BlueprintDetail` carries lifecycle
        # fields (`status`/`drift_status`/`hit_count`/`catalog_sha`) a recall
        # `Candidate` does not.
        self._details: dict[str, BlueprintDetail] = dict(details or {})
        self.calls: list[tuple[str, int]] = []

    def add(self, candidate: Candidate, vector: list[float]) -> None:
        """Seed one candidate and its stored vector into the fake index."""
        self._entries.append((candidate, vector))

    def add_detail(self, detail: BlueprintDetail) -> None:
        """Seed one blueprint's keyed projection for `get_blueprint`."""
        self._details[detail.id] = detail

    async def recall(
        self, *, query_vector: list[float], kind: str, k: int
    ) -> list[Candidate]:
        self.calls.append((kind, k))
        if self._fail:
            return []
        scored: list[tuple[float, Candidate]] = [
            (cosine(query_vector, vector), candidate)
            for candidate, vector in self._entries
            if candidate.kind == kind
        ]
        # Descending similarity; a stable tiebreak on id keeps recall order
        # deterministic for candidates with identical similarity.
        scored.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [replace(candidate, score=sim) for sim, candidate in scored[:k]]

    async def get_blueprint(self, blueprint_id: str) -> BlueprintDetail | None:
        """Answer from the seeded details; `fail=True` degrades to `None` (the
        store-unavailable path the tool renders as `{found: false}`)."""
        if self._fail:
            return None
        return self._details.get(blueprint_id)


# --------------------------------------------------------------------------
# Neo4jVectorIndex (Slice 2) — the real recall over a neo4j native vector index
# --------------------------------------------------------------------------

# Per-corpus (vector index name, node label) — `recall(kind=...)` selects the
# index by name so each corpus is a clean top-k with NO post-filter on `kind`
# (neo4j-corpus-design §1.2 / §2.2). An unknown kind → `[]` (never raises).
_CORPUS_INDEX: dict[str, str] = {
    "blueprint": "blueprint_intent_vec",
    "knowledge": "knowledge_text_vec",
}
_CORPUS_LABEL: dict[str, str] = {
    "blueprint": "Blueprint",
    "knowledge": "KnowledgeChunk",
}

# One parameterized read per corpus. The `WHERE node.embedding_model =
# $expected_model` clause is the read-time parity guard (design §2.3): a corpus
# embedded with a different model never comes back (empty → the pipeline's
# empty-corpus degrade, D86), rather than garbage neighbours in a mismatched
# vector space. ORDER BY score DESC keeps recall order-preserving.
_BLUEPRINT_RECALL_QUERY = """
CALL db.index.vector.queryNodes($index_name, $k, $query_vector)
YIELD node, score
WHERE node.embedding_model = $expected_model
RETURN node.id AS id, node.intent AS text, node.slots_summary AS slots_summary,
       node.uses AS uses, score
ORDER BY score DESC
"""

_KNOWLEDGE_RECALL_QUERY = """
CALL db.index.vector.queryNodes($index_name, $k, $query_vector)
YIELD node, score
WHERE node.embedding_model = $expected_model
RETURN node.id AS id, node.text AS text, node.title AS title,
       node.doc_id AS doc_id, score
ORDER BY score DESC
"""

_CORPUS_QUERY: dict[str, str] = {
    "blueprint": _BLUEPRINT_RECALL_QUERY,
    "knowledge": _KNOWLEDGE_RECALL_QUERY,
}


def _coerce_uses(raw: Any) -> frozenset[str] | None:
    """Coerce a stored `uses` value into a `frozenset[str]`, or `None` when it is
    UNDETERMINED — the fail-closed contract (B1 / QA flag 1).

    `Candidate.uses is None` makes the scope pre-filter DROP the blueprint
    (`scope_filter.is_blueprint_in_scope`: `None` → fail-closed, never
    allow-all). Returning a `frozenset()` instead would pass EVERY scope
    (`frozenset() <= anything`) — a silent fail-OPEN. So anything that is not a
    clean, non-empty list of `str` (null, empty, a bare string that would
    explode into a char-set, non-`str` elements, non-list, unhashable elements)
    maps to `None`, not to an empty/garbage frozenset.
    """
    if not raw:
        return None  # null or empty → undetermined → DROP fail-closed
    if not isinstance(raw, list):
        return None  # e.g. a bare string (would char-explode) → undetermined
    if not all(isinstance(item, str) for item in raw):
        return None  # non-str elements → undetermined (never a non-str frozenset)
    return frozenset(raw)


def map_blueprint_record(record: Mapping[str, Any]) -> Candidate:
    """Map one blueprint recall row → `Candidate`.

    THE load-bearing conversion: `Candidate.uses` carries the byte-exact
    `"database.table.column"` strings stored on the node (NOT re-derived) as a
    `frozenset[str]`, or `None` when the stored value is undetermined/corrupt
    (`_coerce_uses`, fail-closed — the scope pre-filter then drops it rather
    than fail-open, neo4j-corpus-design §0 / §8).
    """
    text = record["text"]
    return Candidate(
        id=record["id"],
        kind="blueprint",
        text=text,
        uses=_coerce_uses(record["uses"]),
        payload={"intent": text, "slots_summary": record.get("slots_summary") or ""},
        score=record["score"],
    )


def map_knowledge_record(record: Mapping[str, Any]) -> Candidate:
    """Map one knowledge recall row → `Candidate` (entity-agnostic, `uses=None`)."""
    text = record["text"]
    return Candidate(
        id=record["id"],
        kind="knowledge",
        text=text,
        uses=None,
        payload={
            "title": record.get("title"),
            "chunk": text,
            "doc_id": record.get("doc_id"),
        },
        score=record["score"],
    )


_CORPUS_MAPPER = {
    "blueprint": map_blueprint_record,
    "knowledge": map_knowledge_record,
}

# Keyed single-blueprint fetch for `getBlueprint` (read-tools §1.2 / §4). Reads
# the stored D87 projection PLUS the additive full-DAG JSON properties now that
# the `runBlueprint` brick lands (runblueprint-design §1.3 / OQ-T1). The recall
# query (`_BLUEPRINT_RECALL_QUERY`) is UNCHANGED — it selects its own explicit
# field list, so the new properties are simply not read by recall (D87 invariant
# preserved). No parity `WHERE embedding_model` guard: a keyed metadata read, not
# a vector-space recall (§1.2), so a model-mismatched vector is irrelevant.
_GET_BLUEPRINT_QUERY = """
MATCH (b:Blueprint {id: $id})
RETURN b.id AS id, b.intent AS intent, b.slots_summary AS slots_summary,
       b.uses AS uses, b.status AS status, b.drift_status AS drift_status,
       b.hit_count AS hit_count, b.catalog_sha AS catalog_sha,
       b.resolves_json AS resolves_json, b.slots_json AS slots_json,
       b.uses_rules_json AS uses_rules_json, b.sql_template AS sql_template,
       b.composes_json AS composes_json, b.result_grain_json AS result_grain_json
"""


def _decode_json(raw: Any) -> Any:
    """JSON-decode a stored `*_json` string property, tolerating null/malformed
    (→ `None`) so a corrupt DAG field degrades to "absent" rather than crashing
    the keyed fetch — the tool then simply omits it (additive, fail-soft)."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        _logger.warning("skipping malformed blueprint DAG JSON property", exc_info=True)
        return None


def map_blueprint_detail_record(record: Mapping[str, Any]) -> BlueprintDetail:
    """Map one `getBlueprint` row → `BlueprintDetail` (read-tools §1.2, grown with
    the full DAG in runblueprint-design §1.3).

    `uses` is coerced with the SAME fail-closed `_coerce_uses` as recall so an
    undetermined/corrupt stored value becomes `None` (scope check then drops it,
    never fail-open). `hit_count` defaults to 0 on a null/non-int stored value.
    The additive DAG fields are JSON-decoded (`_decode_json`), defaulting to `None`
    when absent/corrupt — a blueprint with no stored DAG maps exactly as before.
    """
    raw_hits = record.get("hit_count")
    hit_count = raw_hits if isinstance(raw_hits, int) and not isinstance(raw_hits, bool) else 0
    resolves = _decode_json(record.get("resolves_json"))
    slots = _decode_json(record.get("slots_json"))
    uses_rules = _decode_json(record.get("uses_rules_json"))
    composes = _decode_json(record.get("composes_json"))
    result_grain = _decode_json(record.get("result_grain_json"))
    sql_template = record.get("sql_template")
    return BlueprintDetail(
        id=record["id"],
        intent=record.get("intent") or "",
        slots_summary=record.get("slots_summary") or "",
        uses=_coerce_uses(record.get("uses")),
        status=record.get("status") or "",
        drift_status=record.get("drift_status") or "",
        hit_count=hit_count,
        catalog_sha=record.get("catalog_sha") or "",
        resolves=resolves if isinstance(resolves, dict) else None,
        slots=slots if isinstance(slots, list) else None,
        uses_rules=uses_rules if isinstance(uses_rules, list) else None,
        sql_template=sql_template if isinstance(sql_template, str) else None,
        composes=composes if isinstance(composes, list) else None,
        result_grain=result_grain if isinstance(result_grain, (list, dict)) else None,
    )


class Neo4jVectorIndex:
    """Real `VectorIndex` — recall from a neo4j native vector index (Slice 2).

    A pure drop-in behind the frozen `VectorIndex` protocol: the pipeline,
    scope filter and render are UNCHANGED. It holds a long-lived async driver
    (one per process, created at construction — driver creation is lazy and
    makes no connection until the first query), runs ONE
    `db.index.vector.queryNodes` per corpus keyed by `kind`, maps each row to a
    `Candidate`, and — critically — NEVER raises: every driver/query/timeout
    failure is caught and returned as `[]` (D86 degrade; the pipeline already
    observes index degrades). `close()` shuts the pool down (called from the
    app lifespan, design §2.4).
    """

    def __init__(
        self,
        *,
        url: str,
        auth: tuple[str, str],
        expected_model: str,
        database: str = "neo4j",
        timeout_seconds: float = 10.0,
        driver: AsyncDriver | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._expected_model = expected_model
        self._database = database
        self._timeout_seconds = timeout_seconds
        self._tracer = tracer
        if driver is not None:
            self._driver: AsyncDriver = driver
        else:
            from neo4j import AsyncGraphDatabase

            # Driver-level timeouts bound the "unreachable host" degrade: a bad
            # URI / down neo4j fails connection acquisition within the budget and
            # is caught in `recall` → `[]` (never a hung turn, design §2.4).
            self._driver = AsyncGraphDatabase.driver(
                url,
                auth=auth,
                connection_timeout=timeout_seconds,
                connection_acquisition_timeout=timeout_seconds,
                max_transaction_retry_time=timeout_seconds,
            )

    async def recall(
        self, *, query_vector: list[float], kind: str, k: int
    ) -> list[Candidate]:
        """Recall up to *k* nearest `Candidate`s of *kind*, most-similar first.

        Never raises: an unknown kind, unreachable neo4j, auth failure, query
        error or timeout ALL return `[]` (D86 degrade). A non-empty corpus that
        the parity guard drops entirely (total model mismatch) sets a shape-only
        `retrieval.model_mismatch` attribute on the current span (design §2.3).
        """
        query = _CORPUS_QUERY.get(kind)
        if query is None:
            return []  # defensive — unknown corpus, never raise
        # QUERY-level failure (unreachable neo4j / auth / timeout / query error)
        # degrades the WHOLE recall to [] (D86). ROW-level malformation degrades
        # only that row (QA flag 2): a single corrupt record must not discard the
        # good neighbours alongside it.
        try:
            records = await self._run(
                query,
                {
                    "index_name": _CORPUS_INDEX[kind],
                    "k": k,
                    "query_vector": query_vector,
                    "expected_model": self._expected_model,
                },
            )
        except Exception:  # noqa: BLE001 - any driver/query failure degrades to []
            _logger.warning(
                "neo4j recall failed for corpus %s; returning empty", kind, exc_info=True
            )
            return []

        mapper = _CORPUS_MAPPER[kind]
        candidates: list[Candidate] = []
        for record in records:
            try:
                candidates.append(mapper(record))
            except Exception:  # noqa: BLE001 - one bad row is skipped, not fatal
                _logger.warning(
                    "skipping malformed neo4j recall row for corpus %s", kind, exc_info=True
                )
        if not candidates:
            await self._flag_model_mismatch(kind)
        return candidates

    async def get_blueprint(self, blueprint_id: str) -> BlueprintDetail | None:
        """Keyed fetch of one blueprint's stored projection (read-tools §1.2).

        Never raises: a missing id, unreachable neo4j, auth failure, query error
        or timeout ALL return `None` (D86 degrade). A single MATCH through the
        same `_run` seam recall uses, so Layer-1 drives it without a live store.
        """
        try:
            records = await self._run(_GET_BLUEPRINT_QUERY, {"id": blueprint_id})
        except Exception:  # noqa: BLE001 - any driver/query failure degrades to None
            # Log a bounded, quoted slice of the model-supplied id (never the
            # raw unbounded value) — it is parameterized in the query, not
            # interpolated, so this is purely a safe log hygiene measure.
            _logger.warning(
                "neo4j getBlueprint failed for id %r; returning None",
                blueprint_id[:80],
                exc_info=True,
            )
            return None
        if not records:
            return None
        try:
            return map_blueprint_detail_record(records[0])
        except Exception:  # noqa: BLE001 - a malformed row is a miss, not a crash
            _logger.warning(
                "skipping malformed getBlueprint row for id %r", blueprint_id[:80], exc_info=True
            )
            return None

    async def _run(
        self, query: str, parameters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Run one read query and return its rows as plain dicts.

        Factored out as the single driver-touching seam so Layer-1 tests can
        drive `recall` (mapping + degrade + parity) without a live neo4j by
        monkeypatching this method.
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, parameters)  # type: ignore[arg-type]
            return await result.data()

    async def _flag_model_mismatch(self, kind: str) -> None:
        """When a corpus recall came back empty, probe whether the corpus has any
        nodes at all. If it does, the parity `WHERE` dropped everything (total
        model mismatch) — set a shape-only `retrieval.model_mismatch` attribute
        on the current span so the misconfiguration is observable (design §2.3).
        Best-effort: any probe failure is swallowed (it is pure observability)."""
        label = _CORPUS_LABEL.get(kind)
        if label is None:
            return
        try:
            rows = await self._run(f"MATCH (n:{label}) RETURN count(n) AS c", {})
            count = rows[0]["c"] if rows else 0
        except Exception:  # noqa: BLE001 - probe is best-effort observability only
            return
        if count > 0:
            trace.get_current_span().set_attribute("retrieval.model_mismatch", True)

    async def close(self) -> None:
        """Close the driver connection pool (app lifespan shutdown, design §2.4)."""
        await self._driver.close()


__all__ = [
    "FakeVectorIndex",
    "Neo4jVectorIndex",
    "VectorIndex",
    "map_blueprint_detail_record",
    "map_blueprint_record",
    "map_knowledge_record",
]
