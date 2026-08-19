"""D94 Part 3 — SOFT seed/load-time catalog-skew WARNING in
`corpus_loader.load_corpus` (design docs/decisions/none-provenance-stranding-design.md §4).

A blueprint whose `uses` references a `db.table` absent from the supplied
`CatalogHandle` is the seed-time signature of the catalog/extractor skew that
strands an `ok`+`None` result at runtime. `load_corpus` (and its helper
`_warn_on_catalog_skew`) log a LOUD WARNING naming the blueprint id + missing
`db.table`(s), but load ALWAYS proceeds — a blueprint may legitimately reference
tables absent from a partial/dev catalog snapshot, so this never raises
`CorpusLoadError` (record correction: prod safety is MCP-fails-closed + catalogs
in agreement, NOT this dev-time check).

Mirrors `tests/runtime/retrieval/test_corpus_loader.py` /
`test_corpus_loader_qa2.py` for the seed value objects; adds infra-free fake
neo4j driver + embedding doubles so the full `load_corpus` path can be exercised
without a live neo4j.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.corpus_loader import (
    BlueprintSeed,
    CorpusLoadError,
    _warn_on_catalog_skew,
    load_corpus,
)

_LOGGER_NAME = "data_agent.runtime.retrieval.corpus_loader"


# ---------------------------------------------------------------------------
# Infra-free neo4j driver + embedding doubles (enough for load_corpus)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self._row = row

    async def single(self) -> dict[str, Any] | None:
        return self._row

    async def data(self) -> list[dict[str, Any]]:
        # apply_schema's dimension-parity introspection reads `.data()`; no existing
        # vector index in this fake → empty set of dims (parity passes at target dim).
        return []


class _FakeRunner:
    """A session OR managed-transaction stand-in — anything with `.run`."""

    async def run(self, query: str, **_kwargs: Any) -> _FakeResult:
        # `fetch_existing_models` expects a `{"models": [...]}` row; everything
        # else (DDL, upserts, awaitIndexes) just needs an awaitable no-op result.
        if "embedding_model" in query or "models" in query.lower():
            return _FakeResult({"models": []})  # fresh index -> no parity conflict
        return _FakeResult(None)


class _FakeSession(_FakeRunner):
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def execute_write(self, fn: Any) -> Any:
        return await fn(_FakeRunner())


class _FakeDriver:
    def session(self, *, database: str = "neo4j") -> _FakeSession:  # noqa: ARG002
        return _FakeSession()


class _FakeEmbeddingClient:
    def __init__(self) -> None:
        self.embedded: list[str] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return [[0.0] * 768 for _ in texts]


def _bp(bp_id: str, uses: list[str]) -> BlueprintSeed:
    # A single-node seed with NO sql_template and NO composes: passes
    # `validate_blueprint_dag` vacuously, so the skew check is what's under test.
    return BlueprintSeed(id=bp_id, intent="an intent", slots_summary="", uses=uses)


async def _load(
    blueprints: list[BlueprintSeed], catalog: CatalogHandle | None
) -> tuple[Any, _FakeEmbeddingClient]:
    embed = _FakeEmbeddingClient()
    report = await load_corpus(
        _FakeDriver(),  # type: ignore[arg-type]
        embed,  # type: ignore[arg-type]
        blueprints,
        [],
        model_id="all-mpnet-base-v2",
        catalog=catalog,
    )
    return report, embed


# ---------------------------------------------------------------------------
# _warn_on_catalog_skew — the pure helper (names id + missing db.table)
# ---------------------------------------------------------------------------


def test_warn_on_catalog_skew_names_blueprint_and_missing_table(
    caplog: pytest.LogCaptureFixture,
) -> None:
    catalog = CatalogHandle({"dbpcm_warehouse.employee": {"EmployeeCode": "String"}})
    bp = _bp("bp-skewed", ["dbpcm_warehouse.payroll.Amount"])  # payroll uncatalogued

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _warn_on_catalog_skew([bp], catalog)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "bp-skewed" in msg
    assert "dbpcm_warehouse.payroll" in msg


def test_warn_on_catalog_skew_silent_when_all_uses_catalogued(
    caplog: pytest.LogCaptureFixture,
) -> None:
    catalog = CatalogHandle(
        {
            "dbpcm_warehouse.employee": {"EmployeeCode": "String", "Department": "String"},
            "dbpcm_warehouse.payroll": {"Amount": "Decimal(18,2)"},
        }
    )
    bp = _bp(
        "bp-clean",
        ["dbpcm_warehouse.employee.Department", "dbpcm_warehouse.payroll.Amount"],
    )
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _warn_on_catalog_skew([bp], catalog)
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_warn_on_catalog_skew_deduplicates_missing_table_per_blueprint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    catalog = CatalogHandle({"dbpcm_warehouse.employee": {"EmployeeCode": "String"}})
    # Two columns of the SAME uncatalogued table -> one warning naming it once.
    bp = _bp(
        "bp-two-cols",
        ["dbpcm_warehouse.payroll.Amount", "dbpcm_warehouse.payroll.PeriodEnd"],
    )
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _warn_on_catalog_skew([bp], catalog)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage().count("dbpcm_warehouse.payroll") == 1


# ---------------------------------------------------------------------------
# Row 9 — load_corpus with a skewed catalog WARNS and PROCEEDS (no error)
# ---------------------------------------------------------------------------


async def test_load_corpus_with_skewed_catalog_warns_but_proceeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    catalog = CatalogHandle({"dbpcm_warehouse.employee": {"EmployeeCode": "String"}})
    bp = _bp("bp-skewed", ["dbpcm_warehouse.payroll.Amount"])

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        report, embed = await _load([bp], catalog)

    # Load PROCEEDED — no CorpusLoadError, the blueprint was embedded + written.
    assert report.blueprints_written == 1
    assert embed.embedded == ["an intent"]
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and r.name == _LOGGER_NAME
    ]
    assert any("bp-skewed" in r.getMessage() and "dbpcm_warehouse.payroll" in r.getMessage() for r in warnings)


# ---------------------------------------------------------------------------
# Row 10 — no CatalogHandle supplied: unchanged, no skew warning
# ---------------------------------------------------------------------------


async def test_load_corpus_without_catalog_emits_no_skew_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bp = _bp("bp-any", ["dbpcm_warehouse.payroll.Amount"])
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        report, _ = await _load([bp], None)  # no catalog
    assert report.blueprints_written == 1
    # No skew warning fired (the check was skipped entirely).
    assert not any(
        "absent from the supplied catalog" in r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER_NAME
    )


async def test_load_corpus_with_catalog_covering_all_uses_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    catalog = CatalogHandle(
        {
            "dbpcm_warehouse.employee": {"Department": "String"},
            "dbpcm_warehouse.payroll": {"Amount": "Decimal(18,2)"},
        }
    )
    bp = _bp(
        "bp-covered",
        ["dbpcm_warehouse.employee.Department", "dbpcm_warehouse.payroll.Amount"],
    )
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        report, _ = await _load([bp], catalog)
    assert report.blueprints_written == 1
    assert not any(
        "absent from the supplied catalog" in r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER_NAME
    )


async def test_load_corpus_skew_is_soft_not_a_corpus_load_error() -> None:
    """Belt-and-suspenders: a skewed catalog must NOT raise CorpusLoadError —
    that exception stays reserved for genuine corpus-internal-consistency
    failures (the design's explicit SOFT decision)."""
    catalog = CatalogHandle({"dbpcm_warehouse.employee": {"EmployeeCode": "String"}})
    bp = _bp("bp-skewed", ["dbpcm_warehouse.ghost.Col"])
    try:
        report, _ = await _load([bp], catalog)
    except CorpusLoadError as exc:  # pragma: no cover - the bug this guards against
        raise AssertionError(f"skew must be SOFT, but load_corpus raised: {exc}") from exc
    assert report.blueprints_written == 1
