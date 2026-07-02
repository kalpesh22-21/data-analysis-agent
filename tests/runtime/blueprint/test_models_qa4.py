"""QA4 Layer-1: models.py parse-layer robustness (runblueprint-design §2.1).

Adversarial extensions of `test_models.py`: wrong JSON types at every field, a
node referencing an undefined node / self-edge / duplicate order (these DAG-shape
invariants are enforced by the LOADER, not the parse layer — pinned here), a
deeply-nested composes list (no crash at parse), and the `result_grain` shape
matrix. The parse layer is STRUCTURAL + fail-loud; it does NOT do DAG-graph
validation (that is `corpus_loader._validate_dag_structure`).

ADD-only; does not modify the reviewer-owned `test_models.py`.
"""

from __future__ import annotations

import pytest

from data_agent.runtime.blueprint.models import (
    Blueprint,
    BlueprintParseError,
    Node,
    ResultGrain,
    SlotSpec,
)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"slots": ["not-a-dict"]},
        {"slots": [{"name": "s", "type": "string", "required": "yes"}]},  # bad bool
        {"slots": [{"name": "s", "type": "string", "binds_to": 5}]},
        {"composes": "not-a-list"},
        {"composes": [{"order": "one"}]},  # non-int order
        {"uses_rules": {"not": "a list"}},
        {"sql_template": 123},
        {"resolves": {"k": 9}},
        {"resolves": [1, 2]},
    ],
)
def test_wrong_types_at_every_field_rejected(kwargs: dict) -> None:
    with pytest.raises(BlueprintParseError):
        Blueprint.parse(id="b", intent="x", **kwargs)


def test_node_feeds_from_bool_rejected() -> None:
    # bool is an int subclass — feeds_from must be TRUE integers, not booleans.
    with pytest.raises(BlueprintParseError):
        Node.parse({"order": 1, "feeds_from": [True], "output": {"a": "scalar"}})


def test_node_order_bool_rejected() -> None:
    with pytest.raises(BlueprintParseError):
        Node.parse({"order": True, "output": {"a": "scalar"}})


def test_node_consumes_wrong_type_rejected() -> None:
    with pytest.raises(BlueprintParseError):
        Node.parse({"order": 1, "consumes": ["x"], "output": {"a": "scalar"}})


def test_parse_layer_does_not_reject_self_edge_or_dup_order() -> None:
    # PIN: the parse layer records shape faithfully; a self-edge and a duplicate
    # `order` PARSE cleanly here — the LOADER's `_validate_dag_structure` is what
    # rejects them (see test_corpus_loader_dag_qa4). This pins the layering.
    bp = Blueprint.parse(
        id="b",
        intent="x",
        composes=[
            {"order": 1, "feeds_from": [1], "output": {"a": "scalar"}},  # self-edge
            {"order": 1, "feeds_from": [], "output": {"b": "scalar"}},  # dup order
        ],
    )
    assert len(bp.composes) == 2
    assert bp.composes[0].feeds_from == (1,)


def test_parse_layer_does_not_reject_undefined_feeds_from() -> None:
    # PIN: a dangling `feeds_from` also parses — the loader rejects it, not models.
    bp = Blueprint.parse(
        id="b",
        intent="x",
        composes=[{"order": 1, "feeds_from": [99], "output": {"a": "scalar"}}],
    )
    assert bp.composes[0].feeds_from == (99,)


def test_deeply_nested_composes_list_parses_without_crash() -> None:
    # A large flat composes list parses in a comprehension (no per-node recursion).
    nodes = [{"order": i, "output": {"a": "scalar"}} for i in range(1, 2001)]
    bp = Blueprint.parse(id="b", intent="x", composes=nodes)
    assert len(bp.composes) == 2000


def test_result_grain_none_defaults_verifiable_empty() -> None:
    rg = ResultGrain.parse(None)
    assert rg.columns == () and rg.verifiable is True


def test_result_grain_dict_bad_verifiable_rejected() -> None:
    with pytest.raises(BlueprintParseError):
        ResultGrain.parse({"columns": ["Department"], "verifiable": "yes"})


def test_result_grain_empty_string_column_rejected() -> None:
    with pytest.raises(BlueprintParseError):
        ResultGrain.parse(["Department", ""])


def test_slot_enum_values_non_string_rejected() -> None:
    with pytest.raises(BlueprintParseError):
        SlotSpec.parse({"name": "s", "type": "enum", "enum_values": ["A", 5]})


def test_is_single_node_requires_template_and_no_composes() -> None:
    leaf = Blueprint.parse(id="b", intent="x", sql_template="SELECT 1")
    assert leaf.is_single_node is True
    dag = Blueprint.parse(
        id="b",
        intent="x",
        sql_template="SELECT 1",
        composes=[{"order": 1, "output": {"a": "scalar"}}],
    )
    assert dag.is_single_node is False  # a template PLUS a DAG is not single-node
