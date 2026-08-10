"""QA: the seeder's `structural_key` derivation under load, and the canon/fixture guard.

`test_corpus_loader_structural_key.py` pins the derivation on hand-built seeds. This file
covers the two things that only show up at the LOAD level:

  1. WHAT THE FAIL-SOFT ACTUALLY DOES AT LOAD SCALE — and it is NOT what the phrase
     suggests. `_seed_structural_key` is fail-soft, but `load_corpus` never reaches it
     with a bad template: `_validate_blueprint_dag` rejects unparseable SQL in the
     unconditional pre-write pass and aborts the WHOLE load (fail-CLOSED, pre-existing).
     Both policies are pinned here so they are not mistaken for one another. Also pinned:
     each blueprint gets its OWN key across a batch, which is the live risk introduced by
     hoisting the derivation into a third `zip(..., strict=True)` sequence.
  2. CANON/FIXTURE DRIFT — `test_every_canon_fixture_produces_a_structural_key` asserts
     10/10 coverage against `tests/fixtures/corpus/blueprints.yaml`, the HERMETIC MIRROR
     of the real MCP canon. If the mirror drifts from
     `clickhouse-api/app/corpus/data/blueprints/`, that coverage claim silently stops
     describing the real corpus. A drift guard is what keeps the claim meaningful.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from data_agent.runtime.blueprint.structural_key import structural_key_from_templates
from data_agent.runtime.retrieval.corpus_loader import (
    _UPSERT_BLUEPRINT,
    BlueprintSeed,
    CorpusLoadError,
    KnowledgeSeed,
    _dag_properties,
    _validate_blueprint_dag,
    corpus_seeds_from_export,
    load_corpus,
    resolve_blueprint_references,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "corpus"
_REAL_CANON_DIR = Path("/Users/kalpeshmulye/Development/clickhouse-api/app/corpus/data/blueprints")

# The fields that feed the structural key. Drift in ANY of these changes the digest and
# invalidates the hermetic canon-coverage claim; drift elsewhere (intent, slots, uses) is
# out of scope for this guard.
_HASHED_FIELDS = ("result_grain", "sql_template", "composes")


# --- 1. fail-soft at batch scale ----------------------------------------------


class _Result:
    def __init__(
        self, row: dict[str, Any] | None, rows: list[dict[str, Any]] | None = None
    ) -> None:
        self._row = row
        self._rows = rows if rows is not None else ([] if row is None else [row])

    async def single(self) -> dict[str, Any] | None:
        return self._row

    async def data(self) -> list[dict[str, Any]]:
        return self._rows


class _RecordingTx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, query: str, **params: Any) -> _Result:
        self.calls.append((query, params))
        if "embedding_model" in query or "models" in query.lower():
            return _Result({"models": []})
        if "RETURN collect(column_key)" in query or "missing" in query:
            return _Result({"missing": []})
        return _Result(None)


class _RecordingSession:
    def __init__(self, driver: _RecordingDriver) -> None:
        self._driver = driver

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def run(self, query: str, **params: Any) -> _Result:
        return _Result(None)

    async def execute_write(self, fn: Any) -> Any:
        return await fn(self._driver.tx)


class _RecordingDriver:
    def __init__(self) -> None:
        self.tx = _RecordingTx()

    def session(self, *, database: str = "neo4j") -> _RecordingSession:  # noqa: ARG002
        return _RecordingSession(self)


class _Embedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 768 for _ in texts]


def _seed(
    bp_id: str,
    template: str,
    grain: list[str] | None = None,
    uses: list[str] | None = None,
) -> BlueprintSeed:
    return BlueprintSeed(
        id=bp_id,
        intent=f"intent for {bp_id}",
        slots_summary="",
        uses=uses if uses is not None else ["dbpcm_warehouse.employee.department_name"],
        result_grain=grain if grain is not None else ["Department"],
        sql_template=template,
    )


_GOOD = "SELECT department_name AS department FROM dbpcm_warehouse.employee"
_BAD = "SELECT FROM WHERE (("


def _distinct_seed(i: int) -> BlueprintSeed:
    """A valid, self-consistent blueprint whose template, `uses` footprint and grain are
    all unique to `i` — so a cross-assigned key is detectable."""
    column = f"col_{i:02d}"
    return _seed(
        f"bp-{i:02d}",
        f"SELECT {column} AS department FROM dbpcm_warehouse.employee",
        [f"Grain{i:02d}"],
        [f"dbpcm_warehouse.employee.{column}"],
    )


async def test_an_unparseable_template_aborts_the_entire_load_fail_closed() -> None:
    """CORRECTS A LIKELY MISREADING OF THE FAIL-SOFT CLAIM.

    `structural_key_from_templates` is fail-soft, and `_dag_properties` maps its miss to
    an absent property — but that is NOT what happens when an unparseable blueprint
    reaches `load_corpus`. `_validate_blueprint_dag` runs UNCONDITIONALLY in the pre-write
    pass (corpus_loader.py:2021-2023) and raises `CorpusLoadError` for any template that
    does not parse, taking down the WHOLE load.

    So "one bad blueprint does not abort a load of many" is FALSE at the load level. This
    is PRE-EXISTING, fail-CLOSED-by-design behavior (an authoring mistake must never
    ship) and is not a regression from this slice — but it means the structural key's
    fail-soft branch is defensive depth, not an active load-path behavior. Pinned so the
    two layers' opposite policies are not confused for each other.
    """
    seeds = [_distinct_seed(i) for i in range(10)]
    seeds.insert(5, _seed("bp-broken", _BAD))

    driver = _RecordingDriver()
    with pytest.raises(CorpusLoadError, match="bp-broken"):
        await load_corpus(
            driver,  # type: ignore[arg-type]
            _Embedder(),  # type: ignore[arg-type]
            seeds,
            [KnowledgeSeed(id="kn-1", text="t", doc_id="d")],
            model_id="all-mpnet-base-v2",
        )
    # Fail-closed: NOTHING was written, not even the nine valid blueprints.
    assert not [p for q, p in driver.tx.calls if q == _UPSERT_BLUEPRINT]


@pytest.mark.parametrize(
    "template",
    [
        "SELECT FROM WHERE ((",
        "not sql at all",
        "{{{",
        "SELECT `unclosed",
        "SELECT a FROM t WHERE x = 'unterminated",
    ],
)
def test_every_template_the_key_recipe_rejects_is_also_rejected_by_loader_validation(
    template,
) -> None:
    """Why the fail-soft branch never fires on the load path: the loader's own
    `_validate_blueprint_dag` gate is STRICTLY EARLIER and at least as strict, so a
    template that would yield no structural key never survives to the derivation.

    If a future change ever makes the key recipe stricter than loader validation (e.g.
    adding a normalization pass that raises on something `parse_template` accepts), THIS
    test starts failing and the fail-soft branch becomes live — which is the moment to
    care about it.
    """
    assert structural_key_from_templates(["Department"], template) == ""
    bad = _seed("bp-x", template)
    with pytest.raises(CorpusLoadError):
        _validate_blueprint_dag(bad)


def test_a_dagless_or_broken_seed_binds_none_not_empty_string() -> None:
    """An empty-string key stored on EVERY keyless blueprint would make a naive
    `MATCH (b {structural_key: $k})` match them all as false prior art. Asserted at
    `_dag_properties` (the only layer that can produce a keyless seed, since
    `load_corpus` rejects unparseable templates outright)."""
    broken = _seed("bp-broken", _BAD)
    legacy = BlueprintSeed(id="bp-legacy", intent="i", slots_summary="", uses=["a.b.c"])
    for seed in (broken, legacy):
        value = _dag_properties(seed)["structural_key"]
        assert value is None
        assert value != ""


async def test_every_upsert_binds_the_structural_key_parameter() -> None:
    """The Cypher `SET b.structural_key = $structural_key` needs the parameter bound on
    EVERY upsert — an unbound parameter is a neo4j ParameterMissing error at runtime,
    which no Layer-1 fake driver would otherwise catch."""
    assert "b.structural_key = $structural_key" in _UPSERT_BLUEPRINT
    driver = _RecordingDriver()
    await load_corpus(
        driver,  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        [_distinct_seed(1), _distinct_seed(2)],
        [],
        model_id="all-mpnet-base-v2",
    )
    bp_calls = [p for q, p in driver.tx.calls if q == _UPSERT_BLUEPRINT]
    assert bp_calls
    assert all("structural_key" in p for p in bp_calls)


async def test_each_blueprint_receives_its_own_key_across_a_large_batch() -> None:
    """The new hoisted derivation added a THIRD sequence to the write loop's
    `zip(blueprints, bp_vectors, dag_props, strict=True)`. A mis-ordered or mis-sized
    `dag_props` would silently cross-assign keys — every blueprint written with its
    neighbour's identity, which is the worst possible failure for a prior-art index
    (confident, wrong matches) and is invisible to any single-seed test.

    Twenty distinguishable blueprints; each upsert's key must equal the key derived from
    THAT blueprint alone.
    """
    seeds = [_distinct_seed(i) for i in range(20)]
    expected = {bp.id: _dag_properties(bp)["structural_key"] for bp in seeds}
    assert len(set(expected.values())) == 20  # genuinely distinguishable

    driver = _RecordingDriver()
    await load_corpus(
        driver,  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        seeds,
        [],
        model_id="all-mpnet-base-v2",
    )
    written = {p["id"]: p["structural_key"] for q, p in driver.tx.calls if q == _UPSERT_BLUEPRINT}
    assert written == expected


async def test_the_key_is_derived_once_before_the_write_txn_opens() -> None:
    """The derivation sqlglot-parses every template; doing that INSIDE the write txn
    would hold neo4j locks for the whole parse. The seeds are hoisted out
    (`dag_props = [...]` before `session()`), and the derived value must still arrive
    intact on the upsert."""
    seed = _seed("bp-one", _GOOD)
    expected = _dag_properties(seed)["structural_key"]
    assert expected

    driver = _RecordingDriver()
    await load_corpus(
        driver,  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        [seed],
        [],
        model_id="all-mpnet-base-v2",
    )
    params = next(p for q, p in driver.tx.calls if q == _UPSERT_BLUEPRINT)
    assert params["structural_key"] == expected


# --- 2. the canon/fixture drift guard ------------------------------------------


def _load_yaml(path: Path) -> Any:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(path.read_text())


def _real_canon_docs() -> dict[str, Any]:
    if not _REAL_CANON_DIR.is_dir():
        pytest.skip(f"real MCP canon not checked out at {_REAL_CANON_DIR}")
    docs = {}
    for path in sorted(_REAL_CANON_DIR.glob("bp-*.yaml")):
        doc = _load_yaml(path)
        docs[doc["id"]] = doc
    return docs


def _fixture_docs() -> dict[str, Any]:
    return {d["id"]: d for d in _load_yaml(_FIXTURE_DIR / "blueprints.yaml")}


def test_the_hermetic_fixture_covers_the_same_blueprint_ids_as_the_real_canon() -> None:
    """`test_every_canon_fixture_produces_a_structural_key` asserts `len(blueprints) == 10`
    against the MIRROR. That number is only meaningful if the mirror still has the same
    blueprints as the real MCP canon — otherwise a newly-authored canon blueprint whose
    template the recipe cannot normalize would ship unnoticed."""
    canon, fixture = _real_canon_docs(), _fixture_docs()
    assert set(canon) - set(fixture) == set(), "canon has blueprints the fixture mirror lacks"
    assert set(fixture) - set(canon) == set(), "fixture has blueprints the real canon lacks"


def test_the_hermetic_fixture_has_not_drifted_from_the_real_canon() -> None:
    """The fields that feed the structural key must be byte-identical between the real
    MCP canon YAMLs and the hermetic mirror the test suite reads.

    QA's `strict=True` xfail here recorded a real, pre-existing drift: the fixture's
    `bp-overtime-by-department` predated the canon's `type_code` slot + predicate and its
    `overtime_pay` -> `earnings` rename, and `bp-active-headcount-by-department` /
    `bp-average-salary-by-department` predated their `department` slot becoming OPTIONAL
    with an `optional_pattern`. The fixture has been re-synced from
    `clickhouse-api/app/corpus/data/blueprints/` and the xfail cleared."""
    canon, fixture = _real_canon_docs(), _fixture_docs()
    drift = [
        (bid, field, canon[bid].get(field), fixture[bid].get(field))
        for bid in sorted(set(canon) & set(fixture))
        for field in _HASHED_FIELDS
        if canon[bid].get(field) != fixture[bid].get(field)
    ]
    assert not drift, "\n".join(
        f"{bid}.{field}:\n  canon  ={cv!r}\n  fixture={fv!r}" for bid, field, cv, fv in drift
    )


def test_the_hermetic_fixture_matches_the_real_canon_on_every_field() -> None:
    """Wider than `_HASHED_FIELDS`: the drift QA found also touched `slots`,
    `slots_summary`, `intent`, `resolves` and `uses` — fields that do NOT feed the digest
    but DO feed the loader's write-time validation, so a fixture that diverges there
    exercises validation rules the shipping corpus never hits."""
    canon, fixture = _real_canon_docs(), _fixture_docs()
    fields = sorted({k for d in canon.values() for k in d} | {k for d in fixture.values() for k in d})
    drift = [
        (bid, field)
        for bid in sorted(set(canon) & set(fixture))
        for field in fields
        if canon[bid].get(field) != fixture[bid].get(field)
    ]
    assert not drift, f"fixture/canon drift: {drift}"


def test_every_real_canon_blueprint_mints_a_distinct_structural_key() -> None:
    """The builder's 10/10 claim, verified against the REAL MCP canon files rather than
    the mirror. This is the assertion that actually protects production: a canon
    blueprint with no structural key is invisible to cross-tier prior-art matching, and
    two canon blueprints sharing one key would be false prior art for each other.

    HISTORY: 10 became 11 when plan §2b extracted
    `bp-employee-check-detail-for-period` from
    `bp-compare-employee-check-detail-two-periods`. The seeds are built through the
    PRODUCTION projection + resolver (`corpus_seeds_from_export` →
    `resolve_blueprint_references`) rather than a local `node_pairs` reader, and that
    swap is the point of the edit, not incidental: a `composes` node may now name
    another blueprint instead of carrying SQL, so a hand-rolled reader would skip those
    nodes and compute a key for a DIFFERENT query than the loader stores — the same
    class of silent mirror drift this file exists to catch. Measured: the composite's
    key is byte-identical before and after the extraction."""
    canon = _real_canon_docs()
    assert len(canon) == 11

    seeds, _ = corpus_seeds_from_export({"blueprints": canon})
    assert len(seeds) == len(canon), "the export projection dropped a canon blueprint"
    keys = {bp.id: _dag_properties(bp)["structural_key"] for bp in resolve_blueprint_references(seeds)}

    keyless = [bid for bid, key in keys.items() if not key]
    assert not keyless, f"real canon blueprints with NO structural key: {keyless}"
    assert all(key.startswith("sha256:") for key in keys.values())
    assert len(set(keys.values())) == len(keys), "two real canon blueprints collide"
