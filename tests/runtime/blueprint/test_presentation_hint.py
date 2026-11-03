"""A blueprint's reviewed presentation reaches the model, filtered to enabled kinds.

The canon declares `presentation` (kind, category_columns, measure_columns, optional
by_group_by); the export carries it, the loader stores it as `presentation_json`, the
keyed fetch maps it back, and getBlueprint renders it as `suggested_presentation`. A
kind the deployment disabled is never suggested: the composer refuses it outright, so
suggesting it would only cost the model a repair round.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from data_agent.corpus.seeds import _seed_from_entry
from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.compiler import dag_properties
from data_agent.runtime.composer.components import (
    CURRENT_COMPONENTS,
    DEFAULT_COMPONENTS,
    component_guidance,
    suggested_presentation,
)
from data_agent.runtime.retrieval.corpus_loader import _UPSERT_BLUEPRINT, BlueprintSeed
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.tools import GetBlueprintTool
from data_agent.runtime.retrieval.vector_index import (
    _GET_BLUEPRINT_QUERY,
    FakeVectorIndex,
    map_blueprint_detail_record,
)

_P = "dbpcm_warehouse.payroll"
HINT: dict[str, Any] = {
    "kind": "bars",
    "category_columns": ["Group"],
    "measure_columns": ["Payroll Amount"],
    "by_group_by": {"month": "line", "company": "figure"},
}


@contextmanager
def enabled(values):
    token = CURRENT_COMPONENTS.set(tuple(values))
    try:
        yield
    finally:
        CURRENT_COMPONENTS.reset(token)


def _seed(**overrides: Any) -> BlueprintSeed:
    base: dict[str, Any] = {
        "id": "bp-payroll-summary",
        "intent": "Payroll amount by group",
        "slots_summary": "group_by",
        "uses": [f"{_P}.amount"],
        "sql_template": "SELECT 'Company' AS `Group`, sum(amount) AS `Payroll Amount` FROM dbpcm_warehouse.payroll",
    }
    base.update(overrides)
    return BlueprintSeed(**base)


def _detail(presentation: dict[str, Any] | None) -> BlueprintDetail:
    return BlueprintDetail(
        id="bp-payroll-summary", intent="Payroll amount by group", slots_summary="group_by",
        uses=frozenset({f"{_P}.amount"}), status="validated", drift_status="clean",
        hit_count=0, catalog_sha="", presentation=presentation,
    )


async def _get_blueprint(detail: BlueprintDetail) -> dict[str, Any]:
    index = FakeVectorIndex()
    index.add_detail(detail)
    creds = RuntimeCredentials(session_id="s", jwt="t", column_scope=frozenset(detail.uses or ()))
    result = await GetBlueprintTool(vector_index=index).run({"id": detail.id}, creds)
    assert result.status == "ok"
    return result.result_full


# --- export -> seed -> node -> detail ---------------------------------------


def test_the_export_entry_keeps_its_presentation_on_the_seed() -> None:
    entry = {"intent": "x", "slots_summary": "", "uses": [], "presentation": HINT}
    assert _seed_from_entry("bp-x", entry, kind="blueprint").presentation == HINT


def test_the_seed_writes_the_presentation_to_the_node_and_null_when_absent() -> None:
    assert dag_properties(_seed(presentation=HINT))["presentation_json"] is not None
    assert dag_properties(_seed())["presentation_json"] is None  # neo4j removes a null SET
    assert "b.presentation_json = $presentation_json" in _UPSERT_BLUEPRINT


def test_the_keyed_fetch_maps_the_stored_presentation_and_drops_a_corrupt_one() -> None:
    assert "b.presentation_json AS presentation_json" in _GET_BLUEPRINT_QUERY
    stored = dag_properties(_seed(presentation=HINT))["presentation_json"]
    assert map_blueprint_detail_record({"id": "bp-x", "uses": [], "presentation_json": stored}).presentation == HINT
    assert map_blueprint_detail_record({"id": "bp-x", "uses": [], "presentation_json": "[1]"}).presentation is None


# --- the enabled-kind filter -----------------------------------------------


def test_default_components_pass_the_reviewed_presentation_through() -> None:
    with enabled(DEFAULT_COMPONENTS):
        assert suggested_presentation(HINT) == HINT


@pytest.mark.parametrize(
    ("allowed", "expected"),
    [
        # bars disabled: the default becomes a table (no columns), overrides survive.
        (("text", "table", "figure", "facts", "line"),
         {"kind": "table", "by_group_by": {"month": "line", "company": "figure"}}),
        # line disabled: the month override falls back to the bars default and is dropped.
        (("text", "table", "figure", "facts", "bars"),
         {**HINT, "by_group_by": {"company": "figure"}}),
        # figure disabled: the company override falls back to facts.
        (("text", "table", "facts", "bars", "line"),
         {**HINT, "by_group_by": {"month": "line", "company": "facts"}}),
        # only the mandatory pair: everything is a table.
        (("text", "table"), {"kind": "table"}),
    ],
)
def test_a_disabled_kind_is_replaced_by_the_nearest_enabled_one(allowed, expected) -> None:
    with enabled(allowed):
        assert suggested_presentation(HINT) == expected


def test_a_line_that_falls_back_to_bars_keeps_one_category_and_one_measure() -> None:
    line = {"kind": "line", "category_columns": ["Month"], "measure_columns": ["Gross", "Net"]}
    with enabled(("text", "table", "bars")):
        assert suggested_presentation(line) == {"kind": "bars", "category_columns": ["Month"], "measure_columns": ["Gross"]}
    with enabled(DEFAULT_COMPONENTS):
        assert suggested_presentation(line) == line  # a line keeps its measure alternatives


def test_figure_falls_back_to_facts_then_table_and_line_to_bars() -> None:
    figure = {"kind": "figure", "measure_columns": ["Total"]}
    line = {"kind": "line", "category_columns": ["Month"], "measure_columns": ["Total"]}
    with enabled(("text", "table", "facts", "bars")):
        assert suggested_presentation(figure) == {"kind": "facts", "measure_columns": ["Total"]}
        assert suggested_presentation(line) == {**line, "kind": "bars"}
    with enabled(("text", "table")):
        assert suggested_presentation(figure) == {"kind": "table"}


@pytest.mark.parametrize("bad", [None, {}, {"kind": 3}, "bars"])
def test_a_missing_or_malformed_hint_suggests_nothing(bad) -> None:
    assert suggested_presentation(bad) is None


# --- what the model reads -------------------------------------------------


async def test_get_blueprint_renders_the_filtered_suggestion() -> None:
    with enabled(DEFAULT_COMPONENTS):
        assert (await _get_blueprint(_detail(HINT)))["suggested_presentation"] == HINT
    with enabled(("text", "table", "figure", "facts", "line")):
        found = await _get_blueprint(_detail(HINT))
    assert found["suggested_presentation"]["kind"] == "table"


async def test_get_blueprint_omits_the_key_when_the_blueprint_declares_none() -> None:
    assert "suggested_presentation" not in await _get_blueprint(_detail(None))


def test_the_model_is_told_how_to_use_the_suggestion() -> None:
    guidance = component_guidance()
    assert "suggested_presentation" in guidance
    assert "unless the user asks for another view or the actual result does not fit" in guidance


def test_a_donut_hint_is_kept_where_enabled_and_becomes_bars_by_default() -> None:
    donut = {"kind": "donut", "category_columns": ["Expense Type"], "measure_columns": ["Approved Amount"]}
    with enabled((*DEFAULT_COMPONENTS, "donut")):
        assert suggested_presentation(donut) == donut
    with enabled(DEFAULT_COMPONENTS):  # donut is off by default
        assert suggested_presentation(donut) == {**donut, "kind": "bars"}
    with enabled(("text", "table", "figure", "facts", "line")):
        assert suggested_presentation(donut) == {"kind": "table"}
