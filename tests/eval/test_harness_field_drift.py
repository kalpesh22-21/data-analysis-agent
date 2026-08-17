"""The harness-fidelity tripwire — `conftest.blueprint_detail` may not silently
drop a field the corpus seed carries.

WHY THIS FILE EXISTS. `conftest.blueprint_detail` hand-copies `BlueprintSeed` →
`BlueprintDetail` with an explicit keyword list. Both dataclasses grow additively,
every new field defaults to `None`, and a `BlueprintDetail` missing one constructs,
renders and serialises perfectly well — so a field added to the seed, to the fixture
YAML and to the runtime, but NOT to that keyword list, disables its feature for the
whole A2 live eval while every existing assertion keeps passing.

That is not a hypothetical. J7 added `window_anchor`; the constructor was not
updated; the A2 eval served anchor-less details, `getBlueprint` rendered no window
note, `blueprint/executor.py` stamped no anchor on any result, and zero of the live
L3 runs' model payloads contained the note the feature exists to deliver. The eval
was green throughout. `learning/promotion/mcp_export.py::_blueprint_doc` produced the
identical failure from the identical cause (J7c) — two hand-copied field lists, two
silent drops, one class.

THE ASSERTION IS DERIVED, NEVER ENUMERATED. The shared-field set is
`fields(BlueprintSeed) ∩ fields(BlueprintDetail)`, computed at test time. A field
added to both dataclasses tomorrow is covered tomorrow, with no edit here — which is
the only property that makes this a tripwire rather than a third hand-copied list.

Two independent readings, because they fail for different reasons:

  * `test_every_shared_field_survives_the_conftest_constructor` — a SYNTHETIC seed
    carrying a distinctive probe value in every shared field. Catches a dropped
    field even when no committed fixture happens to populate it.
  * `test_committed_corpus_fields_survive_the_conftest_constructor` — the REAL
    fixtures. Catches a field the constructor assigns but mangles/conditionally
    drops on the shapes the eval actually serves.

Plus one end-to-end pin (`test_get_blueprint_serves_the_window_anchor_note`) over
the SHIPPED `GetBlueprintTool` and the eval's own index seeding — the exact read J7
regressed on, asserted where it was actually observable.
"""

from __future__ import annotations

import types
import typing
from dataclasses import fields
from typing import Any, Union, get_args, get_origin

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.models import WINDOW_ANCHORS
from data_agent.runtime.retrieval.corpus_loader import BlueprintSeed
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.tools import GetBlueprintTool
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

from .conftest import blueprint_detail, seed_corpus

# The whole point: computed from the two dataclasses, never written down. `hit_count`
# is `BlueprintDetail`-only (graph-side runtime state the seed has no notion of) and
# `source`/`verified`/`created_by`/`structural_key` are seed-only (write-path
# provenance the retrieval projection deliberately does not carry) — the intersection
# excludes all of them without anyone having to remember to.
SHARED_FIELDS: frozenset[str] = frozenset(
    f.name for f in fields(BlueprintSeed)
) & frozenset(f.name for f in fields(BlueprintDetail))


def _probe_value(name: str, hint: Any) -> Any:
    """A distinctive value for one shared field, chosen from its DECLARED TYPE.

    Type-driven rather than name-driven so a newly added field is probed
    automatically. An unhandled type raises rather than silently probing nothing —
    a new field with an exotic annotation must be taught to this factory, and a
    tripwire that quietly skips a field it does not understand is no tripwire.

    Values are deliberately implausible (`probe-<field>`): nothing downstream of the
    conftest constructor validates them, and a value that could be mistaken for a
    real one makes a failure harder to read.
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
        "Teach it the type — a shared field this tripwire cannot probe is a shared "
        "field it cannot protect."
    )


def _comparable(seed_value: Any, detail_value: Any) -> tuple[Any, Any]:
    """Normalise one (seed, detail) pair for comparison.

    The projection legitimately changes CONTAINER but never CONTENT: `uses` is a
    seed `list[str]` and a detail `frozenset[str]`. Both sides are sorted when either
    is a set so the comparison stays about the values.
    """
    if isinstance(detail_value, (set, frozenset)) or isinstance(seed_value, (set, frozenset)):
        return sorted(seed_value or ()), sorted(detail_value or ())
    return seed_value, detail_value


def test_shared_field_set_is_non_trivial() -> None:
    """The derivation itself has to be load-bearing.

    If a refactor renamed the fields apart, `SHARED_FIELDS` would silently empty and
    every assertion below would pass over nothing. Pin the fields whose drop is a
    known, shipped incident (`window_anchor`, J7) plus the DAG set the runBlueprint
    brick depends on — not as the enumeration under test, but as proof the
    intersection still describes the copy this file guards.
    """
    assert {
        "id",
        "intent",
        "slots_summary",
        "uses",
        "status",
        "drift_status",
        "catalog_sha",
        "resolves",
        "slots",
        "uses_rules",
        "sql_template",
        "composes",
        "result_grain",
        "window_anchor",
    } <= SHARED_FIELDS


def test_every_shared_field_survives_the_conftest_constructor() -> None:
    """THE TRIPWIRE. A synthetic seed with a probe in every shared field; the detail
    the eval would serve must carry all of them.

    Fails with the offending field named the moment `BlueprintSeed` and
    `blueprint_detail` disagree — including for a field added to both dataclasses and
    to nothing else, which is precisely the state J7 shipped in.
    """
    hints = typing.get_type_hints(BlueprintSeed)
    probes = {name: _probe_value(name, hints[name]) for name in sorted(SHARED_FIELDS)}
    detail = blueprint_detail(BlueprintSeed(**probes))

    dropped: list[str] = []
    for name, probe in probes.items():
        expected, actual = _comparable(probe, getattr(detail, name))
        if expected != actual:
            dropped.append(f"{name}: seed={expected!r} → detail={actual!r}")
    assert not dropped, (
        "tests/eval/conftest.py::blueprint_detail dropped or mangled "
        f"{len(dropped)} field(s) the seed carries: {dropped}. The A2 live eval "
        "serves that detail to a real model and to blueprint/executor.py, so a "
        "dropped field disables its feature for the entire eval while every other "
        "assertion stays green. Assign it from the seed in that constructor."
    )


def test_committed_corpus_fields_survive_the_conftest_constructor() -> None:
    """The same contract over the REAL fixtures the eval actually seeds.

    The synthetic test proves the keyword is present; this proves the VALUE arrives
    intact on the shapes `tests/fixtures/corpus/blueprints.yaml` produces — a
    conditional (`x or None`) that collapses a legitimate value, or a coercion that
    loses one, shows up here and not above.

    Only fields the seed actually POPULATES are compared: a seed that declares no
    `sql_template` has nothing for the detail to carry, and `None == None` is not a
    fidelity claim worth making.
    """
    mismatches: list[str] = []
    for seed in seed_corpus().values():
        detail = blueprint_detail(seed)
        for name in sorted(SHARED_FIELDS):
            seed_value = getattr(seed, name)
            if not seed_value:
                continue
            expected, actual = _comparable(seed_value, getattr(detail, name))
            if expected != actual:
                mismatches.append(f"{seed.id}.{name}: {expected!r} → {actual!r}")
    assert not mismatches, (
        f"the eval's blueprint details diverge from the committed corpus: {mismatches}"
    )


def test_committed_corpus_declares_window_anchors() -> None:
    """The J7 regression pin's PREMISE, asserted separately so it cannot rot silently.

    `test_committed_corpus_fields_survive_...` only compares fields a seed populates,
    so if every fixture lost its `window_anchor` that test would pass by skipping it
    — and the eval would once again be a place where the anchor is never exercised.
    """
    anchored = {
        seed.id: seed.window_anchor
        for seed in seed_corpus().values()
        if seed.window_anchor is not None
    }
    assert anchored, (
        "no committed blueprint fixture declares a `window_anchor`; the A2 eval can "
        "no longer exercise the J7 window note at all."
    )
    assert set(anchored.values()) <= set(WINDOW_ANCHORS), anchored


async def test_get_blueprint_serves_the_window_anchor_note() -> None:
    """END TO END over the SHIPPED tool and the eval's own index seeding.

    The two tests above are about the constructor; this is about the read that
    regressed. `GetBlueprintTool` is the real one, the index is seeded exactly as
    `conftest.build_retrieval` / `test_routing_live._full_corpus_retrieval` seed it,
    and the assertion is on the `result_full` a live model would receive — the
    payload that carried no window note across every L3 run.
    """
    seeds = seed_corpus()
    anchored = next(seed for seed in seeds.values() if seed.window_anchor is not None)

    index = FakeVectorIndex()
    for seed in seeds.values():
        index.add_detail(blueprint_detail(seed))
    tool = GetBlueprintTool(vector_index=index)

    scope = frozenset(column for seed in seeds.values() for column in seed.uses)
    result = await tool.run(
        {"id": anchored.id},
        RuntimeCredentials(session_id="s", jwt="j", column_scope=scope),
    )

    rf = result.result_full
    assert rf["found"] is True, rf
    assert "window_anchor" in rf, (
        f"getBlueprint({anchored.id!r}) served no window-anchor note though the seed "
        f"declares {anchored.window_anchor!r} — the model has no way to tell a "
        "data-anchored blueprint from a calendar-anchored one."
    )
    # The rendered form is `"<anchor> — <gloss>"`, not the bare enum: the note is
    # self-describing at the model or it is not worth sending.
    assert rf["window_anchor"].startswith(f"{anchored.window_anchor} — ")


@pytest.mark.parametrize("anchor", sorted(WINDOW_ANCHORS))
def test_both_anchors_round_trip_through_the_harness_projection(anchor: str) -> None:
    """Neither anchor is special-cased away by the projection.

    Cheap, and it closes the "one value happens to survive" reading of the tests
    above — `data` and `calendar` mean opposite things to a windowed blueprint, and
    a projection that carried only the one the fixtures use most would be a subtler
    version of the same bug.
    """
    seed = BlueprintSeed(
        id="bp-probe",
        intent="probe",
        slots_summary="",
        uses=["db.t.c"],
        window_anchor=anchor,
    )
    assert blueprint_detail(seed).window_anchor == anchor
