"""KNOWN LIMITATION: a `period_range` blueprint cannot be learned — S4 cannot emit it.

## Status

Written by QA as a DEFECT file with three failing tests. Resolved 2026-08-10 by
WITHDRAWING the type rather than repairing the rewriter — see "RESOLUTION" below the
fixtures. The failing tests are converted, not deleted: each now asserts the decline and
keeps its original finding as the justification. The rewriter proofs are untouched.

## What this file is

Plan §3a widened `extractor/models.py::SLOT_TYPES` with `relative_window` and
`period_range`, carved `binds_to` out for both, and fixed golden replay's sampler. Its
own summary of the replay fix says:

    "Without this the two types extract but can never PROMOTE, which is a silent dead
     end."

That was true of `relative_window`, which genuinely clears the whole path now — extract,
generalize, replay, land, and the replay SQL runs on real ClickHouse
(`tests/integration/test_windowed_replay_clickhouse_live.py`). It was NOT true of
`period_range`, and the reason is a CONSUMER of the widened vocabulary that the slice
did not enumerate: the S4 AST rewrite. The slice enumerated READERS of `slot["type"]`;
the rewrite is a WRITER, and it depends on the type only through the ARITY of the bind
sites the type implies — which no grep for the field can find.

## The gap

`generalize/rewrite.py::rewrite_sql_to_template` substitutes exactly one placeholder per
located literal, named verbatim after the slot:

    literal.replace(exp.Placeholder(this=name))     # name = slot["name"]

The runtime's grammar for a `period_range` is TWO bind tokens, `{name}_start` and
`{name}_end` (`runtime/blueprint/slots.py::slot_token_names` — the anti-drift helper the
replay fix correctly adopted). Nothing in S3 or S4 knows that grammar: grep `_start` in
`learning/extractor/` and `learning/generalize/` returns nothing. So the two shapes a
model can actually emit both dead-end, and each dead-ends somewhere DIFFERENT:

  | plan shape                        | S4 template                       | fails at        |
  |-----------------------------------|-----------------------------------|-----------------|
  | one slot `w`, two predicates      | `{w} ... {w}` (same token twice)  | `Blueprint.parse`: duplicate slot name |
  | slots `w_start`/`w_end`, both     | `{w_start} ... {w_end}`           | corpus loader: those slots declare `w_start_start`/`w_start_end`/... so BOTH tokens are undeclared |
  | typed `period_range`              |                                   |                 |

## Why the unit suite is green

`tests/learning/promotion/test_windowed_replay_sampling_qa.py::
test_a_period_range_template_binds_both_bounds_as_dates` hand-writes a slot named
`hire_window` NEXT TO a template referencing `{hire_window_start}`/`{hire_window_end}`.
That pairing is correct for a hand-authored canon blueprint (`bp-hires-in-range` is
exactly it) and is unreachable from the S3/S4 pipeline, which is the only producer the
learning loop has. The fix is real and the test is right about the sampler; what neither
covers is that no S4 output can ever present that pairing to it.

The blast radius of a widened vocabulary is "every consumer that keys off a slot's
declared type" — and the rewrite keys off `slot["name"]` while the runtime keys off
`slot_token_names(spec)`, which is the same mirror-drift shape the replay fix's own
docstring warns about, one seam upstream.

Slug: PA-period-range-s4-rewrite-gap.
"""

from __future__ import annotations

import pytest

from data_agent.learning.candidate.models import build_envelope
from data_agent.learning.extractor.models import (
    UNSUPPORTED_SLOT_TYPES,
    Decline,
    ExtractedCandidate,
)
from data_agent.learning.extractor.schema import SLOT_TYPE_ENUM
from data_agent.learning.extractor.validation import to_candidate
from data_agent.learning.generalize.stage import GeneralizeStage
from data_agent.learning.stage import StageContext
from data_agent.learning.summary.models import SessionSummary, ToolCallSummary, TurnSummary
from data_agent.learning.triage import TriageVerdict
from data_agent.runtime.blueprint.models import Blueprint, BlueprintParseError, SlotSpec
from data_agent.runtime.blueprint.slots import slot_token_names
from data_agent.runtime.blueprint.template import referenced_slots
from data_agent.runtime.retrieval.corpus_loader import (
    BlueprintSeed,
    CorpusLoadError,
    _validate_blueprint_dag,
)

_KEEP = TriageVerdict(decision="keep", reason="K1", target_hints=("blueprint",))

_CATALOG = {
    "dbpcm_warehouse.employee": {
        "employee_code": "String",
        "employee_status": "Nullable(String)",
        "most_recent_hire_date": "Nullable(DateTime64(6))",
    }
}

_USES = [
    "dbpcm_warehouse.employee.employee_code",
    "dbpcm_warehouse.employee.most_recent_hire_date",
]

# The accepted SQL of a session that asked for hires inside an EXPLICIT date range —
# the shape `bp-hires-in-range` exists for, and the reason `period_range` is a type.
_RANGE_SQL = (
    "SELECT toStartOfMonth(most_recent_hire_date) AS month, "
    "COUNT(DISTINCT employee_code) AS hires "
    "FROM dbpcm_warehouse.employee "
    "WHERE most_recent_hire_date >= '2024-01-01' "
    "AND most_recent_hire_date < '2024-07-01' "
    "GROUP BY toStartOfMonth(most_recent_hire_date)"
)

# The same query with ONE bound — used by the token-arity invariant, which drives a
# single-slot plan and would otherwise trip D97 totality on the second predicate.
_ONE_BOUND_SQL = (
    "SELECT toStartOfMonth(most_recent_hire_date) AS month, "
    "COUNT(DISTINCT employee_code) AS hires "
    "FROM dbpcm_warehouse.employee "
    "WHERE most_recent_hire_date >= '2024-01-01' "
    "GROUP BY toStartOfMonth(most_recent_hire_date)"
)


def _summary(sql: str = _RANGE_SQL) -> SessionSummary:
    call = ToolCallSummary(
        turn_index=0, tool_call_ref="tc1", tool_name="runQuery", args={"sql": sql},
        sql=sql, status="ok", error_code=None, provenance=frozenset(),
        result_columns=("month", "hires"), result_row_count=6, result_full_ref=None,
        full_result_loaded=False,
    )
    turn = TurnSummary(
        turn_index=0, user_nl="hires per month between January and July 2024",
        assistant_text=None, tool_call_refs=("tc1",),
    )
    return SessionSummary(
        session_id="s-range", user_id="u1", scope_ref="sc", trace_id="tr",
        content_hash="h", turns=(turn,), tool_calls=(call,), blueprint_usages=(),
        askuser_exchanges=(), failed_fixed_sql=(), accepted_signal="no_correction",
    )


def _candidate(parameterization: list[dict]) -> dict:
    return {
        "type": "blueprint", "confidence": 0.9,
        "evidence": [{"turn_ref": 0, "tool_call_ref": "tc1", "quote": "hires per month"}],
        "rationale": "reusable range report", "proposed_action": "new",
        "entity_self_check": {"contains_entities": False, "found": []},
        "depends_on": [],
        "payload": {
            "intent": "new hires per month within an explicit start/end range",
            "kind": "single",
            "resolves": {"hires": "dbpcm_warehouse.employee.employee_code"},
            "source_tool_call_refs": ["tc1"],
            "accepted_signal": "no_correction",
            "parameterization": parameterization,
            "result_signature": None,
            "notes": "",
        },
    }


def _range_candidate(*names: str) -> dict:
    """One `role=slot` entry per literal bound, each typed `period_range` (D97 totality
    demands one entry per literal predicate, and there are two)."""
    return _candidate([
        {
            "locator": {"table": "dbpcm_warehouse.employee",
                        "column": "most_recent_hire_date", "value": value},
            "role": "slot",
            "slot": {"name": name, "type": "period_range",
                     "binds_to": None, "required": True},
        }
        for name, value in zip(names, ("2024-01-01", "2024-07-01"), strict=True)
    ])


async def _generalized(raw: dict, sql: str = _RANGE_SQL) -> dict:
    """Run the REAL S3 validation + S4 generalize stage and return the generalization."""
    summary = _summary(sql)
    candidate = to_candidate(raw, summary, known_rules=frozenset())
    assert isinstance(candidate, ExtractedCandidate), candidate
    env = build_envelope(candidate, summary, candidate_id="c1", evidence_refs=("e1",))
    result = await GeneralizeStage(catalog_schema=_CATALOG).process(
        env, StageContext(summary=summary, verdict=_KEEP)
    )
    return result.envelope.payload


def _land(payload: dict, template: str) -> None:
    """The two landing gates a promoted blueprint must clear, in order."""
    slots = [
        p["slot"] for p in payload["parameterization"]
        if p.get("role") == "slot" and p.get("slot")
    ]
    Blueprint.parse(
        id="bp-x", intent="i", resolves=None, slots=slots, uses_rules=[],
        sql_template=template, composes=None, result_grain=["month"],
    )
    _validate_blueprint_dag(
        BlueprintSeed(
            id="bp-x", intent="i", slots_summary="", uses=_USES, status="validated",
            slots=slots, sql_template=template, uses_rules=[], composes=[],
            result_grain=["month"], resolves={},
        )
    )


# --- RESOLUTION (2026-08-10): the type is withdrawn, not repaired -----------------
#
# Both shapes below FAILED when this file was written, and the fix taken was NOT to
# teach the rewriter the two-token grammar. `period_range` is withheld from the prompt
# enum and declined at validation (`extractor/models.py::UNSUPPORTED_SLOT_TYPES`), so
# S3 no longer accepts either shape and neither can reach S4 at all.
#
# WHY withdrawal rather than repair. The gap is not a missing suffix. `parameterization`
# is one entry per literal predicate, each carrying its own `slot`, so there is no way
# to say "these two predicates are the two BOUNDS of one range". Repair needs a payload
# shape, a rewriter change, AND a rule deciding which bound each predicate is — and
# inferring that wrong silently inverts a date filter, the D56 wrong-answer class. That
# is its own slice with its own review, and shipping the type meanwhile would leave the
# silent dead end this file found: golden replay reports `passed=True` on both shapes,
# because the fake probe never executes the SQL.
#
# The two tests below therefore now assert the DECLINE, and keep the original finding
# as the justification for it. The rewriter proofs further down are untouched and are
# what makes the decline defensible rather than superstitious.


async def test_one_period_range_slot_over_two_bounds_is_declined_at_extraction():
    """WAS: "…_lands", failing. The model names ONE range slot (the runtime's own model:
    one slot, two bind sites) and D97 totality forces one plan entry per literal, so the
    rewrite stamped the SAME placeholder twice and declared the slot twice
    (`BlueprintParseError: duplicate slot name`). Now stopped at S3, where the reason
    names S4 instead of surfacing as a parse error four stages later."""
    out = to_candidate(
        _range_candidate("hire_window", "hire_window"), _summary(), known_rules=frozenset()
    )
    assert isinstance(out, Decline)
    assert "S4 cannot yet generalize" in out.detail


async def test_two_bound_named_period_range_slots_are_declined_at_extraction():
    """WAS: "…_land", failing. The model spells the two tokens itself. S4 produced a
    template that LOOKED right — `{hire_window_start} ... {hire_window_end}` — but each
    slot is typed `period_range`, so each declared its own two tokens
    (`hire_window_start_start`, …) and neither referenced token was declared by
    anything (`CorpusLoadError: undeclared slot`)."""
    out = to_candidate(
        _range_candidate("hire_window_start", "hire_window_end"),
        _summary(),
        known_rules=frozenset(),
    )
    assert isinstance(out, Decline)
    assert "S4 cannot yet generalize" in out.detail


# --- the invariant that would have caught it, stated once ------------------------


@pytest.mark.parametrize("slot_type", sorted(SLOT_TYPE_ENUM))
async def test_every_template_token_s4_emits_is_a_declared_bind_token(slot_type: str):
    """WAS: a single failing case. Now a LIVE RULE over every type the extractor
    actually offers, which is the form it should have had all along.

    The rule the corpus loader enforces at the far end: `referenced_slots(template)`
    must be a subset of the union of `slot_token_names(spec)` over the declared slots.
    It holds for every offered type and failed only for `period_range`, because S4
    spells the placeholder `{slot.name}` while the runtime spells the bind sites
    `slot_token_names(spec)`. Parametrizing over `SLOT_TYPE_ENUM` means re-enabling a
    withdrawn type without teaching S4 its grammar fails HERE, immediately."""
    if slot_type == "enum":
        pytest.skip("an enum slot needs enum_values; its token arity is the scalar one")
    # ONE predicate: D97 totality wants one plan entry per literal, so a single-entry
    # plan needs a single-predicate query. `_RANGE_SQL` has two by design.
    payload = await _generalized(
        _candidate([{
            "locator": {"table": "dbpcm_warehouse.employee",
                        "column": "most_recent_hire_date", "value": "2024-01-01"},
            "role": "slot",
            "slot": {
                "name": "hire_window", "type": slot_type, "required": True,
                "binds_to": (
                    None if slot_type == "relative_window"
                    else "dbpcm_warehouse.employee.most_recent_hire_date"
                ),
            },
        }]),
        sql=_ONE_BOUND_SQL,
    )
    template = payload["generalization"]["sql_template"]
    declared: set[str] = set()
    for raw in (
        p["slot"] for p in payload["parameterization"]
        if p.get("role") == "slot" and p.get("slot")
    ):
        declared |= slot_token_names(SlotSpec.parse(raw))
    assert referenced_slots(template) <= declared, (
        f"S4 emitted undeclared bind tokens "
        f"{sorted(referenced_slots(template) - declared)}; declared={sorted(declared)}"
    )


def test_the_withdrawal_is_what_closes_this_gap():
    """Ties the finding to its resolution, so neither can be changed alone: the type is
    still in the mirror (the runtime executes it), and still out of the prompt enum."""
    assert "period_range" in UNSUPPORTED_SLOT_TYPES
    assert "period_range" not in SLOT_TYPE_ENUM


# --- the control: relative_window, which DOES clear the whole path ---------------


_WINDOW_SQL = (
    "SELECT toStartOfMonth(most_recent_hire_date) AS month, "
    "COUNT(DISTINCT employee_code) AS hires "
    "FROM dbpcm_warehouse.employee "
    "WHERE most_recent_hire_date >= now() - INTERVAL 6 MONTH "
    "GROUP BY toStartOfMonth(most_recent_hire_date)"
)


async def test_relative_window_clears_extract_generalize_and_land():
    """PASSES — the half of the widening that is genuinely end-to-end. Kept here as the
    control, so a future fix to the range case cannot quietly break the window case, and
    so "the widening does not work" is never read as the finding."""
    raw = _candidate([{
        "locator": {"table": "dbpcm_warehouse.employee",
                    "column": "most_recent_hire_date", "value": "6"},
        "role": "slot",
        "slot": {"name": "window_months", "type": "relative_window",
                 "binds_to": None, "required": True},
    }])
    summary = _summary(_WINDOW_SQL)
    candidate = to_candidate(raw, summary, known_rules=frozenset())
    assert isinstance(candidate, ExtractedCandidate), candidate
    env = build_envelope(candidate, summary, candidate_id="c1", evidence_refs=("e1",))
    result = await GeneralizeStage(catalog_schema=_CATALOG).process(
        env, StageContext(summary=summary, verdict=_KEEP)
    )
    payload = result.envelope.payload
    template = payload["generalization"]["sql_template"]
    assert "{window_months}" in template
    assert payload["generalization"]["static_validation"]["outcome"] == "ok"
    _land(payload, template)


# --- the unit fixture the sampler fix is pinned against is not S4 output ---------


def test_the_sampler_fixture_pairing_is_not_something_s4_can_produce():
    """PASSES, and is the reason the two failures above are invisible today.

    `test_windowed_replay_sampling_qa` pairs a slot NAMED `hire_window` with a template
    referencing `{hire_window_start}`/`{hire_window_end}`. That pairing is correct — it
    is `bp-hires-in-range`, hand-authored — and it is exactly what the rewrite cannot
    emit, because the rewrite's placeholder IS the slot name."""
    from data_agent.learning.generalize.rewrite import rewrite_sql_to_template

    rendered = rewrite_sql_to_template(
        _RANGE_SQL,
        [
            {"locator": {"table": "dbpcm_warehouse.employee",
                         "column": "most_recent_hire_date", "value": v},
             "role": "slot",
             "slot": {"name": "hire_window", "type": "period_range"}}
            for v in ("2024-01-01", "2024-07-01")
        ],
    )
    assert referenced_slots(rendered) == {"hire_window"}
    assert "hire_window_start" not in rendered


def test_the_landing_gates_this_file_relies_on_still_say_what_it_says_they_do():
    """Belt: if either gate is relaxed, the failures above stop meaning what the
    docstring claims and this test is what notices."""
    dup = [{"name": "w", "type": "period_range", "required": True}] * 2
    with pytest.raises(BlueprintParseError, match="duplicate slot name"):
        Blueprint.parse(id="b", intent="i", resolves=None, slots=dup, uses_rules=[],
                        sql_template="SELECT 1 AS x FROM t WHERE c = {w}",
                        composes=None, result_grain=[])
    seed = BlueprintSeed(
        id="b", intent="i", slots_summary="", uses=_USES, status="validated",
        slots=[{"name": "w_start", "type": "period_range", "required": True}],
        sql_template=(
            "SELECT COUNT(DISTINCT employee_code) AS hires "
            "FROM dbpcm_warehouse.employee WHERE most_recent_hire_date >= {w_start}"
        ),
        uses_rules=[], composes=[], result_grain=[], resolves={},
    )
    with pytest.raises(CorpusLoadError, match="undeclared slot"):
        _validate_blueprint_dag(seed)
