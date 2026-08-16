"""A malformed `composes` DAG must DECLINE, never raise (`_validate_compose_nodes`).

`to_candidate` runs on LLM output inside the learning consumer. An uncaught
exception there does not just lose the candidate: the message is never acked →
reclaim → eventual dead-letter, so a single shape-confused node costs the whole
session AND the traceable reason it was rejected. `_compose_nodes` used to do
`int(rn["order"])` with no gate at all (KeyError / ValueError straight out).

The gate is STRUCTURAL only. DAG semantics (duplicate orders, cycles, output kinds)
stay with S4's `check_dag`, which routes them to `fail_to_review` — the human-review
valve a hard `malformed_candidate` reject would bypass; the last two tests pin that
split.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.learning.extractor.models import Decline, ExtractedCandidate
from data_agent.learning.extractor.validation import REASON_MALFORMED, to_candidate

from .helpers import blueprint_raw, make_summary, make_tool_call, param_slot

_NODE_SQL = {
    "tc1": "SELECT sum(gross_pay) AS dept_total FROM payroll.payroll_fact WHERE department = '0420'",
    "tc2": "SELECT sum(gross_pay) AS company_total FROM payroll.payroll_fact",
}


def _good_node(order: int = 0, **overrides: Any) -> dict[str, Any]:
    node = {
        "order": order,
        "node_kind": "query",
        "step_intent": "department earnings",
        "feeds_from": [],
        "consumes": {},
        "output": {"dept_total": "scalar"},
        "source_tool_call_ref": "tc1",
        "when": None,
        "requires_approval": None,
    }
    node.update(overrides)
    return node


def _validate(composes: Any):
    """A composite candidate whose only variable is `composes` (the plan covers the
    single `department` predicate of `_NODE_SQL`, so totality never fires first)."""
    raw = blueprint_raw(
        kind="composite",
        parameterization=[param_slot("department", value="0420")],
        source_refs=("tc1", "tc2"),
    )
    raw["payload"]["composes"] = composes
    summary = make_summary(
        tool_calls=tuple(make_tool_call(ref=ref, sql=sql) for ref, sql in _NODE_SQL.items())
    )
    return to_candidate(raw, summary, known_rules=frozenset())


def test_extractor_node_kinds_mirror_matches_the_runtime():
    """The gate above is only as correct as `extractor/models.py::NODE_KINDS`, which
    USED to be a hand-kept mirror of the runtime's set — and the sibling `SLOT_TYPES`
    mirror demonstrably drifted narrower than the runtime's. It is now a downward
    IMPORT re-exported under the same name, so `is` holds and drift is unrepresentable.
    Kept as the tripwire: if someone re-declares a local frozenset here, equality would
    still pass on the day of the change and this does not."""
    from data_agent.learning.extractor.models import NODE_KINDS
    from data_agent.runtime.blueprint.models import NODE_KINDS as RUNTIME_NODE_KINDS

    assert NODE_KINDS is RUNTIME_NODE_KINDS


def test_wellformed_composite_still_extracts():
    out = _validate([_good_node(0), _good_node(1, source_tool_call_ref="tc2")])
    assert isinstance(out, ExtractedCandidate)
    assert len(out.payload.composes) == 2


@pytest.mark.parametrize(
    ("composes", "needle"),
    [
        pytest.param([{"node_kind": "query"}], "no 'order'", id="order-missing"),
        pytest.param([_good_node(order="first")], "not an integer", id="order-not-numeric"),
        pytest.param([_good_node(order=None)], "not an integer", id="order-null"),
        pytest.param([_good_node(order=True)], "not an integer", id="order-bool"),
        pytest.param([_good_node(order=1.5)], "not an integer", id="order-fractional"),
        pytest.param([_good_node(node_kind="guard")], "node_kind", id="node-kind-retired"),
        pytest.param([_good_node(node_kind=None)], "node_kind", id="node-kind-null"),
        pytest.param([_good_node(feeds_from="0")], "'feeds_from' is not a list", id="feeds-string"),
        pytest.param([_good_node(feeds_from=["a"])], "feeds_from", id="feeds-entry-not-int"),
        pytest.param([_good_node(consumes=[["x", "$0"]])], "'consumes'", id="consumes-list"),
        pytest.param([_good_node(consumes={"x": {"ref": "$0"}})], "'consumes'", id="consumes-nested"),
        pytest.param([_good_node(output=["dept_total"])], "'output'", id="output-list"),
        pytest.param([_good_node(output={"dept_total": 1})], "'output'", id="output-non-string"),
        pytest.param([_good_node(requires_approval="yes")], "requires_approval", id="approval-str"),
        pytest.param(["not-an-object"], "not an object", id="node-not-an-object"),
        pytest.param("node0", "composes is not a list", id="composes-not-a-list"),
        pytest.param({"0": _good_node()}, "composes is not a list", id="composes-a-map"),
    ],
)
def test_malformed_compose_node_declines_malformed_not_raises(composes, needle):
    out = _validate(composes)
    assert isinstance(out, Decline)
    assert out.reason == REASON_MALFORMED
    assert needle in out.detail


def test_numeric_string_order_and_feeds_are_tolerated():
    # A real model routinely emits indices as strings; that is a formatting quirk, not
    # a malformed plan — coerce it rather than throwing the session away.
    out = _validate([_good_node("0"), _good_node("1", feeds_from=["0"], source_tool_call_ref="tc2")])
    assert isinstance(out, ExtractedCandidate)
    assert [n.order for n in out.payload.composes] == [0, 1]
    assert out.payload.composes[1].feeds_from == (0,)


def test_absent_composes_is_not_a_decline():
    # A single (non-composite) blueprint declares no DAG.
    out = _validate(None)
    assert isinstance(out, ExtractedCandidate)
    assert out.payload.composes == ()


def test_non_string_source_tool_call_ref_is_coerced_not_carried_raw():
    # S4 looks the ref up in a dict; an unhashable value would raise there.
    out = _validate([_good_node(source_tool_call_ref=17)])
    assert isinstance(out, ExtractedCandidate)
    assert out.payload.composes[0].source_tool_call_ref == "17"


# --- the review valve stays with S4, not this gate ----------------------------


def test_duplicate_orders_are_not_a_malformed_decline():
    """Structurally fine, semantically a broken DAG — `check_dag` fails it to
    review rather than hard-rejecting the candidate here."""
    out = _validate([_good_node(0), _good_node(0, source_tool_call_ref="tc2")])
    assert isinstance(out, ExtractedCandidate)


def test_unknown_output_kind_is_not_a_malformed_decline():
    out = _validate([_good_node(output={"dept_total": "frame"})])
    assert isinstance(out, ExtractedCandidate)
