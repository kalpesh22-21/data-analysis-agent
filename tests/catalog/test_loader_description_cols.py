"""Unit tests for the loader's `load_description_cols()` / `_extract_description_cols`.

Layer: 1 — Unit (pure logic; temp-dir YAML fixtures for the parse edge cases plus
the committed export fixture for the shape/alignment checks; no ClickHouse).

`load_description_cols()` is a third projection of the same catalog dict that
`build_sqlglot_schema()` and `load_semantic_catalog()` build from. It returns the
AUTHORED {code_col -> sibling-label-col} linkage so `resolveValues` can SELECT both
columns jointly instead of relying on the `_candidate_description_columns` naming
convention (which case-mismatches, e.g. `distributedDepartmentDescription`, and
misses non-convention names, e.g. `FieldId` -> `FieldLabel`).

D75 Wave 1b: the real-catalog shape/alignment checks are driven from the export
fixture via the in-memory cores (`load_description_cols_from_catalog` /
`build_sqlglot_schema_from_catalog`); the parse edge cases still exercise the
dir-reading `load_description_cols()` against temp-dir YAML fixtures.
"""

from __future__ import annotations

from pathlib import Path

from data_agent.catalog.loader import (
    _extract_description_cols,
    build_sqlglot_schema_from_catalog,
    load_description_cols,
    load_description_cols_from_catalog,
)
from tests._catalog_fixture import fixture_catalog

_CATALOG = fixture_catalog()

_P = "dbpcm_warehouse.payroll"
_PAF = "dbpcm_warehouse.personnel_action_form_changes"


# ---------------------------------------------------------------------------
# 1. load_description_cols() shape — {db_table: {code_col: desc_col}}
# ---------------------------------------------------------------------------


def test_load_description_cols_returns_declared_linkage(tmp_path: Path) -> None:
    (tmp_path / "codes.yaml").write_text(
        """
database: dbpcm_warehouse
table: codes
columns:
  FieldId:
    type: Nullable(String)
    description_col: FieldLabel
    description: A client-defined code.
  FieldLabel:
    type: Nullable(String)
    description: The human-readable label.
  PlainValue:
    type: Nullable(String)
    description: A column with no sibling label.
""",
        encoding="utf-8",
    )
    result = load_description_cols(tmp_path)
    assert result == {"dbpcm_warehouse.codes": {"FieldId": "FieldLabel"}}


def test_column_without_description_col_absent_from_inner_map(tmp_path: Path) -> None:
    """A column that omits `description_col` must not appear in the inner map — it
    is not padded with a convention-guessed default."""
    (tmp_path / "codes.yaml").write_text(
        """
database: dbpcm_warehouse
table: codes
columns:
  FieldId: { type: Nullable(String), description_col: FieldLabel }
  FieldLabel: { type: Nullable(String) }
  PlainValue: { type: Nullable(String) }
""",
        encoding="utf-8",
    )
    inner = load_description_cols(tmp_path)["dbpcm_warehouse.codes"]
    assert "PlainValue" not in inner
    assert "FieldLabel" not in inner  # the label column itself declares no link
    assert inner == {"FieldId": "FieldLabel"}


def test_table_with_no_links_present_as_empty_inner_map(tmp_path: Path) -> None:
    """Every catalogued table is a key; a table with zero declared links maps to {}
    (kept for symmetry with build_sqlglot_schema / load_semantic_catalog)."""
    (tmp_path / "plain.yaml").write_text(
        """
database: dbpcm_warehouse
table: plain
columns:
  A: { type: Nullable(String) }
  B: { type: Nullable(String) }
""",
        encoding="utf-8",
    )
    result = load_description_cols(tmp_path)
    assert result == {"dbpcm_warehouse.plain": {}}


# ---------------------------------------------------------------------------
# 2. _extract_description_cols robustness — never raises on malformed defs
# ---------------------------------------------------------------------------


def test_extract_ignores_non_dict_col_defs() -> None:
    raw = {
        "BareValue": "Nullable(String)",  # bare scalar, not a dict
        "NullCol": None,
        "ListCol": ["a", "b"],
        "Real": {"type": "Nullable(String)", "description_col": "RealLabel"},
    }
    assert _extract_description_cols(raw) == {"Real": "RealLabel"}


def test_extract_ignores_blank_and_non_string_description_col() -> None:
    raw = {
        "Blank": {"description_col": ""},
        "Whitespace": {"description_col": "   "},
        "NoneVal": {"description_col": None},
        "IntVal": {"description_col": 123},
        "ListVal": {"description_col": ["x"]},
        "BoolVal": {"description_col": True},
        "Good": {"description_col": "GoodLabel"},
    }
    # No raise, and only the truthy-string declaration survives.
    assert _extract_description_cols(raw) == {"Good": "GoodLabel"}


def test_extract_empty_columns_block() -> None:
    assert _extract_description_cols({}) == {}


# ---------------------------------------------------------------------------
# 3. Real-catalog projection + alignment (keys match the sqlglot schema)
# ---------------------------------------------------------------------------


def test_real_catalog_declared_links_present() -> None:
    result = load_description_cols_from_catalog(_CATALOG)
    assert result[_PAF] == {"FieldId": "FieldLabel"}
    # payroll declares two: TypeCode and the mixed-case DistributedDepartmentCode.
    assert result[_P]["TypeCode"] == "TypeCodeDescription"
    assert result[_P]["DistributedDepartmentCode"] == "distributedDepartmentDescription"


def test_real_catalog_case_preserved_in_link_target() -> None:
    """The declared target casing is preserved exactly (D70) — the lowercase-leading
    `distributedDepartmentDescription` is the case the naming convention misses."""
    payroll = load_description_cols_from_catalog(_CATALOG)[_P]
    assert payroll["DistributedDepartmentCode"] == "distributedDepartmentDescription"
    assert payroll["DistributedDepartmentCode"] != "DistributedDepartmentDescription"


def test_description_cols_keys_align_with_sqlglot_schema() -> None:
    """Same keying / file-skip rules as build_sqlglot_schema — one entry per table,
    never drifting from the schema view."""
    desc = load_description_cols_from_catalog(_CATALOG)
    schema = build_sqlglot_schema_from_catalog(_CATALOG)
    assert set(desc.keys()) == set(schema.keys())


def test_declared_targets_are_real_columns_in_the_same_table() -> None:
    """Every declared description_col target must be an actual column on its table
    (guards against an author typo shipping in the real catalog)."""
    desc = load_description_cols_from_catalog(_CATALOG)
    schema = build_sqlglot_schema_from_catalog(_CATALOG)
    for db_table, links in desc.items():
        cols = schema[db_table]
        for code_col, desc_col in links.items():
            assert code_col in cols, f"{db_table}.{code_col} missing"
            assert desc_col in cols, f"{db_table}.{desc_col} (target of {code_col}) missing"
