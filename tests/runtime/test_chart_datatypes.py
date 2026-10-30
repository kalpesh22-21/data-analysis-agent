"""Warehouse types normalize to one JSON-safe chart contract without guessing units."""
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from data_agent.runtime.composer import PresentationError, _visual


def chart(kind, rows):
    return _visual(
        {"kind": kind, "result_id": "q", "category_column": "category",
         "measure_column": "amount", "label": "Amount"},
        ["category", "amount"], rows,
    )


@pytest.mark.parametrize("value,expected", [
    (0, 0), (-3, -3), (1.25, 1.25), (Decimal("123.4500"), 123.45),
    ("+1.25e3", 1250), ("  -2.5E-2  ", -0.025), (".5", 0.5),
    ("1.", 1), ("00042", 42), (2**53 - 1, 2**53 - 1),
])
def test_numeric_measure_types_produce_json_numbers_with_original_display(value, expected):
    rendered = chart("bars", [["A", value]])
    item = rendered["items"][0]
    assert item["raw"] == expected
    if isinstance(value, (str, Decimal)):
        assert item["value"] == str(value)
    json.dumps(rendered, allow_nan=False)


@pytest.mark.parametrize("value", [
    True, False, float("nan"), float("inf"), Decimal("NaN"), Decimal("Infinity"),
    2**53, 2**53 + 1, 10**400, 1e20, "1e999", "1e-999",
    Decimal("0.1234567890123456789"), "1,234.56", "1.234,56", "$12", "12%", "", [], {},
])
def test_ambiguous_nonfinite_and_lossy_measure_types_are_rejected(value):
    with pytest.raises(PresentationError):
        chart("bars", [["A", value]])


@pytest.mark.parametrize("category,label", [
    (2026, "2026"), (2**63, str(2**63)), (1.25, "1.25"),
    (Decimal("1.20"), "1.20"), (True, "Yes"), (False, "No"),
    (None, "Not available"), (date(2026, 1, 2), "2026-01-02"),
    (datetime(2026, 1, 2, 12, tzinfo=UTC), "2026-01-02T12:00:00+00:00"),
    ("001", "001"),
])
def test_bars_accept_scalar_categories_without_changing_identity(category, label):
    result = chart("bars", [[category, 1]])
    assert result["items"][0]["label"] == label
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("categories", [[1, "1"], [True, "Yes"], [None, "Not available"], ["A", " A "]])
def test_category_normalization_cannot_merge_distinct_source_rows(categories):
    with pytest.raises(PresentationError, match="distinct"):
        chart("bars", [[c, 1] for c in categories])


@pytest.mark.parametrize("category", [[], {}, float("nan"), Decimal("Infinity"), " "])
def test_invalid_categories_do_not_get_silently_stringified(category):
    with pytest.raises(PresentationError):
        chart("bars", [[category, 1]])


@pytest.mark.parametrize("periods,anchors", [
    ([2024, 2025, 2026], ["2024-01-01", "2025-01-01", "2026-01-01"]),
    (["2026-Q1", "2026-Q2", "2026-Q3"], ["2026-01-01", "2026-04-01", "2026-07-01"]),
    (["2026-01", "2026-02", "2026-03"], ["2026-01-01", "2026-02-01", "2026-03-01"]),
    ([date(2026, 1, d) for d in (1, 2, 3)], ["2026-01-01", "2026-01-02", "2026-01-03"]),
    ([datetime(2026, 1, d, 12) for d in (1, 2, 3)], [f"2026-01-0{d}T12:00:00" for d in (1, 2, 3)]),
    ([f"2026-01-0{d} 12:00:00Z" for d in (1, 2, 3)], [f"2026-01-0{d}T12:00:00+00:00" for d in (1, 2, 3)]),
])
def test_lines_adapt_calendar_types_preserving_labels_and_null_gaps(periods, anchors):
    result = chart("line", [[periods[0], Decimal("1.00")], [periods[1], None], [periods[2], "3e0"]])
    points = result["points"]
    assert [p["time"] for p in points] == anchors
    assert [p["label"] for p in points] == [p.isoformat() if isinstance(p, date) else str(p) for p in periods]
    assert [p["value"] for p in points] == [1, None, 3]
    assert points[1]["formatted_value"] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("periods", [
    [1700000000, 1700000010, 1700000020],
    ["01/02/2026", "02/02/2026", "03/02/2026"],
    ["2026", "2026-Q2", "2026-07"],
    ["2026-01", "2026-02-01", "2026-03"],
    ["2026-Q1", "2026-Q2", "2026-Q5"],
    ["2026-01-01", "2026-02-30", "2026-03-01"],
    ["2026-01-01T00:00:00Z", "2026-01-01T01:00:00+01:00", "2026-01-02T00:00:00Z"],
    ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00", "2026-01-03T00:00:00Z"],
    [True, False, None],
])
def test_lines_reject_ambiguous_mixed_or_duplicate_periods(periods):
    with pytest.raises(PresentationError):
        chart("line", [[p, 1] for p in periods])


def test_null_measure_is_a_line_gap_not_an_invented_zero_height_bar():
    with pytest.raises(PresentationError, match="missing is not zero"):
        chart("bars", [["A", None]])


def test_chart_float_display_does_not_round_away_supported_precision():
    value = 1.23456789012345
    item = chart("bars", [["A", value]])["items"][0]
    assert item["raw"] == value
    assert item["value"] == str(value)


@pytest.mark.parametrize("rows", [
    [["Sales", "Salary", 10], ["Sales", "Hourly", 20]],
    [["Sales", None, 10], ["Sales", True, 20]],
])
def test_compound_bar_categories_preserve_dimensions_and_row_values(rows):
    rendered = _visual(
        {"kind": "bars", "result_id": "q", "category_columns": ["Department", "Pay Type"],
         "measure_column": "n", "label": "Employees"}, ["Department", "Pay Type", "n"], rows,
    )
    assert len({i["label"] for i in rendered["items"]}) == 2
    assert all(i["label"].startswith("Sales · ") for i in rendered["items"])
    assert [i["raw"] for i in rendered["items"]] == [10, 20]


def test_compound_label_collisions_still_require_a_table():
    with pytest.raises(PresentationError, match="distinct"):
        _visual({"kind": "bars", "result_id": "q", "category_columns": ["a", "b"],
                 "measure_column": "n", "label": "Employees"}, ["a", "b", "n"],
                [["A · B", "C", 1], ["A", "B · C", 2]])


def test_line_sorts_whole_points_without_mutating_source_rows():
    rows = [["2026-03", "3.00"], ["2026-01", "1.00"], ["2026-02", None]]
    rendered = chart("line", rows)
    assert [p["label"] for p in rendered["points"]] == ["2026-01", "2026-02", "2026-03"]
    assert [p["formatted_value"] for p in rendered["points"]] == ["1.00", None, "3.00"]
    assert rows[0] == ["2026-03", "3.00"]
