"""Governed-corpus trust-gate + reconcile invariants (Phase 2), Layer-1 (no infra).

The security-critical contracts:

  1. RECALL serves ONLY `source='mcp'` — the trust gate is BARE equality (never
     `coalesce`), so a `source='learning'` node OR a node with NO `source` is excluded.
  2. Corpus GC (`gc=True`) is `source='mcp'`-scoped, so it can NEVER delete a
     `source='learning'` staging node.
  3. `source`/`verified` flow export → seed → node: the seed defaults present the
     fixture/offline path as trusted canon; `load_corpus` binds them onto every upsert.
  4. The B1 no-op fast path skips on a matching `:CorpusMeta`; an empty `corpus_sha`
     (landing writer / legacy callers) never skips and never touches `:CorpusMeta`.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.retrieval.corpus_loader import (
    _GC_BLUEPRINTS,
    _GC_KNOWLEDGE,
    _READ_CORPUS_META,
    _UPSERT_BLUEPRINT,
    _UPSERT_CORPUS_META,
    _UPSERT_KNOWLEDGE,
    BlueprintSeed,
    KnowledgeSeed,
    corpus_content_sha,
    corpus_seeds_from_export,
    load_corpus,
)
from data_agent.runtime.retrieval.vector_index import (
    _BLUEPRINT_RECALL_QUERY,
    _GET_BLUEPRINT_QUERY,
    _KNOWLEDGE_RECALL_QUERY,
)

# ---------------------------------------------------------------------------
# 1. The recall trust gate — BARE `source = 'mcp'` in every read query
# ---------------------------------------------------------------------------


def test_blueprint_recall_gate_is_bare_source_equality() -> None:
    assert "node.source = 'mcp'" in _BLUEPRINT_RECALL_QUERY
    # SAFETY-CRITICAL: it must NOT be a coalesce (which would fail-OPEN, admitting a
    # node with no `source`). A bare equality excludes both learning + absent-source.
    assert "coalesce(node.source" not in _BLUEPRINT_RECALL_QUERY


def test_knowledge_recall_gate_is_bare_source_equality() -> None:
    assert "node.source = 'mcp'" in _KNOWLEDGE_RECALL_QUERY
    assert "coalesce(node.source" not in _KNOWLEDGE_RECALL_QUERY


def test_get_blueprint_keyed_fetch_is_source_gated() -> None:
    # Defense-in-depth: getBlueprint by id can never return a learning node either.
    assert "b.source = 'mcp'" in _GET_BLUEPRINT_QUERY
    assert "coalesce(b.source" not in _GET_BLUEPRINT_QUERY


# ---------------------------------------------------------------------------
# 2. The corpus GC is source='mcp'-scoped — can never reap a learning node
# ---------------------------------------------------------------------------


def test_gc_blueprints_is_scoped_to_mcp_source() -> None:
    assert "b.source = 'mcp'" in _GC_BLUEPRINTS
    assert "DETACH DELETE b" in _GC_BLUEPRINTS


def test_gc_knowledge_is_scoped_to_mcp_source() -> None:
    assert "k.source = 'mcp'" in _GC_KNOWLEDGE
    assert "DETACH DELETE k" in _GC_KNOWLEDGE


# ---------------------------------------------------------------------------
# 3. source/verified flow export → seed (defaults + verbatim + whitelist)
# ---------------------------------------------------------------------------


def test_seed_defaults_are_trusted_canon() -> None:
    # The fixture/offline path never carries source/verified — the dataclass defaults
    # present a seed as TRUSTED canon by construction.
    bp = BlueprintSeed(id="b", intent="i", slots_summary="", uses=["a.b.c"])
    kn = KnowledgeSeed(id="k", text="t", doc_id="d")
    assert (bp.source, bp.verified) == ("mcp", True)
    assert (kn.source, kn.verified) == ("mcp", True)


def test_corpus_seeds_from_export_carries_source_verified_and_whitelists() -> None:
    export = {
        "blueprints": {
            "bp-1": {
                "id": "bp-1",
                "intent": "total x",
                "slots_summary": "",
                "uses": ["db.t.c"],
                "source": "mcp",
                "verified": True,
                # An unknown/extra field the MCP might add must be DROPPED (whitelist),
                # not spread into the dataclass (which would TypeError).
                "unexpected_future_field": {"nested": 1},
            }
        },
        "blueprints_sha": "bsha",
        "knowledge": {
            "kn-1": {"id": "kn-1", "text": "policy", "doc_id": "doc", "source": "mcp", "verified": True}
        },
        "knowledge_sha": "ksha",
    }
    blueprints, knowledge = corpus_seeds_from_export(export)
    assert len(blueprints) == 1 and len(knowledge) == 1
    assert blueprints[0].id == "bp-1"
    assert blueprints[0].source == "mcp" and blueprints[0].verified is True
    assert knowledge[0].source == "mcp" and knowledge[0].verified is True


def test_corpus_seeds_from_export_uses_key_as_id_and_skips_non_dict() -> None:
    export = {
        "blueprints": {"bp-key": {"intent": "i", "slots_summary": "", "uses": ["a.b.c"]}, "bad": 7},
        "knowledge": {},
    }
    blueprints, knowledge = corpus_seeds_from_export(export)
    assert [b.id for b in blueprints] == ["bp-key"]  # dict key became the id
    assert knowledge == []


def test_corpus_seeds_from_export_skips_malformed_entry_and_keeps_the_rest() -> None:
    # DEGRADE-not-fail: a malformed entry (missing the required `text` for a
    # KnowledgeSeed, or missing `intent`/`uses` for a BlueprintSeed) is SKIPPED with a
    # warning — it must NEVER raise and brick the whole seed (the cache would then retry
    # the same bad export every turn forever).
    export = {
        "blueprints": {
            "good": {"intent": "i", "slots_summary": "", "uses": ["a.b.c"]},
            "bad-missing-required": {"slots_summary": "no intent/uses"},  # TypeError bait
        },
        "knowledge": {
            "good-kn": {"text": "t", "doc_id": "d"},
            "bad-kn": {"doc_id": "d"},  # missing required `text`
            "": {"text": "t", "doc_id": "d"},  # falsy id — skipped
        },
    }
    blueprints, knowledge = corpus_seeds_from_export(export)
    assert [b.id for b in blueprints] == ["good"]
    assert [k.id for k in knowledge] == ["good-kn"]


def test_corpus_content_sha_is_stable_and_content_sensitive() -> None:
    bp = [BlueprintSeed(id="b", intent="i", slots_summary="", uses=["a.b.c"])]
    kn = [KnowledgeSeed(id="k", text="t", doc_id="d")]
    sha = corpus_content_sha(bp, kn)
    assert isinstance(sha, str) and len(sha) == 40
    assert corpus_content_sha(bp, kn) == sha  # deterministic
    changed = [BlueprintSeed(id="b", intent="i2", slots_summary="", uses=["a.b.c"])]
    assert corpus_content_sha(changed, kn) != sha


# ---------------------------------------------------------------------------
# Recording fake driver — captures every tx.run so we can assert the binds
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self._row = row

    async def single(self) -> dict[str, Any] | None:
        return self._row


class _RecordingTx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, query: str, **params: Any) -> _Result:
        self.calls.append((query, params))
        if "embedding_model" in query or "models" in query.lower():
            return _Result({"models": []})  # fresh index — no parity conflict
        if "RETURN collect(column_key)" in query or "missing" in query:
            return _Result({"missing": []})
        if query in (_GC_BLUEPRINTS, _GC_KNOWLEDGE):
            return _Result({"deleted": 0})
        return _Result(None)


class _RecordingSession:
    def __init__(self, driver: _RecordingDriver) -> None:
        self._driver = driver

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def run(self, query: str, **params: Any) -> _Result:
        self._driver.session_calls.append((query, params))
        if query == _READ_CORPUS_META:
            row = {"corpus_sha": self._driver.meta_sha} if self._driver.meta_sha is not None else None
            return _Result(row)
        return _Result(None)

    async def execute_write(self, fn: Any) -> Any:
        return await fn(self._driver.tx)


class _RecordingDriver:
    def __init__(self, *, meta_sha: str | None = None) -> None:
        self.meta_sha = meta_sha
        self.tx = _RecordingTx()
        self.session_calls: list[tuple[str, dict[str, Any]]] = []

    def session(self, *, database: str = "neo4j") -> _RecordingSession:  # noqa: ARG002
        return _RecordingSession(self)


class _Embedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 768 for _ in texts]


def _bp(bp_id: str, *, source: str = "mcp", verified: bool = True) -> BlueprintSeed:
    return BlueprintSeed(
        id=bp_id, intent="i", slots_summary="", uses=["a.b.c"], source=source, verified=verified
    )


def _upsert_params(tx: _RecordingTx, query: str) -> dict[str, Any]:
    return next(params for q, params in tx.calls if q == query)


# ---------------------------------------------------------------------------
# 3 (cont). load_corpus binds source/verified/corpus_sha onto every upsert
# ---------------------------------------------------------------------------


async def test_load_corpus_binds_source_verified_corpus_sha_on_upserts() -> None:
    driver = _RecordingDriver()
    await load_corpus(
        driver,  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        [_bp("bp-1")],
        [KnowledgeSeed(id="kn-1", text="t", doc_id="d")],
        model_id="all-mpnet-base-v2",
        corpus_sha="corpus-sha-1",
    )
    bp_params = _upsert_params(driver.tx, _UPSERT_BLUEPRINT)
    assert bp_params["source"] == "mcp"
    assert bp_params["verified"] is True
    assert bp_params["corpus_sha"] == "corpus-sha-1"

    kn_params = _upsert_params(driver.tx, _UPSERT_KNOWLEDGE)
    assert kn_params["source"] == "mcp"
    assert kn_params["verified"] is True
    assert kn_params["corpus_sha"] == "corpus-sha-1"


async def test_load_corpus_binds_learning_source_verbatim() -> None:
    # A landing-writer seed (source='learning', verified=False) is bound VERBATIM —
    # load_corpus never coerces it back to canon.
    driver = _RecordingDriver()
    await load_corpus(
        driver,  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        [_bp("bp-learn", source="learning", verified=False)],
        [],
        model_id="all-mpnet-base-v2",
    )
    bp_params = _upsert_params(driver.tx, _UPSERT_BLUEPRINT)
    assert bp_params["source"] == "learning"
    assert bp_params["verified"] is False


# ---------------------------------------------------------------------------
# 2 (cont). gc=True issues the source-scoped GC + meta stamp, in one txn
# ---------------------------------------------------------------------------


async def test_load_corpus_gc_true_issues_source_scoped_gc_and_meta_stamp() -> None:
    driver = _RecordingDriver()
    await load_corpus(
        driver,  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        [_bp("bp-1")],
        [KnowledgeSeed(id="kn-1", text="t", doc_id="d")],
        model_id="all-mpnet-base-v2",
        corpus_sha="sha-x",
        gc=True,
    )
    issued = [q for q, _ in driver.tx.calls]
    assert _GC_BLUEPRINTS in issued
    assert _GC_KNOWLEDGE in issued
    assert _UPSERT_CORPUS_META in issued
    # The meta stamp carries the run's sha.
    assert _upsert_params(driver.tx, _UPSERT_CORPUS_META)["corpus_sha"] == "sha-x"


async def test_load_corpus_gc_false_issues_no_gc() -> None:
    driver = _RecordingDriver()
    await load_corpus(
        driver,  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        [_bp("bp-1")],
        [],
        model_id="all-mpnet-base-v2",
        corpus_sha="sha-x",
        gc=False,
    )
    issued = [q for q, _ in driver.tx.calls]
    assert _GC_BLUEPRINTS not in issued
    assert _GC_KNOWLEDGE not in issued
    # Meta is still stamped (a truthy corpus_sha), enabling the next-run no-op.
    assert _UPSERT_CORPUS_META in issued


# ---------------------------------------------------------------------------
# 4. B1 no-op fast path + the empty-sha (landing writer) guard
# ---------------------------------------------------------------------------


async def test_load_corpus_skips_when_meta_matches_sha() -> None:
    embedder = _Embedder()
    driver = _RecordingDriver(meta_sha="sha-x")  # already at this content
    report = await load_corpus(
        driver,  # type: ignore[arg-type]
        embedder,  # type: ignore[arg-type]
        [_bp("bp-1")],
        [],
        model_id="all-mpnet-base-v2",
        corpus_sha="sha-x",
        # ensure_schema left DEFAULT (True): the skip must precede apply_schema, so a
        # sha-match cold fetch pays NO DDL + awaitIndexes cost.
    )
    assert report.skipped is True
    assert report.blueprints_written == 0
    # NOTHING was written — no upsert txn ran, and NO DDL (the meta read is the ONLY
    # session call, proving the skip short-circuits ahead of apply_schema).
    assert driver.tx.calls == []
    assert [q for q, _ in driver.session_calls] == [_READ_CORPUS_META]


async def test_load_corpus_empty_sha_never_skips_and_never_touches_meta() -> None:
    # The landing writer + Layer-1 callers pass no corpus_sha: no skip, no CorpusMeta
    # write (so the online no-op fast path can never be corrupted by a learning write).
    driver = _RecordingDriver(meta_sha="whatever")
    report = await load_corpus(
        driver,  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        [_bp("bp-1")],
        [],
        model_id="all-mpnet-base-v2",
    )
    assert report.skipped is False
    issued = [q for q, _ in driver.tx.calls]
    assert _UPSERT_BLUEPRINT in issued
    assert _UPSERT_CORPUS_META not in issued  # never stamps meta on the empty-sha path
    # The meta was never even READ (the fast path is gated on a truthy corpus_sha).
    assert _READ_CORPUS_META not in [q for q, _ in driver.session_calls]
