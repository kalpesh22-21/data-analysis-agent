"""Layer-1: the seeder path mints the cross-tier `structural_key` (PriorArtIndex Slice 1).

MCP-canon blueprints carry a `sql_template`/`composes` but NO `canonical_ast_norm`, so
the loader derives one at write time via the shared runtime recipe. These tests pin the
three things the derivation must get right: every real canon-shaped fixture produces a
key, a seed that carries an explicit key keeps it (the learning landing path), and a
template sqlglot cannot normalize fails SOFT to an absent property rather than breaking
the load.
"""

from __future__ import annotations

from pathlib import Path

from data_agent.runtime.blueprint.compiler import dag_properties
from data_agent.runtime.blueprint.structural_key import (
    structural_key_from_templates,
    structural_key_recipe,
)
from data_agent.runtime.retrieval.corpus_loader import (
    _UPSERT_BLUEPRINT,
    BlueprintSeed,
    load_seed_fixtures,
    resolve_blueprint_references,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "corpus"


def test_every_canon_fixture_produces_a_structural_key() -> None:
    """Every canon-shaped blueprint (single-node AND composite) must mint a key —
    a keyless canon tier is invisible to cross-tier prior-art matching, which is the
    entire purpose of the property.

    The count was 10 until `bp-employee-check-detail-for-period` was extracted from
    `bp-compare-employee-check-detail-two-periods` (plan §2b). References are resolved
    FIRST, mirroring `load_corpus`: a `composes` node may name another blueprint
    instead of carrying SQL, and the key is derived from the INLINED templates — the
    SQL the node actually runs. Deriving it from unresolved seeds would silently key a
    composite on its remaining nodes only."""
    blueprints, _ = load_seed_fixtures(_FIXTURE_DIR)
    blueprints = resolve_blueprint_references(blueprints)
    assert len(blueprints) == 11
    keyed = {bp.id: dag_properties(bp)["structural_key"] for bp in blueprints}
    assert all(key and key.startswith("sha256:") for key in keyed.values()), keyed
    # Distinct blueprints must not collide (several share the `Department` grain, so
    # this also proves the AST half of the digest is doing real work).
    assert len(set(keyed.values())) == len(keyed)


def test_derivation_is_deterministic_across_calls() -> None:
    blueprints, _ = load_seed_fixtures(_FIXTURE_DIR)
    bp = blueprints[0]
    assert dag_properties(bp)["structural_key"] == dag_properties(bp)["structural_key"]


def test_an_explicit_structural_key_wins_over_a_re_derive() -> None:
    """The learning landing seed stamps the key from the S4-computed
    `canonical_ast_norm` — the exact string S6 hashed. Re-deriving it from the stored
    template could drift (a bumped sqlglot re-renders the AST), so an explicit value is
    never recomputed."""
    bp = BlueprintSeed(
        id="bp-x",
        intent="x",
        slots_summary="",
        uses=["dbpcm_warehouse.employee.department_name"],
        result_grain=["Department"],
        sql_template="SELECT department_name FROM dbpcm_warehouse.employee",
        structural_key="sha256:pinned-by-s4",
    )
    assert dag_properties(bp)["structural_key"] == "sha256:pinned-by-s4"


def test_a_dag_less_seed_carries_no_structural_key() -> None:
    """`None`, not `""` — an empty-string key stored on many nodes would make a naive
    equality lookup match them all as false prior art."""
    bp = BlueprintSeed(id="legacy", intent="x", slots_summary="", uses=["a.b.c"])
    assert dag_properties(bp)["structural_key"] is None


def test_an_unparseable_template_fails_soft(caplog) -> None:
    """A template sqlglot cannot normalize must cost only the key, never the load."""
    bp = BlueprintSeed(
        id="bp-broken",
        intent="x",
        slots_summary="",
        uses=["a.b.c"],
        result_grain=["Department"],
        sql_template="SELECT FROM WHERE ((",
    )
    with caplog.at_level("WARNING"):
        props = dag_properties(bp)
    assert props["structural_key"] is None
    assert "bp-broken" in caplog.text


def test_every_keyed_node_carries_the_recipe_stamp() -> None:
    """The stamp exists to make ONE silent failure detectable: canon keys are re-derived
    on every reseed and track the current render, while learning keys are stamped once at
    landing and persist. A sqlglot bump therefore splits the tiers — reseeded canon nodes
    get new digests, already-landed learning nodes keep old ones — and every cross-tier
    lookup starts missing with no signal at all. Nothing reads it yet; it is written now
    because adding it later would mean backfilling nodes whose seeds are long gone."""
    blueprints, _ = load_seed_fixtures(_FIXTURE_DIR)
    for bp in blueprints:
        props = dag_properties(bp)
        assert props["structural_key"]
        assert props["structural_key_recipe"] == structural_key_recipe()


def test_the_recipe_stamp_names_the_sqlglot_version_and_a_hand_bumped_revision() -> None:
    """Both halves must be present: the sqlglot version covers the render, the revision
    covers the normalization rules that live in our own module (a new fold, a changed
    grain rule) and which no dependency version would reflect."""
    import sqlglot

    recipe = structural_key_recipe()
    assert recipe.startswith("r")
    assert f"sqlglot{sqlglot.__version__}" in recipe


def test_a_keyless_node_carries_no_recipe_stamp_either() -> None:
    """A recipe stamp on a keyless node describes nothing, and would make a future
    "which nodes are stale?" scan report blueprints that have no key to be stale about."""
    legacy = BlueprintSeed(id="legacy", intent="x", slots_summary="", uses=["a.b.c"])
    broken = BlueprintSeed(
        id="bp-broken",
        intent="x",
        slots_summary="",
        uses=["a.b.c"],
        result_grain=["Department"],
        sql_template="SELECT FROM WHERE ((",
    )
    for seed in (legacy, broken):
        props = dag_properties(seed)
        assert props["structural_key"] is None
        assert props["structural_key_recipe"] is None


def test_the_upsert_binds_the_recipe_parameter() -> None:
    """An unbound Cypher parameter is a neo4j ParameterMissing at runtime, which no
    Layer-1 fake driver would surface."""
    assert "b.structural_key_recipe = $structural_key_recipe" in _UPSERT_BLUEPRINT
    bp = BlueprintSeed(
        id="bp-x",
        intent="x",
        slots_summary="",
        uses=["db.t.a"],
        result_grain=["Department"],
        sql_template="SELECT a FROM db.t",
    )
    assert "structural_key_recipe" in dag_properties(bp)


def test_a_composite_seed_keys_off_its_node_templates() -> None:
    """A composite carries no top-level `sql_template`; the key comes from the
    `composes[*].sql_template` join, and an output-only node (no SQL) is skipped rather
    than contributing an empty line that would shift the join."""
    nodes = [
        {"order": 0, "sql_template": "SELECT AVG(x) AS a FROM db.t"},
        {"order": 1, "output": {}},  # canon authors output-only DAG nodes
        {"order": 2, "sql_template": "SELECT y FROM db.t WHERE y > {a}"},
    ]
    bp = BlueprintSeed(
        id="bp-composite",
        intent="x",
        slots_summary="",
        uses=["db.t.x"],
        result_grain=["Department"],
        composes=nodes,
    )
    expected = structural_key_from_templates(
        ["Department"],
        None,
        [(0, "SELECT AVG(x) AS a FROM db.t"), (2, "SELECT y FROM db.t WHERE y > {a}")],
    )
    assert expected
    assert dag_properties(bp)["structural_key"] == expected
