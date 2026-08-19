"""QA (delta pass): the `mapping.py` call-site change, and the unusable-vs-absent grain.

Two things this slice changed that nothing else pins:

  1. `blueprint_seed_from_candidate` moved from the PRECOMPUTED `gen.canonical_ast_norm`
     to a RE-DERIVE from S4's templates. That creates a second, independent derivation of
     the same value (the loader's `_seed_structural_key` fallback derives it AGAIN from
     `bp.sql_template`/`bp.composes`/`bp.result_grain`). Two derivations that can disagree
     is the classic silent-divergence shape, so the INVARIANT — stamp == independent
     re-derive — is asserted directly.

  2. `normalize_structural_grain` now distinguishes an UNUSABLE grain (`None`, no key)
     from an ABSENT one (`[]`, a legitimate key). That distinction is only worth anything
     if it survives all the way to the neo4j property, and if "no key" lands as an ABSENT
     property rather than `""` — an empty string would make a naive
     `MATCH (b {structural_key: $k})` match every keyless blueprint as false prior art.
"""

from __future__ import annotations

import pytest

from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.dedup.canonical_key import compute_canonical_key
from data_agent.learning.generalize.canonical import canonical_ast_norm
from data_agent.learning.generalize.mapping import blueprint_seed_from_candidate
from data_agent.runtime.blueprint.compiler import (
    _compose_node_templates,
    _seed_structural_key,
    dag_properties,
)
from data_agent.runtime.blueprint.structural_key import (
    normalize_structural_grain,
    structural_key_from_templates,
)
from data_agent.runtime.retrieval.corpus_loader import BlueprintSeed

from ..promotion.helpers import make_blueprint_candidate

_BP_KEY = "sha256:single-bp"


def _seed(**overrides):
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=_BP_KEY)
    for path, value in overrides.items():
        env.payload["generalization"][path] = value
    return env


# --- 1. the two derivations must agree -----------------------------------------


@pytest.mark.parametrize("grain_verifiable", [False, True])
def test_the_stamped_key_equals_what_the_loader_would_independently_derive(
    grain_verifiable,
) -> None:
    """THE INVARIANT the call-site change introduces. `mapping.py` stamps the key from
    `gen.result_grain` + `gen.sql_template` + `gen.node_templates`; the loader's fallback
    would derive it from `bp.result_grain` + `bp.sql_template` + `bp.composes`. Those are
    different projections of the same candidate (`composes` in particular is rebuilt by
    `_compose_docs` from the S3 payload, NOT copied from `gen.node_templates`).

    If they ever disagree, whichever path runs decides the blueprint's identity — and the
    stamp wins, so the disagreement would be invisible. Assert them equal explicitly.
    """
    env = make_blueprint_candidate(
        status=CandidateStatus.VALIDATED, canonical_key=_BP_KEY, grain_verifiable=grain_verifiable
    )
    seed = blueprint_seed_from_candidate(env, id=f"bp::{_BP_KEY}")
    assert seed.structural_key.startswith("sha256:")

    # Re-derive from the SEED's own fields, exactly as the loader's fallback branch would
    # if the stamp had been absent.
    independent = structural_key_from_templates(
        seed.result_grain, seed.sql_template, _compose_node_templates(seed.composes)
    )
    assert independent == seed.structural_key


def test_the_loader_fallback_reproduces_the_stamp_when_the_stamp_is_stripped() -> None:
    """The same invariant exercised through the real loader entry point rather than a
    hand-rolled re-derive: blank the stamp and let `_seed_structural_key` take its
    derive branch."""
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=_BP_KEY)
    seed = blueprint_seed_from_candidate(env, id=f"bp::{_BP_KEY}")
    stamped = seed.structural_key

    stripped = BlueprintSeed(
        id=seed.id,
        intent=seed.intent,
        slots_summary=seed.slots_summary,
        uses=list(seed.uses),
        result_grain=seed.result_grain,
        sql_template=seed.sql_template,
        composes=list(seed.composes),
        structural_key="",
    )
    assert _seed_structural_key(stripped) == stamped


# --- item 4: a usable canonical_ast_norm whose templates yield NO structural key -


def test_a_malformed_grain_member_costs_the_structural_key_while_the_frozen_key_survives()  -> None:
    """THE ONE CASE where S4 has a perfectly usable `canonical_ast_norm` (and mints a
    frozen D48 key) but the structural re-derive yields NOTHING.

    It is not a template problem — both keys read the same templates. It is the GRAIN:
    `ResultGrainStamp` applies no type validation (`columns=tuple(doc.get("columns") or ())`),
    so an S3 plan whose `result_signature.grain.columns` carries a null or an unquoted
    number flows straight through, and `normalize_structural_grain` correctly refuses to
    guess — no key.

    This is DESIGNED behavior (a confident wrong key is worse than an absent one) and NOT
    a regression, because the structural key is new: the seed loses nothing it previously
    had. Pinned so the asymmetry between the two keys is a recorded decision rather than a
    surprise during an incident.
    """
    template = "SELECT d AS department, sum(amount) AS total FROM db.payroll GROUP BY d"
    norm = canonical_ast_norm(template)
    assert norm, "the template normalizes fine — this is purely a grain failure"

    broken = {"columns": [None], "verifiable": True}
    assert normalize_structural_grain(broken) is None
    assert structural_key_from_templates(broken, template) == ""
    # ...while the FROZEN D48 key is minted without complaint.
    assert compute_canonical_key({}, [], broken, norm).startswith("sha256:")


def test_a_mixed_type_grain_raises_in_the_frozen_key_a_preexisting_sharp_edge() -> None:
    """PRE-EXISTING, out of this slice's scope, recorded because it was found while
    verifying the one above: `compute_canonical_key` sorts `result_grain["columns"]`
    directly, so a grain mixing a null with a string raises `TypeError` out of the FROZEN
    key rather than failing soft.

    The structural key handles the same input cleanly (no key). Not introduced here and
    not fixed here — pinned so the difference in robustness between the two keys is
    visible.
    """
    mixed = {"columns": ["department", None], "verifiable": True}
    assert normalize_structural_grain(mixed) is None
    assert structural_key_from_templates(mixed, "SELECT a FROM db.t") == ""
    with pytest.raises(TypeError):
        compute_canonical_key({}, [], mixed, "SELECT a FROM db.t")


def test_no_template_shape_makes_the_re_derive_lose_a_key_the_precomputed_norm_had() -> None:
    """The other half of item 4, and it comes back NEGATIVE — which is the reassuring
    answer. Both the frozen `canonical_ast_norm` and the structural render read the SAME
    `(sql_template, node_templates)` through the SAME `_join_nodes`, so any template set
    that produces a non-empty frozen norm also produces a non-empty structural one.

    Swept over the shapes S4 can emit: single, composite, composite with a template-less
    node, and the fail-to-review shape.
    """
    from data_agent.learning.candidate.generalization import NodeTemplate

    shapes = [
        ("SELECT a FROM db.t", []),
        (None, [NodeTemplate(order=0, sql_template="SELECT a FROM db.t")]),
        (
            None,
            [
                NodeTemplate(order=0, sql_template="SELECT a FROM db.t"),
                NodeTemplate(order=1, sql_template="SELECT SUM(b) FROM db.u"),
            ],
        ),
        (None, [NodeTemplate(order=0, sql_template="")]),  # fail-to-review-ish
        (None, []),
    ]
    for sql_template, nodes in shapes:
        frozen = canonical_ast_norm(sql_template, nodes)
        structural = structural_key_from_templates(
            {"columns": ["department"], "verifiable": True},
            sql_template,
            [(n.order, n.sql_template) for n in nodes],
        )
        # Non-empty frozen norm <=> a structural key exists. Never one without the other.
        assert bool(frozen) == bool(structural), (sql_template, nodes)


# --- item 5: unusable vs absent, end to end to the node property ---------------


@pytest.mark.parametrize(
    "grain",
    [
        {"columns": [None], "verifiable": True},
        {"columns": [2024], "verifiable": True},
        {"columns": ["department", None], "verifiable": True},
        {"columns": [["nested"]], "verifiable": True},
        {"columns": [{"col": "d"}], "verifiable": True},
        [None],
        ["department", 7],
    ],
)
def test_an_unusable_grain_lands_as_an_absent_property_not_an_empty_string(grain) -> None:
    """END TO END for the broken-grain half. A malformed grain with a PERFECTLY GOOD
    template must reach the neo4j write as `None` (absent), never `""`.

    This is the path the builder's own test does not cover: its keyless cases are a broken
    TEMPLATE and a DAG-less seed. A broken grain is different — the seed has a valid
    template, so it reaches the derivation and gets refused there instead.
    """
    seed = BlueprintSeed(
        id="bp-broken-grain",
        intent="i",
        slots_summary="",
        uses=["db.t.a"],
        result_grain=grain,
        sql_template="SELECT a AS department FROM db.t",
    )
    value = dag_properties(seed)["structural_key"]
    assert value is None
    assert value != ""


@pytest.mark.parametrize(
    "grain",
    [
        None,
        [],
        {},
        {"columns": []},
        {"columns": [], "verifiable": False},
        "department",  # an unrecognized CONTAINER, read as "no grain declared"
        42,
    ],
)
def test_an_absent_or_unrecognized_grain_container_still_mints_a_key(grain) -> None:
    """The other side of the split, and the reason it exists: `result_grain: []` is
    LEGITIMATE (bp-hires-projection ships it) and canon omits the field entirely on
    several blueprints. Those must still get a key — collapsing them into the
    broken-grain bucket would make a fifth of the canon corpus invisible to prior-art
    matching."""
    seed = BlueprintSeed(
        id="bp-grainless",
        intent="i",
        slots_summary="",
        uses=["db.t.a"],
        result_grain=grain,
        sql_template="SELECT a AS department FROM db.t",
    )
    value = dag_properties(seed)["structural_key"]
    assert value is not None
    assert value.startswith("sha256:")


def test_broken_and_absent_grain_are_not_the_same_outcome() -> None:
    """The distinction stated as one assertion: same template, two grains, opposite
    outcomes. If a refactor ever collapsed `None` into `[]`, a malformed grain would start
    minting a confident key from a guess — the exact failure the split prevents."""
    template = "SELECT a AS department FROM db.t"
    common = {"id": "bp-x", "intent": "i", "slots_summary": "", "uses": ["db.t.a"]}
    broken = dag_properties(
        BlueprintSeed(**common, result_grain={"columns": [None]}, sql_template=template)
    )["structural_key"]
    absent = dag_properties(
        BlueprintSeed(**common, result_grain=[], sql_template=template)
    )["structural_key"]
    assert broken is None
    assert absent is not None and absent.startswith("sha256:")


def test_a_broken_grain_never_reaches_the_learning_landing_seed_at_all() -> None:
    """THE REASSURING RESULT, and it upgrades the finding above from "designed asymmetry"
    to "unreachable on the learning path".

    `blueprint_seed_from_candidate` calls `blueprint_from_generalization` FIRST, which
    runs `Blueprint.parse` -> `ResultGrain.parse`, and that rejects a non-string grain
    column fail-CLOSED with `BlueprintParseError`. So a malformed grain aborts the landing
    entirely — the seed is never built, and the "usable canonical_ast_norm but no
    structural key" state cannot be reached through the learning writer.

    `normalize_structural_grain`'s `None` branch is therefore DEFENSIVE DEPTH on this
    path, not live behavior. Pinned so that if someone ever loosens `ResultGrain.parse`,
    the depth becomes load-bearing and this test says so.
    """
    from data_agent.runtime.blueprint.models import BlueprintParseError

    env = _seed(result_grain={"columns": [None], "verifiable": True})
    with pytest.raises(BlueprintParseError, match="non-empty strings"):
        blueprint_seed_from_candidate(env, id=f"bp::{_BP_KEY}")

    # And the canon/loader tier rejects the same shape in its own pre-write pass.
    from data_agent.runtime.blueprint.compiler import validate_blueprint_dag
    from data_agent.runtime.retrieval.corpus_loader import CorpusLoadError

    seed = BlueprintSeed(
        id="bp-x",
        intent="i",
        slots_summary="",
        uses=["db.t.a"],
        result_grain={"columns": [None]},
        sql_template="SELECT a AS department FROM db.t",
    )
    with pytest.raises(CorpusLoadError, match="non-empty strings"):
        validate_blueprint_dag(seed)


def test_the_keyless_warning_names_the_grain_when_the_grain_is_what_failed(caplog) -> None:
    """FIXED (was QA D1). `_seed_structural_key` used to emit ONE message for every miss:

        "could not derive a structural_key (template did not normalize)"

    but the miss has TWO distinct causes — an unparseable template AND an unusable grain
    — which lead an operator to opposite places. For the grain case the message was
    actively wrong: it sent them to debug SQL that parses perfectly well. Since the whole
    point of the container/member split is that a malformed grain is an AUTHORING BUG
    worth surfacing, the one diagnostic that surfaces it must name it.
    """
    import logging

    seed = BlueprintSeed(
        id="bp-badcols",
        intent="i",
        slots_summary="",
        uses=["db.t.a"],
        result_grain={"columns": [None]},
        sql_template="SELECT a AS department FROM db.t",  # parses fine
    )
    with caplog.at_level(logging.WARNING):
        assert _seed_structural_key(seed) == ""

    messages = [r.getMessage() for r in caplog.records]
    assert any("bp-badcols" in m for m in messages)
    assert any("result_grain has a non-string member" in m for m in messages), messages
    # ...and it no longer blames the template, which really does normalize.
    assert canonical_ast_norm(seed.sql_template), "the template really does normalize"
    assert not any("template did not normalize" in m for m in messages), messages


def test_the_keyless_warning_still_blames_the_template_when_the_template_failed(caplog) -> None:
    """The other side of the same fix: a genuinely unparseable template must still be
    named as the template, not misattributed to the grain."""
    import logging

    seed = BlueprintSeed(
        id="bp-badsql",
        intent="i",
        slots_summary="",
        uses=["db.t.a"],
        result_grain=["Department"],  # perfectly usable
        sql_template="SELECT FROM WHERE ((",
    )
    with caplog.at_level(logging.WARNING):
        assert _seed_structural_key(seed) == ""

    messages = [r.getMessage() for r in caplog.records]
    assert any("bp-badsql" in m and "template did not normalize" in m for m in messages), messages
    assert not any("result_grain" in m for m in messages), messages
