"""`check_dag` must accept every DAG shape the RUNTIME executes (S4 `dag_ok`).

The learning validator was scalar-only long after the executor learned to
materialize `table` intermediates through the D93 scratch side-channel, so the
canon's `bp-earnings-by-department-via-scratch-join` was a blueprint the runtime
runs but the loop could never emit — a silent one-way parity break. The
canon-shape test below is the standing guard on that parity: it feeds the HERMETIC
canon mirror (`tests/fixtures/corpus/blueprints.yaml`, byte-mirrored from the real
MCP canon) straight into `check_dag`.

The other tests pin the two halves of the replacement invariant: the closed
`NODE_OUTPUT_KINDS` set, and "a table passed downstream must be CONSUMED as a
table" (the shape the executor otherwise reports UNSUPPORTED).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from data_agent.learning.generalize.validate import check_dag
from data_agent.runtime.blueprint.models import NODE_OUTPUT_KINDS

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "corpus"
_CANON_SCRATCH_JOIN_ID = "bp-earnings-by-department-via-scratch-join"


def _canon_composes(blueprint_id: str) -> list[dict[str, Any]]:
    yaml = pytest.importorskip("yaml")
    docs = yaml.safe_load((_FIXTURE_DIR / "blueprints.yaml").read_text())
    doc = next(d for d in docs if d["id"] == blueprint_id)
    return doc["composes"]


# --- runtime/learning parity: the canon's table-intermediate DAG --------------


def test_learning_can_express_the_canon_scratch_join_dag():
    """The exact `composes` the canon ships (and the executor runs end-to-end in
    `tests/integration/test_table_intermediate_live.py`) passes S4's DAG gate."""
    composes = _canon_composes(_CANON_SCRATCH_JOIN_ID)
    # Guard the guard: if the canon shape stops using a table intermediate this test
    # would pass vacuously.
    assert any("table" in (n.get("output") or {}).values() for n in composes)
    assert check_dag(composes) is True


def test_learning_dag_gate_agrees_with_the_runtime_output_kinds():
    """`check_dag` accepts EXACTLY the kinds `Node.parse` accepts — the set that
    drifted. Any kind added to `NODE_OUTPUT_KINDS` must pass here too."""
    for kind in NODE_OUTPUT_KINDS:
        assert check_dag([{"order": 0, "output": {"o": kind}}]) is True


def test_the_consume_grammars_come_from_the_runtime_node_contract():
    """S4 must read a `consumes` ref exactly as the executor and the loader do. This
    started as a `.pattern` comparison across three hand-copied regexes; the grammars
    now live in `blueprint/models.py` and this asserts the offline validator uses THOSE
    objects (the full anti-drift matrix is in `test_dag_loader_parity_qa.py`)."""
    from data_agent.learning.generalize import validate as learning_validate
    from data_agent.runtime.blueprint.models import SCALAR_CONSUME_REF, TABLE_CONSUME_REF

    assert learning_validate.TABLE_CONSUME_REF is TABLE_CONSUME_REF
    assert learning_validate.SCALAR_CONSUME_REF is SCALAR_CONSUME_REF


@pytest.mark.parametrize("kind", ["frame", "SCALAR", "", None, 1])
def test_output_kind_outside_the_closed_set_is_rejected(kind):
    # Not "any string is fine now": an unknown kind would only fail later, at
    # `Node.parse` on the landing path.
    assert check_dag([{"order": 0, "output": {"o": kind}}]) is False


def test_non_dict_output_is_rejected():
    # Previously slipped through (the check was `isinstance(output, dict) and ...`).
    assert check_dag([{"order": 0, "output": ["scalar"]}]) is False


# --- the invariant that replaced "scalar-converging" --------------------------


def test_table_output_consumed_as_a_table_is_accepted():
    composes = [
        {"order": 0, "output": {"emp": "table"}},
        {"order": 1, "feeds_from": [0], "consumes": {"emp": "$0"}, "output": {}},
    ]
    assert check_dag(composes) is True


def test_table_output_passed_downstream_without_a_table_consume_is_rejected():
    # The executor cannot scalar-pass a table: `_has_table_intermediate` with no
    # `_table_consumed_orders` → UNSUPPORTED. Emitting it would land a dead blueprint.
    composes = [
        {"order": 0, "output": {"emp": "table", "n": "scalar"}},
        {"order": 1, "feeds_from": [0], "consumes": {"n": "$0.n"}, "output": {}},
    ]
    assert check_dag(composes) is False


def test_terminal_table_output_is_accepted():
    # A SINK node's table result is returned to the caller, never passed — fine.
    composes = [
        {"order": 0, "output": {"total": "scalar"}},
        {"order": 1, "feeds_from": [0], "consumes": {"t": "$0.total"}, "output": {"rows": "table"}},
    ]
    assert check_dag(composes) is True


def test_non_dict_consumes_is_rejected():
    assert check_dag([{"order": 0, "consumes": [["emp", "$0"]], "output": {}}]) is False


# --- the pre-existing DAG invariants still hold -------------------------------


def test_scalar_converging_dag_still_passes():
    composes = [
        {"order": 0, "output": {"dept_total": "scalar"}},
        {"order": 1, "output": {"company_total": "scalar"}},
    ]
    assert check_dag(composes) is True


@pytest.mark.parametrize(
    "composes",
    [
        [{"order": 0, "output": {}}, {"order": 0, "output": {}}],  # duplicate order
        [{"order": 0, "feeds_from": [7], "output": {}}],  # dangling edge
        [{"order": 0, "feeds_from": [0], "output": {}}],  # self-cycle
        [{"order": 0, "feeds_from": [1], "output": {}}, {"order": 1, "output": {}}],  # forward edge
        [{"order": "0", "output": {}}],  # non-integer order
    ],
)
def test_structural_dag_violations_still_rejected(composes):
    assert check_dag(composes) is False
