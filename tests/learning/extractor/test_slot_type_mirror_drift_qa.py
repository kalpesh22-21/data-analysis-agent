"""PARITY (was: a KNOWN LIMITATION) — the `SLOT_TYPES` mirror vs. the runtime's.

## History

This file was written as a pin on a defect and is now a guard on its fix. Both states
are recorded because the reasoning is what keeps the guard honest.

**Before (pinned, plan §3a "3a — Vocabulary"):** `learning/extractor/models.py`
declared six slot types; `runtime/blueprint/models.py` declared eight — the extractor
mirror was missing `relative_window` and `period_range`, the two WINDOWED types
D41/D49 added. The mirror is a deliberate decoupling (the extractor package must not
import the request-path blueprint module), which is exactly why it needed a parity
test; the sibling `NODE_KINDS` mirror got one and `SLOT_TYPE_GLOSS` had had one all
along, so this was the odd one out. The pin was
`test_the_extractor_slot_type_mirror_matches_the_runtime`, `xfail(strict=True)`.

The blast radius was TWO-LAYERED, and the second layer was the dangerous one:

  1. PROMPT: `extractor/schema.py::SLOT_TYPE_ENUM` is `sorted(SLOT_TYPES)`, so the
     forced-structured-output tool schema never OFFERED the two types. A compliant
     model literally could not emit them.
  2. VALIDATION: if one arrived anyway, `_validate_roles` DECLINED it —
     `role_inconsistent`, a HARD reject, not the `fail_to_review` human valve.

Net: never mis-typed at the boundary — but because layer 1 foreclosed the correct
answer, the realistic model behaviour was to reach for `period` or `string` instead,
and THOSE extracted cleanly with no warning. Three of the canon's ten blueprints
(`bp-hires-per-month`, `bp-hires-projection`, `bp-hires-in-range`) were blueprints the
RUNTIME executes that the learning loop could never re-derive.

**After (this slice):** the mirror was widened to eight and the strict xfail flipped to
the plain parity assertion below. Widening alone would NOT have been enough, and the
tests in "the second half of the fix" say why: the tool schema marks `binds_to`
required for every slot, while `runtime/blueprint/models.py::SlotSpec.parse` REFUSES a
`binds_to` on a windowed type ("a windowed-period slot consumes no domain"). Offering
the type without carving out `binds_to` would have traded a clean extraction-time
decline for a `BlueprintParseError` raised at LANDING, several stages downstream — a
strictly worse failure. So `binds_to` became nullable, `WINDOWED_SLOT_TYPES` became the
one place the rule is written, and `_validate_roles` enforces BOTH directions.

The decline behaviour changed as intended: a windowed slot is now ACCEPTED (when it
omits `binds_to`) instead of hard-rejected. What used to be
`test_a_windowed_slot_is_hard_declined_not_failed_to_review` and
`test_the_realistic_fallback_type_is_accepted_with_no_warning` are replaced by their
positive counterparts below.

**Later (the Tier-1 dedup):** the mirror is GONE — `extractor/models.py` now imports
`SLOT_TYPES`/`NODE_KINDS` from `runtime/blueprint/models.py` and re-exports them under
the same names. The decoupling rationale ("the extractor package must not import the
request-path blueprint module") had already lapsed on its own: `extractor/validation.py`
imports `runtime.blueprint.template` and `generalize/canonical.py` imports
`runtime.blueprint.structural_key`. D58c only forbids the OTHER direction. The parity
assertion below is kept, and tightened to `is`, as a tripwire against re-mirroring.

**Still pinned, one hop further out:** `test_windowed_replay_sampling_qa.py` covers
what golden replay does with these types, which is the next thing that keys off them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from data_agent.learning.extractor.models import SLOT_TYPES as EXTRACTOR_SLOT_TYPES
from data_agent.learning.extractor.models import (
    UNSUPPORTED_SLOT_TYPES,
    WINDOWED_SLOT_TYPES,
    Decline,
    ExtractedCandidate,
)
from data_agent.learning.extractor.schema import SLOT_TYPE_ENUM, build_extractor_tool
from data_agent.learning.extractor.validation import REASON_BAD_ROLE, to_candidate
from data_agent.runtime.blueprint.models import SLOT_TYPES as RUNTIME_SLOT_TYPES
from data_agent.runtime.blueprint.models import BlueprintParseError, SlotSpec

from .helpers import blueprint_raw, make_summary, make_tool_call, param_slot

_REAL_CANON_DIR = Path("/Users/kalpeshmulye/Development/clickhouse-api/app/corpus/data/blueprints")
_WINDOWED = ("relative_window", "period_range")
# The subset of `_WINDOWED` the pipeline can actually CARRY. `period_range` is withheld
# (`UNSUPPORTED_SLOT_TYPES`) because S4's rewriter emits one bind token and the type
# needs two — a separate finding with its own file,
# `tests/learning/generalize/test_period_range_s4_rewrite_gap_qa.py`. Every test here
# that drives a candidate through validation therefore uses `_OFFERED_WINDOWED`, while
# the tests about the MIRROR itself still use `_WINDOWED`: the mirror is about which
# types exist, the enum is about which we can produce, and conflating the two is what
# this file exists to prevent.
_OFFERED_WINDOWED = tuple(t for t in _WINDOWED if t not in UNSUPPORTED_SLOT_TYPES)
_SQL = (
    "SELECT COUNT(employee_code) AS hires FROM dbpcm_warehouse.employee "
    "WHERE window_months = 6"
)


def _validate_slot_of_type(slot_type: str, **slot_kwargs: Any):
    raw = blueprint_raw(
        parameterization=[
            param_slot(
                "window_months",
                slot_type=slot_type,
                value="6",
                table="dbpcm_warehouse.employee",
                **slot_kwargs,
            )
        ],
    )
    summary = make_summary(tool_calls=(make_tool_call(ref="tc1", sql=_SQL),))
    return to_candidate(raw, summary, known_rules=frozenset())


# --- the parity guard (was the xfail pin) --------------------------------------


def test_the_extractor_slot_type_mirror_matches_the_runtime() -> None:
    """THE guard this file exists for, now one notch stronger than it was.

    It began as set EQUALITY, not a subset: a type the extractor knows and the runtime
    does not is un-landable, and a type the runtime knows and the extractor does not is
    un-learnable. Both are silent.

    The mirror has since been REPLACED by a downward import — `extractor/models.py`
    re-exports `runtime.blueprint.models.SLOT_TYPES` — so the assertion is IDENTITY.
    That is deliberately stricter than the property under test: equality is what
    actually matters, but equality is also what a freshly re-introduced local
    frozenset would satisfy on the day it was written, drifting only later. `is`
    fails the moment someone re-mirrors, which is the failure this file exists for."""
    assert EXTRACTOR_SLOT_TYPES is RUNTIME_SLOT_TYPES


def test_the_windowed_types_are_the_ones_that_were_missing() -> None:
    """Names the two the mirror drifted on, so a future reader can find this history
    from either side of the fix."""
    assert set(_WINDOWED) <= EXTRACTOR_SLOT_TYPES
    assert WINDOWED_SLOT_TYPES == set(_WINDOWED)


# --- layer 1: the prompt now offers them --------------------------------------


@pytest.mark.parametrize("slot_type", _OFFERED_WINDOWED)
def test_the_prompt_enum_offers_the_windowed_types(slot_type: str) -> None:
    """The layer that mattered most. `SLOT_TYPE_ENUM` is derived from the mirror, so
    the gap was baked into the tool schema the model is FORCED to conform to — a model
    cannot emit a type the enum omits, whatever the validator would have done with it."""
    assert slot_type in SLOT_TYPE_ENUM
    assert slot_type in json.dumps(build_extractor_tool())


def test_the_prompt_enum_is_the_mirror_minus_what_s4_cannot_carry() -> None:
    """Pins the derivation, so changing `SLOT_TYPES` automatically changes the prompt —
    ONE place to change, not two — and makes the withdrawal an explicit subtraction
    rather than a second hand-maintained list. Drift (the mirror silently disagreeing
    about which types EXIST) and withdrawal (knowingly not offering one we cannot
    generalize) are different statements, and this is where they are kept apart."""
    assert SLOT_TYPE_ENUM == sorted(EXTRACTOR_SLOT_TYPES - UNSUPPORTED_SLOT_TYPES)
    assert UNSUPPORTED_SLOT_TYPES <= EXTRACTOR_SLOT_TYPES


def test_the_schema_tells_the_model_what_a_relative_window_carries() -> None:
    """An enum value with no gloss is an invitation to guess. The runtime resolves a
    `relative_window` to a BARE integer (the unit lives in the template), and a model
    that emits "6 months" produces a slot the resolver refuses at bind time."""
    schema = json.dumps(build_extractor_tool()).lower()
    assert "relative_window" in schema
    assert "unit" in schema and "6" in schema


# --- layer 2: what actually happens to such a candidate now -------------------


@pytest.mark.parametrize("slot_type", _OFFERED_WINDOWED)
def test_a_windowed_slot_without_binds_to_is_accepted(slot_type: str) -> None:
    """The point of the whole change: the canon's own shape now extracts. Both canon
    windowed slots (`bp-hires-per-month`, `bp-hires-in-range`) declare `name`/`type`/
    `required` and NO `binds_to`."""
    out = _validate_slot_of_type(slot_type, binds_to=None)
    assert isinstance(out, ExtractedCandidate)
    slot = out.payload.parameterization[0].slot
    assert slot.type == slot_type
    assert slot.binds_to is None


# --- the second half of the fix: binds_to is type-dependent -------------------


@pytest.mark.parametrize("slot_type", _OFFERED_WINDOWED)
@pytest.mark.parametrize("binds_to", ["dbpcm_warehouse.employee.hire_date", ""])
def test_a_windowed_slot_declaring_binds_to_is_declined_here(
    slot_type: str, binds_to: str
) -> None:
    """WHY widening the enum alone would have been a regression, stated as a test.

    The tool schema marks `binds_to` required for every slot, so the first thing a
    model does with the newly-offered type is attach one — and `SlotSpec.parse` refuses
    that (see the companion test below). Catching it at extraction turns a landing-time
    raise into a traceable `role_inconsistent` decline naming the slot.

    `""` IS ONE OF THE CASES, and it is the one the first cut of this fix got wrong.
    The guard was `if p.slot.binds_to:` — written from the intent ("must not declare a
    binding target") rather than from the operation downstream, which is
    `binds_to is not None` (`models.py:170`). The two agree on every input except the
    empty string, which is exactly what an "emit null" instruction routinely produces
    and what the schema's `["string","null"]` permits. It extracted cleanly, passed S4
    (`builder.py:75` also skips falsy) and raised at LANDING — the failure this rule
    exists to eliminate, reintroduced by the fix for it."""
    out = _validate_slot_of_type(slot_type, binds_to=binds_to)
    assert isinstance(out, Decline)
    assert out.reason == REASON_BAD_ROLE
    assert "must not declare binds_to" in out.detail


@pytest.mark.parametrize("slot_type", _WINDOWED)
@pytest.mark.parametrize("binds_to", ["db.t.c", ""])
def test_the_runtime_really_does_refuse_binds_to_on_these(
    slot_type: str, binds_to: str
) -> None:
    """Guard on the guard: the decline above is only justified while the runtime keeps
    refusing, and it must refuse the SAME set of values. Kept over BOTH windowed types
    (not just the offered one) because the runtime's rule is about the type, not about
    what we happen to offer this month. Parametrized over `""` as well — if `SlotSpec.parse` ever loosened to a truthiness test, the extractor
    would be rejecting something that now lands fine, and this fails first."""
    with pytest.raises(BlueprintParseError, match="must not declare 'binds_to'"):
        SlotSpec.parse(
            {"name": "w", "type": slot_type, "binds_to": binds_to, "required": True}
        )


def test_an_empty_binds_to_is_still_rejected_for_a_non_windowed_slot() -> None:
    """The other side of the asymmetry, and why the two branches cannot share a
    predicate: `""` belongs on the ACCEPT side of neither. Nothing downstream RAISES on
    it here — the harm is a slot landing with no domain to probe — so this branch stays
    a truthiness test while the windowed one is `is not None`."""
    out = _validate_slot_of_type("entity", binds_to="")
    assert isinstance(out, Decline)
    assert "has no binds_to" in out.detail


def test_a_non_windowed_slot_still_requires_binds_to() -> None:
    """The converse, and the reason `binds_to` was made conditionally-optional rather
    than optional. Making it nullable for everyone would have let an `entity` slot land
    with no column domain — no DISTINCT probe, and S4's `binds_to ⊆ uses` assertion
    vacuously true."""
    out = _validate_slot_of_type("entity", binds_to=None)
    assert isinstance(out, Decline)
    assert out.reason == REASON_BAD_ROLE
    assert "has no binds_to" in out.detail


@pytest.mark.parametrize("slot_type", _OFFERED_WINDOWED)
def test_a_windowed_slot_round_trips_into_the_runtime_slot_spec(slot_type: str) -> None:
    """End of the chain, and the test that would have caught the `""` bug on its own:
    the extractor's slot doc is what `generalize/mapping.py::_slot_docs` hands straight
    to `SlotSpec.parse` at landing. Parsing it HERE is what stops "extracts cleanly" and
    "lands cleanly" drifting apart, which is the only failure mode this whole rule is
    about."""
    out = _validate_slot_of_type(slot_type, binds_to=None)
    assert isinstance(out, ExtractedCandidate)
    spec = SlotSpec.parse(out.payload.parameterization[0].slot.to_doc())
    assert spec.type == slot_type
    assert spec.binds_to is None


# --- the canon blueprints this unblocks ---------------------------------------


def _canon_docs() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    if not _REAL_CANON_DIR.is_dir():
        pytest.skip(f"real MCP canon not checked out at {_REAL_CANON_DIR}")
    return {
        (doc := yaml.safe_load(path.read_text()))["id"]: doc
        for path in sorted(_REAL_CANON_DIR.glob("bp-*.yaml"))
    }


def test_the_canon_really_does_ship_blueprints_using_the_windowed_types() -> None:
    """Guard on the guard: if the canon ever stops using the windowed types the fix
    shrinks to theoretical, and this test says so."""
    canon = _canon_docs()
    used = {
        slot.get("type")
        for doc in canon.values()
        for slot in (doc.get("slots") or [])
    }
    assert used & set(_WINDOWED), f"canon slot types in use: {sorted(t for t in used if t)}"
    assert "relative_window" in {
        s.get("type") for s in canon["bp-hires-per-month"]["slots"]
    }


def test_no_canon_slot_type_outside_the_runtime_set() -> None:
    """The runtime set is the authority; confirm it is not ITSELF behind the canon
    (which would make the extractor two hops stale instead of zero)."""
    canon = _canon_docs()
    used = {
        slot.get("type")
        for doc in canon.values()
        for slot in (doc.get("slots") or [])
        if slot.get("type")
    }
    assert used <= RUNTIME_SLOT_TYPES, f"canon uses unknown slot types: {used - RUNTIME_SLOT_TYPES}"


def test_no_canon_windowed_slot_declares_binds_to() -> None:
    """The `binds_to: null` rule is not invented here — it is what the canon already
    does, because the loader would refuse anything else. Pinning it against the real
    YAMLs is what makes the extractor-side rule a MIRROR rather than an opinion."""
    canon = _canon_docs()
    offenders = [
        f"{bid}:{slot['name']}"
        for bid, doc in canon.items()
        for slot in (doc.get("slots") or [])
        if slot.get("type") in _WINDOWED and slot.get("binds_to")
    ]
    assert offenders == []


def test_the_loop_can_re_derive_every_canon_slot_type() -> None:
    """The one-line statement of the blast radius. Was False for exactly the
    `bp-hires-per-month` / `bp-hires-projection` / `bp-hires-in-range` family; is now
    empty. A regression here means the mirror has drifted again."""
    canon = _canon_docs()
    unlearnable = sorted(
        {
            f"{bid}:{slot['type']}"
            for bid, doc in canon.items()
            for slot in (doc.get("slots") or [])
            if slot.get("type") and slot["type"] not in EXTRACTOR_SLOT_TYPES
        }
    )
    assert unlearnable == [], (
        "a canon slot type is outside the extractor's mirror again — the loop cannot "
        "re-derive these blueprints. See this file's History section."
    )
