"""KNOWN LIMITATION: the `SLOT_TYPES` mirror has drifted narrower than the runtime's.

`learning/extractor/models.py:27` declares six slot types; `runtime/blueprint/
models.py:21` declares eight — the extractor mirror is missing `relative_window` and
`period_range`, the two WINDOWED types D41/D49 added. The mirror is a deliberate
decoupling (the extractor package must not import the request-path blueprint module),
which is exactly why it needs a parity test; the sibling `NODE_KINDS` mirror got one
in this slice and `SLOT_TYPE_GLOSS` has had one all along, so this is the odd one out.

Blast radius, measured (not assumed) below — the gap is TWO-LAYERED:

  1. PROMPT: `extractor/schema.py::SLOT_TYPE_ENUM` is `sorted(SLOT_TYPES)` off the
     narrow mirror, so the forced-structured-output tool schema never OFFERS the two
     types. A compliant model literally cannot emit them.
  2. VALIDATION: if one arrives anyway (a non-enforcing model, a replayed candidate,
     a hand-fed payload), `_validate_roles` DECLINES it — `role_inconsistent`, a HARD
     reject, not the `fail_to_review` human valve.

Net: DECLINED, never mis-typed at the boundary — but because layer 1 forecloses the
correct type, the realistic model behaviour is to reach for `period` or `string`
instead, which is accepted silently. That is the mis-typing risk, and it is one layer
in from where you would look for it.

Concretely: THREE of the canon's ten blueprints — `bp-hires-per-month` and
`bp-hires-projection` (a `relative_window` slot each) and `bp-hires-in-range` (a
`period_range` slot) — are blueprints the RUNTIME executes that the learning loop can
never re-derive.

NOT FIXED HERE — pinned as a limitation per the brief.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from data_agent.learning.extractor.models import SLOT_TYPES as EXTRACTOR_SLOT_TYPES
from data_agent.learning.extractor.models import Decline, ExtractedCandidate
from data_agent.learning.extractor.schema import SLOT_TYPE_ENUM, build_extractor_tool
from data_agent.learning.extractor.validation import REASON_BAD_ROLE, to_candidate
from data_agent.runtime.blueprint.models import SLOT_TYPES as RUNTIME_SLOT_TYPES

from .helpers import blueprint_raw, make_summary, make_tool_call, param_slot

_REAL_CANON_DIR = Path("/Users/kalpeshmulye/Development/clickhouse-api/app/corpus/data/blueprints")
_MISSING = ("relative_window", "period_range")
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


# --- the drift itself ----------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "MEDIUM (pre-existing, flagged not fixed): `learning/extractor/models.py::"
        "SLOT_TYPES` is missing `relative_window` and `period_range`, which "
        "`runtime/blueprint/models.py::SLOT_TYPES` has carried since D41/D49. The "
        "mirror is intentional (no request-path import from the extractor package); "
        "the MISSING parity test is not. Fixing it is a two-line edit plus a prompt "
        "gloss, but it is out of scope for this slice."
    ),
)
def test_the_extractor_slot_type_mirror_matches_the_runtime() -> None:
    assert EXTRACTOR_SLOT_TYPES == RUNTIME_SLOT_TYPES


def test_the_drift_is_exactly_the_two_windowed_types() -> None:
    """Bound the finding: nothing else has drifted, and it has drifted in only one
    direction (the extractor is a strict SUBSET, so no extractor type is un-landable)."""
    assert RUNTIME_SLOT_TYPES - EXTRACTOR_SLOT_TYPES == set(_MISSING)
    assert EXTRACTOR_SLOT_TYPES - RUNTIME_SLOT_TYPES == set()


# --- layer 1: the prompt never offers the two types ---------------------------


@pytest.mark.parametrize("slot_type", _MISSING)
def test_the_prompt_enum_has_the_same_gap(slot_type: str) -> None:
    """`SLOT_TYPE_ENUM` is derived from the narrow mirror, so the gap is not just a
    validation-side check — it is baked into the tool schema the model is forced to
    conform to. A model CANNOT emit the correct type for a windowed slot."""
    assert slot_type not in SLOT_TYPE_ENUM
    assert slot_type not in json.dumps(build_extractor_tool())


def test_the_prompt_enum_is_the_narrow_mirror_verbatim() -> None:
    """Pins the derivation, so fixing `SLOT_TYPES` automatically fixes the prompt —
    i.e. there is ONE place to change, not two."""
    assert SLOT_TYPE_ENUM == sorted(EXTRACTOR_SLOT_TYPES)


# --- layer 2: what actually happens to such a candidate -----------------------


@pytest.mark.parametrize("slot_type", _MISSING)
def test_a_windowed_slot_is_hard_declined_not_failed_to_review(slot_type: str) -> None:
    """The answer to 'declined, mis-typed, or silently coerced?': DECLINED — and via
    `role_inconsistent`, a HARD reject that bypasses the human-review valve. The
    session's windowed blueprint is dropped with no path to a human."""
    out = _validate_slot_of_type(slot_type)
    assert isinstance(out, Decline)
    assert out.reason == REASON_BAD_ROLE
    assert "invalid type" in out.detail


def test_it_is_not_silently_coerced_to_a_neighbouring_type() -> None:
    """Rule out the worst reading: the value is rejected, never quietly rewritten to
    `period`/`string` by the validator itself."""
    out = _validate_slot_of_type("relative_window")
    assert isinstance(out, Decline)


@pytest.mark.parametrize("fallback", ["period", "string", "entity", "as_of_date", "list"])
def test_the_realistic_fallback_type_is_accepted_with_no_warning(fallback: str) -> None:
    """The actual exposure. Because layer 1 removes the right answer from the enum, a
    model describing a trailing window picks the nearest offered type — and every one
    of them extracts CLEANLY. The runtime resolves a `relative_window` to a BOUNDED
    integer bound as an `INTERVAL {n} MONTH` NUMBER literal; a `period`/`string` slot
    resolves and binds as a typed STRING. Nothing between here and landing flags the
    difference."""
    out = _validate_slot_of_type(fallback)
    assert isinstance(out, ExtractedCandidate)
    assert out.payload.parameterization[0].slot.type == fallback


# --- the canon blueprints this forecloses -------------------------------------


def _canon_docs() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    if not _REAL_CANON_DIR.is_dir():
        pytest.skip(f"real MCP canon not checked out at {_REAL_CANON_DIR}")
    return {
        (doc := yaml.safe_load(path.read_text()))["id"]: doc
        for path in sorted(_REAL_CANON_DIR.glob("bp-*.yaml"))
    }


def test_the_canon_really_does_ship_blueprints_using_the_missing_types() -> None:
    """Guard on the guard: if the canon ever stops using the windowed types the
    finding shrinks to theoretical, and this test says so."""
    canon = _canon_docs()
    used = {
        slot.get("type")
        for doc in canon.values()
        for slot in (doc.get("slots") or [])
    }
    assert used & set(_MISSING), f"canon slot types in use: {sorted(t for t in used if t)}"
    assert "relative_window" in {
        s.get("type") for s in canon["bp-hires-per-month"]["slots"]
    }


def test_no_canon_slot_type_outside_the_runtime_set() -> None:
    """The runtime set is the authority; confirm it is not ITSELF behind the canon
    (which would make the extractor two hops stale instead of one)."""
    canon = _canon_docs()
    used = {
        slot.get("type")
        for doc in canon.values()
        for slot in (doc.get("slots") or [])
        if slot.get("type")
    }
    assert used <= RUNTIME_SLOT_TYPES, f"canon uses unknown slot types: {used - RUNTIME_SLOT_TYPES}"


def test_the_loop_can_re_derive_every_canon_slot_type() -> None:
    """The one-line statement of the blast radius. Currently False for exactly the
    `bp-hires-per-month` / `bp-hires-in-range` family."""
    canon = _canon_docs()
    unlearnable = sorted(
        {
            f"{bid}:{slot['type']}"
            for bid, doc in canon.items()
            for slot in (doc.get("slots") or [])
            if slot.get("type") and slot["type"] not in EXTRACTOR_SLOT_TYPES
        }
    )
    assert unlearnable == [
        "bp-hires-in-range:period_range",
        "bp-hires-per-month:relative_window",
        "bp-hires-projection:relative_window",
    ], (
        "the set of canon blueprints the learning loop cannot re-derive has changed; "
        "update this pin (or delete it, if SLOT_TYPES parity has been fixed)"
    )
