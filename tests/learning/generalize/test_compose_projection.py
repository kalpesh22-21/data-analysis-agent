"""`composes_json` is an ALLOWLIST projection, not a copy of the S3 plan (D17).

The plan node is session-scoped working state; the landed `:Blueprint` is a
governed, globally-recallable, entity-free artifact. Copying the plan dict carried
`source_tool_call_ref` — a SESSION identifier — into the corpus, where session
linkage has no business (it belongs in the `evidence_ref`s in the access-controlled
`learning_audit` store). It also carried `step_intent`, free model prose that the S5
leakage gate does NOT scan (`_ENTITY_FREE_SURFACES` covers `intent`/`notes`/
`result_signature`, and the landing last-gate can only match spans S5 already
found), so an entity there would have reached the global corpus unscanned.

`test_a_new_plan_field_does_not_reach_the_corpus` is the load-bearing one: it pins
the DEFAULT (a field added to `ComposeNodePlan` stays out until someone allowlists
it), which a copy-minus-two projection cannot give.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.generalize.mapping import (
    _NODE_DOC_FIELDS,
    blueprint_seed_from_candidate,
)
from data_agent.runtime.blueprint.models import Node

from ..promotion.helpers import FIXTURES
from .helpers import load_expected, load_plan

_LANDING_ID = "bp::sha256:composite-bp"


def _composite_candidate(**node_overrides: Any) -> CandidateEnvelope:
    """A validated COMPOSITE candidate: the frozen S3 plan + the frozen S4
    generalization, on the frozen envelope shell (no composite envelope is fixtured)."""
    doc = copy.deepcopy(
        json.loads((FIXTURES / "s4_enriched_blueprint.json").read_text())["single"]["envelope"]
    )
    payload = copy.deepcopy(load_plan()["composite"])
    payload["generalization"] = copy.deepcopy(load_expected()["composite"]["generalization"])
    for node in payload["composes"]:
        node.update(node_overrides)
    doc["payload"] = payload
    return CandidateEnvelope.from_doc(doc)


def _landed_composes() -> list[dict[str, Any]]:
    return blueprint_seed_from_candidate(_composite_candidate(), id=_LANDING_ID).composes


def test_session_identifier_never_lands_in_the_corpus():
    landed = _landed_composes()
    assert landed, "fixture must be composite"
    for node in landed:
        assert "source_tool_call_ref" not in node
    # The whole serialized property, not just the top-level keys (this is what the
    # landing writer JSON-dumps onto `b.composes_json`).
    assert "tc1" not in json.dumps(landed)
    assert "tc2" not in json.dumps(landed)


def test_step_intent_does_not_land():
    landed = _landed_composes()
    for node in landed:
        assert "step_intent" not in node
    assert "department earnings for the year" not in json.dumps(landed)


def test_a_new_plan_field_does_not_reach_the_corpus():
    # A field added to the PLAN model tomorrow must be an explicit decision, never a
    # silent default-carry into the governed corpus.
    seed = blueprint_seed_from_candidate(
        _composite_candidate(analyst_note="raw session prose about employee 4711"),
        id=_LANDING_ID,
    )
    assert "4711" not in json.dumps(seed.composes)
    for node in seed.composes:
        assert "analyst_note" not in node


def test_the_projection_keeps_everything_the_runtime_reads():
    landed = _landed_composes()
    by_order = {n["order"]: n for n in landed}
    assert set(by_order) == {0, 1}
    node0 = by_order[0]
    assert node0["node_kind"] == "query"
    assert node0["feeds_from"] == []
    assert node0["consumes"] == {}
    assert node0["output"] == {"dept_total": "scalar"}
    # The S4 per-node template is JOINED in (by `order`), not copied from the plan.
    assert node0["sql_template"] == load_expected()["composite"]["generalization"][
        "node_templates"
    ][0]["sql_template"]
    # And the result still parses as a runtime Node — the projection cannot drop a
    # field `Node.parse` needs.
    parsed = Node.parse(node0)
    assert parsed.order == 0 and parsed.output == {"dept_total": "scalar"}


def test_the_allowlist_is_exactly_what_node_parse_reads():
    """`_NODE_DOC_FIELDS` ⊆ the keys `Node.parse` reads. A key that is NOT read is
    dead weight in the corpus; a read key that is missing would be a silent drop."""
    read_by_parse = {
        "order",
        "node_kind",
        "feeds_from",
        "consumes",
        "output",
        "sql_template",
        "when",
        "requires_approval",
    }
    # `sql_template` is joined in from S4, so it is deliberately not in the allowlist.
    assert set(_NODE_DOC_FIELDS) == read_by_parse - {"sql_template"}
