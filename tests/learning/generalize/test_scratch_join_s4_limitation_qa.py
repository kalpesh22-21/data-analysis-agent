"""KNOWN LIMITATION: `check_dag` now accepts a table intermediate, but S4 still
cannot validate one — the loop STILL cannot learn a scratch-join composite.

Fixing `check_dag` moved `dag_ok` from False to True for the canon's
`bp-earnings-by-department-via-scratch-join` shape. It did not make that shape
learnable, because `dag_ok` is the THIRD check `decide_outcome` consults and the
FIRST one (`explain_ok`) fails first:

    builder._provenance_uses(templates, catalog_schema)
      -> sqlparse.extract_column_provenance(node_1_template, catalog)
      -> ProvenanceExtractionError  (D64, scratch fail-closed)
      -> explain_ok=False -> ("fail_to_review", "explain_failed")

And the mechanism is harder than "the scratch table is not in the catalog": the
extractor rejects ANY `scratch.*` reference that has no bound `session_id`, so
registering the scratch table in `catalog_schema` does not help either (pinned
below). The corpus loader solves the same problem differently — it never calls the
provenance extractor for composites; it builds a synthetic per-node scratch schema
(`corpus_loader._scratch_schema_for_node`) and hands it to `qualify_columns`. S4 has
no equivalent seam.

So: fix 1 is PREPARATORY for the shape that motivated it. The one capability it does
deliver today is the TERMINAL table output (last test) — previously rejected by the
blanket `any(v != "scalar")` rule.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.learning.generalize.builder import generalize_blueprint
from data_agent.learning.generalize.rewrite import rewrite_sql_to_template
from data_agent.learning.generalize.validate import REASON_DAG, REASON_EXPLAIN, check_dag
from data_agent.sqlparse import ProvenanceExtractionError, extract_column_provenance

_CATALOG: dict[str, dict[str, str]] = {
    "dbpcm_warehouse.payroll": {
        "employee_code": "String",
        "register_type": "String",
        "amount": "Float64",
    },
    "dbpcm_warehouse.employee": {
        "employee_code": "String",
        "department_name": "String",
    },
}

# The accepted SQL a scratch-join session would leave in the tool trail — the canon's
# `bp-earnings-by-department-via-scratch-join` templates with their literals back in.
_SQL_PRODUCER = (
    "SELECT toString(p.employee_code) AS employee_code, toFloat64(SUM(p.amount)) AS earnings "
    "FROM dbpcm_warehouse.payroll AS p WHERE p.register_type = 'EARN' GROUP BY p.employee_code"
)
_SQL_JOIN = (
    "SELECT e.department_name AS department, SUM(x.earnings) AS total_earnings "
    "FROM scratch.emp_earnings AS x "
    "JOIN dbpcm_warehouse.employee AS e ON e.employee_code = x.employee_code "
    "WHERE e.department_name = 'Analytics' GROUP BY e.department_name"
)

_PARAMETERIZATION: list[dict[str, Any]] = [
    {
        "locator": {
            "table": "dbpcm_warehouse.employee",
            "column": "department_name",
            "value": "Analytics",
        },
        "role": "slot",
        "slot": {
            "name": "department",
            "type": "string",
            "binds_to": "dbpcm_warehouse.employee.department_name",
            "required": True,
            "optional_pattern": None,
        },
    },
    {
        "locator": {
            "table": "dbpcm_warehouse.payroll",
            "column": "register_type",
            "value": "EARN",
        },
        "role": "inline",
        "why": "defines the 'earnings' metric",
    },
]


def _scratch_join_payload() -> dict[str, Any]:
    return {
        "intent": "total earnings by department via a materialized per-employee join",
        "kind": "composite",
        "resolves": {},
        "source_tool_call_refs": ["tc0", "tc1"],
        "accepted_signal": "no_correction",
        "parameterization": [dict(p) for p in _PARAMETERIZATION],
        "composes": [
            {
                "order": 0, "node_kind": "query", "step_intent": "per-employee earnings",
                "feeds_from": [], "consumes": {}, "output": {"emp_earnings": "table"},
                "source_tool_call_ref": "tc0", "when": None, "requires_approval": None,
            },
            {
                "order": 1, "node_kind": "query", "step_intent": "join to departments",
                "feeds_from": [0], "consumes": {"emp_earnings": "$0"}, "output": {},
                "source_tool_call_ref": "tc1", "when": None, "requires_approval": None,
            },
        ],
        "result_signature": {
            "shape": [], "grain": {"columns": ["department"], "verifiable": True},
            "invariants": [],
        },
        "notes": "",
    }


def _generalize() -> Any:
    return generalize_blueprint(
        _scratch_join_payload(), {"tc0": _SQL_PRODUCER, "tc1": _SQL_JOIN}, _CATALOG
    )


# --- the limitation, end to end ------------------------------------------------


def test_the_dag_gate_now_passes_for_the_scratch_join_shape() -> None:
    """Fix 1 did what it says: `dag_ok` is True where it used to be False."""
    gen = _generalize()
    assert gen.static_validation.dag_ok is True
    assert check_dag(_scratch_join_payload()["composes"]) is True


def test_a_scratch_join_composite_still_fails_to_review_on_explain_not_dag() -> None:
    """KNOWN LIMITATION. `explain_ok` is checked before `dag_ok`, so the outcome and
    the S7 routing tag are unchanged from before fix 1 — only the tag's REASON moved
    from `dag_invalid` to `explain_failed`."""
    sv = _generalize().static_validation
    assert sv.outcome == "fail_to_review"
    assert sv.reason == REASON_EXPLAIN
    assert sv.reason != REASON_DAG
    assert sv.explain_ok is False


def test_the_whole_generalization_is_degraded_not_just_the_explain_flag() -> None:
    """`_provenance_uses` returning `(False, ())` cascades: `uses` is empty, so
    `binds_to_subset_uses` is False and `read_only_select` is short-circuited False
    too. Nothing downstream of S4 has anything to work with."""
    gen = _generalize()
    assert gen.uses == ()
    assert gen.static_validation.binds_to_subset_uses is False
    assert gen.static_validation.read_only_select is False
    # The per-node templates DID rewrite — the rewrite is not the blocker.
    assert len(gen.node_templates) == 2


def test_the_blocker_is_the_d64_scratch_fail_closed_rule_not_a_missing_catalog_entry() -> None:
    """The precise mechanism, pinned so the next slice fixes the right thing.
    `extract_column_provenance` rejects a `scratch.*` reference with no bound
    `session_id` OUTRIGHT — adding the scratch table to `catalog_schema` (the obvious
    first attempt) changes nothing."""
    template = rewrite_sql_to_template(_SQL_JOIN, _PARAMETERIZATION, strict=False)
    assert "scratch.emp_earnings" in template

    with pytest.raises(ProvenanceExtractionError) as bare:
        extract_column_provenance(template, _CATALOG)
    assert "session_id" in str(bare.value)

    catalog_with_scratch = dict(_CATALOG)
    catalog_with_scratch["scratch.emp_earnings"] = {"employee_code": "TEXT", "earnings": "TEXT"}
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(template, catalog_with_scratch)


def test_the_loader_has_the_seam_s4_lacks() -> None:
    """The asymmetry, stated as a test so the next slice knows where to look: the
    loader validates a scratch-join node by synthesizing the producing node's output
    columns as a `scratch.<placeholder>` schema. `builder`/`validate` import no such
    helper — S4 goes straight to `extract_column_provenance`."""
    import data_agent.learning.generalize.builder as builder
    from data_agent.runtime.retrieval.corpus_loader import _scratch_schema_for_node

    class _Src:
        sql_template = _SQL_PRODUCER

    class _Consumer:
        consumes = {"emp_earnings": "$0"}

    schema = _scratch_schema_for_node(_Consumer(), {0: _Src()})
    assert schema == {"emp_earnings": {"employee_code": "TEXT", "earnings": "TEXT"}}
    assert not hasattr(builder, "_scratch_schema_for_node")
    assert builder.extract_column_provenance is extract_column_provenance


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN LIMITATION (not a regression — pre-dates this slice and is NOT closed "
        "by it): the learning loop cannot emit a scratch-join composite because S4's "
        "explain check has no scratch seam. Closing it means giving `_provenance_uses` "
        "the loader's treatment — either a per-node scratch schema (the "
        "`_scratch_schema_for_node` equivalent) or a session_id-bearing provenance "
        "call. When that lands, this flips to XPASS and the guard becomes real."
    ),
)
def test_the_loop_can_learn_the_canon_scratch_join_blueprint() -> None:
    gen = _generalize()
    assert gen.static_validation.outcome == "ok"
    assert set(gen.uses) >= {
        "dbpcm_warehouse.employee.department_name",
        "dbpcm_warehouse.payroll.amount",
    }


# --- what fix 1 DOES buy today -------------------------------------------------


_SQL_COUNT = "SELECT COUNT(employee_code) AS n FROM dbpcm_warehouse.employee"
_SQL_BY_DEPT = (
    "SELECT department_name AS department, COUNT(employee_code) AS headcount "
    "FROM dbpcm_warehouse.employee WHERE department_name = 'Analytics' "
    "GROUP BY department_name"
)


def _terminal_output_payload(terminal_output: dict[str, str]) -> dict[str, Any]:
    return {
        "intent": "headcount by department against the company total",
        "kind": "composite",
        "resolves": {},
        "source_tool_call_refs": ["tc0", "tc1"],
        "accepted_signal": "no_correction",
        "parameterization": [dict(_PARAMETERIZATION[0])],
        "composes": [
            {"order": 0, "node_kind": "query", "step_intent": "", "feeds_from": [],
             "consumes": {}, "output": {"n": "scalar"}, "source_tool_call_ref": "tc0",
             "when": None, "requires_approval": None},
            {"order": 1, "node_kind": "query", "step_intent": "", "feeds_from": [0],
             "consumes": {"n": "$0.n"}, "output": terminal_output,
             "source_tool_call_ref": "tc1", "when": None, "requires_approval": None},
        ],
        "result_signature": None,
        "notes": "",
    }


@pytest.mark.parametrize(
    "terminal_output",
    [{"rows": "table"}, {"rows": "table", "n": "scalar"}],
    ids=["terminal-table", "terminal-table-and-scalar"],
)
def test_a_terminal_table_output_composite_now_validates_ok(
    terminal_output: dict[str, str],
) -> None:
    """The ONE capability fix 1 delivers today. The old rule rejected `output` values
    other than `scalar` outright, so a SINK node declaring the rowset it returns went
    to `fail_to_review/dag_invalid`. Its templates read only warehouse tables, so the
    explain check passes and the candidate now reaches `ok`."""
    gen = generalize_blueprint(
        _terminal_output_payload(terminal_output),
        {"tc0": _SQL_COUNT, "tc1": _SQL_BY_DEPT},
        _CATALOG,
    )
    assert gen.static_validation.outcome == "ok"
    assert gen.static_validation.dag_ok is True


def test_the_old_scalar_only_rule_would_have_rejected_that_same_shape() -> None:
    """Non-vacuity guard for the test above: reproduce the PRE-fix predicate and show
    it rejects the terminal-table composite. Without this, `outcome == "ok"` above
    could be true for reasons unrelated to the change."""
    composes = _terminal_output_payload({"rows": "table"})["composes"]
    old_rule_rejects = any(
        isinstance(node.get("output"), dict)
        and any(v != "scalar" for v in node["output"].values())
        for node in composes
    )
    assert old_rule_rejects is True
    assert check_dag(composes) is True
