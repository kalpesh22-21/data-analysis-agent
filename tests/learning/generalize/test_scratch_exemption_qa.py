"""QA round 3 on the S4 scratch slice — the `declared_scratch` exemption.

The change under test RELAXES a fail-closed guard: `extract_column_provenance` /
`_build_alias_map` gained `declared_scratch: frozenset[str]`, and any `scratch.<name>` in
that set SKIPS `_validate_scratch_name` (the D64 session-ownership check). The only caller
is `builder._provenance_uses`, fed by `_declared_scratch(node)` — the keys of a node's
`consumes` whose ref matches `TABLE_CONSUME_REF`.

Two things make that worth attacking rather than reading:

  1. For a MINED candidate the `consumes` KEYS are LLM-authored free text. Nothing
     constrains a placeholder from being spelled exactly like a real materialized scratch
     table (`s_<sid>_<suffix>`), so the exemption's input is untrusted.
  2. The exemption also removes those columns from `uses`, and `uses` is the footprint
     access control reads. Anything that mis-attributes a WAREHOUSE column to a scratch
     source therefore silently shrinks the declared footprint.

Findings are split three ways in the test names:

  * `test_guard_*`   — a property that HOLDS; kept so the next relaxation has to break it.
  * `test_defect_*`  — FAILS against the code as committed. A real defect.
  * `test_pinned_*`  — current behaviour that is not (yet) a defect but is the sharp edge
                       the next change will cut itself on.

PRIORITY-1 VERDICT (cross-session contamination): DISPROVEN as a data read. A victim-named
placeholder does validate `ok` in S4 and does LAND, but the executor rewrites every
`scratch.<placeholder>` token to THIS session's materialized table before dispatch, and the
MCP's own extractor (separate repo, no `declared_scratch` parameter) re-checks the emitted
SQL against the caller's session. Both guards are pinned below so a future change that
removes either one fails here.

PRIORITY-2 VERDICT: the exemption DOES under-declare `uses`. `_build_alias_map` keys its
alias map by BARE table name, so a placeholder spelled like a warehouse table
(`consumes: {"payroll": "$0"}` → `FROM … JOIN scratch.payroll`) overwrites the warehouse
entry and every `payroll.<col>` reference is then dropped as "a scratch column". See
`test_guard_a_placeholder_shadowing_a_warehouse_table_fails_closed`.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from data_agent.learning.generalize.builder import (
    _declared_scratch,
    _provenance_uses,
    _scratch_column_schema,
    generalize_blueprint,
)
from data_agent.learning.generalize.validate import check_dag
from data_agent.runtime.blueprint.compiler import validate_blueprint_dag
from data_agent.runtime.blueprint.executor import _node_table_bindings
from data_agent.runtime.blueprint.models import Node
from data_agent.runtime.blueprint.template import bind_template
from data_agent.runtime.retrieval.corpus_loader import BlueprintSeed, CorpusLoadError
from data_agent.sqlparse import (
    ProvenanceExtractionError,
    ScratchSessionError,
    extract_column_provenance,
)

from .helpers import (
    CATALOG as FIXTURE_CATALOG,
)
from .helpers import (
    composite_sql_by_ref,
    load_plan,
    single_sql_by_ref,
)

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

# A name shaped EXACTLY like a real materialized scratch table: the write side mints
# `s_<sid>_bp_<uuid4hex>` and the runtime sanitizer mints an underscore-free `s<32hex>` sid,
# so `_validate_scratch_name` reads the owning session out of this as `deadbeefcafe`.
_VICTIM_TABLE = "s_deadbeefcafe_bp_0123456789abcdef"

_PRODUCER_SQL = (
    "SELECT toString(p.employee_code) AS employee_code, toFloat64(SUM(p.amount)) AS earnings "
    "FROM dbpcm_warehouse.payroll AS p WHERE p.register_type = 'EARN' GROUP BY p.employee_code"
)


def _join_sql(placeholder: str) -> str:
    return (
        "SELECT e.department_name AS department, SUM(x.earnings) AS total_earnings "
        f"FROM scratch.{placeholder} AS x "
        "JOIN dbpcm_warehouse.employee AS e ON e.employee_code = x.employee_code "
        "GROUP BY e.department_name"
    )


def _composite(
    placeholder: str,
    producer_sql: str = _PRODUCER_SQL,
    consumer_sql: str | None = None,
    *,
    consumes: dict[str, str] | None = None,
    output: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A two-node table-intermediate plan, with every attacker-controlled field open."""
    return {
        "intent": "earnings by department via a materialized per-employee join",
        "kind": "composite",
        "resolves": {},
        "source_tool_call_refs": ["tc0", "tc1"],
        "accepted_signal": "no_correction",
        "parameterization": [],
        "composes": [
            {
                "order": 0, "node_kind": "query", "step_intent": "produce",
                "feeds_from": [], "consumes": {},
                "output": output if output is not None else {placeholder: "table"},
                "source_tool_call_ref": "tc0", "when": None, "requires_approval": None,
            },
            {
                "order": 1, "node_kind": "query", "step_intent": "consume",
                "feeds_from": [0],
                "consumes": consumes if consumes is not None else {placeholder: "$0"},
                "output": {}, "source_tool_call_ref": "tc1",
                "when": None, "requires_approval": None,
            },
        ],
        "result_signature": None,
        "notes": "",
    }


def _generalize(payload: dict[str, Any], producer: str, consumer: str) -> Any:
    return generalize_blueprint(payload, {"tc0": producer, "tc1": consumer}, _CATALOG)


def _loader_verdict(gen: Any, placeholder: str, uses: list[str] | None = None) -> str:
    """`"accepted"`, or the `CorpusLoadError` text, for the blueprint S4 just stamped.

    The landing write is the authority S4 must not be LOOSER than: a candidate that
    reaches `outcome="ok"` has passed the human review valve, so a `CorpusLoadError`
    afterwards is a failure discovered too late to be reviewed.
    """
    seed = BlueprintSeed(
        id="bp-probe",
        intent="probe",
        slots_summary="",
        uses=list(gen.uses if uses is None else uses),
        result_grain=[],
        sql_template=None,
        composes=[
            {
                "order": 0,
                "output": {placeholder: "table"},
                "sql_template": gen.node_templates[0].sql_template,
            },
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {placeholder: "$0"},
                "output": {},
                "sql_template": gen.node_templates[1].sql_template,
            },
        ],
    )
    try:
        validate_blueprint_dag(seed)
    except CorpusLoadError as exc:
        return str(exc)
    return "accepted"


# ===========================================================================
# PRIORITY 1 — the D64 guarantees, unchanged by default
# ===========================================================================


@pytest.mark.parametrize(
    ("sql", "session_id", "needle"),
    [
        (
            f"SELECT x.employee_code AS c FROM scratch.{_VICTIM_TABLE} AS x",
            None,
            "without a bound session_id",
        ),
        (
            f"SELECT x.employee_code AS c FROM scratch.{_VICTIM_TABLE} AS x",
            "mysession",
            "does not match session_id",
        ),
        ("SELECT x.c AS c FROM scratch.not_a_scratch_name AS x", None, "without a bound"),
        ("SELECT x.c AS c FROM scratch.not_a_scratch_name AS x", "mysession", "does not match"),
        # An empty sid is falsy — it must not match `s__<suffix>` by extracting "".
        ("SELECT x.c AS c FROM scratch.s__tail AS x", "", "without a bound session_id"),
    ],
    ids=["victim-no-sid", "victim-wrong-sid", "bare-no-sid", "bare-wrong-sid", "empty-sid"],
)
def test_guard_d64_defaults_are_unchanged_without_declared_scratch(
    sql: str, session_id: str | None, needle: str
) -> None:
    """The whole point of a default-empty parameter: every pre-existing caller must behave
    byte-identically. `declared_scratch` is not passed, so EVERY `scratch.*` reference is
    still ownership-checked and still fail-closes."""
    with pytest.raises(ScratchSessionError) as exc:
        extract_column_provenance(sql, _CATALOG, session_id=session_id)
    assert needle in str(exc.value)


def test_guard_own_session_scratch_still_resolves_without_the_exemption() -> None:
    """Non-vacuity for the block above: the SAME shape with the OWNING session passes,
    so those five are refusals of ownership, not of scratch."""
    uses = extract_column_provenance(
        "SELECT x.employee_code AS c FROM scratch.s_mysid_bp_1 AS x",
        _CATALOG,
        session_id="mysid",
    )
    assert uses == frozenset({("scratch.s_mysid_bp_1", "employee_code")})


def test_guard_an_undeclared_scratch_source_still_fails_closed() -> None:
    """The exemption is per-NAME, not per-query: declaring `ph` does not vouch for the
    OTHER scratch source in the same statement. If this ever stops raising, the
    omit-the-header bypass D64 exists for is reopened."""
    sql = (
        "SELECT e.department_name AS d, SUM(x.earnings) AS n "
        "FROM scratch.ph AS x "
        f"JOIN scratch.{_VICTIM_TABLE} AS v ON v.employee_code = x.employee_code "
        "JOIN dbpcm_warehouse.employee AS e ON e.employee_code = x.employee_code "
        "GROUP BY e.department_name"
    )
    schema = {**_CATALOG, "scratch.ph": {"employee_code": "TEXT", "earnings": "TEXT"}}
    with pytest.raises(ScratchSessionError):
        extract_column_provenance(sql, schema, declared_scratch=frozenset({"ph"}))
    ok, uses = _provenance_uses(
        [sql],
        _CATALOG,
        per_node=[(frozenset({"ph"}), {"scratch.ph": {"employee_code": "TEXT", "earnings": "TEXT"}})],
    )
    assert (ok, uses) == (False, ())


def test_guard_the_exemption_does_not_leak_across_nodes() -> None:
    """Node 1's `consumes` must not vouch for node 0. `per_node` is indexed positionally,
    so a mis-alignment here would exempt the WRONG template — and node 0 is exactly the
    node with no `consumes` at all."""
    payload = _composite("emp")
    node0_reads_victim = (
        f"SELECT v.employee_code AS employee_code, v.earnings AS earnings "
        f"FROM scratch.{_VICTIM_TABLE} AS v"
    )
    gen = _generalize(payload, node0_reads_victim, _join_sql("emp"))
    assert gen.static_validation.outcome == "fail_to_review"
    assert gen.static_validation.reason == "explain_failed"
    assert gen.uses == ()


def test_guard_a_single_blueprint_never_declares_scratch() -> None:
    """`_generalize_single` passes no `per_node`, so the single path is the pre-change
    call verbatim. A `scratch.*` reference in a single template still fail-closes."""
    payload = {
        "intent": "borrowed scratch in a single blueprint",
        "kind": "single",
        "resolves": {},
        "source_tool_call_refs": ["tc0"],
        "accepted_signal": "no_correction",
        "parameterization": [],
        "composes": [],
        "result_signature": None,
        "notes": "",
    }
    gen = generalize_blueprint(
        payload,
        {"tc0": f"SELECT x.employee_code AS c FROM scratch.{_VICTIM_TABLE} AS x"},
        _CATALOG,
    )
    assert gen.static_validation.outcome == "fail_to_review"
    assert gen.static_validation.reason == "explain_failed"


def test_guard_the_runtime_provenance_capture_never_passes_declared_scratch() -> None:
    """DERIVED, not remembered: the D64 guarantee holds for the request path only while
    `capture_provenance` keeps calling the extractor with `session_id` and nothing else.
    Read the source rather than trusting the docstring."""
    from data_agent.runtime.provenance import capture

    source = inspect.getsource(capture)
    assert "declared_scratch" not in source
    signature = inspect.signature(extract_column_provenance)
    assert signature.parameters["declared_scratch"].default == frozenset()


_MCP_PROVENANCE = Path(
    "/Users/kalpeshmulye/Development/clickhouse-api/app/sqlparse/provenance.py"
)
_MCP_SERVICE = Path("/Users/kalpeshmulye/Development/clickhouse-api/app/service.py")


@pytest.mark.skipif(
    not _MCP_PROVENANCE.exists(), reason="the MCP repo is not checked out beside this one"
)
def test_guard_the_learning_plane_cannot_influence_the_mcp_recheck() -> None:
    """The enforcement point is the MCP's OWN copy of the extractor, in a separate repo.
    It has no `declared_scratch` parameter at all, so no learning-plane artifact can
    widen it — the emitted SQL is ownership-checked against the caller's session or the
    call raises SCRATCH_SESSION_VIOLATION. This is the reason the S4 relaxation is not a
    cross-session read; if the MCP ever grows the parameter, that reason evaporates."""
    assert "declared_scratch" not in _MCP_PROVENANCE.read_text()
    service = _MCP_SERVICE.read_text()
    assert "extract_column_provenance(clean_sql, catalog, session_id=session_id)" in service
    assert "SCRATCH_SESSION_VIOLATION" in service


# ===========================================================================
# PRIORITY 1 — the attack: an LLM-authored placeholder spelled like a real table
# ===========================================================================


def test_guard_a_placeholder_spelled_like_a_materialized_table_is_never_declared() -> None:
    """THE ATTACK INPUT — closed by `_MATERIALIZED_SCRATCH_NAME` in `_declared_scratch`.

    `_declared_scratch` reads `consumes` KEYS, which for a mined candidate are model text, so
    nothing structural stops a placeholder being spelled `s_<victim>_bp_<hex>`. The builder
    refuses to VOUCH for such a name, which drops it back onto the ordinary D64 path rather
    than special-casing it — the conservative direction, and the reason the whole shape ends
    at `fail_to_review/explain_failed` instead of `ok`.

    Both halves asserted: the declaration is empty, AND the candidate is refused. The first
    alone would pass if the extractor stopped checking; the second alone would pass for any
    unrelated reason."""
    payload = _composite(_VICTIM_TABLE)
    assert _declared_scratch(payload["composes"][1]) == frozenset()

    gen = _generalize(payload, _PRODUCER_SQL, _join_sql(_VICTIM_TABLE))
    assert gen.static_validation.outcome == "fail_to_review"
    assert gen.static_validation.reason == "explain_failed"
    assert gen.uses == ()
    # `check_dag` is blind to it — the refusal is the provenance walk's, not the DAG gate's.
    assert check_dag(payload["composes"]) is True


@pytest.mark.parametrize(
    ("placeholder", "declared"),
    [
        ("emp_earnings", True),
        ("earnings", True),
        ("s_earnings", True),          # one underscore-run: cannot be `s_<sid>_<suffix>`
        (_VICTIM_TABLE, False),
        ("s_a_b", False),
        ("s_emp_earnings", False),     # ⚠ a LEGITIMATE semantic name, refused by shape
    ],
    ids=["semantic", "bare", "s-prefix-only", "victim", "minimal-victim", "false-reject"],
)
def test_pinned_the_declaration_guard_is_a_name_shape_not_a_provenance_check(
    placeholder: str, declared: bool
) -> None:
    """The guard is `^s_.+_.+$` on the placeholder text, so it is exact about materialized
    tables and INEXACT about intent: a perfectly ordinary intermediate named `s_emp_earnings`
    is refused too, and the composite that uses it silently becomes
    `fail_to_review/explain_failed` with a reason that names provenance rather than the
    naming rule. Safe direction, and cheap to live with — pinned so the false-reject is a
    known cost rather than a mystery in a reviewer's queue."""
    node = {"consumes": {placeholder: "$0"}}
    assert (_declared_scratch(node) == frozenset({placeholder})) is declared


def test_guard_the_executor_rewrites_a_victim_named_placeholder_to_this_session() -> None:
    """DEPTH BEHIND THE NAME GUARD — the reason a victim-shaped placeholder was inert even
    before the builder refused to declare it, and the reason it stays inert if the shape
    guard is ever loosened.

    A landed table-consume placeholder is ALWAYS bound: `_node_table_bindings` either maps
    it to this session's materialized table or fails SLOT_INVALID, and `bind_template`
    then AST-rewrites the token. The victim's name never survives into dispatched SQL.
    If this ever emits the placeholder unchanged, the S4 relaxation becomes a live
    cross-session read."""
    node = Node.parse(
        {
            "order": 1,
            "feeds_from": [0],
            "consumes": {_VICTIM_TABLE: "$0"},
            "sql_template": _join_sql(_VICTIM_TABLE),
        }
    )

    bindings, fail = _node_table_bindings(node, {0: "scratch.s_mysession_bp_beef"})
    assert fail is None
    emitted = bind_template(node.sql_template, {}, table_bindings=bindings)
    assert "scratch.s_mysession_bp_beef" in emitted
    assert _VICTIM_TABLE not in emitted

    # …and an unmaterialized upstream is a refusal, not an unbound emit.
    _, missing = _node_table_bindings(node, {})
    assert missing is not None and missing.error_code


def test_pinned_the_unbacked_branch_is_unreachable_for_a_mined_composite() -> None:
    """`_rewrite_scratch_tables` is skipped when `table_bindings` is EMPTY, and that branch
    emits `scratch.<placeholder>` verbatim — the one way the victim's name could reach the
    warehouse. It is unreachable from this change: a node reaches it only with NO table
    consume, and a node with no table consume declares no scratch, so S4 fail-closes on
    its `scratch.*` source before it can ever land.

    Pinned in both halves so that closing the loader's missing converse gate (every
    `scratch.*` source must be a consumed placeholder) does not silently rely on S4."""
    unbacked = bind_template(_join_sql(_VICTIM_TABLE), {}, table_bindings={})
    assert f"scratch.{_VICTIM_TABLE}" in unbacked  # the branch really is a verbatim emit

    # …and S4 refuses to produce such a node: a scalar-only consume declares no scratch.
    payload = _composite(
        "n",
        consumes={"n": "$0.n"},
        output={"n": "scalar"},
    )
    gen = _generalize(
        payload,
        "SELECT COUNT(e.employee_code) AS n FROM dbpcm_warehouse.employee AS e",
        _join_sql(_VICTIM_TABLE),
    )
    assert _declared_scratch(payload["composes"][1]) == frozenset()
    assert gen.static_validation.outcome == "fail_to_review"
    assert gen.static_validation.reason == "explain_failed"


def test_pinned_the_primitive_still_exempts_any_name_the_caller_names() -> None:
    """THE SHARP EDGE, stated once: the name-shape guard lives in the CALLER
    (`builder._declared_scratch`), not in the primitive.

    `extract_column_provenance` itself will exempt ANY name handed to it — including one
    shaped exactly like another session's materialized table, and including when a session
    IS bound (the exemption is not "skip when there is no session", it is "skip, full
    stop"). Today that is safe because the only caller filters first and passes no
    `session_id`. It means a SECOND caller — or a request-path caller — inherits none of
    the protection, so this parameter must never be plumbed anywhere near a bound session."""
    sql = f"SELECT x.employee_code AS c FROM scratch.{_VICTIM_TABLE} AS x"
    schema = {**_CATALOG, f"scratch.{_VICTIM_TABLE}": {"employee_code": "TEXT"}}

    with pytest.raises(ScratchSessionError):
        extract_column_provenance(sql, schema, session_id="mysession")

    assert extract_column_provenance(
        sql,
        schema,
        session_id="mysession",
        declared_scratch=frozenset({_VICTIM_TABLE}),
    ) == frozenset({(f"scratch.{_VICTIM_TABLE}", "employee_code")})


# ===========================================================================
# PRIORITY 2 — correctness of the footprint
# ===========================================================================


def test_guard_uses_never_names_a_scratch_column() -> None:
    """The scope-honesty half of the change: the footprint is a claim about the WAREHOUSE."""
    gen = _generalize(_composite("emp_earnings"), _PRODUCER_SQL, _join_sql("emp_earnings"))
    assert gen.static_validation.outcome == "ok"
    assert gen.uses
    assert not any(u.startswith("scratch.") for u in gen.uses)
    assert set(gen.uses) == {
        "dbpcm_warehouse.employee.department_name",
        "dbpcm_warehouse.employee.employee_code",
        "dbpcm_warehouse.payroll.amount",
        "dbpcm_warehouse.payroll.employee_code",
        "dbpcm_warehouse.payroll.register_type",
    }


# The shadowing shape, used by the three defect tests below. The placeholder is spelled
# `payroll` — a legal `consumes` key, and also the name of a real warehouse table.
_SHADOW_CONSUMER = (
    "SELECT payroll.employee_code AS ec, SUM(payroll.amount) AS s "
    "FROM dbpcm_warehouse.payroll "
    "JOIN scratch.payroll AS x ON x.employee_code = payroll.employee_code "
    "WHERE payroll.register_type = 'EARN' "
    "GROUP BY payroll.employee_code"
)
_SHADOW_TRUTH = {
    "dbpcm_warehouse.payroll.amount",
    "dbpcm_warehouse.payroll.employee_code",
    "dbpcm_warehouse.payroll.register_type",
}


def test_guard_a_placeholder_shadowing_a_warehouse_table_fails_closed() -> None:
    """DEFECT (real, footprint under-declaration — the fail-OPEN direction).

    `_build_alias_map` keys its alias map by BARE table name, so the second source named
    `payroll` overwrites the first. With `FROM dbpcm_warehouse.payroll JOIN scratch.payroll
    AS x`, the SCRATCH entry wins, every `payroll.<col>` reference resolves to
    `scratch.payroll`, and `_provenance_uses` then DROPS all of them as scratch columns.

    Observed: ok=True, uses=()  — a template that demonstrably reads three warehouse
    columns declares an empty warehouse footprint.
    Expected: uses ⊇ the three columns the same template yields with the scratch JOIN
    removed (asserted below as the non-vacuity control), or a fail-closed refusal.

    This is the exact class the D70 comments in `provenance.py` call fail-open: an
    understated USES set is "⊆ any scope". It is only reachable because the scratch branch
    no longer refuses; before this change the template raised ScratchSessionError."""
    honest = _SHADOW_CONSUMER.replace(
        "JOIN scratch.payroll AS x ON x.employee_code = payroll.employee_code ", ""
    )
    truth_ok, truth_uses = _provenance_uses([honest], _CATALOG)
    assert (truth_ok, set(truth_uses)) == (True, _SHADOW_TRUTH)  # non-vacuity control

    ok, uses = _provenance_uses(
        [_SHADOW_CONSUMER],
        _CATALOG,
        per_node=[
            (
                frozenset({"payroll"}),
                {"scratch.payroll": {"employee_code": "TEXT", "amount": "TEXT"}},
            )
        ],
    )
    # FIXED: a placeholder shadowing a catalog table name is no longer DECLARED, so the
    # reference falls back onto the ordinary D64 fail-closed path instead of resolving to
    # the scratch table and having its warehouse columns dropped.
    assert ok is False
    assert not uses  # refused, so nothing is claimed


def test_guard_a_shadowed_composite_is_refused_at_s4_not_at_the_landing_write() -> None:
    """DEFECT (real, S4 LOOSER than the loader) — the consequence of the defect above.

    The under-declared footprint is caught by no offline gate: `check_dag` never sees
    templates and `decide_outcome` only reads flags, so the candidate reaches
    `outcome="ok"`, passes the human review valve as promotable, and dies at the LANDING
    write with a CorpusLoadError. `test_dag_loader_parity_qa.py` names this direction as
    the class it exists to prevent — "the candidate promotes and then dies at the landing
    write, past the review valve that was supposed to catch it".

    The producer here reads only `employee`, so nothing else contributes the shadowed
    `payroll` columns to `uses` and the omission is visible. (With a producer that happens
    to read the same table the omission is masked — which is why the defect above is
    asserted against the extractor directly.)

    Observed: S4 outcome="ok"; loader refuses the whole blueprint.
    Expected: S4 refuses whatever the loader refuses."""
    employee_producer = (
        "SELECT toString(e.employee_code) AS employee_code, "
        "e.department_name AS department_name FROM dbpcm_warehouse.employee AS e"
    )
    payload = _composite("payroll")
    gen = _generalize(payload, employee_producer, _SHADOW_CONSUMER)
    assert gen.static_validation.outcome == "fail_to_review"
    assert not any(u.startswith("dbpcm_warehouse.payroll.") for u in gen.uses)

    # NO DIVERGENCE LEFT TO ASSERT. This used to land and then be refused by the loader —
    # late, loud, and past the review valve. S4 now refuses first, so there is no landing
    # attempt to compare against; that the two agree IS the property.


def test_guard_a_shadowing_placeholder_cannot_fabricate_a_warehouse_uses_entry() -> None:
    """DEFECT (real, the OTHER direction — an invented footprint entry).

    Reverse the source order and the warehouse entry wins the alias-map collision instead,
    so the SCRATCH table's columns are attributed to the WAREHOUSE table. The producer's
    SELECT aliases are model-chosen, so the model gets to write arbitrary column names into
    a `database.table.column` scope key — here `dbpcm_warehouse.employee.secret_bonus`,
    a column that does not exist in the catalog.

    `validate_blueprint_uses` only shape-checks the key (3 non-empty dotted parts), so the
    lie lands. Direction is over-declaring, so it is not an access-control hole; it is a
    corpus-integrity one — the scope pre-filter at recall now demands a column no scope can
    grant, and the blueprint is silently unrecallable.

    Observed: uses contains `dbpcm_warehouse.employee.secret_bonus`.
    Expected: `uses` contains only columns of tables the template actually reads."""
    catalog = {"dbpcm_warehouse.employee": {"employee_code": "String", "department_name": "String"}}
    producer = (
        "SELECT toString(e.employee_code) AS employee_code, 'x' AS secret_bonus "
        "FROM dbpcm_warehouse.employee AS e"
    )
    consumer = (
        "SELECT employee.secret_bonus AS b, e.department_name AS d "
        "FROM scratch.employee "
        "JOIN dbpcm_warehouse.employee AS e ON e.employee_code = employee.employee_code "
        "GROUP BY e.department_name"
    )
    payload = _composite("employee")
    gen = generalize_blueprint(payload, {"tc0": producer, "tc1": consumer}, catalog)

    assert gen.static_validation.outcome == "fail_to_review"
    # THE LIE NO LONGER LANDS. It used to: the scratch table's model-chosen aliases were
    # attributed to the warehouse table, writing `database.table.column` scope keys for columns
    # that do not exist — over-declaring, so not an access hole, but the recall scope pre-filter
    # then demanded a column no scope can grant and the blueprint was silently unrecallable.
    assert "not in the declared uses footprint" in _loader_verdict(gen, "employee")
    invented = [
        u for u in gen.uses if u.split(".")[-1] not in catalog["dbpcm_warehouse.employee"]
    ]
    assert invented == [], f"uses invents warehouse column(s) {invented}"


def test_defect_registering_the_scratch_schema_shifts_the_inferred_default_database() -> None:
    """DEFECT (real, silent capability loss).

    `extract_column_provenance` infers the default database for BARE table names from the
    catalog keys: one distinct database ⇒ that database, otherwise the hardcoded
    `dbpcm_warehouse`. `_provenance_uses` merges `scratch.<placeholder>` into the same flat
    dict, which makes the catalog look TWO-database — so a node that JOINs a scratch
    intermediate resolves bare table names against `dbpcm_warehouse` while its sibling node
    in the SAME candidate resolves them against the real database.

    Uses this repo's own S4 fixture catalog (`helpers.CATALOG`, single database `payroll`),
    so this is not a synthetic catalog shape.

    Observed: ok=True without the scratch JOIN, ok=False with it — the whole candidate goes
    to fail_to_review/explain_failed because of a table it never named.
    Expected: the scratch registration must not change how warehouse names are qualified."""
    without = "SELECT sum(f.gross_pay) AS t FROM payroll_fact AS f"
    with_scratch = (
        "SELECT sum(f.gross_pay) AS t FROM payroll_fact AS f "
        "JOIN scratch.ph AS x ON x.department = f.department"
    )
    assert _provenance_uses([without], FIXTURE_CATALOG) == (
        True,
        ("payroll.payroll_fact.gross_pay",),
    )
    # CONTROL, and the pointer at the cause: the SAME statement with the SAME declaration
    # but WITHOUT the scratch key merged into the catalog qualifies fine. It is the
    # registration, not the scratch source, that moves the default database.
    assert extract_column_provenance(
        with_scratch, FIXTURE_CATALOG, declared_scratch=frozenset({"ph"})
    ) >= frozenset({("payroll.payroll_fact", "gross_pay")})

    ok, _uses = _provenance_uses(
        [with_scratch],
        FIXTURE_CATALOG,
        per_node=[(frozenset({"ph"}), {"scratch.ph": {"department": "TEXT"}})],
    )
    assert ok is True, (
        "a bare table name that qualifies in a scratch-free node stops qualifying once a "
        "scratch placeholder is registered — the default database flipped to the module "
        "fallback"
    )


# ---------------------------------------------------------------------------
# PRIORITY 2 — the shapes that DO behave
# ---------------------------------------------------------------------------


def test_guard_a_producer_with_no_named_selects_fails_closed() -> None:
    """`_template_output_columns` yields no usable columns, the placeholder registers an
    EMPTY column map, and sqlglot's MappingSchema refuses it — caught as a
    ProvenanceExtractionError, so the candidate is reviewed rather than promoted with an
    unverifiable scratch source. Matches the loader's own `un_schemad` refusal."""
    ok, uses = _provenance_uses(
        [_join_sql("ph")],
        _CATALOG,
        per_node=[(frozenset({"ph"}), {"scratch.ph": {}})],
    )
    assert (ok, uses) == (False, ())


def test_pinned_an_unaliased_producer_projection_registers_a_nameless_column() -> None:
    """PRE-EXISTING (mirrored, not introduced): `_template_output_columns` returns a single
    EMPTY name for a projection with no alias, so `_scratch_column_schema` registers
    `{"": "TEXT"}` — non-empty, so the loader's `un_schemad` fail-closed gate (`if not
    cols`) does not fire on this shape either. The builder faithfully mirrors the loader,
    including this. Pinned so the gap is a known shape of the seam."""
    schema = _scratch_column_schema(
        {"consumes": {"a": "$0"}},
        {0: "SELECT COUNT(*) FROM dbpcm_warehouse.payroll"},
    )
    assert schema == {"scratch.a": {"": "TEXT"}}
    assert schema["scratch.a"], "a truthy-but-nameless map slips past `if not cols`"


def test_guard_a_consumer_reading_an_unprojected_scratch_column_fails_closed() -> None:
    """The registered scratch schema is a real constraint, not decoration: a column the
    producer never projected is unresolvable and fails closed."""
    sql = (
        "SELECT e.department_name AS d, SUM(x.bogus) AS n "
        "FROM scratch.ph AS x JOIN dbpcm_warehouse.employee AS e "
        "ON e.employee_code = x.employee_code GROUP BY e.department_name"
    )
    ok, _ = _provenance_uses(
        [sql], _CATALOG, per_node=[(frozenset({"ph"}), {"scratch.ph": {"employee_code": "TEXT"}})]
    )
    assert ok is False


def test_guard_an_ambiguous_column_shared_with_the_scratch_schema_fails_closed() -> None:
    """A bare column present in BOTH the scratch schema and a warehouse source cannot be
    attributed, so it is refused rather than silently assigned to scratch (which would be
    the same under-declaration as the shadowing defect, by a different route)."""
    sql = (
        "SELECT department_name, COUNT(*) AS c "
        "FROM scratch.ph AS x JOIN dbpcm_warehouse.employee AS e "
        "ON e.department_name = x.department_name GROUP BY department_name"
    )
    ok, _ = _provenance_uses(
        [sql], _CATALOG, per_node=[(frozenset({"ph"}), {"scratch.ph": {"department_name": "TEXT"}})]
    )
    assert ok is False


def test_guard_a_node_consuming_a_scalar_and_a_table_validates() -> None:
    """The mixed shape: only the `$N` consume is declared scratch; the `$N.name` scalar is
    a `{token}` bind and contributes nothing to the exemption."""
    node = {"consumes": {"emp": "$0", "avg": "$0.avg_amount", "junk": 7}}
    assert _declared_scratch(node) == frozenset({"emp"})

    producer = (
        "SELECT toString(p.employee_code) AS employee_code, "
        "toFloat64(SUM(p.amount)) AS earnings, toFloat64(1) AS avg_amount "
        "FROM dbpcm_warehouse.payroll AS p GROUP BY p.employee_code"
    )
    consumer = (
        "SELECT e.department_name AS d, SUM(x.earnings) AS n "
        "FROM scratch.emp AS x JOIN dbpcm_warehouse.employee AS e "
        "ON e.employee_code = x.employee_code WHERE x.earnings > 5 "
        "GROUP BY e.department_name"
    )
    payload = _composite(
        "emp",
        consumes={"emp": "$0", "avg": "$0.avg_amount"},
        output={"emp": "table", "avg_amount": "scalar"},
    )
    gen = _generalize(payload, producer, consumer)
    assert gen.static_validation.outcome == "ok"
    assert not any(u.startswith("scratch.") for u in gen.uses)


def test_guard_two_consumers_of_different_tables_stay_aligned() -> None:
    """`per_node` is a POSITIONAL list zipped against `templates`. A three-node DAG where
    each consumer reads a DIFFERENT placeholder would silently exempt the wrong name if the
    two lists ever drifted; here both are built from `node_templates` in one order."""
    payload: dict[str, Any] = {
        "intent": "two intermediates",
        "kind": "composite",
        "resolves": {},
        "source_tool_call_refs": ["tc0", "tc1", "tc2"],
        "accepted_signal": "no_correction",
        "parameterization": [],
        "composes": [
            {"order": 0, "node_kind": "query", "step_intent": "", "feeds_from": [],
             "consumes": {}, "output": {"a": "table"}, "source_tool_call_ref": "tc0",
             "when": None, "requires_approval": None},
            {"order": 1, "node_kind": "query", "step_intent": "", "feeds_from": [],
             "consumes": {}, "output": {"b": "table"}, "source_tool_call_ref": "tc1",
             "when": None, "requires_approval": None},
            {"order": 2, "node_kind": "query", "step_intent": "", "feeds_from": [0, 1],
             "consumes": {"a": "$0", "b": "$1"}, "output": {},
             "source_tool_call_ref": "tc2", "when": None, "requires_approval": None},
        ],
        "result_signature": None,
        "notes": "",
    }
    gen = generalize_blueprint(
        payload,
        {
            "tc0": "SELECT toString(p.employee_code) AS employee_code FROM dbpcm_warehouse.payroll AS p",
            "tc1": "SELECT e.employee_code AS employee_code, e.department_name AS department_name "
                   "FROM dbpcm_warehouse.employee AS e",
            "tc2": "SELECT b.department_name AS d, COUNT(a.employee_code) AS n "
                   "FROM scratch.a AS a JOIN scratch.b AS b "
                   "ON b.employee_code = a.employee_code GROUP BY b.department_name",
        },
        _CATALOG,
    )
    assert check_dag(payload["composes"]) is True
    assert gen.static_validation.outcome == "ok"
    assert set(gen.uses) == {
        "dbpcm_warehouse.employee.department_name",
        "dbpcm_warehouse.employee.employee_code",
        "dbpcm_warehouse.payroll.employee_code",
    }


def test_guard_a_consume_ref_to_a_nonexistent_order_never_reaches_the_schema_builder() -> None:
    """`_scratch_column_schema` degrades to an empty column map for an unresolvable `$N`
    (which then fails closed), but the DAG gate runs FIRST and refuses the plan outright —
    both halves pinned so neither can be removed as 'unreachable'."""
    payload = _composite("emp", consumes={"emp": "$7"})
    assert check_dag(payload["composes"]) is False
    gen = _generalize(payload, _PRODUCER_SQL, _join_sql("emp"))
    assert gen.static_validation.outcome == "fail_to_review"
    assert gen.static_validation.reason == "dag_invalid"

    assert _scratch_column_schema({"consumes": {"emp": "$7"}}, {0: _PRODUCER_SQL}) == {
        "scratch.emp": {}
    }


def test_guard_the_scratch_schema_is_read_from_templates_not_from_the_plan_node() -> None:
    """The regression the change's own docstring names: an S3 `composes` entry carries
    `source_tool_call_ref` and never SQL, so sourcing the producer's columns from the plan
    node registers ZERO columns and fails identically to no fix at all."""
    node = {"consumes": {"emp": "$0"}, "source_tool_call_ref": "tc0", "order": 1}
    from_templates = _scratch_column_schema(node, {0: _PRODUCER_SQL})
    assert from_templates == {"scratch.emp": {"employee_code": "TEXT", "earnings": "TEXT"}}
    assert _scratch_column_schema(node, {}) == {"scratch.emp": {}}


@pytest.mark.parametrize(
    "node", [None, [], "consumes", {"consumes": None}, {"consumes": []}, {}]
)
def test_guard_declared_scratch_is_total_on_a_poisoned_plan_node(node: Any) -> None:
    """S4's contract is that it never raises for a bad candidate; `consumes` is rehydrated
    model JSON, so every wrong type must degrade to 'declares nothing'."""
    assert _declared_scratch(node) == frozenset()


# ===========================================================================
# PRIORITY 3 — regression: the parameter is inert everywhere else
# ===========================================================================


def test_guard_the_single_fixture_generalizes_exactly_as_before() -> None:
    """The frozen S4 contract fixture. The single path passes no `per_node` at all."""
    gen = generalize_blueprint(load_plan()["single"], single_sql_by_ref(), FIXTURE_CATALOG)
    assert gen.static_validation.outcome == "ok"
    assert gen.uses == (
        "payroll.payroll_fact.department",
        "payroll.payroll_fact.gross_pay",
        "payroll.payroll_fact.pay_period",
        "payroll.payroll_fact.record_type",
        "payroll.payroll_fact.region",
    )


def test_guard_the_scalar_composite_fixture_generalizes_exactly_as_before() -> None:
    """A SCALAR composite: `_declared_scratch` is empty for every node, `_scratch_column_
    schema` is `{}`, so the extractor call is byte-identical to the pre-change one."""
    plan = load_plan()["composite"]
    gen = generalize_blueprint(plan, composite_sql_by_ref(), FIXTURE_CATALOG)
    assert gen.static_validation.outcome == "ok"
    assert all(_declared_scratch(n) == frozenset() for n in plan["composes"])

    templates = [t.sql_template for t in gen.node_templates]
    assert _provenance_uses(templates, FIXTURE_CATALOG) == (True, gen.uses)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT sum(gross_pay) AS t FROM payroll.payroll_fact WHERE region = 'NA'",
        "SELECT department, count() AS c FROM payroll.payroll_fact GROUP BY department "
        "ORDER BY c DESC",
        "WITH x AS (SELECT gross_pay, department FROM payroll.payroll_fact) "
        "SELECT department, sum(gross_pay) AS t FROM x GROUP BY department",
    ],
    ids=["filter", "group-order", "cte"],
)
def test_guard_an_empty_declaration_is_the_pre_change_call(sql: str) -> None:
    """Derived equivalence rather than a remembered one: for any template with no scratch
    source, the three call shapes (no `per_node`, an empty declaration, and the bare
    extractor) must agree exactly."""
    baseline = _provenance_uses([sql], FIXTURE_CATALOG)
    assert baseline == _provenance_uses(
        [sql], FIXTURE_CATALOG, per_node=[(frozenset(), {})]
    )
    direct = extract_column_provenance(sql, FIXTURE_CATALOG)
    assert baseline == (True, tuple(sorted(f"{t}.{c}" for t, c in direct)))


def test_guard_a_scratch_free_composite_is_unaffected_by_the_new_code_path() -> None:
    """Belt and braces on the merge: with no table consume the merged schema IS the catalog
    object, not a copy — so nothing about warehouse qualification can have changed."""
    payload = _composite("n", consumes={"n": "$0.n"}, output={"n": "scalar"})
    gen = _generalize(
        payload,
        "SELECT COUNT(e.employee_code) AS n FROM dbpcm_warehouse.employee AS e",
        "SELECT e.department_name AS d, COUNT(e.employee_code) AS c "
        "FROM dbpcm_warehouse.employee AS e GROUP BY e.department_name",
    )
    assert gen.static_validation.outcome == "ok"
    assert set(gen.uses) == {
        "dbpcm_warehouse.employee.department_name",
        "dbpcm_warehouse.employee.employee_code",
    }
    assert _scratch_column_schema(payload["composes"][1], {0: "SELECT 1 AS a"}) == {}


def test_guard_an_unparsable_template_is_still_a_provenance_failure() -> None:
    """The exemption must not swallow the ordinary fail-closed paths."""
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance("DROP TABLE x", _CATALOG, declared_scratch=frozenset({"x"}))
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance("", _CATALOG, declared_scratch=frozenset({"x"}))
