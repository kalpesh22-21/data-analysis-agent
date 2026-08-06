"""Layer-1 tests for the configurable/inferred embedding dimension (Part B) and the
DESTRUCTIVE nuke + rebuild-from-MCP path (Part C) — no live neo4j.

Part B: the vector-index dimension is resolved from config or inferred from the live
embedder, `schema_statements(dim)` renders it, and a pre-existing index at a DIFFERENT
dimension raises `DimensionMismatchError` (loud signal that the model changed).

Part C: `nuke_graph` drops the vector indexes + constraints and DETACH-DELETEs every
node (incl. the freshness singletons, so the re-seed is not short-circuited); the
`claim_rebuild_lock` single-flight guard gates who rebuilds.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.runtime.retrieval.corpus_loader import (
    _CLAIM_REBUILD_LOCK,
    _EXISTING_VECTOR_DIMS,
    _NUKE_DELETE_NODES,
    _NUKE_STATEMENTS,
    _REBUILD_LOCK_CONSTRAINT,
    CorpusLoadError,
    DimensionMismatchError,
    apply_schema,
    check_dimension_parity,
    claim_rebuild_lock,
    nuke_graph,
    resolve_embedding_dimension,
    schema_statements,
)

# ---------------------------------------------------------------------------
# Recording fakes — capture every query so the DDL/nuke Cypher is assertable
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, *, row: dict[str, Any] | None = None, rows: list[dict[str, Any]] | None = None) -> None:
        self._row = row
        self._rows = rows or []

    async def single(self) -> dict[str, Any] | None:
        return self._row

    async def data(self) -> list[dict[str, Any]]:
        return self._rows


class _Session:
    def __init__(self, driver: _Driver) -> None:
        self._driver = driver

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def run(self, query: str, **params: Any) -> _Result:
        self._driver.calls.append((query, params))
        if query == _EXISTING_VECTOR_DIMS:
            return _Result(rows=list(self._driver.existing_dim_rows))
        if query == _CLAIM_REBUILD_LOCK:
            return _Result(row={"claimed": self._driver.claim_result})
        return _Result(row=None)


class _Driver:
    def __init__(
        self,
        *,
        existing_dim_rows: list[dict[str, Any]] | None = None,
        claim_result: bool = True,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.existing_dim_rows = existing_dim_rows or []
        self.claim_result = claim_result

    def session(self, *, database: str = "neo4j") -> _Session:  # noqa: ARG002
        return _Session(self)


class _Embedder:
    """Records probe calls; returns fixed-length vectors so inference reads len()."""

    def __init__(self, dim: int = 768) -> None:
        self._dim = dim
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.0] * self._dim for _ in texts]


# ---------------------------------------------------------------------------
# Part B — schema_statements renders the dimension
# ---------------------------------------------------------------------------


def test_schema_statements_render_the_requested_dimension() -> None:
    for dim in (768, 384, 1024):
        statements = schema_statements(dim)
        vec = [s for s in statements if "VECTOR INDEX" in s]
        assert len(vec) == 2
        for ddl in vec:
            assert f"`vector.dimensions`: {dim}" in ddl
            # The OPTIONS map is well-formed (balanced braces around the config).
            assert ddl.count("{") == ddl.count("}") == 2


# ---------------------------------------------------------------------------
# Part B — resolve_embedding_dimension: config wins; else inferred
# ---------------------------------------------------------------------------


async def test_resolve_dimension_returns_configured_without_embedding() -> None:
    embedder = _Embedder(dim=768)
    dim = await resolve_embedding_dimension(embedder, configured=384)  # type: ignore[arg-type]
    assert dim == 384
    assert embedder.calls == []  # config short-circuits — no probe embed


async def test_resolve_dimension_infers_from_sample_vectors_without_probe() -> None:
    embedder = _Embedder(dim=768)
    dim = await resolve_embedding_dimension(
        embedder,  # type: ignore[arg-type]
        configured=None,
        sample_vectors=[[0.1, 0.2, 0.3, 0.4]],
    )
    assert dim == 4
    assert embedder.calls == []  # a present sample avoids the probe


async def test_resolve_dimension_probes_when_no_config_or_sample() -> None:
    embedder = _Embedder(dim=17)
    dim = await resolve_embedding_dimension(embedder, configured=None)  # type: ignore[arg-type]
    assert dim == 17
    assert len(embedder.calls) == 1  # exactly one probe embed


async def test_resolve_dimension_cross_checks_config_against_sample_length() -> None:
    # S1: a configured value that disagrees with the live model's real vector length is
    # the cryptic-empty-recall trap — raise naming BOTH numbers rather than build a
    # broken index.
    embedder = _Embedder()
    with pytest.raises(CorpusLoadError) as exc:
        await resolve_embedding_dimension(
            embedder,  # type: ignore[arg-type]
            configured=768,
            sample_vectors=[[0.0] * 384],
        )
    assert "768" in str(exc.value)
    assert "384" in str(exc.value)


async def test_resolve_dimension_config_matches_sample_passes() -> None:
    embedder = _Embedder()
    dim = await resolve_embedding_dimension(
        embedder,  # type: ignore[arg-type]
        configured=384,
        sample_vectors=[[0.0] * 384],
    )
    assert dim == 384


# ---------------------------------------------------------------------------
# Part B — check_dimension_parity
# ---------------------------------------------------------------------------


def test_check_dimension_parity_passes_on_empty_or_matching() -> None:
    check_dimension_parity(set(), 768)  # fresh graph — no index yet
    check_dimension_parity({768}, 768)  # already at the target dim


def test_check_dimension_parity_raises_on_a_differing_dim() -> None:
    with pytest.raises(DimensionMismatchError) as exc:
        check_dimension_parity({384}, 768)
    # The error names BOTH dims and points at the rebuild flag.
    assert "384" in str(exc.value)
    assert "768" in str(exc.value)
    assert "NEO4J_REBUILD_FROM_MCP" in str(exc.value)


async def test_apply_schema_raises_on_a_preexisting_mismatched_index() -> None:
    # SHOW VECTOR INDEXES reports the two indexes already at 384; targeting 768 must
    # raise BEFORE any CREATE (a `CREATE ... IF NOT EXISTS` would keep the old dim).
    driver = _Driver(
        existing_dim_rows=[
            {"name": "blueprint_intent_vec", "dimensions": 384},
            {"name": "knowledge_text_vec", "dimensions": 384},
        ]
    )
    with pytest.raises(DimensionMismatchError):
        await apply_schema(driver, dimension=768)  # type: ignore[arg-type]
    # No CREATE VECTOR INDEX was issued — the check fires first.
    assert not any("CREATE VECTOR INDEX" in q for q, _ in driver.calls)


async def test_apply_schema_creates_at_dim_on_a_fresh_graph() -> None:
    driver = _Driver(existing_dim_rows=[])  # no existing vector index
    await apply_schema(driver, dimension=512)  # type: ignore[arg-type]
    issued = [q for q, _ in driver.calls]
    assert any("`vector.dimensions`: 512" in q for q in issued)
    assert any("awaitIndexes" in q for q in issued)


# ---------------------------------------------------------------------------
# Part C — nuke_graph drops the full object set + DETACH DELETE
# ---------------------------------------------------------------------------


async def test_nuke_graph_drops_indexes_constraints_and_deletes_all_nodes() -> None:
    driver = _Driver()
    await nuke_graph(driver)  # type: ignore[arg-type]
    issued = [q for q, _ in driver.calls]

    # Every drop statement ran…
    for stmt in _NUKE_STATEMENTS:
        assert stmt in issued
    # …the 2 vector indexes + 6 constraints are all covered.
    assert sum("DROP INDEX" in q for q in issued) == 2
    assert sum("DROP CONSTRAINT" in q for q in issued) == 6
    # …and every node (incl. the CatalogMeta/CorpusMeta singletons) is deleted.
    assert _NUKE_DELETE_NODES in issued
    assert "DETACH DELETE" in _NUKE_DELETE_NODES


def test_nuke_spares_the_rebuild_lock_and_its_constraint() -> None:
    # BLOCKER fix: the nuke must NOT delete its own single-flight lock (else a replica
    # booting mid-rebuild sees no lock, re-claims, and re-nukes — a torn graph the B1
    # guards certify as healthy). The delete is label-guarded and no DROP touches the
    # lock's keying constraint.
    assert "WHERE NOT n:RebuildLock" in _NUKE_DELETE_NODES
    assert not any("rebuild_lock" in stmt.lower() for stmt in _NUKE_STATEMENTS)
    # The lock constraint is idempotent so it survives / self-heals the nuke.
    assert "IF NOT EXISTS" in _REBUILD_LOCK_CONSTRAINT


# ---------------------------------------------------------------------------
# Part C — claim_rebuild_lock single-flight
# ---------------------------------------------------------------------------


async def test_claim_rebuild_lock_returns_true_when_claimed() -> None:
    driver = _Driver(claim_result=True)
    got = await claim_rebuild_lock(driver, holder="me")  # type: ignore[arg-type]
    assert got is True
    issued = [q for q, _ in driver.calls]
    # The uniqueness constraint is ensured BEFORE the MERGE (closes double-MERGE).
    assert _REBUILD_LOCK_CONSTRAINT in issued
    assert issued.index(_REBUILD_LOCK_CONSTRAINT) < issued.index(_CLAIM_REBUILD_LOCK)
    # The claim binds this holder + a stale window.
    claim = next(p for q, p in driver.calls if q == _CLAIM_REBUILD_LOCK)
    assert claim["holder"] == "me"
    assert "stale_seconds" in claim


async def test_claim_rebuild_lock_returns_false_when_another_holds_it() -> None:
    driver = _Driver(claim_result=False)
    got = await claim_rebuild_lock(driver, holder="me")  # type: ignore[arg-type]
    assert got is False
