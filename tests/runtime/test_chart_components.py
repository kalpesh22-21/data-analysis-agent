"""New presentations are opt-in at every model boundary and share exact sources."""

import asyncio
import json
from contextlib import contextmanager
from copy import deepcopy

import pytest

from data_agent.runtime.composer import (
    PROPOSAL_SCHEMA,
    PresentationError,
    _visual,
    proposal_to_internal,
)
from data_agent.runtime.composer.components import (
    ALL_COMPONENTS,
    CURRENT_COMPONENTS,
    DEFAULT_COMPONENTS,
    component_guidance,
    restrict_schema,
)
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.loop.answer_judge import build_judge_tool
from tests.runtime.test_answer_document import final, section


@contextmanager
def enabled(values=ALL_COMPONENTS):
    token = CURRENT_COMPONENTS.set(values)
    try:
        yield
    finally:
        CURRENT_COMPONENTS.reset(token)


def proposal(kind, **extra):
    return {"takeaway": "Results", "sections": [section(kind, **extra)], "evidence": ["q"]}


def selection(kind):
    p = {"kind": kind, "result_id": "q", "category_column": "Month", "label": "Earnings (USD)"}
    if kind in {"multi_series_line", "grouped_bars", "stacked_bars"}:
        p["series"] = [
            {"measure_column": "Regular", "label": "Regular"},
            {"measure_column": "Overtime", "label": "Overtime"},
        ]
        if kind == "stacked_bars":
            p["additive"] = True
    else:
        p["measure_column"] = "Regular"
        if kind == "heatmap":
            p["series_column"] = "Department"
        else:
            p["composition"] = True
    return p


@pytest.mark.parametrize("kind", ALL_COMPONENTS[6:])
def test_new_chart_opt_in_schema_and_exact_selector_output(kind):
    p = selection(kind)
    raw = proposal(kind, **{k: v for k, v in p.items() if k not in {"kind", "result_id"}})
    with pytest.raises(PresentationError):
        proposal_to_internal(raw)
    with enabled():
        assert proposal_to_internal(raw)["sections"][0]["presentation"] == p
        rendered = _visual(
            p,
            ["Month", "Department", "Regular", "Overtime"],
            [],
            column_formats={
                "Regular": {"kind": "currency", "currency": "USD"},
                "Overtime": {"kind": "currency", "currency": "USD"},
            },
        )
    assert rendered["kind"] == kind and rendered["x_column"] == "Month"
    assert not {"rows", "points", "items", "values"} & rendered.keys()
    if "series" in p:
        assert rendered["series"][1]["y_column"] == "Overtime"


def test_defaults_hide_disabled_types_from_agent_judge_and_recovery_schemas():
    from data_agent.runtime.composer import RECOVERY_SCHEMA

    before = deepcopy(PROPOSAL_SCHEMA)
    for schema in (
        restrict_schema(PROPOSAL_SCHEMA),
        restrict_schema(RECOVERY_SCHEMA),
        build_judge_tool("exit_table"),
    ):
        wire = json.dumps(schema)
        assert all(kind not in wire for kind in ALL_COMPONENTS[6:])
    assert PROPOSAL_SCHEMA == before
    assert RuntimeSettings(_env_file=None).allowed_ui_components == DEFAULT_COMPONENTS


@pytest.mark.parametrize(
    "value", [("text",), ("text", "table", "pie"), ("text", "table", "line", "line")]
)
def test_invalid_configuration_fails_at_startup(value):
    with pytest.raises(ValueError):
        RuntimeSettings(_env_file=None, allowed_ui_components=value)


def test_environment_allowlist(monkeypatch):
    monkeypatch.setenv("ALLOWED_UI_COMPONENTS", '["text","table","donut"]')
    assert RuntimeSettings(_env_file=None).allowed_ui_components == ("text", "table", "donut")


@pytest.mark.parametrize(
    "change", ["currency", "unit", "scale", "duplicate", "axis", "percent_stack"]
)
def test_multi_series_rejects_conflicting_or_ambiguous_selectors(change):
    p = selection("stacked_bars")
    formats = {
        "Regular": {"kind": "currency", "currency": "USD"},
        "Overtime": {"kind": "currency", "currency": "USD"},
    }
    if change in {"currency", "unit", "scale"}:
        formats["Overtime"][change] = "different"
    elif change == "duplicate":
        p["series"][1] = dict(p["series"][0])
    elif change == "axis":
        p["series"][0]["measure_column"] = "Month"
    else:
        formats = {name: {"kind": "percent", "scale": "ratio"} for name in formats}
    with enabled(), pytest.raises(PresentationError):
        _visual(p, ["Month", "Regular", "Overtime"], [], column_formats=formats)


async def test_concurrent_component_contexts_do_not_leak():
    async def check(values):
        with enabled(values):
            await asyncio.sleep(0)
            return restrict_schema(PROPOSAL_SCHEMA)["properties"]["sections"]["items"][
                "properties"
            ]["presentation"]["properties"]["kind"]["enum"]

    left, right = await asyncio.gather(check(("text", "table", "donut")), check(DEFAULT_COMPONENTS))
    assert left == ["text", "table", "donut"]
    assert set(right) == set(DEFAULT_COMPONENTS)
    assert CURRENT_COMPONENTS.get() == DEFAULT_COMPONENTS


async def test_enabled_donut_delivers_and_history_preserves_selector():
    from tests.runtime.test_harness_improvements import CREDS, Judge, build, discovery, query, run

    loop, store, model, _, _ = build(
        [
            discovery(),
            query(),
            final(
                section(
                    "donut",
                    category_column="Department",
                    measure_column="n",
                    label="Employees",
                    composition=True,
                )
            ),
        ],
        Judge(),
    )
    loop._allowed_ui_components = ALL_COMPONENTS
    out = await run(loop, "Show headcount composition as a donut.")
    assert out.answer_document["sections"][0]["presentation"]["kind"] == "donut"
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.messages[-1].answer_document == out.answer_document
    assert CURRENT_COMPONENTS.get() == DEFAULT_COMPONENTS


async def test_real_tool_schema_is_filtered_per_loop_and_not_mutated():
    from data_agent.runtime.mcp.tool_schema import FINALIZE_ANSWER_SCHEMA
    from tests.runtime.test_harness_improvements import Judge, build, discovery, query, run

    before = deepcopy(FINALIZE_ANSWER_SCHEMA)

    async def exercise(components):
        loop, _, model, _, _ = build([discovery(), query(), final(section())], Judge())
        loop._allowed_ui_components = components
        original_provider = loop._tools_provider

        async def tools(credentials):
            return [
                FINALIZE_ANSWER_SCHEMA if tool["name"] == "finalizeAnswer" else tool
                for tool in await original_provider(credentials)
            ]

        loop._tools_provider = tools
        await run(loop)
        tool = next(t for t in model.calls[0].tools if t["name"] == "finalizeAnswer")
        assert "preserves all requested information" in tool["description"]
        for kind in ALL_COMPONENTS:
            assert (f"Use {kind} " in tool["description"]) == (kind in components)
        return tool["parameters"]["properties"]["sections"]["items"]["properties"]["presentation"][
            "properties"
        ]["kind"]["enum"]

    defaults, limited = await asyncio.gather(
        exercise(DEFAULT_COMPONENTS), exercise(("text", "table", "donut"))
    )
    assert set(defaults) == set(DEFAULT_COMPONENTS)
    assert limited == ["text", "table", "donut"]
    assert FINALIZE_ANSWER_SCHEMA == before


async def test_disabled_chart_cannot_ship_even_if_model_ignores_allowlist():
    from tests.runtime.test_harness_improvements import Judge, build, discovery, query, run

    loop, store, model, _, _ = build(
        [
            discovery(),
            query(),
            final(
                section(
                    "donut",
                    category_column="Department",
                    measure_column="n",
                    label="Employees",
                    composition=True,
                ),
                ident="disabled",
            ),
            final(section(), ident="corrected"),
        ],
        Judge(),
    )
    out = await run(loop)
    assert out.answer_document["sections"][0]["presentation"]["kind"] == "table"
    from tests.runtime.test_harness_improvements import CREDS

    trail = await store.load_trail(CREDS.session_id)
    assert any(
        t.tool_call_id == "disabled" and t.error_code == "INVALID_TOOL_ARGUMENTS" for t in trail
    )


@pytest.mark.parametrize(
    "allowed", [DEFAULT_COMPONENTS, ("text", "table", "donut"), ALL_COMPONENTS]
)
def test_selection_guidance_matches_configuration_and_preserves_information(allowed):
    with enabled(allowed):
        guidance = component_guidance()
    for kind in ALL_COMPONENTS:
        assert (f"Use {kind} " in guidance) == (kind in allowed)
    assert "Honor an explicit presentation request" in guidance
    assert "preserves all requested information" in guidance
    assert "Never drop measures" in guidance
    assert "not row count alone" in guidance
    assert "Do not ask the user to choose a display format" in guidance
    if "donut" in allowed:
        assert "a small ranking alone is not composition" in guidance
    if "grouped_bars" in allowed:
        assert "additivity is not required" in guidance
    if "stacked_bars" in allowed:
        assert "disjoint additive components" in guidance
