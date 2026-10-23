"""Temporal slot types survive resolution and SQL binding."""
import datetime as dt

import pytest
import sqlglot
from sqlglot import exp

from data_agent.runtime.blueprint.models import SlotSpec
from data_agent.runtime.blueprint.slots import resolve_slot, expand_binding
from data_agent.runtime.blueprint.template import bind_template, TemplateBindError


@pytest.mark.parametrize("start,end,kind", [
    ("2025-01-01", "2026-01-01", "DATE"),
    ("2025-01-01T12:30:00", "2025-01-01T13:30:00", "TIMESTAMP"),
])
@pytest.mark.parametrize("wrapped", [False, True])
def test_period_bounds_keep_type_and_operators(start, end, kind, wrapped):
    spec = SlotSpec(name="window", type="period_range", required=True,
                    temporal_type="date" if kind == "DATE" else "datetime")
    bounds = expand_binding(spec, resolve_slot({"start": start, "end": end}, spec).value)
    left, right = "{window_start}", "{window_end}"
    if wrapped:
        function = "toDate" if kind == "DATE" else "toDateTime"
        left, right = f"{function}({left})", f"{function}({right})"
    sql = bind_template(f"SELECT * FROM events WHERE stamp >= {left} AND stamp < {right}", bounds)
    tree = sqlglot.parse_one(sql, read="clickhouse")
    assert len(list(tree.find_all(exp.GTE))) == 1
    assert len(list(tree.find_all(exp.LT))) == 1
    casts = list(tree.find_all(exp.Cast))
    assert len(casts) == (0 if wrapped else 2)
    assert all(c.args["to"].this.value == ("DATE" if kind == "DATE" else "DATETIME64") for c in casts)
    literals = {n.this for n in tree.find_all(exp.Literal) if n.is_string}
    assert literals == {start.replace("T", " "), end.replace("T", " ")}


def test_ordinary_date_shaped_string_remains_string():
    sql = bind_template("SELECT * FROM events WHERE code={code}", {"code": "2025-01-01"})
    assert not list(sqlglot.parse_one(sql, read="clickhouse").find_all(exp.Cast))


def test_timezone_aware_datetime_is_not_silently_stripped():
    with pytest.raises(TemplateBindError, match="timezone"):
        bind_template("SELECT {value}", {"value": dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)})


@pytest.mark.parametrize("declared, start,end", [
    ("date", "2025-01-01T00:00:00", "2026-01-01T00:00:00"),
    ("datetime", "2025-01-01", "2026-01-01"),
])
def test_model_format_cannot_override_slot_contract(declared, start, end):
    from data_agent.runtime.blueprint.slots import AskUser
    spec = SlotSpec(name="window", type="period_range", temporal_type=declared)
    assert isinstance(resolve_slot({"start": start, "end": end}, spec), AskUser)


def test_temporal_metadata_is_validated_and_exposed():
    from data_agent.runtime.blueprint.models import BlueprintParseError
    from data_agent.runtime.retrieval.tools import _enrich_slot
    raw = {"name": "window", "type": "period_range", "temporal_type": "date"}
    assert SlotSpec.parse(raw).temporal_type == "date"
    assert _enrich_slot(raw)["temporal_type"] == "date"
    for value in ("VARCHAR", [], "date); DROP TABLE x"):
        with pytest.raises(BlueprintParseError):
            SlotSpec.parse({**raw, "temporal_type": value})


@pytest.mark.parametrize("type_", ["string", "period", "as_of_date"])
def test_explicit_scalar_calendar_slot(type_):
    spec = SlotSpec(name="d", type=type_, temporal_type="date")
    result = resolve_slot("2025-01-01", spec, domain=["2025-01-01"])
    sql = bind_template("SELECT {d}", expand_binding(spec, result.value))
    assert "CAST('2025-01-01' AS DATE)" in sql
