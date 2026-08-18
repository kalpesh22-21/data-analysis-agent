"""J7c — a learned windowed blueprint must keep its `window_anchor` all the way to canon.

TWO hand-written field lists sit between a validated candidate and the MCP corpus:

  1. `learning/generalize/mapping.py::blueprint_seed_from_candidate` — candidate payload
     → `BlueprintSeed` (what the landing writer MERGEs into neo4j);
  2. `learning/promotion/mcp_export.py::_blueprint_doc` — that seed → the MCP-format YAML
     a human PRs into the canon repo.

Neither carried `window_anchor`. The drop was INERT when found (the extractor cannot yet
declare an anchor), which is exactly why it needed a pin rather than a note: the first
learned windowed blueprint would have promoted anchor-less, `getBlueprint` would surface
no window note, and J7 (the model judging a data-anchored window unresponsive and
re-deriving it with its own calendar SQL) would return for the learned corpus with every
test still green. Same class, same cause, third occurrence — see the file header of
`tests/eval/test_harness_field_drift.py`, which names this one.

`test_seed_field_parity_is_derived` is the load-bearing test: the required key set is
computed from `fields(BlueprintSeed)` minus an EXPLICIT non-MCP exclusion set, so the
next field added to the seed fails here until someone either emits it or writes down why
it must not reach canon. The round-trip tests pin the value itself, end to end, over the
shipped `build_promotion_emit` and the corpus reader that reads the YAML back.
"""

from __future__ import annotations

import types
import typing
from dataclasses import fields, replace
from typing import Any, Union, get_args, get_origin

import pytest
import yaml

from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import DriftStamp
from data_agent.learning.generalize.mapping import blueprint_seed_from_candidate
from data_agent.learning.promotion import mcp_export
from data_agent.learning.promotion.landing import landing_id
from data_agent.learning.promotion.mcp_export import build_promotion_emit
from data_agent.runtime.blueprint.models import WINDOW_ANCHORS, BlueprintParseError
from data_agent.runtime.retrieval.corpus_loader import BlueprintSeed, _seed_from_entry

from .helpers import make_blueprint_candidate

KEY = "sha256:single-bp"

# The seed fields that must NEVER reach the MCP YAML, written down ONCE here so the
# parity assertion below can be a subtraction rather than a third hand-copied list:
#   * `source`/`verified`   — the governed-corpus trust partition. Canon is trusted by
#                             being canon; the reseed stamps `mcp`/`True` itself.
#   * `created_by`/`source_candidate_id` — learning-loop provenance (a candidate id is
#                             session-adjacent and meaningless in another repo).
#   * `structural_key`      — DERIVED at load from the templates + grain; emitting it
#                             would freeze a value the loader recomputes anyway.
_NON_MCP_SEED_FIELDS = frozenset(
    {"source", "verified", "created_by", "source_candidate_id", "structural_key"}
)


def _windowed_candidate(anchor: Any = "data") -> CandidateEnvelope:
    """A validated blueprint candidate whose S3 plan DECLARES a window anchor.

    Synthetic on purpose: no extractor emits this key today (that is the whole reason
    the drop was invisible), so the fixture stands in for the payload the S3 stage will
    produce once it grows window awareness."""
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY)
    env = replace(env, verified=True, drift=DriftStamp(status="clean"))
    return replace(env, payload={**env.payload, "window_anchor": anchor})


# --- projection 1: candidate → landed seed --------------------------------------


@pytest.mark.parametrize("anchor", sorted(WINDOW_ANCHORS))
def test_landing_seed_carries_the_declared_anchor(anchor: str) -> None:
    seed = blueprint_seed_from_candidate(_windowed_candidate(anchor), id=f"bp::{KEY}")
    assert seed.window_anchor == anchor


def test_landing_seed_makes_no_claim_when_the_plan_declares_none() -> None:
    """Absent stays absent — `None` is "no claim", not a default anchor. Every candidate
    the extractor produces TODAY takes this path, so it is also the inertness pin."""
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY)
    assert "window_anchor" not in env.payload
    assert blueprint_seed_from_candidate(env, id=f"bp::{KEY}").window_anchor is None


@pytest.mark.parametrize("bad", ["date", "", "calendar ", 7, ["data"]])
def test_a_malformed_anchor_fails_the_landing_closed(bad: Any) -> None:
    """A declared-but-invalid anchor raises out of `Blueprint.parse` rather than landing
    silently unanchored — the same fail-closed posture the rest of this projection takes
    (a landing that quietly drops a declaration is the failure J7c is about)."""
    with pytest.raises(BlueprintParseError, match="window_anchor"):
        blueprint_seed_from_candidate(_windowed_candidate(bad), id=f"bp::{KEY}")


# --- projection 2: seed → MCP YAML ----------------------------------------------


@pytest.mark.parametrize("anchor", sorted(WINDOW_ANCHORS))
def test_promotion_yaml_carries_the_declared_anchor(anchor: str) -> None:
    doc = yaml.safe_load(build_promotion_emit(_windowed_candidate(anchor)).yaml)
    assert doc["window_anchor"] == anchor


def test_promotion_yaml_omits_an_undeclared_anchor() -> None:
    """No `window_anchor: null` key in canon: a null declaration reads like a claim, and
    the hand-authored YAMLs simply leave the field out."""
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY)
    assert "window_anchor" not in yaml.safe_load(build_promotion_emit(env).yaml)


def test_promotion_yaml_matches_canon_field_order() -> None:
    """`window_anchor` sits between `result_grain` and `sql_template`, as in the
    hand-authored canon (clickhouse-api `blueprints/bp-hires-per-month.yaml`). The emit
    dumps with `sort_keys=False`, so the diff of a promoted file against its neighbours
    stays readable."""
    keys = list(yaml.safe_load(build_promotion_emit(_windowed_candidate()).yaml))
    assert keys.index("result_grain") < keys.index("window_anchor") < keys.index("sql_template")


# --- the full hop: candidate → YAML → the reader that reseeds it ----------------


def test_round_trip_through_the_corpus_reader() -> None:
    """The contract that actually matters: what a human PRs into canon, read back by the
    loader that reseeds it, is still the anchored blueprint that landed.

    `_seed_from_entry` is the SHIPPED MCP-export reader — it whitelists to
    `fields(BlueprintSeed)`, so an emitted key the seed does not declare is dropped here
    in silence. Asserting on the far side of it is what makes this a round-trip and not
    two spellings of the same projection.
    """
    env = _windowed_candidate("data")
    landed = blueprint_seed_from_candidate(env, id=landing_id(env))
    doc = yaml.safe_load(build_promotion_emit(env).yaml)

    reseeded = _seed_from_entry(doc["id"], doc, kind="blueprint")
    assert reseeded.id == landed.id == landing_id(env)
    assert reseeded.window_anchor == landed.window_anchor == "data"
    # And the reseeded node is canon-trusted, unlike the learning-staging one it replaces.
    assert (reseeded.source, reseeded.verified) == ("mcp", True)
    assert (landed.source, landed.verified) == ("learning", False)


# --- the tripwire ---------------------------------------------------------------


def _probe_value(name: str, hint: Any) -> Any:
    """A distinctive value for one seed field, chosen from its DECLARED TYPE.

    Type-driven, never name-driven, so a field added to `BlueprintSeed` tomorrow is
    probed tomorrow with no edit here. An unhandled annotation raises rather than
    probing nothing: a field this factory cannot build a value for is a field the
    tripwire cannot protect, and it should say so out loud.
    """
    options = [
        arg
        for arg in (get_args(hint) if get_origin(hint) in (Union, types.UnionType) else (hint,))
        if arg is not type(None)
    ]
    for option in options:
        origin = get_origin(option) or option
        if origin is bool:
            return True
        if origin is str:
            return f"probe-{name}"
        if origin is dict:
            return {f"probe-{name}-key": f"probe-{name}-value"}
        if origin is list:
            item = next(iter(get_args(option)), Any)
            if (get_origin(item) or item) is dict:
                return [{f"probe-{name}-key": f"probe-{name}-value"}]
            return [f"probe-{name}"]
    raise AssertionError(
        f"the probe factory does not know how to build a value for {name!r}: {hint!r}. "
        "Teach it the type — an unprobeable seed field is an unguarded one."
    )


def test_the_exclusion_set_is_actually_a_subset_of_the_seed() -> None:
    """The subtraction has to describe the dataclass it subtracts from. A renamed or
    deleted seed field would otherwise leave a stale exclusion silently widening the
    hole this tripwire exists to close."""
    assert _NON_MCP_SEED_FIELDS <= {f.name for f in fields(BlueprintSeed)}


def test_seed_field_parity_is_derived(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE TRIPWIRE. Every `BlueprintSeed` field that is not explicitly excluded must
    appear in the emitted MCP YAML.

    A synthetic fully-populated seed, because `_blueprint_doc` omits empty values — a
    field the fixture candidate happens not to populate would otherwise be untested
    exactly like `window_anchor` was. The seed builder is patched (rather than a payload
    built to populate fourteen fields) so this test is about the SECOND projection only;
    the first is covered above.
    """
    hints = typing.get_type_hints(BlueprintSeed)
    probes = {f.name: _probe_value(f.name, hints[f.name]) for f in fields(BlueprintSeed)}
    monkeypatch.setattr(
        mcp_export, "blueprint_seed_from_candidate", lambda env, *, id: BlueprintSeed(**probes)
    )

    doc = yaml.safe_load(build_promotion_emit(_windowed_candidate()).yaml)

    required = {f.name for f in fields(BlueprintSeed)} - _NON_MCP_SEED_FIELDS
    missing = sorted(required - set(doc))
    assert not missing, (
        f"learning/promotion/mcp_export.py::_blueprint_doc drops {missing} on its way to "
        "the MCP canon YAML. A promoted blueprint is what the agent recalls forever "
        "after; a field lost here disables its feature for the learned corpus with "
        "nothing failing anywhere (J7c). Emit it, or add it to _NON_MCP_SEED_FIELDS "
        "with the reason it must not reach canon."
    )
    # The exclusions are not merely unasserted — their probe values must be absent.
    assert not (_NON_MCP_SEED_FIELDS & set(doc))
