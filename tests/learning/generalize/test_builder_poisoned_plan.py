"""`generalize_blueprint` never raises for a bad candidate — the in-band contract.

S4's whole failure posture (module docstring, `builder.py`) is that every fault
produces a `BlueprintGeneralization` stamped `fail_to_review`, never an exception:
S4 runs inside the same consumer as S3, so a raise there loses the message (un-acked
→ reclaim → dead-letter) instead of recording a reviewable verdict.

The input is not merely LLM output, it is REHYDRATED LLM output: S4 reads
`env.payload` straight out of the candidate store, so it must hold for a poisoned or
legacy record even though S3's gate would have declined the same shape on the way in
(the `_MAX_NODES` cap in `validate.py` already cites that threat model).

That contract was broken on the composite path: `sorted(composes, key=...)`, the
`when` scan and `int(node["order"])` all read the raw plan BEFORE `check_dag` — the
gate that answers whether those reads are safe — and `sql_by_ref.get(ref)` hashed an
untrusted ref. The fix moves `check_dag` to the top of `_generalize_composite` and
shape-gates the two plan collections once in `generalize_blueprint`, so the raw plan
is never read before it is validated.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.learning.candidate.generalization import BlueprintGeneralization
from data_agent.learning.generalize.builder import generalize_blueprint
from data_agent.learning.generalize.validate import REASON_DAG, REASON_UNREWRITABLE

from .helpers import CATALOG, composite_sql_by_ref, load_plan, single_sql_by_ref


def _composite(**payload_overrides: Any) -> dict[str, Any]:
    payload = load_plan()["composite"]
    payload.update(payload_overrides)
    return payload


def _with_node(**node_overrides: Any) -> dict[str, Any]:
    """The frozen composite plan with BOTH nodes carrying *node_overrides*."""
    payload = load_plan()["composite"]
    for node in payload["composes"]:
        node.update(node_overrides)
    return payload


def _generalize(payload: dict[str, Any]) -> BlueprintGeneralization:
    return generalize_blueprint(payload, composite_sql_by_ref(), CATALOG)


# --- the happy paths still work (non-vacuity) ---------------------------------


def test_the_frozen_composite_plan_still_generalizes_ok() -> None:
    gen = _generalize(load_plan()["composite"])
    assert gen.static_validation.outcome == "ok"
    assert gen.static_validation.dag_ok is True
    assert len(gen.node_templates) == 2


def test_the_frozen_single_plan_still_generalizes_ok() -> None:
    gen = generalize_blueprint(load_plan()["single"], single_sql_by_ref(), CATALOG)
    assert gen.static_validation.outcome == "ok"


# --- the composite path: raw-plan reads that used to raise --------------------


@pytest.mark.parametrize(
    ("payload", "reason", "was"),
    [
        pytest.param(
            _with_node(order="1"),
            REASON_DAG,
            "TypeError: '<' not supported between 'str' and 'int' in sorted()",
            id="orders-of-mixed-types",
        ),
        pytest.param(
            _composite(composes=[{"node_kind": "query", "output": {}}]),
            REASON_DAG,
            "KeyError('order') past the `except RewriteError`",
            id="node-with-no-order",
        ),
        pytest.param(
            _composite(composes=["not-a-node"]),
            REASON_DAG,
            "AttributeError on node.get(...) in the `when` scan",
            id="node-is-not-an-object",
        ),
        pytest.param(
            _composite(composes=[None, {"order": 0, "output": {}}]),
            REASON_DAG,
            "AttributeError on None.get(...)",
            id="node-is-null",
        ),
        pytest.param(
            _with_node(source_tool_call_ref=["tc1"]),
            REASON_UNREWRITABLE,
            "TypeError: unhashable type 'list' in sql_by_ref.get(ref)",
            id="unhashable-source-tool-call-ref",
        ),
        pytest.param(
            _composite(composes={"0": {"order": 0}}),
            REASON_DAG,
            "list(dict) yielded KEY STRINGS as nodes → AttributeError downstream",
            id="composes-is-a-map",
        ),
        pytest.param(
            _composite(composes=7),
            REASON_DAG,
            "TypeError: 'int' object is not iterable in list()",
            id="composes-is-a-scalar",
        ),
        pytest.param(
            _composite(parameterization={"department": {}}),
            REASON_UNREWRITABLE,
            "AttributeError on param.get('role') over a dict's key strings",
            id="parameterization-is-a-map",
        ),
        pytest.param(
            _composite(parameterization=["department"]),
            REASON_UNREWRITABLE,
            "AttributeError on str.get('role') in rewrite_sql_to_template",
            id="parameterization-entry-is-a-string",
        ),
        pytest.param(
            _composite(parameterization=[{"role": "slot", "locator": ["department"]}]),
            REASON_UNREWRITABLE,
            "AttributeError on locator.get('column') in rewrite_sql_to_template",
            id="locator-is-a-list",
        ),
        pytest.param(
            _composite(
                parameterization=[
                    {
                        "role": "slot",
                        "locator": {"table": "t", "column": "department", "value": "0420"},
                        "slot": ["department"],
                    }
                ]
            ),
            REASON_UNREWRITABLE,
            "AttributeError on slot.get('name') in rewrite_sql_to_template",
            id="slot-is-a-list",
        ),
    ],
)
def test_a_poisoned_plan_fails_to_review_instead_of_raising(
    payload: dict[str, Any], reason: str, was: str
) -> None:
    # *was* names the pre-fix exception, so a regression report says which raw-plan
    # read came back rather than just "it raised".
    gen = _generalize(payload)
    assert gen.static_validation.outcome == "fail_to_review", was
    assert gen.static_validation.reason == reason
    # A fail-to-review carries no guessed template and no hash input (S6 fail-soft).
    assert gen.sql_template is None
    assert gen.node_templates == ()
    assert gen.canonical_ast_norm == ""


def test_a_composite_declaring_no_nodes_is_a_dag_fault_not_a_clean_pass() -> None:
    """A plan that says `kind: composite` and declares no nodes used to generalize to
    `outcome="ok"` with ZERO templates — nothing failed, because every check is
    vacuously true over an empty node list. It would then land as a blueprint with
    neither a `sql_template` nor a `composes`, which the corpus loader refuses: a
    landing_failed instead of the review it should have had."""
    gen = _generalize(_composite(composes=[]))
    assert gen.static_validation.outcome == "fail_to_review"
    assert gen.static_validation.reason == REASON_DAG


def test_the_dag_gate_runs_before_the_when_scan() -> None:
    """Ordering matters, not just presence: the `when` scan is itself a raw-plan read
    (`node.get`), so a structurally broken node must be caught BEFORE it — otherwise
    the gate is in the right function but still behind the crash."""
    gen = _generalize(_composite(composes=[{"order": 0, "when": "x > 0"}, "junk"]))
    assert gen.static_validation.reason == REASON_DAG  # not when_bearing_composite


# --- the parameterization gate, checked against its DERIVATION ----------------
#
# `_plan_params_ok` claims to guarantee exactly what the readers of
# `parameterization` need. These tests are that claim, one row of its table at a
# time: each case is a field whose WRONG TYPE crashes a specific reader through a
# specific operation (hash, concatenate, compare), and each must come back as a
# verdict instead. Patching only the fields someone happened to notice is what left
# this gate false-by-luck twice; the value here is the coverage of the derivation.


def _param(**overrides: Any) -> dict[str, Any]:
    param: dict[str, Any] = {
        "locator": {"table": "payroll.payroll_fact", "column": "department", "value": "0420"},
        "role": "slot",
        "slot": {"name": "department", "binds_to": "payroll.payroll_fact.department"},
    }
    param.update(overrides)
    return param


@pytest.mark.parametrize(
    ("param", "reader", "operation"),
    [
        pytest.param(
            {"role": "rule", "rule_id": ["r1"]},
            "_uses_rules",
            "hash — `{p['rule_id'] for p in …}`",
            id="rule_id-unhashable",
        ),
        pytest.param(
            {"role": "rule", "rule_id": 7},
            "_uses_rules",
            "`<` — sorted() over rule ids of mixed types",
            id="rule_id-not-a-string",
        ),
        pytest.param(
            _param(slot={"name": "department", "binds_to": ["payroll.payroll_fact.department"]}),
            "_slot_binds_to → all(b in uses_set …)",
            "hash — membership against the uses set",
            id="binds_to-unhashable",
        ),
        pytest.param(
            _param(slot={"name": ["department"]}),
            "rewrite_sql_to_template",
            'concatenate — `"{" + name + ": }"`',
            id="slot-name-not-a-string",
        ),
        pytest.param(
            _param(slot={"name": 7}),
            "rewrite_sql_to_template",
            'concatenate — a TRUTHY non-string passed the `if not name` guard',
            id="slot-name-truthy-non-string",
        ),
        pytest.param(
            _param(locator={"table": "t", "column": ["department"], "value": "0420"}),
            "rewrite._find_literal",
            "hash — `column not in {col.name for col …}`",
            id="locator-column-unhashable",
        ),
    ],
)
def test_each_derived_field_declines_instead_of_crashing_its_reader(
    param: dict[str, Any], reader: str, operation: str
) -> None:
    gen = _generalize(_composite(parameterization=[param]))
    assert gen.static_validation.outcome == "fail_to_review", f"{reader} / {operation}"
    assert gen.static_validation.reason == REASON_UNREWRITABLE


def test_the_error_path_does_not_crash_on_a_poisoned_rule_id() -> None:
    """`_uses_rules` runs inside `_fail_to_review`, so an unhashable `rule_id` used to
    crash candidates that were ALREADY being declined — the rejection blowing up on
    its way out. The DAG here is broken too, so this exercises the fail path
    specifically rather than the success path."""
    gen = _generalize(
        _composite(composes=["junk"], parameterization=[{"role": "rule", "rule_id": ["r1"]}])
    )
    assert gen.static_validation.outcome == "fail_to_review"
    assert gen.uses_rules == ()


@pytest.mark.parametrize(
    ("param", "why"),
    [
        pytest.param(
            {"role": ["slot"]},
            "`role` is only ever `==`-compared — total on every type, so unconstrained",
            id="role-is-a-list",
        ),
        pytest.param(
            _param(locator={"table": "t", "column": "department", "value": ["0420"]}),
            "`locator.value` is `str()`-ed before use — total, so unconstrained",
            id="value-is-a-list",
        ),
        pytest.param(
            {"role": "inline", "why": "defines the metric"},
            "no locator, no slot, no rule_id: absent is always legal",
            id="all-optional-fields-absent",
        ),
    ],
)
def test_the_gate_does_not_over_reject_what_no_reader_can_choke_on(
    param: dict[str, Any], why: str
) -> None:
    """The other half of "derived": a gate that rejects MORE than the readers need
    turns working candidates into review queue. Each of these reaches the real
    checks (it is not a `fail_to_review` for a malformed plan)."""
    gen = _generalize(_composite(parameterization=[param]))
    assert gen.static_validation.outcome == "ok", why


def test_a_wellformed_plan_still_carries_rules_and_binds_through() -> None:
    """Non-vacuity for the gate: the fields it type-checks still arrive at the
    readers that consume them."""
    gen = _generalize(
        _composite(
            parameterization=[
                _param(),
                {
                    "role": "rule",
                    "locator": {"table": "t", "column": "record_type", "value": "EARNING"},
                    "rule_id": "rule.earning_record_type",
                },
            ]
        )
    )
    assert gen.uses_rules == ("rule.earning_record_type",)
    assert gen.static_validation.binds_to_subset_uses is True


# --- the SINGLE path carries the same untrusted-ref read ----------------------


@pytest.mark.parametrize(
    ("source_refs", "was"),
    [
        pytest.param(
            [["tc1"]], "TypeError: unhashable type 'list' in sql_by_ref.get(ref)", id="unhashable"
        ),
        pytest.param(7, "TypeError: 'int' object is not iterable", id="not-a-list"),
        pytest.param({"tc1": 1}, "iterating a dict yields keys, not refs", id="a-map"),
    ],
)
def test_a_poisoned_source_ref_list_fails_to_review_on_the_single_path(
    source_refs: Any, was: str
) -> None:
    """`_accepted_sql_for_single` does the same `sql_by_ref.get(ref)` the composite
    loop does, so it carried the same unhashable-ref crash — fixed in the same pass
    rather than left as the one remaining instance."""
    payload = load_plan()["single"]
    payload["source_tool_call_refs"] = source_refs
    gen = generalize_blueprint(payload, single_sql_by_ref(), CATALOG)
    assert gen.static_validation.outcome == "fail_to_review", was
    assert gen.static_validation.reason == REASON_UNREWRITABLE


# --- the error path must not have its own error path --------------------------


@pytest.mark.parametrize(
    "result_signature",
    [
        pytest.param(["share"], id="signature-is-a-list"),
        pytest.param("share", id="signature-is-a-string"),
        pytest.param({"grain": ["department"]}, id="grain-is-a-list"),
        pytest.param({"grain": "department"}, id="grain-is-a-string"),
        pytest.param({"grain": {"columns": "department"}}, id="columns-is-a-string"),
        pytest.param({"grain": {"columns": None}}, id="columns-is-null"),
    ],
)
def test_a_poisoned_result_signature_degrades_to_an_empty_grain(
    result_signature: Any,
) -> None:
    """`_result_grain` runs on EVERY path INCLUDING `_fail_to_review`, so an
    `AttributeError` there would turn a clean in-band rejection into the exception the
    rejection existed to avoid. Exercised through the fail path for that reason.

    `columns-is-a-string` is the sharp one: `tuple("department")` would have produced
    a 10-COLUMN grain of single characters, which is worse than empty — a bogus D56
    row-count check rather than a skipped one."""
    gen = _generalize(_composite(composes=["junk"], result_signature=result_signature))
    assert gen.static_validation.outcome == "fail_to_review"
    assert gen.result_grain.columns == ()


def test_a_wellformed_grain_still_survives_the_defensive_read() -> None:
    gen = _generalize(
        _composite(result_signature={"grain": {"columns": ["department"], "verifiable": True}})
    )
    assert gen.result_grain.columns == ("department",)
    assert gen.result_grain.verifiable is True
