"""Adversarial probe of the new `composes` structural gate (`_validate_compose_nodes`).

The gate's whole stated purpose is that NO shape-confused LLM node escapes
`to_candidate` as an exception: an uncaught raise there leaves the consumer message
un-acked → reclaim → dead-letter, losing the whole session AND the traceable reason.
This file attacks the gate with the shapes a real model actually emits when it gets
the JSON type wrong, plus the Python-specific traps (`True == 1`, falsy-vs-absent,
unhashable containers, unicode/underscore numerals).

Two REAL holes were found here and are now FIXED; both were strict xfails and are
kept as passing regression guards (history in each test's docstring):

  * `node_kind` as a LIST or DICT — `node_kind not in NODE_KINDS` against a
    `frozenset` raised `TypeError: unhashable type`, straight out of `to_candidate`
    and out of `LearningExtractor.extract`. This was precisely the failure mode the
    gate was written to prevent, reachable from ordinary model output
    (`"node_kind": ["query"]`). Every OTHER untrusted field in this gate was already
    `str()`-coerced or `isinstance`-checked first; `node_kind` alone was fed raw to a
    set-membership test. FIXED by isinstance-before-membership; the identical bug in
    `runtime/blueprint/models.py::Node.parse` was fixed the same way (see
    `test_dag_loader_parity_qa.py`).
  * `feeds_from: 0` — the falsy-scalar hole. `rn.get("feeds_from") or []` turned the
    single most likely scalar-instead-of-list mistake ("this node feeds from node 0")
    into a SILENTLY EDGELESS node, while every other scalar (`feeds_from: 1`)
    declined. The result was a candidate whose `consumes` referenced a node absent
    from its `feeds_from` — which `check_dag` also did not catch, so it passed S4 and
    died at landing with a `CorpusLoadError`. FIXED, and then widened: the
    falsy-normalizing read is gone entirely — ABSENT means "none" and every other
    wrong type declines, for `feeds_from`, `consumes` and `output` alike (see
    `test_a_falsy_non_container_declines_like_every_other_wrong_type` for why the
    narrower index-only rule was overturned). `check_dag` independently cross-checks
    consume refs against `feeds_from`, closing the second half.

Everything else below passes and is pinned as the gate's documented behaviour.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.learning.extractor.models import Decline, ExtractedCandidate
from data_agent.learning.extractor.validation import REASON_MALFORMED, to_candidate

from .helpers import (
    KEEP_VERDICT,
    blueprint_raw,
    make_extractor,
    make_summary,
    make_tool_call,
    param_slot,
    scripted_turn,
)

_NODE_SQL = {
    "tc1": "SELECT sum(gross_pay) AS dept_total FROM payroll.payroll_fact WHERE department = '0420'",
    "tc2": "SELECT sum(gross_pay) AS company_total FROM payroll.payroll_fact",
}


def _node(order: Any = 0, **overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
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


def _raw(composes: Any) -> dict[str, Any]:
    raw = blueprint_raw(
        kind="composite",
        parameterization=[param_slot("department", value="0420")],
        source_refs=("tc1", "tc2"),
    )
    raw["payload"]["composes"] = composes
    return raw


def _summary():
    return make_summary(
        tool_calls=tuple(make_tool_call(ref=ref, sql=sql) for ref, sql in _NODE_SQL.items())
    )


def _validate(composes: Any):
    return to_candidate(_raw(composes), _summary(), known_rules=frozenset())


# --- FINDING 1 (FIXED): an unhashable `node_kind` used to raise out of the gate ---


@pytest.mark.parametrize(
    "node_kind",
    [
        pytest.param(["query"], id="list"),
        pytest.param({"kind": "query"}, id="dict"),
        pytest.param([], id="empty-list"),
    ],
)
def test_unhashable_node_kind_declines_rather_than_raising(node_kind: Any) -> None:
    """WAS a strict xfail (HIGH): `node_kind not in NODE_KINDS` hashed the raw value,
    so a list/dict raised `TypeError` out of `to_candidate` — the exact dead-letter
    path the gate exists to close. FIXED in `_validate_compose_nodes` by testing
    `isinstance(node_kind, str)` first, the same posture every other field in the gate
    already had."""
    out = _validate([_node(node_kind=node_kind)])
    assert isinstance(out, Decline)
    assert out.reason == REASON_MALFORMED


async def test_unhashable_node_kind_does_not_escape_the_extractor() -> None:
    """WAS a strict xfail (HIGH), the end-to-end half of FINDING 1: `to_candidate` is
    still called with no try/except around it, so the gate returning a Decline rather
    than raising is the ONLY thing keeping one bad node from costing the whole
    session's extraction.

    The script is three identical turns because a `malformed_candidate` is now
    CORRECTABLE: the extractor tells the model which field is wrong and lets it
    re-emit, twice, before giving up (`ExtractorConfig.max_shape_corrections`). A model
    that repeats the same bad node still ends where it always did — one decline, no
    candidates, no exception — and the decline now records that it WAS asked again."""
    bad = _raw([_node(node_kind=["query"])])
    extractor = make_extractor([scripted_turn([bad]) for _ in range(3)])
    result = await extractor.extract(_summary(), KEEP_VERDICT)
    assert result.candidates == ()
    assert [d.reason for d in result.declines] == [REASON_MALFORMED]
    assert result.declines[0].corrections_attempted == 2
    assert result.corrections == 2


# --- FINDING 2 (FIXED): `feeds_from: 0` was silently dropped -------------------


def test_scalar_feeds_from_zero_is_not_silently_dropped() -> None:
    """WAS a strict xfail (MEDIUM): `rn.get('feeds_from') or []` swallowed the FALSY
    scalar, so `feeds_from: 0` became an edgeless node with no decline while
    `feeds_from: 1` declined. FIXED by rejecting a bare node INDEX (`_node_index`)
    before the falsy-normalizing read."""
    out = _validate([_node(0), _node(1, feeds_from=0, source_tool_call_ref="tc2")])
    assert isinstance(out, Decline), (
        "feeds_from=0 was accepted and flattened to (): "
        f"{out.payload.composes[1].feeds_from if isinstance(out, ExtractedCandidate) else out}"
    )
    assert "'feeds_from' is not a list" in out.detail


def test_the_asymmetry_a_truthy_scalar_feeds_from_does_decline() -> None:
    """The contrast that made FINDING 2 a bug and not a policy: the same shape with a
    NON-zero value was always rejected; only the falsy one slipped. Both decline now —
    kept so the two halves of the rule can never diverge again."""
    out = _validate([_node(0), _node(2, feeds_from=1, source_tool_call_ref="tc2")])
    assert isinstance(out, Decline)
    assert "'feeds_from' is not a list" in out.detail


def test_feeds_from_zero_no_longer_produces_a_candidate_the_loader_would_refuse() -> None:
    """The consequence FINDING 2 carried, pinned end to end at the S3 layer. It USED
    to extract cleanly with `feeds_from=()` and a `consumes` whose source was not in
    `feeds_from` — a shape the corpus loader refuses at landing (§1.2(h), see
    test_dag_loader_parity_qa). It is now declined at extraction, and the same shape
    is independently rejected by `check_dag`, which cross-checks consume refs against
    `feeds_from` (the belt for a candidate built any other way)."""
    from data_agent.learning.generalize.validate import check_dag

    out = _validate(
        [
            _node(0, output={"dept_total": "scalar"}),
            _node(1, feeds_from=0, consumes={"t": "$0.dept_total"}, output={},
                  source_tool_call_ref="tc2"),
        ]
    )
    assert isinstance(out, Decline)
    assert out.reason == REASON_MALFORMED
    assert check_dag(
        [
            {"order": 0, "output": {"dept_total": "scalar"}},
            {"order": 1, "feeds_from": [], "consumes": {"t": "$0.dept_total"}, "output": {}},
        ]
    ) is False


# --- the gate holds against the rest of the untrusted-shape surface -----------


@pytest.mark.parametrize(
    ("composes", "needle"),
    [
        # `feeds_from` as every non-list container a model reaches for.
        pytest.param([_node(feeds_from={"0": 1})], "'feeds_from' is not a list", id="feeds-dict"),
        pytest.param([_node(feeds_from={0, 1})], "'feeds_from' is not a list", id="feeds-set"),
        pytest.param([_node(feeds_from=b"01")], "'feeds_from' is not a list", id="feeds-bytes"),
        pytest.param([_node(feeds_from=(0,))], "'feeds_from' is not a list", id="feeds-tuple"),
        pytest.param([_node(feeds_from=[[0]])], "'feeds_from' entry", id="feeds-nested-list"),
        pytest.param([_node(feeds_from=[{"n": 0}])], "'feeds_from' entry", id="feeds-entry-dict"),
        pytest.param([_node(feeds_from=[None])], "'feeds_from' entry", id="feeds-entry-null"),
        # `True == 1` — the bool trap, on both index fields.
        pytest.param([_node(feeds_from=[True])], "'feeds_from' entry", id="feeds-entry-bool"),
        pytest.param([_node(order=False)], "'order'", id="order-bool-false"),
        # non-finite / fractional floats are typos, never truncated.
        pytest.param([_node(order=float("inf"))], "not an integer", id="order-inf"),
        pytest.param([_node(order=float("nan"))], "not an integer", id="order-nan"),
        pytest.param([_node(order=-0.5)], "not an integer", id="order-fractional-negative"),
        pytest.param([_node(order=[0])], "not an integer", id="order-list"),
        pytest.param([_node(order={"n": 0})], "not an integer", id="order-dict"),
        # non-string KEYS as well as values, on both maps.
        pytest.param([_node(consumes={0: "$0"})], "'consumes'", id="consumes-int-key"),
        pytest.param([_node(consumes={"x": None})], "'consumes'", id="consumes-null-value"),
        pytest.param([_node(consumes={"x": ["$0"]})], "'consumes'", id="consumes-list-value"),
        pytest.param([_node(output={0: "scalar"})], "'output'", id="output-int-key"),
        pytest.param([_node(output={"o": None})], "'output'", id="output-null-value"),
        pytest.param([_node(output={"o": {"kind": "scalar"}})], "'output'", id="output-nested"),
        # `requires_approval` must be an object, never a scalar.
        pytest.param([_node(requires_approval=True)], "requires_approval", id="approval-true"),
        pytest.param([_node(requires_approval=[{}])], "requires_approval", id="approval-list"),
        # the container itself.
        pytest.param([[_node()]], "is not an object", id="node-is-a-list"),
        pytest.param([None], "is not an object", id="node-is-null"),
        pytest.param([_node(), None], "composes[1] is not an object", id="second-node-null"),
        pytest.param(b"[]", "composes is not a list", id="composes-bytes"),
        pytest.param(0.0, "composes is not a list", id="composes-float"),
    ],
)
def test_shape_confusion_declines_and_never_raises(composes: Any, needle: str) -> None:
    out = _validate(composes)
    assert isinstance(out, Decline), f"expected a Decline, got {type(out).__name__}"
    assert out.reason == REASON_MALFORMED
    assert needle in out.detail


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("feeds_from", ""),
        ("feeds_from", {}),
        ("consumes", []),
        ("consumes", 0),
        ("consumes", ""),
        ("output", []),
        ("output", 0),
        ("output", ""),
    ],
)
def test_a_falsy_non_container_declines_like_every_other_wrong_type(
    field: str, value: Any
) -> None:
    """HISTORY — this test asserted the OPPOSITE (`..._normalizes_to_empty_rather_
    than_declining`) and was documented-not-filed: `rn.get(f) or {}` treated every
    falsy value as absent, which loses no information for a falsy CONTAINER
    (`consumes: []` and `consumes: {}` make the same statement) and so was judged
    harmless, with only the meaning-bearing `feeds_from: 0` filed as FINDING 2.

    Overturned deliberately. This parses untrusted model output: "the model sent a
    string where a list belongs" is exactly the evidence you want visible in a decline
    reason when tuning the prompt, and normalizing it away throws that evidence out.
    The `0`-vs-`""` split was defensible on "does it carry a node index" but gave two
    type violations of the same kind different treatment for a reason nobody would
    remember. Now uniform: ABSENT means none, every other wrong type declines."""
    out = _validate([_node(**{field: value})])
    assert isinstance(out, Decline)
    assert out.reason == REASON_MALFORMED
    assert field in out.detail
    assert type(value).__name__ in out.detail  # the reason names the type it got


@pytest.mark.parametrize("field", ["feeds_from", "consumes", "output"])
def test_an_absent_collection_field_is_still_the_empty_statement(field: str) -> None:
    """The other half of the uniform rule: a node that simply does not declare the
    field (or declares it null) is well-formed — a node with no edges, no consumes or
    no outputs is a legitimate DAG node, and a real model omits empty fields."""
    node = _node()
    del node[field]
    assert isinstance(_validate([node]), ExtractedCandidate)

    out = _validate([_node(**{field: None})])
    assert isinstance(out, ExtractedCandidate)
    assert getattr(out.payload.composes[0], field) in ((), {})


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        pytest.param("0", 0, id="plain-numeric-string"),
        pytest.param("  7  ", 7, id="whitespace-padded"),
        pytest.param("+3", 3, id="explicit-plus"),
        pytest.param(2.0, 2, id="integral-float"),
        pytest.param(1e3, 1000, id="exponent-float"),
        pytest.param("1_0", 10, id="python-underscore-numeral"),
        pytest.param("٢", 2, id="arabic-indic-digit"),
        pytest.param(-1, -1, id="negative"),
        pytest.param(10**30, 10**30, id="astronomically-large"),
    ],
)
def test_int_coercion_is_identical_in_the_gate_and_in_compose_nodes(
    order: Any, expected: int
) -> None:
    """The gate's stated precondition is that `_node_index` and `_compose_nodes`'
    bare `int(...)` agree on every value the gate lets through. They do — including
    the surprising ones: Python's `int()` accepts `"1_0"` (→ 10) and unicode decimal
    digits, and neither negative nor astronomically large orders are bounded here.
    None of those are wrong (`check_dag`/`Node.parse` accept any int order and the
    node CAP is on the node COUNT), but they are the gate's real domain, so pin it."""
    out = _validate([_node(order=order)])
    assert isinstance(out, ExtractedCandidate)
    assert out.payload.composes[0].order == expected


def test_a_deeply_nested_blob_where_a_node_belongs_declines() -> None:
    blob: Any = {"a": [{"b": [{"c": {"order": 0}}]}]}
    out = _validate([blob])
    assert isinstance(out, Decline)
    assert out.reason == REASON_MALFORMED
    assert "no 'order'" in out.detail


def test_a_unicode_placeholder_key_survives_as_a_placeholder_name() -> None:
    """Not a defect — a non-ASCII `consumes`/`output` KEY is a legal placeholder name
    and is carried through verbatim. Pinned so a future key-charset restriction is a
    deliberate change, not a silent one."""
    out = _validate([_node(consumes={"café": "$0"}, output={"сум": "scalar"})])
    assert isinstance(out, ExtractedCandidate)
    assert out.payload.composes[0].consumes == {"café": "$0"}
    assert out.payload.composes[0].output == {"сум": "scalar"}


def test_two_byte_identical_node_objects_reach_the_review_valve_not_a_hard_reject() -> None:
    """Duplicate nodes are a DAG-semantics fault: `check_dag` fails them to review
    (the human valve). A `malformed_candidate` here would bypass that."""
    node = _node(0)
    out = _validate([node, dict(node)])
    assert isinstance(out, ExtractedCandidate)
    assert [n.order for n in out.payload.composes] == [0, 0]


def test_a_node_count_far_over_the_dag_cap_is_not_a_malformed_reject() -> None:
    """Same split: `_MAX_NODES` (16) belongs to `check_dag`/the loader, not the
    structural gate. 64 nodes extract cleanly and fail to review downstream."""
    from data_agent.learning.generalize.validate import check_dag

    composes = [_node(i, source_tool_call_ref="tc1") for i in range(64)]
    out = _validate(composes)
    assert isinstance(out, ExtractedCandidate)
    assert check_dag([n.to_doc() for n in out.payload.composes]) is False
