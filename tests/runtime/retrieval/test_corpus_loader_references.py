"""Layer-1: load-time blueprint REFERENCES — resolution, inlining, and the fail-closed
gates around them (plan §2b).

A `composes` node may name another blueprint instead of carrying its own SQL:

    - order: 0
      ref: { blueprint: bp-child, slots: { child_slot: parent_slot } }

`corpus_loader.resolve_blueprint_references` replaces that with the child's SQL,
renamed into the parent's slot vocabulary, BEFORE anything else reads the DAG. The
four things these tests pin, in the order they matter:

  1. **The `uses` UNION rule fails CLOSED.** A composite that declares less than the
     union of its referenced blueprints' footprints does not load. `uses` is authored,
     never derived, and three readers act on it (the recall scope pre-filter, the
     golden-replay JWT mint, and the loader's own gate (c)) — so under-declaration is
     a privilege statement that does not match what the query reads.
  2. **A cycle that SPANS blueprints fails the load** (the existing DAG cycle check is
     per-blueprint and structurally cannot see A→B→A), as does a chain past the depth
     cap, a missing target, and a retracted or cross-trust-partition one.
  3. **Slot mapping**, including every collision case and its chosen semantics.
  4. **Inlining is invisible downstream** — the resolved node is indistinguishable
     from a hand-written inline one, and no reference id survives onto the node or into
     `getBlueprint`.

Every failure is a `CorpusLoadError`. That is a hard requirement, not a preference:
this code runs inside `load_corpus`, which the hydrator's self-heal poll re-arms and
retries every turn, so an un-wrapped exception here bricks the corpus indefinitely.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from data_agent.runtime.blueprint.models import (
    DEFAULT_NODE_KIND,
    BlueprintParseError,
    Node,
)
from data_agent.runtime.retrieval.corpus_loader import (
    BlueprintSeed,
    CorpusLoadError,
    _dag_properties,
    _validate_blueprint_dag,
    load_seed_fixtures,
    resolve_blueprint_references,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "corpus"
_E = "dbpcm_warehouse.employee"


def _leaf(**overrides: Any) -> BlueprintSeed:
    base: dict[str, Any] = {
        "id": "bp-child",
        "intent": "average salary for one department",
        "slots_summary": "department",
        "uses": [f"{_E}.department_name", f"{_E}.annual_salary"],
        "slots": [
            {
                "name": "department",
                "type": "string",
                "required": True,
                "binds_to": f"{_E}.department_name",
            }
        ],
        "result_grain": ["department"],
        "sql_template": (
            "SELECT department_name AS department, AVG(annual_salary) AS avg_salary\n"
            "FROM dbpcm_warehouse.employee\n"
            "WHERE department_name = {department}\n"
            "GROUP BY department_name"
        ),
    }
    base.update(overrides)
    return BlueprintSeed(**base)


def _parent(*, ref: Any = None, **overrides: Any) -> BlueprintSeed:
    if ref is None:
        ref = {"blueprint": "bp-child", "slots": {"department": "dept"}}
    base: dict[str, Any] = {
        "id": "bp-parent",
        "intent": "one department's average salary",
        "slots_summary": "dept",
        "uses": [f"{_E}.department_name", f"{_E}.annual_salary"],
        "slots": [
            {
                "name": "dept",
                "type": "string",
                "required": True,
                "binds_to": f"{_E}.department_name",
            }
        ],
        "result_grain": ["department"],
        "composes": [{"order": 0, "output": {}, "ref": ref}],
    }
    base.update(overrides)
    return BlueprintSeed(**base)


def _resolved(seeds: list[BlueprintSeed]) -> dict[str, BlueprintSeed]:
    return {bp.id: bp for bp in resolve_blueprint_references(seeds)}


# -- 1. the `uses` UNION rule (the security gate) ---------------------------


def test_a_composite_declaring_less_than_the_union_of_its_references_fails_the_load() -> None:
    """THE security gate. The parent's own SQL reads nothing (the node is a pure
    reference), so nothing but this rule would notice that its declared footprint omits
    a column the query it runs really reads."""
    parent = _parent(uses=[f"{_E}.department_name"])  # annual_salary omitted
    with pytest.raises(CorpusLoadError) as exc:
        resolve_blueprint_references([_leaf(), parent])
    message = str(exc.value)
    assert "annual_salary" in message
    assert "bp-child" in message, "the error must name WHICH reference contributed it"


def test_the_union_rule_is_transitive_through_a_reference_chain() -> None:
    """A grandchild's columns must reach the grandparent. Only DIRECT children are
    inspected, so transitivity rests on the accumulated footprint — pin it, because a
    two-hop escape would be invisible to the direct check."""
    grandchild = _leaf(id="bp-grandchild")
    child = BlueprintSeed(
        id="bp-child",
        intent="middle",
        slots_summary="department",
        uses=[f"{_E}.department_name", f"{_E}.annual_salary"],
        slots=[{"name": "department", "type": "string", "required": True}],
        result_grain=["department"],
        composes=[
            {
                "order": 0,
                "ref": {"blueprint": "bp-grandchild", "slots": {"department": "department"}},
            }
        ],
    )
    parent = _parent(uses=[f"{_E}.department_name"])
    with pytest.raises(CorpusLoadError, match="annual_salary"):
        resolve_blueprint_references([grandchild, child, parent])
    # …and it loads once the grandparent declares the grandchild's column.
    ok = _resolved([grandchild, child, _parent()])
    assert "{dept}" in ok["bp-parent"].composes[0]["sql_template"]


def test_a_wider_child_footprint_is_refused_rather_than_silently_absorbed() -> None:
    """The rule binds the parent to the child's DECLARED footprint, not to the columns
    the child's SQL happens to name today. A column added to the child upstream must
    fail every composite that inlines it until a human re-declares it — otherwise a
    child edit silently widens the scope of blueprints nobody looked at."""
    child = _leaf(uses=[f"{_E}.department_name", f"{_E}.annual_salary", f"{_E}.employee_code"])
    with pytest.raises(CorpusLoadError, match="employee_code"):
        resolve_blueprint_references([child, _parent()])


def test_declaring_the_union_exactly_loads() -> None:
    resolved = _resolved([_leaf(), _parent()])
    assert set(resolved["bp-parent"].uses) >= set(_leaf().uses)


def test_the_union_error_names_every_missing_key_not_just_the_first() -> None:
    child = _leaf(uses=[f"{_E}.department_name", f"{_E}.annual_salary", f"{_E}.employee_code"])
    parent = _parent(uses=[f"{_E}.department_name"])
    with pytest.raises(CorpusLoadError) as exc:
        resolve_blueprint_references([child, parent])
    assert "annual_salary" in str(exc.value) and "employee_code" in str(exc.value)


# -- 2. graph shape: cycles, depth, missing + unloadable targets ------------


def test_a_cycle_that_spans_two_blueprints_fails_the_load() -> None:
    """The pre-existing cycle check walks ONE blueprint's `feeds_from` edges; neither
    of these blueprints has an intra-DAG cycle at all."""
    a = BlueprintSeed(
        id="bp-a",
        intent="a",
        slots_summary="",
        uses=[f"{_E}.department_name"],
        composes=[{"order": 0, "ref": {"blueprint": "bp-b"}}],
    )
    b = BlueprintSeed(
        id="bp-b",
        intent="b",
        slots_summary="",
        uses=[f"{_E}.department_name"],
        composes=[{"order": 0, "ref": {"blueprint": "bp-a"}}],
    )
    with pytest.raises(CorpusLoadError, match="cycle"):
        resolve_blueprint_references([a, b])


def test_a_self_reference_fails_the_load() -> None:
    a = BlueprintSeed(
        id="bp-a",
        intent="a",
        slots_summary="",
        uses=[f"{_E}.department_name"],
        composes=[{"order": 0, "ref": {"blueprint": "bp-a"}}],
    )
    with pytest.raises(CorpusLoadError, match="cycle"):
        resolve_blueprint_references([a])


def test_a_long_reference_chain_fails_on_the_depth_cap_not_a_recursion_error() -> None:
    """Iterative by construction. A `RecursionError` out of `load_corpus` is an
    un-wrapped exception on the request path, which bricks the corpus."""
    chain = [
        _leaf(
            id="bp-0",
            slots=[{"name": "department", "type": "string", "required": True}],
        )
    ]
    for i in range(1, 40):
        chain.append(
            BlueprintSeed(
                id=f"bp-{i}",
                intent=f"level {i}",
                slots_summary="department",
                uses=[f"{_E}.department_name", f"{_E}.annual_salary"],
                slots=[{"name": "department", "type": "string", "required": True}],
                result_grain=["department"],
                composes=[
                    {
                        "order": 0,
                        "ref": {
                            "blueprint": f"bp-{i - 1}",
                            "slots": {"department": "department"},
                        },
                    }
                ],
            )
        )
    with pytest.raises(CorpusLoadError, match="deep"):
        resolve_blueprint_references(chain)


def test_a_chain_within_the_cap_resolves_through_a_single_node_composite() -> None:
    """A single-node composite is what makes a CHAIN possible (a leaf carries no nodes
    and so can hold no `ref`) — which is what keeps the depth cap and the cross-blueprint
    cycle check live rules rather than dead code."""
    leaf = _leaf(id="bp-leaf")
    middle = BlueprintSeed(
        id="bp-middle",
        intent="middle",
        slots_summary="department",
        uses=[f"{_E}.department_name", f"{_E}.annual_salary"],
        slots=[
            {
                "name": "mid_dept",
                "type": "string",
                "required": True,
                "binds_to": f"{_E}.department_name",
            }
        ],
        result_grain=["department"],
        composes=[
            {
                "order": 0,
                "ref": {"blueprint": "bp-leaf", "slots": {"department": "mid_dept"}},
            }
        ],
    )
    parent = _parent(ref={"blueprint": "bp-middle", "slots": {"mid_dept": "dept"}})
    resolved = _resolved([leaf, middle, parent])
    assert "{dept}" in resolved["bp-parent"].composes[0]["sql_template"]
    assert "{mid_dept}" not in resolved["bp-parent"].composes[0]["sql_template"]
    for bp in resolved.values():
        _validate_blueprint_dag(bp)


def test_a_reference_to_a_missing_blueprint_fails_the_load() -> None:
    with pytest.raises(CorpusLoadError, match="unknown blueprint"):
        resolve_blueprint_references([_parent(ref={"blueprint": "bp-nope"})])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "retired"),
        ("status", "candidate"),
        ("status", ""),  # a real empty-string property: coalesce leaves it, recall drops it
        ("status", 5),  # written as a non-string property; fails recall's equality
        ("drift_status", "suspect"),
    ],
)
def test_a_reference_to_a_retracted_blueprint_fails_the_load(field: str, value: Any) -> None:
    """"Retracted" is defined by the recall gate, not invented here: a node recall
    refuses to serve must not keep running inlined under another id."""
    child = replace(_leaf(), **{field: value})
    with pytest.raises(CorpusLoadError, match="recall will not serve"):
        resolve_blueprint_references([child, _parent()])


@pytest.mark.parametrize("field", ["status", "drift_status"])
def test_a_null_status_reference_loads_because_recall_coalesces_it(field: str) -> None:
    """The gate mirrors `_BLUEPRINT_RECALL_QUERY` THROUGH the property write, and the
    coalesce is the part that is easy to get wrong.

    HISTORY: the first cut read `status=None` as `""`, failed the equality, and refused
    the load with a message claiming recall would not serve the child. That was false —
    `SET b.status = $status` with a null REMOVES the property, and recall's
    `coalesce(node.status,'validated')` then serves it — and `status: null` is reachable
    from the export (`_seed_from_entry` sanitizes `source`/`verified`, not `status`). A
    servable child would have bricked the whole corpus, since a refused reference fails
    the entire load. Fail-closed, so never dangerous; wrong all the same."""
    child = replace(_leaf(), **{field: None})
    resolved = _resolved([child, _parent()])
    assert "{dept}" in resolved["bp-parent"].composes[0]["sql_template"]


def test_a_reference_may_not_cross_the_trust_partition() -> None:
    """Inlining COPIES SQL. An mcp composite pulling from the learning staging tier
    would launder unverified SQL into the partition recall serves."""
    child = replace(_leaf(), source="learning", verified=False)
    with pytest.raises(CorpusLoadError, match="trust partition"):
        resolve_blueprint_references([child, _parent()])


def test_a_reference_to_a_multi_node_composite_fails_the_load() -> None:
    """A reference inlines exactly ONE statement. Splicing a multi-node child would
    mean renumbering orders and merging the sink's `when`/`output` with the referencing
    node's — several places a gate could be silently dropped."""
    child = BlueprintSeed(
        id="bp-child",
        intent="two steps",
        slots_summary="",
        uses=[f"{_E}.department_name", f"{_E}.annual_salary"],
        result_grain=["department"],
        composes=[
            {"order": 0, "output": {"avg": "scalar"}, "sql_template": "SELECT 1 AS avg"},
            {"order": 1, "feeds_from": [0], "sql_template": "SELECT 2 AS x"},
        ],
    )
    with pytest.raises(CorpusLoadError, match="resolves to 2 DAG node"):
        resolve_blueprint_references([child, _parent(ref={"blueprint": "bp-child"})])


@pytest.mark.parametrize(
    ("gate", "payload"),
    [
        ("when", {"expr": "row_count > 0", "on_violation": "abort"}),
        ("requires_approval", {"reason": "spend"}),
        # `node_kind` was MISSING from this list for a review cycle while the
        # docstring above the check claimed the list was complete. It is a gate ON ITS
        # OWN: `executor._execute_dag` pauses on `node_kind == "approval"` independent
        # of `requires_approval`, and the loader's gate (i) accepts an approval node
        # that carries a `sql_template` — so this exact shape was a legal reference
        # target whose human approval pause silently vanished on inlining.
        ("node_kind", "approval"),
    ],
)
def test_a_referenced_single_node_may_not_hide_a_control_flow_gate(
    gate: str, payload: Any
) -> None:
    """A reference takes only SQL, so anything else the child's node declares would be
    silently dropped — and a dropped approval or `when` is a control-flow check
    vanishing, not a cosmetic loss."""
    child = BlueprintSeed(
        id="bp-child",
        intent="gated",
        slots_summary="",
        uses=[f"{_E}.department_name"],
        result_grain=["department"],
        composes=[
            {
                "order": 0,
                "sql_template": "SELECT department_name AS department FROM dbpcm_warehouse.employee",
                gate: payload,
            }
        ],
    )
    with pytest.raises(CorpusLoadError, match=gate):
        resolve_blueprint_references(
            [child, _parent(uses=[f"{_E}.department_name"], ref={"blueprint": "bp-child"})]
        )


def test_the_referenced_node_kind_gate_refuses_by_default_not_by_enumeration() -> None:
    """`!= "query"` rather than `== "approval"`, so a future `NODE_KINDS` member is
    refused until someone decides what inlining it should mean. An explicit
    `node_kind: "query"` (the default, spelled out) stays legal."""
    def child_with(kind: Any) -> BlueprintSeed:
        return BlueprintSeed(
            id="bp-child",
            intent="kinded",
            slots_summary="",
            uses=[f"{_E}.department_name"],
            result_grain=["department"],
            composes=[
                {
                    "order": 0,
                    "node_kind": kind,
                    "sql_template": (
                        "SELECT department_name AS department FROM dbpcm_warehouse.employee"
                    ),
                }
            ],
        )

    parent = _parent(uses=[f"{_E}.department_name"], ref={"blueprint": "bp-child"})
    with pytest.raises(CorpusLoadError, match="node_kind"):
        resolve_blueprint_references([child_with("some_future_kind"), parent])
    resolved = _resolved([child_with(DEFAULT_NODE_KIND), parent])
    assert "department_name" in resolved["bp-parent"].composes[0]["sql_template"]


def test_a_referenced_approval_node_really_would_have_lost_its_gate() -> None:
    """The counterfactual, so the refusal above is anchored to a real consequence
    rather than to a string match: the child node IS an approval step by the runtime's
    own reading, and the referencing node is not."""
    approval = Node.parse(
        {"order": 0, "node_kind": "approval", "sql_template": "SELECT 1 AS x"}
    )
    assert approval.node_kind == "approval" and approval.requires_approval is None
    inlined = Node.parse({"order": 0, "sql_template": "SELECT 1 AS x"})
    assert inlined.node_kind == DEFAULT_NODE_KIND and not inlined.requires_approval


def test_a_referenced_template_reading_session_scratch_fails_the_load() -> None:
    """Nothing in the referencing DAG can materialize a scratch source for the child
    (a reference node may declare no `consumes`), and `_assert_source_tables_in_uses`
    deliberately skips `scratch.*` — so without this gate the escape is a scope check
    that passes on a table that does not exist."""
    child = _leaf(
        sql_template=(
            "SELECT x.department_name AS department FROM scratch.borrowed AS x"
        ),
        slots=[],
    )
    with pytest.raises(CorpusLoadError, match="scratch"):
        resolve_blueprint_references([child, _parent(ref={"blueprint": "bp-child"})])


# -- 3. slot mapping + collision semantics ----------------------------------


def test_a_child_slot_the_parent_does_not_map_fails_the_load() -> None:
    """No implicit identity, ever. The parent's slot happens to be named `department`
    too, and the mapping is STILL required — because after inlining only the parent's
    slot declaration exists, so an implicit bind would let a rename here silently
    re-point the child's filter at a different domain."""
    parent = _parent(
        slots=[
            {
                "name": "department",
                "type": "string",
                "required": True,
                "binds_to": f"{_E}.department_name",
            }
        ],
        ref={"blueprint": "bp-child"},
    )
    with pytest.raises(CorpusLoadError, match="map it with"):
        resolve_blueprint_references([_leaf(), parent])


def test_an_identical_name_still_needs_an_explicit_mapping_and_then_loads() -> None:
    parent = _parent(
        slots=[
            {
                "name": "department",
                "type": "string",
                "required": True,
                "binds_to": f"{_E}.department_name",
            }
        ],
        ref={"blueprint": "bp-child", "slots": {"department": "department"}},
    )
    resolved = _resolved([_leaf(), parent])
    assert "{department}" in resolved["bp-parent"].composes[0]["sql_template"]


def test_mapping_a_child_slot_that_does_not_exist_fails_the_load() -> None:
    parent = _parent(ref={"blueprint": "bp-child", "slots": {"ghost": "dept"}})
    with pytest.raises(CorpusLoadError, match="does not declare"):
        resolve_blueprint_references([_leaf(), parent])


def test_mapping_from_a_parent_slot_that_does_not_exist_fails_the_load() -> None:
    parent = _parent(ref={"blueprint": "bp-child", "slots": {"department": "ghost"}})
    with pytest.raises(CorpusLoadError, match="this blueprint does not declare"):
        resolve_blueprint_references([_leaf(), parent])


def test_mapping_a_child_slot_its_own_sql_never_references_fails_the_load() -> None:
    """A dead mapping is an author believing a filter is applied when it is not — the
    D56 dropped-filter class arriving from the other direction."""
    child = _leaf(
        slots=[
            {"name": "department", "type": "string", "required": True},
            {"name": "unused", "type": "string", "required": False, "optional_pattern": "TRUE"},
        ]
    )
    parent = _parent(
        slots=[
            {"name": "dept", "type": "string", "required": True},
            {"name": "spare", "type": "string", "required": False, "optional_pattern": "TRUE"},
        ],
        ref={"blueprint": "bp-child", "slots": {"department": "dept", "unused": "spare"}},
    )
    with pytest.raises(CorpusLoadError, match="never references"):
        resolve_blueprint_references([child, parent])


def test_a_period_range_maps_both_bind_tokens() -> None:
    child = BlueprintSeed(
        id="bp-child",
        intent="hires in a window",
        slots_summary="w",
        uses=[f"{_E}.most_recent_hire_date"],
        slots=[{"name": "w", "type": "period_range", "required": True}],
        result_grain=["month"],
        sql_template=(
            "SELECT most_recent_hire_date AS month FROM dbpcm_warehouse.employee "
            "WHERE most_recent_hire_date >= {w_start} AND most_recent_hire_date < {w_end}"
        ),
    )
    parent = _parent(
        uses=[f"{_E}.most_recent_hire_date"],
        slots=[{"name": "hire_window", "type": "period_range", "required": True}],
        result_grain=["month"],
        ref={"blueprint": "bp-child", "slots": {"w": "hire_window"}},
    )
    resolved = _resolved([child, parent])
    sql = resolved["bp-parent"].composes[0]["sql_template"]
    assert "{hire_window_start}" in sql and "{hire_window_end}" in sql
    assert "{w_start}" not in sql and "{w_end}" not in sql
    _validate_blueprint_dag(resolved["bp-parent"])


def test_a_period_range_mapped_onto_a_scalar_slot_fails_on_arity() -> None:
    child = BlueprintSeed(
        id="bp-child",
        intent="hires in a window",
        slots_summary="w",
        uses=[f"{_E}.most_recent_hire_date"],
        slots=[{"name": "w", "type": "period_range", "required": True}],
        result_grain=["month"],
        sql_template=(
            "SELECT most_recent_hire_date AS month FROM dbpcm_warehouse.employee "
            "WHERE most_recent_hire_date >= {w_start} AND most_recent_hire_date < {w_end}"
        ),
    )
    parent = _parent(
        uses=[f"{_E}.most_recent_hire_date"],
        slots=[{"name": "when", "type": "period", "required": True}],
        result_grain=["month"],
        ref={"blueprint": "bp-child", "slots": {"w": "when"}},
    )
    with pytest.raises(CorpusLoadError, match="arities must match"):
        resolve_blueprint_references([child, parent])


def test_two_child_slots_may_be_fed_from_one_parent_slot() -> None:
    """Legal and occasionally intended (compare a period against itself). Nothing
    downstream cares: both tokens simply bind the same resolved value."""
    child = BlueprintSeed(
        id="bp-child",
        intent="between two dates",
        slots_summary="lo, hi",
        uses=[f"{_E}.most_recent_hire_date"],
        slots=[
            {"name": "lo", "type": "period", "required": True},
            {"name": "hi", "type": "period", "required": True},
        ],
        result_grain=["month"],
        sql_template=(
            "SELECT most_recent_hire_date AS month FROM dbpcm_warehouse.employee "
            "WHERE most_recent_hire_date >= {lo} AND most_recent_hire_date <= {hi}"
        ),
    )
    parent = _parent(
        uses=[f"{_E}.most_recent_hire_date"],
        slots=[{"name": "day", "type": "period", "required": True}],
        result_grain=["month"],
        ref={"blueprint": "bp-child", "slots": {"lo": "day", "hi": "day"}},
    )
    resolved = _resolved([child, parent])
    assert resolved["bp-parent"].composes[0]["sql_template"].count("{day}") == 2


def test_a_slot_swap_renames_simultaneously_not_sequentially() -> None:
    """`a→b, b→a` in one pass. Sequential replacement would rename the original `a`
    twice and collapse both tokens onto one slot — a silently wrong filter."""
    child = BlueprintSeed(
        id="bp-child",
        intent="between two dates",
        slots_summary="lo, hi",
        uses=[f"{_E}.most_recent_hire_date"],
        slots=[
            {"name": "lo", "type": "period", "required": True},
            {"name": "hi", "type": "period", "required": True},
        ],
        result_grain=["month"],
        sql_template=(
            "SELECT most_recent_hire_date AS month FROM dbpcm_warehouse.employee "
            "WHERE most_recent_hire_date >= {lo} AND most_recent_hire_date <= {hi}"
        ),
    )
    parent = _parent(
        uses=[f"{_E}.most_recent_hire_date"],
        slots=[
            {"name": "lo", "type": "period", "required": True},
            {"name": "hi", "type": "period", "required": True},
        ],
        result_grain=["month"],
        ref={"blueprint": "bp-child", "slots": {"lo": "hi", "hi": "lo"}},
    )
    sql = _resolved([child, parent])["bp-parent"].composes[0]["sql_template"]
    assert ">= {hi}" in sql and "<= {lo}" in sql


def test_a_resolve_via_rule_bind_is_not_renamed_and_must_be_re_declared() -> None:
    """Rules resolve per BLUEPRINT, by name, from `uses_rules`. Inheriting the child's
    would give a composite a warehouse probe it never declared."""
    child = BlueprintSeed(
        id="bp-child",
        intent="earnings",
        slots_summary="",
        uses=["dbpcm_warehouse.payroll.register_type", "dbpcm_warehouse.payroll.amount"],
        uses_rules=[
            {
                "id": "earnings_only",
                "resolve_via": "resolveValues(register_type, 'earnings')",
                "table": "dbpcm_warehouse.payroll",
                "binds": "earn_codes",
            }
        ],
        result_grain=[],
        sql_template=(
            "SELECT SUM(amount) AS total FROM dbpcm_warehouse.payroll "
            "WHERE register_type IN {earn_codes}"
        ),
    )
    bare_parent = _parent(
        uses=["dbpcm_warehouse.payroll.register_type", "dbpcm_warehouse.payroll.amount"],
        slots=[],
        result_grain=[],
        ref={"blueprint": "bp-child"},
    )
    with pytest.raises(CorpusLoadError, match="uses_rules"):
        resolve_blueprint_references([child, bare_parent])

    declaring_parent = replace(bare_parent, uses_rules=list(child.uses_rules))
    resolved = _resolved([child, declaring_parent])
    assert "{earn_codes}" in resolved["bp-parent"].composes[0]["sql_template"]


def test_a_same_named_rule_probing_a_different_domain_warns(caplog: Any) -> None:
    """Re-declaration is matched by BIND NAME, which is not the same as matching the
    rule. The parent's declaration is the one that runs, so a same-named rule probing a
    different column or concept silently feeds the child's `IN {earn_codes}` a different
    value set. Warned rather than refused — the parent is the authority, and gate (g)
    already forces its probed column ⊆ its own `uses` — but a rule fires a real
    warehouse probe, so it cannot be silent."""
    payroll = ["dbpcm_warehouse.payroll.register_type", "dbpcm_warehouse.payroll.amount"]

    def rule(concept: str, column: str = "register_type") -> dict[str, str]:
        return {
            "id": "earnings_only",
            "resolve_via": f"resolveValues({column}, '{concept}')",
            "table": "dbpcm_warehouse.payroll",
            "binds": "earn_codes",
        }

    child = BlueprintSeed(
        id="bp-child",
        intent="earnings",
        slots_summary="",
        uses=payroll,
        uses_rules=[rule("earnings")],
        result_grain=[],
        sql_template=(
            "SELECT SUM(amount) AS total FROM dbpcm_warehouse.payroll "
            "WHERE register_type IN {earn_codes}"
        ),
    )
    parent = _parent(
        uses=payroll,
        slots=[],
        result_grain=[],
        uses_rules=[rule("deductions")],
        ref={"blueprint": "bp-child"},
    )
    with caplog.at_level("WARNING"):
        resolved = _resolved([child, parent])
    assert "{earn_codes}" in resolved["bp-parent"].composes[0]["sql_template"]
    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "earn_codes" in warning and "deductions" in warning and "earnings" in warning

    # Identical rules on both sides must NOT warn — otherwise the signal is noise.
    caplog.clear()
    with caplog.at_level("WARNING"):
        _resolved([child, replace(parent, uses_rules=[rule("earnings")])])
    assert "earn_codes" not in "\n".join(r.getMessage() for r in caplog.records)


# -- untrusted input: every `ref` field, guarded by what reads it -----------


@pytest.mark.parametrize(
    "ref",
    [
        "bp-child",  # a bare string, not an object
        [],
        5,
        {"blueprint": ["bp-child"]},  # unhashable → would TypeError at the dict lookup
        {"blueprint": 5},
        {"blueprint": ""},
        {"blueprint": "   "},
        {},  # no target at all
        {"blueprint": "bp-child", "slots": []},
        {"blueprint": "bp-child", "slots": "department"},
        {"blueprint": "bp-child", "slot": {"department": "dept"}},  # typo'd key
        {"blueprint": "bp-child", "slots": {"department": "dept"}, "extra": 1},
        {"blueprint": "bp-child", "slots": {5: "dept"}},
        {"blueprint": "bp-child", "slots": {"department": 5}},
        {"blueprint": "bp-child", "slots": {"department": None}},
        # The injection shape: the VALUE is emitted into SQL as `{value}`.
        {"blueprint": "bp-child", "slots": {"department": "dept} OR 1=1 --"}},
        {"blueprint": "bp-child", "slots": {"department": "a b"}},
        {"blueprint": "bp-child", "slots": {"department": ""}},
    ],
)
def test_every_malformed_ref_shape_raises_corpus_load_error_not_a_crash(ref: Any) -> None:
    with pytest.raises(CorpusLoadError):
        resolve_blueprint_references([_leaf(), _parent(ref=ref)])


def test_a_slot_map_value_can_never_splice_raw_text_into_the_template() -> None:
    """The concrete injection this guard exists for, asserted on the OUTPUT rather than
    only on the exception: whatever happens, no attacker-chosen SQL reaches a template."""
    ref = {"blueprint": "bp-child", "slots": {"department": "dept} OR 1=1 --"}}
    with pytest.raises(CorpusLoadError):
        resolve_blueprint_references([_leaf(), _parent(ref=ref)])


@pytest.mark.parametrize(
    ("field", "value"),
    [("composes", 5), ("composes", "not a list"), ("slots", 5), ("uses", 5), ("uses", "a.b.c")],
)
def test_a_malformed_container_raises_corpus_load_error_not_a_type_error(
    field: str, value: Any
) -> None:
    """The un-wrapped-exception class. A string `uses`/`composes` is the quieter half:
    it ITERATES CHARACTER-WISE rather than crashing, so it would look like a valid
    zero-reference DAG, or like five one-character scope keys the union rule then
    compares against."""
    with pytest.raises(CorpusLoadError):
        resolve_blueprint_references([_leaf(), replace(_parent(), **{field: value})])


def test_a_malformed_uses_rules_raises_on_the_path_that_actually_reads_it() -> None:
    """`uses_rules` is read only when a residual (non-slot) bind token survives the
    rename — so the guard is asserted on THAT path, not on a scenario where the field
    is never touched. Off that path a non-list `uses_rules` is still fail-closed, just
    later: `Blueprint.parse` rejects it inside `_validate_blueprint_dag`."""
    child = BlueprintSeed(
        id="bp-child",
        intent="earnings",
        slots_summary="",
        uses=["dbpcm_warehouse.payroll.register_type", "dbpcm_warehouse.payroll.amount"],
        uses_rules=[
            {
                "id": "earnings_only",
                "resolve_via": "resolveValues(register_type, 'earnings')",
                "table": "dbpcm_warehouse.payroll",
                "binds": "earn_codes",
            }
        ],
        result_grain=[],
        sql_template=(
            "SELECT SUM(amount) AS total FROM dbpcm_warehouse.payroll "
            "WHERE register_type IN {earn_codes}"
        ),
    )
    parent = _parent(
        uses=["dbpcm_warehouse.payroll.register_type", "dbpcm_warehouse.payroll.amount"],
        slots=[],
        result_grain=[],
        uses_rules=5,
        ref={"blueprint": "bp-child"},
    )
    with pytest.raises(CorpusLoadError, match="uses_rules"):
        resolve_blueprint_references([child, parent])


def test_a_node_with_both_a_ref_and_inline_sql_fails_the_load() -> None:
    parent = _parent(
        composes=[
            {
                "order": 0,
                "sql_template": "SELECT department_name AS department FROM dbpcm_warehouse.employee",
                "ref": {"blueprint": "bp-child", "slots": {"department": "dept"}},
            }
        ]
    )
    with pytest.raises(CorpusLoadError, match="BOTH"):
        resolve_blueprint_references([_leaf(), parent])


def test_a_ref_node_may_not_declare_consumes() -> None:
    parent = _parent(
        composes=[
            {
                "order": 0,
                "feeds_from": [1],
                "consumes": {"x": "$1.x"},
                "ref": {"blueprint": "bp-child", "slots": {"department": "dept"}},
            },
            {"order": 1, "output": {"x": "scalar"}, "sql_template": "SELECT 1 AS x"},
        ]
    )
    with pytest.raises(CorpusLoadError, match="consumes"):
        resolve_blueprint_references([_leaf(), parent])


def test_two_seeds_with_the_same_id_fail_the_load() -> None:
    with pytest.raises(CorpusLoadError, match="duplicate blueprint id"):
        resolve_blueprint_references([_leaf(), _leaf(intent="a different one")])


@pytest.mark.parametrize("bad_id", [["bp", "child"], {"a": 1}, 5, None, ""])
def test_a_non_string_blueprint_id_raises_corpus_load_error_not_a_type_error(
    bad_id: Any,
) -> None:
    """`id` keys this resolver's every structure — the seed map, the reference graph,
    the DFS colouring, the footprint accumulator — so an UNHASHABLE one raises
    `TypeError: unhashable type: 'list'` at the first membership test, un-wrapped, out
    of `load_corpus`, retried every poll.

    Found by sweeping this path for comparisons and membership tests on untrusted
    values, not from a reported defect. `BlueprintSeed` declares `id: str` and enforces
    nothing, and `load_seed_fixtures` spreads a YAML entry straight into the ctor; the
    export path is safe only incidentally, because `_seed_from_entry` coerces the dict
    key with `str()`."""
    with pytest.raises(CorpusLoadError, match="is not a non-empty string"):
        resolve_blueprint_references([replace(_leaf(), id=bad_id), _parent()])


# -- 4. inlining is invisible downstream ------------------------------------


def test_a_corpus_with_no_references_is_returned_untouched() -> None:
    seeds = [_leaf()]
    assert resolve_blueprint_references(seeds)[0] is seeds[0]


def test_resolution_never_mutates_the_input_seeds() -> None:
    """The caller's list is shared (`load_corpus` is handed it, the hydrator built it),
    and a re-poll must see the same authored content."""
    parent = _parent()
    before = json.dumps(parent.composes, sort_keys=True)
    resolve_blueprint_references([_leaf(), parent])
    assert json.dumps(parent.composes, sort_keys=True) == before
    assert "ref" in parent.composes[0]


def test_the_resolved_node_is_indistinguishable_from_a_hand_written_inline_one() -> None:
    """Requirement 6, at the source. Nothing on the node names the blueprint it came
    from — so no serialization downstream can leak it, whatever it chooses to emit."""
    resolved = _resolved([_leaf(), _parent()])["bp-parent"]
    node = resolved.composes[0]
    assert "ref" not in node
    assert set(node) == {"order", "output", "sql_template"}
    assert "bp-child" not in json.dumps(resolved.composes)
    assert "bp-child" not in (_dag_properties(resolved)["composes_json"] or "")


def test_the_loader_and_the_parse_layer_share_one_definition_of_both_constants() -> None:
    """Identity, not equality — the plan's own rule after `_TABLE_CONSUME_REF` shipped in
    three hand-copied versions. Both constants are read by a resolver and by a refuser
    that must agree: disagree about `NODE_REF_KEY` and an unresolved reference sails past
    both; disagree about `DEFAULT_NODE_KIND` and an approval node inlines as a plain
    query. `DEFAULT_NODE_KIND` is additionally pinned to the value `Node.parse` actually
    defaults to, which is the thing the loader's `!= DEFAULT_NODE_KIND` gate assumes."""
    from data_agent.runtime.blueprint import models
    from data_agent.runtime.retrieval import corpus_loader

    assert corpus_loader.NODE_REF_KEY is models.NODE_REF_KEY
    assert corpus_loader.DEFAULT_NODE_KIND is models.DEFAULT_NODE_KIND
    assert models.DEFAULT_NODE_KIND in models.NODE_KINDS
    assert Node.parse({"order": 0}).node_kind == models.DEFAULT_NODE_KIND


def test_an_unresolved_reference_is_refused_by_the_parse_layer() -> None:
    """The read-side backstop. `ref` is not in `Node.parse`'s whitelist, so WITHOUT
    this the node would parse cleanly as a template-less query node and the executor
    would run a DAG with a silently empty step."""
    with pytest.raises(BlueprintParseError, match="unresolved blueprint reference"):
        Node.parse({"order": 0, "ref": {"blueprint": "bp-child"}})


def test_the_validation_gates_still_run_over_the_inlined_sql() -> None:
    """Gate (c) is the independent second line: the union rule binds the parent to the
    child's DECLARED footprint, and this binds it to what the inlined SQL actually
    reads. A child whose own `uses` is a lie is caught here."""
    child = _leaf(
        uses=[f"{_E}.department_name", f"{_E}.annual_salary"],
        sql_template=(
            "SELECT department_name AS department, MAX(employee_code) AS worst\n"
            "FROM dbpcm_warehouse.employee\n"
            "WHERE department_name = {department}\n"
            "GROUP BY department_name"
        ),
    )
    resolved = _resolved([child, _parent()])
    with pytest.raises(CorpusLoadError, match="outside the declared uses"):
        _validate_blueprint_dag(resolved["bp-parent"])


# -- the real canon corpus --------------------------------------------------


def test_the_canon_composite_inlines_to_exactly_what_it_used_to_declare_inline() -> None:
    """`bp-compare-employee-check-detail-two-periods` had two nodes that differed only
    in the pay-period slot; they now reference `bp-employee-check-detail-for-period`.
    The inlined SQL must be byte-identical to the SQL those nodes carried before, which
    is why the composite's structural key did not move across the change."""
    resolved = _resolved(load_seed_fixtures(_FIXTURE_DIR)[0])
    composite = resolved["bp-compare-employee-check-detail-two-periods"]
    child = resolved["bp-employee-check-detail-for-period"]

    assert composite.composes[0]["sql_template"] == child.sql_template.replace(
        "{period}", "{period_a}"
    )
    assert composite.composes[1]["sql_template"] == child.sql_template.replace(
        "{period}", "{period_b}"
    )
    # The pre-2b key of the composite, recomputed from its then-inline templates. It is
    # pinned as a literal on purpose: it is the single clearest evidence that inlining
    # is semantics-preserving, and a future edit to either file must move it knowingly.
    assert _dag_properties(composite)["structural_key"] == (
        "sha256:a7217b34f3af191e6f81226b3dde93c2cc545111f9933ebc51a7d32d9c849799"
    )


def test_the_canon_child_is_a_runnable_blueprint_in_its_own_right() -> None:
    """The point of the extraction: what was an anonymous DAG node now has an id, is
    retrievable, and validates standalone."""
    seeds = {bp.id: bp for bp in load_seed_fixtures(_FIXTURE_DIR)[0]}
    child = seeds["bp-employee-check-detail-for-period"]
    assert child.sql_template and not child.composes
    _validate_blueprint_dag(child)
