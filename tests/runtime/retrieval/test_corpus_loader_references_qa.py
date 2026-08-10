"""QA (adversarial): load-time blueprint REFERENCES — the gaps left by
`test_corpus_loader_references.py`.

That file pins the designed behaviour. This one attacks it, and is organized around
the three things an independent reviewer has to establish rather than take on trust:

  1. **Is inlining genuinely byte-preserving?** Not "the digest matches" — the digest
     is derived from the same templates the resolver produced. The nodes the resolver
     emits are compared against the SQL the composite carried BEFORE the conversion,
     pinned here verbatim from the pre-`§2b` YAML, key set and all.
  2. **Is the `uses` UNION rule a second assertion, or the same one twice?** Gate (c)
     re-checks the inlined SQL against the parent's `uses`, so any test where the
     child's SQL reads the missing column proves nothing about the union rule. The
     tests below make the two gates DISAGREE — a column the child DECLARES and its
     SQL never names — so only the union rule can fire, and the contrast case shows
     gate (c) passing on exactly that shape.
  3. **Does anything escape as a non-`CorpusLoadError`?** `resolve_blueprint_references`
     runs inside `load_corpus`, which the hydrator re-arms and retries every turn, so
     an un-wrapped exception bricks the corpus. Three of these fail today.

FAILING TESTS IN THIS FILE ARE DEFECT REPORTS, NOT SCAFFOLDING. Each carries the
reproduction and the consequence in its docstring.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from data_agent.runtime.blueprint.models import Blueprint, BlueprintParseError, Node
from data_agent.runtime.retrieval.corpus_loader import (
    BlueprintSeed,
    CorpusLoadError,
    _dag_properties,
    _validate_blueprint_dag,
    load_corpus,
    load_seed_fixtures,
    resolve_blueprint_references,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "corpus"
_E = "dbpcm_warehouse.employee"
_COMPOSITE = "bp-compare-employee-check-detail-two-periods"
_CHILD = "bp-employee-check-detail-for-period"


# --------------------------------------------------------------------------
# helpers — a leaf child and a parent whose only node is a pure reference
# --------------------------------------------------------------------------


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


class _StubEmbedder:
    """Deterministic fixed-width vectors — `load_corpus` only needs a shape."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 8 for _ in texts]

    async def embed_one(self, text: str) -> list[float]:
        return [0.1] * 8


class _StubResult:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    async def single(self) -> dict[str, Any] | None:
        return self._row

    async def data(self) -> list[dict[str, Any]]:
        return [] if self._row is None else [self._row]


class _RecordingTx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, query: str, **params: Any) -> _StubResult:
        self.calls.append((query, params))
        if "embedding_model" in query or "models" in query.lower():
            return _StubResult({"models": []})
        if "RETURN collect(column_key)" in query or "missing" in query:
            return _StubResult({"missing": []})
        return _StubResult(None)


class _RecordingSession:
    def __init__(self, driver: _RecordingDriver) -> None:
        self._driver = driver

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def run(self, query: str, **params: Any) -> _StubResult:
        return await self._driver.tx.run(query, **params)

    async def execute_write(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await fn(self._driver.tx, *args, **kwargs)

    async def execute_read(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await fn(self._driver.tx, *args, **kwargs)


class _RecordingDriver:
    def __init__(self) -> None:
        self.tx = _RecordingTx()

    def session(self, **kwargs: Any) -> _RecordingSession:
        return _RecordingSession(self)


# ==========================================================================
# 1. byte-preservation, verified against the PRE-conversion YAML
# ==========================================================================

# `bp-compare-employee-check-detail-two-periods`' `composes` list EXACTLY as it stood
# at the commit before plan §2b, copied verbatim out of
# `clickhouse-api/app/corpus/data/blueprints/` at that revision. This is the only
# independent witness available: every other artifact (the structural key, the child
# blueprint, the fixture mirror) is downstream of the conversion, so comparing the
# resolver's output against any of them is circular. Nodes 0 and 1 are the two that
# became references; node 2 never changed and is included so the comparison covers
# the WHOLE list — a reference that perturbed a sibling node would be just as bad.
_PRE_2B_COMPOSES: list[dict[str, Any]] = [
    {
        "order": 0,
        "output": {"detail_a": "table"},
        "sql_template": (
            "SELECT toString(register_type) AS register_type,\n"
            "       toString(type_code) AS type_code,\n"
            "       any(toString(type_code_description)) AS type_code_description,\n"
            "       toFloat64(SUM(amount)) AS amount,\n"
            "       toFloat64(SUM(type_hours)) AS hours\n"
            "FROM dbpcm_warehouse.payroll\n"
            "WHERE employee_code = {employee}\n"
            "  AND pay_period_end_date = {period_a}\n"
            "GROUP BY register_type, type_code\n"
        ),
    },
    {
        "order": 1,
        "output": {"detail_b": "table"},
        "sql_template": (
            "SELECT toString(register_type) AS register_type,\n"
            "       toString(type_code) AS type_code,\n"
            "       any(toString(type_code_description)) AS type_code_description,\n"
            "       toFloat64(SUM(amount)) AS amount,\n"
            "       toFloat64(SUM(type_hours)) AS hours\n"
            "FROM dbpcm_warehouse.payroll\n"
            "WHERE employee_code = {employee}\n"
            "  AND pay_period_end_date = {period_b}\n"
            "GROUP BY register_type, type_code\n"
        ),
    },
    {
        "order": 2,
        "feeds_from": [0, 1],
        "consumes": {"detail_a": "$0", "detail_b": "$1"},
        "output": {},
        "sql_template": (
            "SELECT coalesce(a.register_type, b.register_type) AS register_type,\n"
            "       coalesce(a.type_code, b.type_code) AS type_code,\n"
            "       coalesce(a.type_code_description, b.type_code_description) AS "
            "type_code_description,\n"
            "       coalesce(a.amount, 0) AS amount_a,\n"
            "       coalesce(b.amount, 0) AS amount_b,\n"
            "       coalesce(b.amount, 0) - coalesce(a.amount, 0) AS amount_delta,\n"
            "       coalesce(a.hours, 0) AS hours_a,\n"
            "       coalesce(b.hours, 0) AS hours_b\n"
            "FROM scratch.detail_a AS a\n"
            "FULL OUTER JOIN scratch.detail_b AS b\n"
            "  ON a.register_type = b.register_type AND a.type_code = b.type_code\n"
            "ORDER BY coalesce(a.register_type, b.register_type),\n"
            "         coalesce(a.type_code, b.type_code)\n"
        ),
    },
]


def test_the_resolved_canon_composite_is_byte_identical_to_the_pre_conversion_yaml() -> None:
    """Requirement 1, checked against the SQL the composite carried before §2b rather
    than against anything the conversion produced.

    Stronger than the digest claim in two ways. The structural key normalizes through
    sqlglot, so it survives whitespace, casing and comment changes that the executor
    would not care about but that a reviewer reading a diff would; equality of the raw
    node dicts survives nothing. And it covers the node KEY SETS, not just the
    templates — `_inline_reference` builds a fresh dict, and a dropped `output` or a
    leaked `ref` would still hash to the same key."""
    resolved = _resolved(load_seed_fixtures(_FIXTURE_DIR)[0])[_COMPOSITE]
    assert resolved.composes == _PRE_2B_COMPOSES


def test_inlining_moves_no_other_canon_blueprint(request: Any) -> None:
    """The conversion touched one file, but resolution runs over the WHOLE corpus and
    rebuilds every seed's list. A key that moved on an unrelated blueprint would mean
    the resolver is not the identity on reference-free input."""
    seeds = load_seed_fixtures(_FIXTURE_DIR)[0]
    before = {bp.id: _dag_properties(bp)["structural_key"] for bp in seeds if bp.id != _COMPOSITE}
    after = {
        bp.id: _dag_properties(bp)["structural_key"]
        for bp in resolve_blueprint_references(seeds)
        if bp.id != _COMPOSITE
    }
    assert after == before
    # …and the seeds that hold no reference come back by IDENTITY, not by copy.
    resolved = {bp.id: bp for bp in resolve_blueprint_references(seeds)}
    originals = {bp.id: bp for bp in seeds}
    for bp_id, bp in originals.items():
        if bp_id == _COMPOSITE:
            continue
        assert resolved[bp_id] is bp, f"{bp_id} was rebuilt despite holding no reference"


def test_the_extracted_child_carries_the_composites_casts_verbatim() -> None:
    """The child's `toString`/`toFloat64` casts are load-bearing for the composite's
    terminal FULL OUTER JOIN (stable String join keys, stable Float64 measures). A
    child edit that dropped one would change the join's behaviour, not just its text —
    so pin that the child's SQL IS the pre-conversion node SQL, modulo the slot."""
    child = {bp.id: bp for bp in load_seed_fixtures(_FIXTURE_DIR)[0]}[_CHILD]
    assert child.sql_template == _PRE_2B_COMPOSES[0]["sql_template"].replace(
        "{period_a}", "{period}"
    )


# ==========================================================================
# 2. DEFECT — an un-wrapped `TypeError` escapes `load_corpus`
# ==========================================================================


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({1: "x", "y": "z"}, id="int-and-str-keys"),
        # `on:`/`off:`/`yes:`/`no:`/`y:`/`n:` all load as BOOLEANS under PyYAML's
        # YAML-1.1 resolver, which is how this arrives from a hand-edited corpus file
        # far more often than a deliberate integer key does.
        pytest.param({True: "x", "y": "z"}, id="bool-and-str-keys"),
        pytest.param({None: "x", "y": "z"}, id="null-and-str-keys"),
    ],
)
def test_a_ref_with_mixed_type_unknown_keys_raises_corpus_load_error_not_a_type_error(
    extra: dict[Any, Any],
) -> None:
    """DEFECT. `_node_reference` reports unknown `ref` keys with
    `sorted(k for k in raw if k not in _REF_KEYS)`. With TWO unknown keys whose types
    do not order against each other, `sorted()` raises

        TypeError: '<' not supported between instances of 'str' and 'bool'

    un-wrapped, out of `resolve_blueprint_references` and therefore out of
    `load_corpus`. That is the exact class the module docstring forbids and the
    reference resolver was written to close: the hydrator's self-heal poll re-arms and
    retries the same poisoned entry every turn, so the corpus stays bricked until a
    human edits the YAML.

    Reproduction, straight from a `.yaml` file (no JSON needed — YAML keys are typed):

        composes:
        - order: 0
          ref:
            blueprint: bp-child
            on: something          # -> the BOOLEAN key True
            note: whatever         # -> the STRING key 'note'

    ONE unknown key of any type is fine (a one-element `sorted()` never compares), so
    the guard looks correct in every single-key test — the crash needs two.

    The fix is to sort on a total key, e.g. `sorted(map(repr, ...))` or
    `sorted(raw, key=lambda k: (type(k).__name__, repr(k)))`; the error message wants
    the offending keys either way."""
    ref = {"blueprint": "bp-child", "slots": {"department": "dept"}, **extra}
    with pytest.raises(CorpusLoadError):
        resolve_blueprint_references([_leaf(), _parent(ref=ref)])


async def test_the_mixed_key_crash_escapes_load_corpus_itself() -> None:
    """The blast radius of the defect above, at the boundary the rule is written
    about. `resolve_blueprint_references` is a helper; `load_corpus` is the thing the
    hydrator calls on a poll, and the requirement is that NOTHING leaves it except a
    `CorpusLoadError`. Asserted here so a fix that only tightens the helper's unit
    test cannot be mistaken for closing the hole."""
    ref = {"blueprint": "bp-child", "slots": {"department": "dept"}, True: "x", "y": "z"}
    with pytest.raises(CorpusLoadError):
        await load_corpus(
            _RecordingDriver(),  # type: ignore[arg-type]
            _StubEmbedder(),  # type: ignore[arg-type]
            [_leaf(), _parent(ref=ref)],
            [],
            model_id="all-mpnet-base-v2",
        )


def test_the_unknown_ref_key_error_still_names_the_offending_keys() -> None:
    """The companion to the fix: whatever ordering replaces the bare `sorted()`, the
    author still has to be told WHICH key was rejected, or the refusal is unactionable
    on a file with a dozen nodes."""
    ref = {"blueprint": "bp-child", "slots": {"department": "dept"}, True: "x", "y": "z"}
    with pytest.raises(CorpusLoadError) as exc:
        resolve_blueprint_references([_leaf(), _parent(ref=ref)])
    assert "y" in str(exc.value)


# ==========================================================================
# 3. DEFECT — `ref:` with no body is silently dropped, both times
# ==========================================================================


def test_a_ref_key_with_a_null_body_is_refused() -> None:
    """DEFECT. `_node_reference` starts `raw = node.get(NODE_REF_KEY)` /
    `if raw is None: return None`, so a node that DECLARES `ref` with an empty body is
    read as "this node holds no reference" and passed through untouched. The
    parse-layer backstop then misses it for the identical reason
    (`if raw.get(NODE_REF_KEY) is not None`).

    Reproduction — one deleted line in a real corpus file:

        composes:
        - order: 0
          output: {detail_a: table}
          ref:                       # body removed; the KEY is still here

    The consequence is precisely the one `Node.parse`'s backstop docstring names: the
    node parses cleanly as a template-LESS query node, and
    `executor._execute_dag` skips it (`if not node.sql_template: ... continue`,
    recording an empty output) — a silently empty step in a DAG the corpus advertises
    as validated.

    `ref: null` is distinguishable from an absent `ref` (`NODE_REF_KEY in node`), so
    the fix is a membership test rather than a `None` test, in BOTH places."""
    parent = _parent(composes=[{"order": 0, "output": {}, "ref": None}])
    with pytest.raises(CorpusLoadError):
        resolve_blueprint_references([_leaf(), parent])


def test_the_parse_layer_backstop_also_catches_a_null_bodied_reference() -> None:
    """The read side of the same defect, asserted separately because the two checks
    are meant to be independent lines: `Node.parse` is the backstop for a reference
    that reached the parse layer at all, and it does not fire either."""
    with pytest.raises(BlueprintParseError, match="unresolved blueprint reference"):
        Node.parse({"order": 0, "ref": None})


def test_a_null_bodied_reference_never_reaches_the_stored_composes_json() -> None:
    """The observable damage, so the refusal above is anchored to a consequence.

    Today this blueprint loads, and `composes_json` is written as
    `[{"order": 0, "output": {}, "ref": null}]` — the literal reference KEY persisted
    onto the node that requirement 6 says must be indistinguishable from an inline
    one. It also costs the blueprint its `structural_key`: `_seed_structural_key`
    finds no usable template, warns, and the node lands with no key at all, invisible
    to cross-tier prior-art matching."""
    bp = BlueprintSeed(
        id="bp-null-ref",
        intent="paycheck detail",
        slots_summary="",
        uses=[f"{_E}.department_name"],
        slots=[],
        result_grain=["department"],
        composes=[{"order": 0, "output": {}, "ref": None}],
    )
    try:
        resolved = resolve_blueprint_references([bp])[0]
    except CorpusLoadError:
        return  # the load was refused: nothing can be written, which is the fix
    props = _dag_properties(resolved)
    assert '"ref"' not in (props["composes_json"] or "")
    assert props["structural_key"], "the blueprint lands with no structural key"


def test_a_template_less_query_node_really_is_an_empty_step() -> None:
    """The counterfactual behind the two refusals above, expressed against the
    runtime's own reading rather than a string match, and written on a node with NO
    `ref` key so it keeps holding after the defect is fixed.

    This is the shape a `ref: null` node collapses to today: an ordinary query node
    (not an approval step) carrying no SQL. `executor._execute_dag` reaches
    `if not node.sql_template:`, records an empty output, and continues — so a
    downstream sibling either binds nothing or fails its table consume at runtime,
    from a blueprint the corpus advertises as validated."""
    node = Node.parse({"order": 0, "output": {"detail_a": "table"}})
    assert node.sql_template is None
    assert node.node_kind == "query"


# ==========================================================================
# 4. DEFECT — the scratch gate is defeated by the spelling of the database
# ==========================================================================


@pytest.mark.parametrize("db", ["scratch", "SCRATCH", "Scratch", "sCrAtCh"])
def test_the_referenced_scratch_gate_is_not_defeated_by_the_databases_spelling(db: str) -> None:
    """DEFECT (partial — `scratch` passes, the rest do not).

    `_inline_reference` refuses a referenced template that reads session scratch, via
    `_scratch_placeholder_names`, which compares `table.text("db") == "scratch"` on a
    RAW parse — case-SENSITIVELY. `SCRATCH.borrowed` therefore reads as an ordinary
    warehouse table and the gate never fires.

    The guard is supposed to be derived from what the scratch surface actually treats
    as scratch, and the authority disagrees with it: clickhouse-api's
    `service._references_scratch_db` matches the scratch database name with
    `re.IGNORECASE`, precisely so a spelling cannot route a query around the session
    gate. Two guards on the same boundary, one case-folding and one not.

    The load-time consequence is a real escape, not just an ugly message: with the
    child declaring `SCRATCH.borrowed.<col>` in its `uses` (which passes the
    `db.table.column` grammar unchanged), `_assert_source_tables_in_uses` finds the
    source declared and the whole corpus loads — shipping a validated blueprint that
    reads a session-scoped table nothing in its DAG can materialize, and minting the
    golden-replay JWT from a `column_scope` containing a scratch key.

    Every other reader on this path already case-folds (sqlglot's
    `normalize_identifiers` lowercases unquoted identifiers before gate (c) sees them),
    which is what makes the mismatch invisible until someone writes the caps."""
    child = BlueprintSeed(
        id="bp-child",
        intent="borrowed rows",
        slots_summary="",
        uses=[f"{db}.borrowed.department_name"],
        slots=[],
        result_grain=["department"],
        sql_template=f"SELECT x.department_name AS department FROM {db}.borrowed AS x",
    )
    parent = BlueprintSeed(
        id="bp-parent",
        intent="p",
        slots_summary="",
        uses=[f"{db}.borrowed.department_name"],
        slots=[],
        result_grain=["department"],
        composes=[{"order": 0, "output": {}, "ref": {"blueprint": "bp-child"}}],
    )
    with pytest.raises(CorpusLoadError, match="scratch"):
        for bp in resolve_blueprint_references([child, parent]):
            _validate_blueprint_dag(bp)


# ==========================================================================
# 5. the `uses` UNION rule — genuinely independent of gate (c)?
# ==========================================================================
#
# Gate (c) (`_validate_blueprint_dag`) re-derives the inlined SQL's column footprint
# and checks it against the parent's `uses`. So EVERY under-declaration test where
# the child's SQL reads the omitted column is satisfied by either gate, and proves
# nothing about which one fired. The shape that separates them is a column the child
# DECLARES and its SQL never names: gate (c) cannot see it, so only the union rule
# can refuse.


def _declares_more_than_it_reads(bp_id: str, **overrides: Any) -> BlueprintSeed:
    """A child whose `uses` names `ssn` while its SQL reads only `department_name`."""
    base: dict[str, Any] = {
        "id": bp_id,
        "intent": "departments",
        "slots_summary": "",
        "uses": [f"{_E}.department_name", f"{_E}.ssn"],
        "slots": [],
        "result_grain": ["department"],
        "sql_template": "SELECT department_name AS department FROM dbpcm_warehouse.employee",
    }
    base.update(overrides)
    return BlueprintSeed(**base)


def _pure_reference_parent(bp_id: str, target: str, uses: list[str]) -> BlueprintSeed:
    return BlueprintSeed(
        id=bp_id,
        intent="composite",
        slots_summary="",
        uses=uses,
        slots=[],
        result_grain=["department"],
        composes=[{"order": 0, "output": {}, "ref": {"blueprint": target}}],
    )


def test_the_union_rule_fires_on_a_column_gate_c_structurally_cannot_see() -> None:
    """One hop. The child DECLARES `ssn`; nothing reads it. The parent omits exactly
    that one key, and the load is refused NAMING it."""
    child = _declares_more_than_it_reads("bp-child")
    parent = _pure_reference_parent("bp-parent", "bp-child", [f"{_E}.department_name"])
    with pytest.raises(CorpusLoadError) as exc:
        resolve_blueprint_references([child, parent])
    message = str(exc.value)
    assert f"{_E}.ssn" in message
    assert "bp-child" in message, "the refusal must name the reference that contributed it"


def test_gate_c_passes_on_exactly_the_shape_the_union_rule_refused() -> None:
    """The contrast that makes the test above mean something. Same inlined SQL, same
    declared `uses` — with the reference removed, gate (c) is satisfied. So the two
    checks are not one assertion written twice: the union rule binds the parent to the
    child's DECLARED footprint, gate (c) binds it to the SQL's actual reads, and only
    the former closes this hole."""
    equivalent_inline = BlueprintSeed(
        id="bp-parent",
        intent="composite",
        slots_summary="",
        uses=[f"{_E}.department_name"],
        slots=[],
        result_grain=["department"],
        composes=[
            {
                "order": 0,
                "output": {},
                "sql_template": (
                    "SELECT department_name AS department FROM dbpcm_warehouse.employee"
                ),
            }
        ],
    )
    _validate_blueprint_dag(equivalent_inline)  # must not raise


def test_the_same_escape_two_hops_down_is_refused_and_names_the_direct_child() -> None:
    """Two hops. Only DIRECT children are inspected, so a grandchild's DECLARED-only
    column reaches the grandparent solely through the accumulated footprint. The
    middle blueprint's SQL reads nothing either, so gate (c) is blind at every level."""
    grandchild = _declares_more_than_it_reads("bp-grandchild")
    middle = _pure_reference_parent(
        "bp-middle", "bp-grandchild", [f"{_E}.department_name", f"{_E}.ssn"]
    )
    grandparent = _pure_reference_parent("bp-grandparent", "bp-middle", [f"{_E}.department_name"])
    with pytest.raises(CorpusLoadError) as exc:
        resolve_blueprint_references([grandchild, middle, grandparent])
    message = str(exc.value)
    assert f"{_E}.ssn" in message
    assert "bp-grandparent" in message and "bp-middle" in message
    # …and it loads the moment the grandparent re-declares it.
    fixed = _pure_reference_parent(
        "bp-grandparent", "bp-middle", [f"{_E}.department_name", f"{_E}.ssn"]
    )
    assert _resolved([grandchild, middle, fixed])["bp-grandparent"].composes[0]["sql_template"]


def test_a_child_widening_its_uses_invalidates_every_composite_that_inlines_it() -> None:
    """Not "a" composite — EVERY one, and independently. A child edit that only broke
    the first parent the resolver happened to reach would let the others ship with a
    stale footprint, which is the whole point of binding to the DECLARED set."""
    narrow = _leaf()
    widened = _leaf(uses=[*narrow.uses, f"{_E}.ssn"])
    parents = [
        _parent(id="bp-p1"),
        _parent(id="bp-p2"),
        _parent(id="bp-p3"),
    ]
    # Baseline: all three load against the un-widened child.
    resolve_blueprint_references([narrow, *parents])
    # Each one, on its own, is refused against the widened child.
    for parent in parents:
        with pytest.raises(CorpusLoadError) as exc:
            resolve_blueprint_references([widened, parent])
        assert parent.id in str(exc.value) and f"{_E}.ssn" in str(exc.value)
    # And the whole corpus together is refused rather than partially resolved.
    with pytest.raises(CorpusLoadError):
        resolve_blueprint_references([widened, *parents])


def test_a_refused_union_leaves_no_partially_resolved_seed_behind() -> None:
    """Fail-closed means the CALLER's seeds are untouched too. `load_corpus` hands the
    hydrator's own list in, and a half-inlined node surviving a raised load would be
    re-read on the next poll as authored content."""
    widened = _leaf(uses=[*_leaf().uses, f"{_E}.ssn"])
    parent = _parent()
    before = json.dumps(parent.composes, sort_keys=True)
    with pytest.raises(CorpusLoadError):
        resolve_blueprint_references([widened, parent])
    assert json.dumps(parent.composes, sort_keys=True) == before


# ==========================================================================
# 6. graph shape — the cases the designed tests skip
# ==========================================================================


def test_a_three_blueprint_cycle_fails_the_load_and_names_the_whole_path() -> None:
    """A→B→C→A. The two-blueprint case can be caught by a `target == self` shortcut;
    only a longer ring exercises the grey-colouring walk, and the reported path is
    what an author uses to find the edge to cut."""

    def node(bp_id: str, target: str) -> BlueprintSeed:
        return BlueprintSeed(
            id=bp_id,
            intent=bp_id,
            slots_summary="",
            uses=[f"{_E}.department_name"],
            composes=[{"order": 0, "ref": {"blueprint": target}}],
        )

    with pytest.raises(CorpusLoadError, match="cycle") as exc:
        resolve_blueprint_references([node("bp-a", "bp-b"), node("bp-b", "bp-c"), node("bp-c", "bp-a")])
    message = str(exc.value)
    for bp_id in ("bp-a", "bp-b", "bp-c"):
        assert bp_id in message


def _chain(depth: int) -> list[BlueprintSeed]:
    """`bp-0` (leaf) ← `bp-1` ← … ← `bp-<depth>`, each a single-node composite."""
    seeds = [_leaf(id="bp-0", slots=[{"name": "department", "type": "string", "required": True}])]
    for i in range(1, depth + 1):
        seeds.append(
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
    return seeds


def test_the_depth_cap_is_an_inclusive_boundary_not_an_off_by_one() -> None:
    """A cap tested only at 40 hops says nothing about where it actually sits. Pin
    both sides: the deepest legal chain resolves all the way down to the leaf's SQL,
    and one more link is refused."""
    at_cap = _resolved(_chain(4))
    assert "AVG(annual_salary)" in at_cap["bp-4"].composes[0]["sql_template"]
    assert "ref" not in at_cap["bp-4"].composes[0]
    with pytest.raises(CorpusLoadError, match="5 deep"):
        resolve_blueprint_references(_chain(5))


def test_a_reference_into_a_disconnected_second_cycle_still_fails() -> None:
    """The walk re-starts per graph key, and a resolvable chain visited FIRST leaves
    the colouring state populated. A ring reached only from a later start must still
    be found rather than short-circuited as already-black."""
    healthy = _chain(2)
    ring = [
        BlueprintSeed(
            id=bp_id,
            intent=bp_id,
            slots_summary="",
            uses=[f"{_E}.department_name"],
            composes=[{"order": 0, "ref": {"blueprint": target}}],
        )
        for bp_id, target in (("bp-x", "bp-y"), ("bp-y", "bp-x"))
    ]
    with pytest.raises(CorpusLoadError, match="cycle"):
        resolve_blueprint_references([*healthy, *ring])


# ==========================================================================
# 7. the trust partition — both directions, and the near-miss spellings
# ==========================================================================


@pytest.mark.parametrize(
    ("parent_source", "child_source"),
    [
        ("mcp", "learning"),  # the laundering direction the gate was written for
        ("learning", "mcp"),  # the reverse: still a crossing, still refused
        ("mcp", "MCP"),  # recall's `source = 'mcp'` is a BARE equality, not a fold
        ("mcp", " mcp"),
        ("mcp", ""),
    ],
)
def test_no_spelling_of_source_lets_a_reference_cross_the_partition(
    parent_source: str, child_source: str
) -> None:
    """Equality is the only rule that cannot be argued into an escalation, so it has
    to hold for every near-miss too — `'MCP'` is NOT the mcp partition as far as
    `vector_index._BLUEPRINT_RECALL_QUERY` is concerned, and inlining from it would
    copy SQL out of a partition recall never serves into one it does."""
    child = replace(_leaf(), source=child_source, verified=False)
    parent = replace(_parent(), source=parent_source)
    with pytest.raises(CorpusLoadError, match="trust partition"):
        resolve_blueprint_references([child, parent])


def test_the_partition_gate_holds_through_a_chain_not_only_at_the_first_hop() -> None:
    """An mcp→mcp→learning chain is a crossing one hop further out. Because children
    resolve first, the refusal has to come from the MIDDLE blueprint's own load — if
    it were deferred to the parent, the middle would already hold laundered SQL."""
    grandchild = replace(_leaf(id="bp-grandchild"), source="learning", verified=False)
    middle = BlueprintSeed(
        id="bp-middle",
        intent="middle",
        slots_summary="department",
        uses=[f"{_E}.department_name", f"{_E}.annual_salary"],
        slots=[{"name": "department", "type": "string", "required": True}],
        result_grain=["department"],
        composes=[
            {"order": 0, "ref": {"blueprint": "bp-grandchild", "slots": {"department": "department"}}}
        ],
    )
    parent = _parent(ref={"blueprint": "bp-middle", "slots": {"department": "dept"}})
    with pytest.raises(CorpusLoadError, match="trust partition") as exc:
        resolve_blueprint_references([grandchild, middle, parent])
    assert "bp-middle" in str(exc.value)


# ==========================================================================
# 8. slot mapping — the arity check in the direction the design tests skip
# ==========================================================================


def test_a_scalar_child_slot_mapped_onto_a_parent_period_range_fails_on_arity() -> None:
    """The mirror of the designed case. One bind token onto two is just as broken as
    two onto one: the parent's `{w_start}`/`{w_end}` would bind nothing the inlined
    SQL names, so the range filter silently disappears (D56)."""
    child = BlueprintSeed(
        id="bp-child",
        intent="hires on a day",
        slots_summary="d",
        uses=[f"{_E}.most_recent_hire_date"],
        slots=[{"name": "d", "type": "period", "required": True}],
        result_grain=["month"],
        sql_template=(
            "SELECT most_recent_hire_date AS month FROM dbpcm_warehouse.employee "
            "WHERE most_recent_hire_date = {d}"
        ),
    )
    parent = _parent(
        uses=[f"{_E}.most_recent_hire_date"],
        slots=[{"name": "w", "type": "period_range", "required": True}],
        result_grain=["month"],
        ref={"blueprint": "bp-child", "slots": {"d": "w"}},
    )
    with pytest.raises(CorpusLoadError, match="arities must match"):
        resolve_blueprint_references([child, parent])


def test_the_simultaneous_swap_survives_a_round_trip_through_the_parse_layer() -> None:
    """The swap is asserted on the resolved TEXT elsewhere; assert it on the thing the
    executor binds. A sequential rename would collapse both tokens onto one slot, and
    the surviving evidence at this layer is that the node references exactly the two
    slots the parent declares, in the swapped positions."""
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
    resolved = _resolved([child, parent])["bp-parent"]
    parsed = Blueprint.parse(
        id=resolved.id,
        intent=resolved.intent,
        slots=resolved.slots,
        composes=resolved.composes,
        result_grain=resolved.result_grain,
    )
    sql = parsed.composes[0].sql_template or ""
    assert ">= {hi}" in sql and "<= {lo}" in sql
    assert sql.count("{lo}") == 1 and sql.count("{hi}") == 1
    _validate_blueprint_dag(resolved)
