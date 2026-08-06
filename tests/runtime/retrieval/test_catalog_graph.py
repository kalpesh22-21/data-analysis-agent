"""Layer-1 tests for the catalog-graph hydration (independent, enriched,
self-healing `:Table`/`:Column` nodes) — the pure prop mappers + row builder, the
key-alignment contract vs `_use_edges`, GC/drift collection, and the
`load_catalog_graph` B1 sha-guard skip + write paths over a recording fake driver.

No live neo4j: the two prop mappers and `_catalog_graph_rows` are pure, and
`load_catalog_graph` is driven through a fake session/txn that records every call.
"""

from __future__ import annotations

import json
from typing import Any

from data_agent.runtime.retrieval.corpus_loader import (
    _CATALOG_GRAPH_CONSTRAINTS,
    _GC_COLUMNS,
    _GC_TABLES,
    _READ_CATALOG_META,
    _UPSERT_CATALOG_META,
    _UPSERT_COLUMNS,
    _UPSERT_TABLES,
    _catalog_graph_rows,
    _column_node_props,
    _effective_catalog_sha,
    _format_edge_drift,
    _referenced_gc_columns,
    _table_node_props,
    _use_edges,
    apply_catalog_graph_schema,
    load_catalog_graph,
    schema_statements,
)
from tests._catalog_fixture import fixture_catalog, load_catalog_export

_E = "dbpcm_warehouse.employee"


# --------------------------------------------------------------------------
# _table_node_props — scalar/array natives + JSON-encoded nested structures
# --------------------------------------------------------------------------


def test_table_props_json_encodes_nested_structures() -> None:
    entry = {
        "database": "dbpcm_warehouse",
        "table": "accrual_events",
        "description": "Accrual log.",
        "grain": ["EmployeeCode", "RequestDate"],
        "grain_verifiable": False,
        "temporal": {"dimensions": [{"name": "d", "date_col": "RequestDate"}]},
        "primary_key": ["EventId"],
        "join_keys": [{"column": "EmployeeCode", "joins": "employee.EmployeeCode"}],
        "measures": {"hrs": {"column": "Hours", "agg": "sum"}},
    }
    props = _table_node_props("dbpcm_warehouse.accrual_events", entry)

    assert props["key"] == "dbpcm_warehouse.accrual_events"
    assert props["database"] == "dbpcm_warehouse"
    assert props["table"] == "accrual_events"
    assert props["description"] == "Accrual log."
    # grain is a NATIVE array-of-primitive (not JSON).
    assert props["grain"] == ["EmployeeCode", "RequestDate"]
    assert props["grain_verifiable"] is False
    # Nested maps / list-of-maps are JSON-encoded to *_json strings.
    assert json.loads(props["temporal_json"]) == entry["temporal"]
    assert json.loads(props["primary_key_json"]) == ["EventId"]
    assert json.loads(props["join_keys_json"]) == entry["join_keys"]
    assert json.loads(props["measures_json"]) == entry["measures"]


def test_table_props_grain_verifiable_defaults_true_when_absent() -> None:
    # Parity with SemanticCatalogHandle._table_grain: absent flag ⇒ True.
    props = _table_node_props(_E, {"database": "dbpcm_warehouse", "table": "employee"})
    assert props["grain_verifiable"] is True
    # A non-bool value also coerces to True (defensive parity).
    props2 = _table_node_props(_E, {"grain_verifiable": "yes"})
    assert props2["grain_verifiable"] is True


def test_table_props_empty_nested_becomes_none_and_db_table_fallback() -> None:
    # Empty/absent nested structures store None (no phantom {}/[]); database/table
    # fall back to the split of db_table when the entry omits them.
    props = _table_node_props("dbpcm_warehouse.employee", {})
    assert props["temporal_json"] is None
    assert props["primary_key_json"] is None
    assert props["join_keys_json"] is None
    assert props["measures_json"] is None
    assert props["grain"] == []
    assert props["database"] == "dbpcm_warehouse"
    assert props["table"] == "employee"


# --------------------------------------------------------------------------
# _column_node_props — casing preserved, values JSON-encoded, sensitive bool
# --------------------------------------------------------------------------


def test_column_props_scalars_arrays_and_values_json() -> None:
    col = {
        "type": "Nullable(String)",
        "description": "Current status code.",
        "sensitive": True,
        "description_col": "EmployeeStatusDescription",
        "synonyms": ["state", "status"],
        "unit": "hours",
        "values": {"A": "active", "T": "terminated"},
        "client_defined": True,  # unknown field — NOT read (whitelist)
        "observed_values": ["A", "T"],  # unknown field — NOT read
    }
    props = _column_node_props(_E, "EmployeeStatus", col)

    assert props["key"] == "dbpcm_warehouse.employee.EmployeeStatus"
    # `name` mirrors the FULL key (node identity, captions in Bloom); the bare short
    # name (casing preserved, D70) is retained separately as `short_name`.
    assert props["name"] == "dbpcm_warehouse.employee.EmployeeStatus"
    assert props["short_name"] == "EmployeeStatus"
    assert props["type"] == "Nullable(String)"
    assert props["description"] == "Current status code."
    assert props["sensitive"] is True
    assert props["description_col"] == "EmployeeStatusDescription"
    assert props["synonyms"] == ["state", "status"]
    assert props["unit"] == "hours"
    assert json.loads(props["values_json"]) == {"A": "active", "T": "terminated"}
    # Whitelist: the unknown fields never leak into the node props.
    assert "client_defined" not in props
    assert "observed_values" not in props


def test_column_props_defaults_when_sparse() -> None:
    props = _column_node_props(_E, "MixedCaseCol", {})
    assert props["name"] == "dbpcm_warehouse.employee.MixedCaseCol"  # full key identity
    assert props["short_name"] == "MixedCaseCol"  # exact casing
    assert props["sensitive"] is False  # default bool
    assert props["synonyms"] == []
    assert props["values_json"] is None
    assert props["type"] is None
    assert props["unit"] is None


# --------------------------------------------------------------------------
# Key alignment (load-bearing): column node key == _use_edges column_key
# --------------------------------------------------------------------------


def test_column_key_matches_use_edges_column_key() -> None:
    key = "dbpcm_warehouse.payroll.Amount"
    db_table, _, name = key.rpartition(".")
    props = _column_node_props(db_table, name, {"type": "Nullable(Decimal(18, 6))"})
    assert props["key"] == _use_edges([key])[0]["column_key"]


# --------------------------------------------------------------------------
# _catalog_graph_rows over the frozen fixture — key sets + table_key grouping
# --------------------------------------------------------------------------


def test_catalog_graph_rows_over_fixture() -> None:
    catalog = fixture_catalog()
    table_rows, column_rows, table_keys, column_keys = _catalog_graph_rows(catalog)

    # One table row per catalogued table; keys are the db.table strings.
    assert table_keys == set(catalog.keys())
    assert len(table_rows) == len(catalog)
    assert {row["key"] for row in table_rows} == table_keys
    assert _E in table_keys

    # Every column key is "<db.table>.<col>" and groups under its owning table_key.
    for row in column_rows:
        assert row["key"] == f"{row['table_key']}.{row['props']['short_name']}"
        assert row["table_key"] in table_keys
    assert {row["key"] for row in column_rows} == column_keys

    # A known column round-trips into a column row with its enriched type.
    emp_status_key = f"{_E}.employee_status"
    assert emp_status_key in column_keys
    emp_status = next(r for r in column_rows if r["key"] == emp_status_key)
    assert emp_status["props"]["type"] == "Nullable(String)"
    # The Wave-1 catalog authors employee_status `values` as the enum-code list.
    assert "A" in json.loads(emp_status["props"]["values_json"])


def test_catalog_graph_rows_tolerates_non_dict_entry() -> None:
    catalog = {"dbpcm_warehouse.employee": {"columns": {"C": {"type": "String"}}}, "bad": 42}
    table_rows, column_rows, table_keys, column_keys = _catalog_graph_rows(catalog)
    assert table_keys == {"dbpcm_warehouse.employee"}  # the non-dict entry is skipped
    assert column_keys == {"dbpcm_warehouse.employee.C"}


# --------------------------------------------------------------------------
# GC / drift collection — pure helpers
# --------------------------------------------------------------------------


def test_referenced_gc_columns_selects_only_still_referenced() -> None:
    gc_rows = [
        {"key": "db.t.a", "refs": ["bp-1", "bp-2"]},
        {"key": "db.t.b", "refs": []},  # unreferenced — not drift
        {"key": "db.t.c", "refs": ["bp-3"]},
    ]
    assert _referenced_gc_columns(gc_rows) == ("db.t.a", "db.t.c")


def test_format_edge_drift_is_stable_sorted() -> None:
    rendered = _format_edge_drift({"bp-z": ["db.t.b", "db.t.a"], "bp-a": ["db.t.c"]})
    assert rendered == "bp-a: ['db.t.c']; bp-z: ['db.t.a', 'db.t.b']"


# --------------------------------------------------------------------------
# load_catalog_graph — fake driver harness (records every call)
# --------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, *, single: Any = None, data: list[dict[str, Any]] | None = None) -> None:
        self._single = single
        self._data = data or []

    async def single(self) -> Any:
        return self._single

    async def data(self) -> list[dict[str, Any]]:
        return self._data


class _FakeTx:
    """Records `tx.run` calls and returns canned results per query constant."""

    def __init__(self, *, gc_cols: list[dict[str, Any]], gc_tables: int) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._gc_cols = gc_cols
        self._gc_tables = gc_tables

    async def run(self, query: str, **params: Any) -> _FakeResult:
        self.calls.append((query, params))
        if query == _GC_COLUMNS:
            return _FakeResult(data=self._gc_cols)
        if query == _GC_TABLES:
            return _FakeResult(single={"deleted": self._gc_tables})
        return _FakeResult()


class _FakeSession:
    def __init__(self, driver: _FakeDriver) -> None:
        self._driver = driver

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def run(self, query: str, **params: Any) -> _FakeResult:
        self._driver.session_calls.append((query, params))
        if query == _READ_CATALOG_META:
            single = (
                {"catalog_sha": self._driver.meta_sha}
                if self._driver.meta_sha is not None
                else None
            )
            return _FakeResult(single=single)
        return _FakeResult()

    async def execute_write(self, fn: Any) -> Any:
        self._driver.execute_write_count += 1
        tx = _FakeTx(gc_cols=self._driver.gc_cols, gc_tables=self._driver.gc_tables)
        self._driver.tx = tx
        return await fn(tx)


class _FakeDriver:
    def __init__(
        self,
        *,
        meta_sha: str | None,
        gc_cols: list[dict[str, Any]] | None = None,
        gc_tables: int = 0,
    ) -> None:
        self.meta_sha = meta_sha
        self.gc_cols = gc_cols or []
        self.gc_tables = gc_tables
        self.session_calls: list[tuple[str, dict[str, Any]]] = []
        self.execute_write_count = 0
        self.tx: _FakeTx | None = None

    def session(self, *, database: str = "neo4j") -> _FakeSession:
        return _FakeSession(self)


async def test_load_catalog_graph_sha_guard_skips_without_writing() -> None:
    export = {"catalog_sha": "abc123", "catalog": {"db.t": {"columns": {"c": {}}}}}
    driver = _FakeDriver(meta_sha="abc123")  # meta already matches ⇒ B1 no-op

    report = await load_catalog_graph(driver, export, ensure_schema=False)  # type: ignore[arg-type]

    assert report.skipped is True
    assert report.catalog_sha == "abc123"
    assert report.tables_upserted == 0
    assert report.columns_upserted == 0
    # No write path was taken.
    assert driver.execute_write_count == 0
    # Only the meta read ran; no upsert/GC ever issued.
    assert [q for q, _ in driver.session_calls] == [_READ_CATALOG_META]


async def test_load_catalog_graph_writes_when_meta_absent() -> None:
    export = load_catalog_export()  # the real fixture (non-empty catalog)
    driver = _FakeDriver(
        meta_sha=None,  # absent meta ⇒ proceed
        gc_cols=[
            {"key": "db.t.dropped", "refs": ["bp-1"]},  # GC'd + still referenced ⇒ drift
            {"key": "db.t.orphan", "refs": []},  # GC'd, no refs
        ],
        gc_tables=2,
    )

    report = await load_catalog_graph(driver, export, ensure_schema=False)  # type: ignore[arg-type]

    assert report.skipped is False
    assert report.catalog_sha == export["catalog_sha"]
    assert report.tables_upserted == len(export["catalog"])
    assert report.columns_upserted > 0
    assert report.columns_gc == 2
    assert report.tables_gc == 2
    assert report.drift_referenced_columns == ("db.t.dropped",)

    # The single write txn issued upserts + both GCs + the meta upsert, in order.
    assert driver.tx is not None
    issued = [q for q, _ in driver.tx.calls]
    assert issued == [
        _UPSERT_TABLES,
        _UPSERT_COLUMNS,
        _GC_COLUMNS,
        _GC_TABLES,
        _UPSERT_CATALOG_META,
    ]
    # Every write carried the run's sha (idempotency stamp).
    for _query, params in driver.tx.calls:
        assert params.get("sha") == export["catalog_sha"]


async def test_load_catalog_graph_proceeds_when_meta_differs() -> None:
    export = {"catalog_sha": "new-sha", "catalog": {"db.t": {"columns": {"c": {}}}}}
    driver = _FakeDriver(meta_sha="old-sha")  # stale meta ⇒ hydrate

    report = await load_catalog_graph(driver, export, ensure_schema=False)  # type: ignore[arg-type]

    assert report.skipped is False
    assert driver.execute_write_count == 1


# --------------------------------------------------------------------------
# gc=False — online B1 self-heal is upsert-only (NEVER deletes)
# --------------------------------------------------------------------------


async def test_load_catalog_graph_gc_false_issues_no_gc_cypher() -> None:
    export = {"catalog_sha": "sha-x", "catalog": {"db.t": {"columns": {"c": {"type": "String"}}}}}
    # Even if the store WOULD return GC rows, gc=False must never issue the GC queries.
    driver = _FakeDriver(meta_sha=None, gc_cols=[{"key": "db.t.z", "refs": ["bp"]}], gc_tables=5)

    report = await load_catalog_graph(driver, export, ensure_schema=False, gc=False)  # type: ignore[arg-type]

    assert report.skipped is False
    assert report.tables_upserted == 1
    assert report.columns_upserted == 1
    # No deletion happened, and no drift signal (GC is the only source of it).
    assert report.columns_gc == 0
    assert report.tables_gc == 0
    assert report.drift_referenced_columns == ()

    assert driver.tx is not None
    issued = [q for q, _ in driver.tx.calls]
    # Upserts + meta ONLY — neither GC query was issued.
    assert issued == [_UPSERT_TABLES, _UPSERT_COLUMNS, _UPSERT_CATALOG_META]
    assert _GC_COLUMNS not in issued
    assert _GC_TABLES not in issued


# --------------------------------------------------------------------------
# Robust catalog_sha — deterministic content-hash fallback (M1)
# --------------------------------------------------------------------------


def test_effective_sha_prefers_explicit_then_falls_back_to_content_hash() -> None:
    assert _effective_catalog_sha({"catalog_sha": "explicit", "catalog": {}}) == "explicit"

    export = {"catalog": {"db.t": {"columns": {"c": {"type": "String"}}}}}  # no sha
    digest = _effective_catalog_sha(export)
    assert isinstance(digest, str) and len(digest) == 40  # SHA-1 hex
    # Deterministic: same content ⇒ same digest; a content change ⇒ a different one.
    assert _effective_catalog_sha(dict(export)) == digest
    changed = {"catalog": {"db.t": {"columns": {"c": {"type": "Int32"}}}}}
    assert _effective_catalog_sha(changed) != digest


async def test_load_catalog_graph_empty_sha_uses_fallback_for_guard_and_gc() -> None:
    export = {"catalog": {"db.t": {"columns": {"c": {"type": "String"}}}}}  # no catalog_sha
    digest = _effective_catalog_sha(export)

    # Skip-guard keys off the derived digest: meta already at the digest ⇒ no-op.
    skip_driver = _FakeDriver(meta_sha=digest)
    skipped = await load_catalog_graph(skip_driver, export, ensure_schema=False)  # type: ignore[arg-type]
    assert skipped.skipped is True
    assert skipped.catalog_sha == digest
    assert skip_driver.execute_write_count == 0

    # Write path: absent meta ⇒ hydrate, stamping the derived digest on every write
    # (so the GC predicate + skip-guard remain functional, never keyed off '').
    write_driver = _FakeDriver(meta_sha=None, gc_tables=0)
    report = await load_catalog_graph(write_driver, export, ensure_schema=False)  # type: ignore[arg-type]
    assert report.catalog_sha == digest
    assert write_driver.tx is not None
    for _query, params in write_driver.tx.calls:
        assert params.get("sha") == digest


# --------------------------------------------------------------------------
# Constraints-only schema-ensure (H1/L2) — no vector index, no awaitIndexes
# --------------------------------------------------------------------------


async def test_apply_catalog_graph_schema_runs_constraints_only() -> None:
    driver = _FakeDriver(meta_sha=None)
    await apply_catalog_graph_schema(driver)  # type: ignore[arg-type]

    ran = [q for q, _ in driver.session_calls]
    assert ran == list(_CATALOG_GRAPH_CONSTRAINTS)
    # NO vector-index await on this lightweight path.
    assert not any("awaitIndexes" in q for q in ran)


def test_catalog_meta_constraint_is_registered() -> None:
    # The :CatalogMeta singleton constraint self-deploys via the graph constraints…
    assert any(
        "CatalogMeta" in stmt and "REQUIRE m.id IS UNIQUE" in stmt
        for stmt in _CATALOG_GRAPH_CONSTRAINTS
    )
    # …and every graph constraint is ALSO in the full schema DDL (seed path).
    for constraint in _CATALOG_GRAPH_CONSTRAINTS:
        assert constraint in schema_statements(768)
