"""S4-payload-maps-to-runtime-blueprint (D89/D102): the S3 PLAN + the S4
generalization map 1:1 onto `runtime/blueprint/models.py::Blueprint` with NO missing
field — a validated candidate promotes (Slice 9) with zero translation."""

from __future__ import annotations

from data_agent.learning.generalize.builder import generalize_blueprint
from data_agent.learning.generalize.mapping import blueprint_from_generalization
from data_agent.runtime.blueprint.models import Blueprint

from .helpers import CATALOG, composite_sql_by_ref, load_plan, single_sql_by_ref


def test_single_payload_round_trips_to_blueprint():
    plan = load_plan()["single"]
    gen = generalize_blueprint(plan, single_sql_by_ref(), CATALOG)
    bp = blueprint_from_generalization(plan, gen, id="bp-single")

    assert isinstance(bp, Blueprint)
    assert bp.is_single_node
    assert bp.sql_template == gen.sql_template
    assert bp.intent == plan["intent"]
    assert bp.resolves == plan["resolves"]
    # Every role=slot param became a SlotSpec (name/type/binds_to/required preserved).
    assert {s.name for s in bp.slots} == {"department", "year", "region"}
    region = bp.slot("region")
    assert region is not None and region.required is False
    assert region.optional_pattern == "TRUE"
    assert bp.result_grain.verifiable is False
    assert bp.uses_rules == tuple(gen.uses_rules)


def test_composite_payload_round_trips_to_blueprint():
    plan = load_plan()["composite"]
    gen = generalize_blueprint(plan, composite_sql_by_ref(), CATALOG)
    bp = blueprint_from_generalization(plan, gen, id="bp-composite")

    assert isinstance(bp, Blueprint)
    assert not bp.is_single_node
    assert bp.sql_template is None
    assert len(bp.composes) == 2
    # Each runtime Node carries the S4 per-node template, keyed by matching `order`.
    by_order = {n.order: n for n in bp.composes}
    assert by_order[0].sql_template == gen.node_templates[0].sql_template
    assert by_order[1].sql_template == gen.node_templates[1].sql_template
    assert by_order[0].output == {"dept_total": "scalar"}
