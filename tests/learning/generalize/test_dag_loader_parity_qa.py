"""S4 `check_dag` vs the two authorities it claims to mirror: the corpus LOADER
(`_validate_blueprint_dag`, landing) and the EXECUTOR (`_has_table_intermediate` /
`_table_consumed_orders`, replay).

`check_dag` is an OFFLINE re-implementation of invariants owned elsewhere. That is a
deliberate choice (it must not import the request-path executor), but it makes the
question "stricter, looser, or aligned?" the only one that matters, in both
directions:

  * LOOSER than the loader ⇒ S4 stamps `dag_ok=True`, the candidate promotes, and the
    landing write dies with a `CorpusLoadError`. Late, loud, and past the review
    valve that was supposed to catch it.
  * STRICTER than the executor ⇒ S4 sends a DAG the runtime executes fine to
    `fail_to_review/dag_invalid`. A silent capability loss.

The matrix below was produced by running all three gates over the same shapes. Four
LOOSE shapes and one STRICT shape were recorded as strict xfails; all five are now
FIXED (`check_dag` validates both consume grammars against `feeds_from` and the
producing node's declared outputs, and its table rule is whole-DAG like the
executor's) and are kept here as passing regression guards. Everything else was
already aligned.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from data_agent.learning.generalize.validate import check_dag
from data_agent.runtime.blueprint.executor import (
    _has_table_intermediate,
    _table_consumed_orders,
    _topo_order,
)
from data_agent.runtime.blueprint.models import Blueprint, BlueprintParseError, Node
from data_agent.runtime.retrieval.corpus_loader import (
    BlueprintSeed,
    CorpusLoadError,
    _validate_blueprint_dag,
)

_REAL_CANON_DIR = Path("/Users/kalpeshmulye/Development/clickhouse-api/app/corpus/data/blueprints")
_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "corpus"

_USES = [
    "dbpcm_warehouse.payroll.employee_code",
    "dbpcm_warehouse.payroll.register_type",
    "dbpcm_warehouse.payroll.amount",
    "dbpcm_warehouse.employee.employee_code",
    "dbpcm_warehouse.employee.department_name",
]
_T_PRODUCER = (
    "SELECT toString(p.employee_code) AS employee_code, toFloat64(SUM(p.amount)) AS earnings\n"
    "FROM dbpcm_warehouse.payroll AS p WHERE p.register_type = 'EARN' GROUP BY p.employee_code\n"
)
_T_JOIN = (
    "SELECT e.department_name AS department, SUM(x.earnings) AS total_earnings\n"
    "FROM scratch.emp_earnings AS x\n"
    "JOIN dbpcm_warehouse.employee AS e ON e.employee_code = x.employee_code\n"
    "GROUP BY e.department_name\n"
)
_T_SCALAR = "SELECT COUNT(employee_code) AS n FROM dbpcm_warehouse.employee\n"

# The canon's `bp-earnings-by-department-via-scratch-join`, with templates attached
# (the loader validates templates; the S3 plan `check_dag` sees does not carry them).
_CANON_SHAPE: list[dict[str, Any]] = [
    {"order": 0, "output": {"emp_earnings": "table"}, "sql_template": _T_PRODUCER},
    {
        "order": 1,
        "feeds_from": [0],
        "consumes": {"emp_earnings": "$0"},
        "output": {},
        "sql_template": _T_JOIN,
    },
]


def _plan(composes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The S3 PLAN view `check_dag` actually receives — no `sql_template` (those live
    in `BlueprintGeneralization.node_templates` and are joined in later by
    `mapping._compose_docs`)."""
    return [{k: v for k, v in node.items() if k != "sql_template"} for node in composes]


def _loader_verdict(composes: list[dict[str, Any]]) -> str:
    seed = BlueprintSeed(
        id="bp-probe",
        intent="probe",
        slots_summary="",
        uses=list(_USES),
        result_grain=["Department"],
        sql_template=None,
        composes=composes,
    )
    try:
        _validate_blueprint_dag(seed)
    except CorpusLoadError as exc:
        return f"reject: {exc}"
    return "ok"


def _executor_verdict(composes: list[dict[str, Any]]) -> str:
    """The executor's pre-walk DAG gate (`executor.py:520-536`) with a scratch client
    assumed wired — the only part of `_execute_composite` reachable without a live
    warehouse."""
    blueprint = Blueprint.parse(
        id="bp-probe",
        intent="p",
        resolves=None,
        slots=None,
        uses_rules=[],
        sql_template=None,
        composes=composes,
        result_grain={"columns": [], "verifiable": True},
    )
    nodes = blueprint.composes
    if _topo_order(nodes) is None:
        return "unsupported"
    if _has_table_intermediate(nodes) and not _table_consumed_orders(nodes):
        return "unsupported"
    return "runs"


# --- LOOSER than the loader: accepted by S4, CorpusLoadError at landing --------


_LOOSE_SHAPES: list[Any] = [
    pytest.param(
        [
            _CANON_SHAPE[0],
            {**_CANON_SHAPE[1], "feeds_from": []},
        ],
        "not in its feeds_from",
        id="table-consume-source-not-in-feeds_from",
    ),
    pytest.param(
        [
            {"order": 0, "output": {"n": "scalar"}, "sql_template": _T_SCALAR},
            {"order": 1, "feeds_from": [], "consumes": {"n": "$0.n"}, "output": {},
             "sql_template": _T_SCALAR},
        ],
        "not in its feeds_from",
        id="scalar-consume-source-not-in-feeds_from",
    ),
    pytest.param(
        [
            {"order": 0, "output": {"n": "scalar"}, "sql_template": _T_SCALAR},
            {"order": 1, "feeds_from": [0], "consumes": {"m": "$0.m"}, "output": {},
             "sql_template": _T_SCALAR},
        ],
        "no declared SCALAR output named 'm'",
        id="scalar-consume-of-an-undeclared-output-name",
    ),
    pytest.param(
        [
            {"order": 0, "output": {"n": "scalar"}, "sql_template": _T_SCALAR},
            {"order": 1, "feeds_from": [0], "consumes": {"n": "emp_earnings"}, "output": {},
             "sql_template": _T_SCALAR},
        ],
        "not a '$N.name' scalar or '$N' table reference",
        id="consume-ref-in-neither-grammar",
    ),
]


@pytest.mark.parametrize(("composes", "loader_needle"), _LOOSE_SHAPES)
def test_the_loader_really_does_reject_these_shapes(
    composes: list[dict[str, Any]], loader_needle: str
) -> None:
    """The guard on the guard: without this the strict xfail below could be recording
    a shape nothing rejects."""
    verdict = _loader_verdict(composes)
    assert loader_needle in verdict, f"loader unexpectedly said: {verdict}"


@pytest.mark.parametrize(("composes", "loader_needle"), _LOOSE_SHAPES)
def test_every_shape_the_loader_rejects_is_already_rejected_by_check_dag(
    composes: list[dict[str, Any]], loader_needle: str
) -> None:
    """WAS a strict xfail (MEDIUM): `check_dag` validated `feeds_from` edges and
    `output` KINDS but never cross-checked `consumes` against either, so all four
    shapes stamped `dag_ok=True`, passed the review valve, and died at the landing
    write. The mechanical cause was that it had copied only the loader's TABLE consume
    pattern and not its scalar sibling. FIXED: both grammars now come from
    `blueprint/models.py` and `check_dag` applies the loader's §1.2(h) rules —
    (1) a ref must match `$N` or `$N.name`; (2) the source must be in the consumer's
    `feeds_from`; (3) `$N.name` must name a DECLARED scalar output of that node."""
    assert check_dag(_plan(composes)) is False


def test_check_dag_cannot_see_the_scratch_from_join_rule_at_all() -> None:
    """DOCUMENTED, not filed: the loader's fourth table rule — a table-consume
    placeholder must appear as a `scratch.<placeholder>` FROM/JOIN token in the
    consumer's template — is STRUCTURALLY unreachable from `check_dag`, which is
    handed the S3 plan BEFORE `mapping._compose_docs` joins the S4 templates in
    (`builder.py:214` passes the raw `composes`, not `node_templates`).

    If S4 is ever to own this rule it must be given the templates. Pinned so the gap
    is a known shape of the seam rather than an oversight."""
    composes = copy.deepcopy(_CANON_SHAPE)
    composes[1]["sql_template"] = _T_SCALAR  # no scratch.emp_earnings source
    assert "must appear as a 'scratch.emp_earnings' source" in _loader_verdict(composes)
    assert check_dag(_plan(composes)) is True
    assert all("sql_template" not in node for node in _plan(composes))


def test_a_table_producer_with_no_template_fails_the_load_as_a_corpus_load_error() -> None:
    """WAS a strict xfail (MEDIUM, loader, pre-existing): a table-consume whose
    producer has no `sql_template` made `_scratch_schema_for_node` build an EMPTY
    column map, and `MappingSchema` then raised a raw sqlglot `SchemaError` OUTSIDE
    the `_assert_template_reads_within_uses` try — so `load_corpus` aborted with an
    un-wrapped third-party exception instead of the `CorpusLoadError` its callers
    handle. `check_dag` accepts this shape (it cannot see templates), so it was
    reachable from the loop. FIXED at both levels: the empty scratch schema is now
    rejected with a precise `CorpusLoadError`, and `MappingSchema` moved inside the
    try so no other schema failure can escape un-wrapped either."""
    composes = [
        {"order": 0, "output": {"emp_earnings": "table"}},  # producer, no template
        {"order": 1, "feeds_from": [0], "consumes": {"emp_earnings": "$0"}, "output": {},
         "sql_template": _T_JOIN},
    ]
    assert check_dag(_plan(composes)) is True
    with pytest.raises(CorpusLoadError):
        _validate_blueprint_dag(
            BlueprintSeed(
                id="bp-probe", intent="p", slots_summary="", uses=list(_USES),
                result_grain=["d"], sql_template=None, composes=composes,
            )
        )


# --- was STRICTER than the runtime: fail_to_review on an executable DAG -------


def test_check_dag_does_not_reject_a_dag_the_executor_and_loader_both_accept() -> None:
    """WAS a strict xfail (LOW): the 'a fed-from table output must be consumed AS a
    table' rule was PER-NODE while the executor's equivalent
    (`_has_table_intermediate(nodes) and not _table_consumed_orders(nodes)`,
    executor.py:534) is WHOLE-DAG, so a node declaring BOTH a table and a scalar
    output and consumed only for its scalar went to `fail_to_review/dag_invalid`
    though it executes fine. FIXED by adopting the executor's whole-DAG predicate
    verbatim rather than keeping a deliberately stricter offline rule."""
    composes = [
        {"order": 0, "output": {"emp_earnings": "table", "n": "scalar"},
         "sql_template": _T_PRODUCER},
        {"order": 1, "feeds_from": [0], "consumes": {"n": "$0.n"},
         "output": {"emp_earnings": "table"}, "sql_template": _T_PRODUCER},
        {"order": 2, "feeds_from": [1], "consumes": {"emp_earnings": "$1"}, "output": {},
         "sql_template": _T_JOIN},
    ]
    assert _loader_verdict(composes) == "ok"
    assert _executor_verdict(composes) == "runs"
    assert check_dag(_plan(composes)) is True


# --- where the three DO agree (regression guards) -----------------------------


def test_the_canon_scratch_join_shape_is_accepted_by_all_three_gates() -> None:
    composes = copy.deepcopy(_CANON_SHAPE)
    assert check_dag(_plan(composes)) is True
    assert _loader_verdict(composes) == "ok"
    assert _executor_verdict(composes) == "runs"


def test_a_table_intermediate_with_no_table_consume_is_rejected_by_s4_and_the_executor() -> None:
    """The one case the new invariant genuinely mirrors: the loader is silent here,
    so S4 + the executor are the ONLY things standing between this shape and a
    blueprint that lands successfully and then degrades to the raw loop on every hit."""
    composes = [
        {"order": 0, "output": {"emp_earnings": "table", "n": "scalar"},
         "sql_template": _T_PRODUCER},
        {"order": 1, "feeds_from": [0], "consumes": {"n": "$0.n"}, "output": {},
         "sql_template": _T_SCALAR},
    ]
    assert check_dag(_plan(composes)) is False
    assert _executor_verdict(composes) == "unsupported"
    assert _loader_verdict(composes) == "ok", "the loader does NOT catch this — S4 must"


def test_a_terminal_table_output_is_accepted_by_all_three_gates() -> None:
    composes = [
        {"order": 0, "output": {"n": "scalar"}, "sql_template": _T_SCALAR},
        {"order": 1, "feeds_from": [0], "consumes": {"n": "$0.n"},
         "output": {"rows": "table"}, "sql_template": _T_SCALAR},
    ]
    assert check_dag(_plan(composes)) is True
    assert _loader_verdict(composes) == "ok"
    assert _executor_verdict(composes) == "runs"


@pytest.mark.parametrize("kind", ["frame", "rows", "SCALAR", "Table", "", None, 1, True])
def test_an_output_kind_outside_the_closed_set_is_rejected_by_s4_and_by_node_parse(
    kind: Any,
) -> None:
    """Both ends of the same closed set: S4 must not emit a kind that `Node.parse`
    would refuse at landing, and `Node.parse` must not accept one S4 rejects."""
    assert check_dag([{"order": 0, "output": {"o": kind}}]) is False
    with pytest.raises(BlueprintParseError):
        Node.parse({"order": 0, "output": {"o": kind}})


@pytest.mark.parametrize("kind", [[], {}, ["scalar"], {"kind": "scalar"}])
def test_an_unhashable_output_kind_is_rejected_rather_than_raising(kind: Any) -> None:
    """WAS a strict xfail (HIGH): `kind not in NODE_OUTPUT_KINDS` hashed an UNHASHABLE
    output kind → TypeError, breaking `check_dag`'s explicit 'never raises' contract
    and `generalize_blueprint`'s 'never raises for a bad candidate'. `[]`/`{}` are
    legal JSON and S4 reads `env.payload`, rehydrated straight from the candidate
    store — the poisoned-READ-record threat model `_MAX_NODES` already cites. FIXED at
    both ends with isinstance-before-membership."""
    assert check_dag([{"order": 0, "output": {"o": kind}}]) is False
    with pytest.raises(BlueprintParseError):
        Node.parse({"order": 0, "output": {"o": kind}})


@pytest.mark.parametrize("node_kind", [["query"], {"kind": "query"}])
def test_node_parse_rejects_an_unhashable_node_kind_cleanly(node_kind: Any) -> None:
    """WAS a strict xfail (HIGH, runtime, pre-existing — the landing-side twin of the
    extractor finding in test_validation_composes_adversarial_qa): `Node.parse` tested
    `node_kind not in NODE_KINDS` with the RAW value, so an unhashable one raised
    TypeError instead of `BlueprintParseError`, escaping `_validate_blueprint_dag`'s
    `except BlueprintParseError` and aborting the whole corpus load with an un-wrapped
    exception. FIXED; `SlotSpec.parse`'s `type` and `WhenClause.parse`'s
    `on_violation` carried the identical bug and were fixed in the same pass (pinned
    below)."""
    with pytest.raises(BlueprintParseError):
        Node.parse({"order": 0, "node_kind": node_kind})


@pytest.mark.parametrize(
    ("field", "raw"),
    [
        ("slot type", {"name": "d", "type": ["string"]}),
        ("slot type", {"name": "d", "type": {"t": "string"}}),
        ("when.on_violation", {"expr": "n > 0", "on_violation": ["abort"]}),
        ("when.on_violation", {"expr": "n > 0", "on_violation": {"o": "abort"}}),
    ],
)
def test_every_other_closed_set_test_in_the_parse_layer_is_unhashable_safe(
    field: str, raw: dict[str, Any]
) -> None:
    """The same defect class swept across the whole parse layer: `SLOT_TYPES` and
    `ON_VIOLATION` are membership-tested on stored values too, and a corrupt record
    reaches them by the same route. Fixing only `Node.parse` would have left two live
    crash sites in the same function family."""
    from data_agent.runtime.blueprint.models import SlotSpec, WhenClause

    parse = SlotSpec.parse if field == "slot type" else WhenClause.parse
    with pytest.raises(BlueprintParseError):
        parse(raw)


# --- the consume grammars: three hand-copies → one shared definition -----------
#
# HISTORY: `$N` was a hand-copied regex in three modules and `$N.name` in one, so the
# offline validator silently owned a SUBSET of the grammar the loader enforces (the
# cause of the four loose shapes above). Both now live in `blueprint/models.py` beside
# `NODE_OUTPUT_KINDS`. These tests were a three-way `.pattern`/flags/behaviour
# comparison; they now assert the single source is actually shared (identity, which
# `.pattern` equality could never prove) and keep the behavioural sweep as a grammar
# pin.


def _consumers_of_the_grammar() -> dict[str, Any]:
    from data_agent.learning.generalize import validate as learning_validate
    from data_agent.runtime.blueprint import executor, models
    from data_agent.runtime.retrieval import corpus_loader

    return {
        "learning/generalize/validate.py": learning_validate,
        "runtime/blueprint/executor.py": executor,
        "runtime/retrieval/corpus_loader.py": corpus_loader,
        "runtime/blueprint/models.py": models,
    }


def test_every_module_matches_consume_refs_with_the_one_shared_object() -> None:
    """Identity, not equality: a re-compiled local copy would pass a `.pattern`/flags
    comparison and still be free to drift on the next edit."""
    from data_agent.runtime.blueprint.models import TABLE_CONSUME_REF

    for name, module in _consumers_of_the_grammar().items():
        assert getattr(module, "TABLE_CONSUME_REF", None) is TABLE_CONSUME_REF, name


def test_no_module_keeps_a_private_copy_of_a_consume_grammar() -> None:
    """The anti-drift guard proper: the three private `_TABLE_CONSUME_REF` /
    `_CONSUME_REF` names are gone and must not come back."""
    for name, module in _consumers_of_the_grammar().items():
        for attr in ("_TABLE_CONSUME_REF", "_CONSUME_REF", "_SCALAR_CONSUME_REF"):
            assert not hasattr(module, attr), f"{name} re-introduced {attr}"


def test_check_dag_now_validates_both_consume_grammars() -> None:
    """The asymmetry that caused the loose shapes, asserted from the other side: the
    offline validator uses the loader's scalar grammar as well as the table one."""
    from data_agent.learning.generalize import validate as learning_validate
    from data_agent.runtime.blueprint.models import SCALAR_CONSUME_REF, TABLE_CONSUME_REF

    assert learning_validate.SCALAR_CONSUME_REF is SCALAR_CONSUME_REF
    assert learning_validate.TABLE_CONSUME_REF is TABLE_CONSUME_REF


@pytest.mark.parametrize(
    "ref",
    [
        "$0", "$1", "$10", "$007",           # table consumes
        "$0.name", "$0.", ".$0", "$", "$a",  # not table consumes
        "$0\n", "\n$0", " $0", "$0 ", "$-1", "$0$1", "$٢", "$1_0", "", "0", "scratch.x",
    ],
)
def test_the_table_consume_grammar_still_classifies_every_ref_the_same_way(ref: str) -> None:
    """The behavioural sweep the three-way comparison used to carry, kept as a GRAMMAR
    pin now that there is one object: hoisting must not have changed what `$N` means.
    Includes the two Python `re` traps, pinned deliberately and not endorsed: `$`
    matches before a trailing newline, so `"$0\\n"` IS a table consume; and `\\d` is
    UNICODE-aware, so `"$٢"` is one too (and `int("٢") == 2`, so it resolves to node 2
    identically in all three gates)."""
    from data_agent.runtime.blueprint.models import TABLE_CONSUME_REF

    expected = {
        "$0": "0", "$1": "1", "$10": "10", "$007": "007", "$0\n": "0", "$٢": "٢",
    }
    match = TABLE_CONSUME_REF.match(ref)
    assert (match.group(1) if match else None) == expected.get(ref)


# --- runtime/learning parity against the REAL canon, not just the mirror -------


def _canon_docs(directory: Path) -> dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    if not directory.is_dir():
        pytest.skip(f"real MCP canon not checked out at {directory}")
    return {
        (doc := yaml.safe_load(path.read_text()))["id"]: doc
        for path in sorted(directory.glob("bp-*.yaml"))
    }


def test_check_dag_accepts_every_composite_in_the_real_mcp_canon() -> None:
    """The builder's parity test reads the HERMETIC MIRROR
    (`tests/fixtures/corpus/blueprints.yaml`). The mirror has drifted from the real
    canon before (three blueprints, recorded in
    `test_corpus_loader_structural_key_qa.py`), so read the REAL canon directly here:
    the mirror-vs-canon guard and this one then fail independently, and a canon-only
    composite the loop cannot express is caught even while the mirror is stale."""
    canon = _canon_docs(_REAL_CANON_DIR)
    composites = {
        bid: doc["composes"] for bid, doc in canon.items() if doc.get("composes")
    }
    assert composites, "the canon ships no composite blueprint — this guard is vacuous"
    for bid, composes in sorted(composites.items()):
        assert check_dag(_plan(composes)) is True, f"{bid} is a DAG the loop cannot emit"


def test_the_canon_composite_the_parity_claim_rests_on_still_uses_a_table_intermediate() -> None:
    canon = _canon_docs(_REAL_CANON_DIR)
    composes = canon["bp-earnings-by-department-via-scratch-join"]["composes"]
    assert any("table" in (n.get("output") or {}).values() for n in composes)
    assert any(str(v).lstrip("$").isdigit() and "." not in str(v)
               for n in composes for v in (n.get("consumes") or {}).values())


def test_the_hermetic_mirror_still_matches_the_real_canon_on_composes() -> None:
    """The learning-side parity test feeds the MIRROR into `check_dag`, so its claim
    ('the loop can express what the runtime runs') is only as true as the mirror.
    `test_corpus_loader_structural_key_qa.py` guards this for the structural-key
    fields; this is the same guard stated from the learning side so a drift shows up
    next to the test that depends on it."""
    yaml = pytest.importorskip("yaml")
    canon = _canon_docs(_REAL_CANON_DIR)
    mirror = {d["id"]: d for d in yaml.safe_load((_FIXTURE_DIR / "blueprints.yaml").read_text())}
    assert set(canon) == set(mirror)
    drift = [bid for bid in sorted(canon) if canon[bid].get("composes") != mirror[bid].get("composes")]
    assert drift == [], f"mirror composes drifted from the real canon: {drift}"
