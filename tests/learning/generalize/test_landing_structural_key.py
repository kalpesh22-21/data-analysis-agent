"""Cross-tier `structural_key` agreement, END TO END through both real writers.

`tests/runtime/blueprint/test_structural_key.py` pins the key FUNCTION. This pins the
two CALL SITES that must agree, because a correct function called with the wrong inputs
on one side still yields a permanent cross-tier miss:

  * the learning landing seed (`generalize/mapping.py::blueprint_seed_from_candidate`),
    which stamps the key from the S4-computed `canonical_ast_norm`; and
  * the MCP-canon seeder (`runtime/retrieval/corpus_loader.py::_dag_properties`), which
    derives it from a bare `sql_template` + a bare-list `result_grain`.

Feed both the SAME blueprint in their OWN native authoring shapes and demand one digest.
"""

from __future__ import annotations

from data_agent.learning.candidate.generalization import BlueprintGeneralization
from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.generalize.mapping import blueprint_seed_from_candidate
from data_agent.runtime.retrieval.corpus_loader import BlueprintSeed, _dag_properties

from ..promotion.helpers import make_blueprint_candidate

_BP_KEY = "sha256:single-bp"


def test_landed_seed_carries_a_structural_key() -> None:
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=_BP_KEY)
    seed = blueprint_seed_from_candidate(env, id=f"bp::{_BP_KEY}")
    assert seed.structural_key.startswith("sha256:")
    # The seed's explicit key survives the loader untouched (never re-derived).
    assert _dag_properties(seed)["structural_key"] == seed.structural_key


def test_learning_seed_matches_the_canon_seed_for_the_same_blueprint() -> None:
    """THE cross-tier assertion. The canon YAML for this blueprint would carry the same
    `sql_template` and a BARE-LIST grain and no `resolves`/`uses_rules` — the D48 hard
    key cannot match it, the structural key must."""
    env = make_blueprint_candidate(
        status=CandidateStatus.VALIDATED, canonical_key=_BP_KEY, grain_verifiable=True
    )
    learning_seed = blueprint_seed_from_candidate(env, id=f"bp::{_BP_KEY}")

    gen = BlueprintGeneralization.from_doc(env.payload["generalization"])
    canon_seed = BlueprintSeed(
        id="bp-total-earnings-by-department",  # a hand-authored id, NOT the landing id
        intent=env.payload["intent"],
        slots_summary="",
        uses=list(gen.uses),
        # Canon authoring shape: a bare-list grain, no resolves, no uses_rules, and no
        # precomputed canonical_ast_norm — the loader derives one.
        result_grain=list(gen.result_grain.columns),
        sql_template=gen.sql_template,
    )

    canon_key = _dag_properties(canon_seed)["structural_key"]
    assert canon_key
    assert canon_key == learning_seed.structural_key


def test_the_landed_key_is_independent_of_the_frozen_canonical_ast_norm() -> None:
    """The landing seed derives its structural key from S4's TEMPLATES, never from
    `gen.canonical_ast_norm`.

    This is load-bearing, not incidental. The structural render additionally folds
    standard function names and strips comments — folds the FROZEN render must never take,
    because its digests are already persisted as `canonical_key` in the corpus bucket.
    Feeding the frozen string to `structural_key` would mint a digest the canon tier can
    never match, and would do so SILENTLY. Blanking (or corrupting) `canonical_ast_norm`
    must therefore leave the structural key completely unchanged.
    """
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=_BP_KEY)
    baseline = blueprint_seed_from_candidate(env, id=f"bp::{_BP_KEY}").structural_key
    assert baseline.startswith("sha256:")

    for tampered in ["", "SELECT something_else FROM elsewhere"]:
        env2 = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=_BP_KEY)
        env2.payload["generalization"]["canonical_ast_norm"] = tampered
        assert blueprint_seed_from_candidate(env2, id=f"bp::{_BP_KEY}").structural_key == baseline


def test_the_s4_fixtures_lowercase_aggregate_matches_canons_uppercase_one() -> None:
    """The concrete payoff of the function-name fold, at the real call sites.

    The frozen S4 fixture writes `sum(gross_pay)` lowercase; a hand-authored canon YAML
    for the same query would write `SUM(gross_pay)`. Before the fold these minted
    different structural keys — which, since every canon blueprint uppercases and the
    extractor does not reliably, was a miss on essentially every aggregate blueprint.
    """
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=_BP_KEY)
    learning_seed = blueprint_seed_from_candidate(env, id=f"bp::{_BP_KEY}")
    gen = BlueprintGeneralization.from_doc(env.payload["generalization"])
    assert "sum(" in (gen.sql_template or ""), "fixture no longer lowercases its aggregate"

    canon_uppercased = BlueprintSeed(
        id="bp-canon",
        intent=env.payload["intent"],
        slots_summary="",
        uses=list(gen.uses),
        result_grain=list(gen.result_grain.columns),
        sql_template=(gen.sql_template or "").replace("sum(", "SUM("),
    )
    assert _dag_properties(canon_uppercased)["structural_key"] == learning_seed.structural_key
