"""promotion/replay.py — golden replay (D29/D36/D98), the STRUCTURE oracle.

A candidate does not promote on replay alone (D98 layer iii), but replay is a hard
GATE on the `candidate → validated` edge: it proves the frozen S4 template still
executes and still produces a result whose STRUCTURE (grain-integrity teeth +
result_signature column shape) is intact. It verifies **structure, not values**
(D98) — there is NO value oracle here; adding one would breach D17 (the recorded
scalar answer is entity-bearing and never stored).

REUSE, not reimplement:
  * the D56 `verify_result` gate (`runtime/blueprint/verify.py`) — the grain-teeth
    (`row_count == COUNT(DISTINCT grain)`) + signature-shape check, verbatim.
  * the runtime template binder (`runtime/blueprint/template.py::bind_template`) —
    the same F1/D10 typed-literal binding the executor uses, so the replay SQL is
    built exactly as a live `runBlueprint` would build it.

Slot values are SAMPLED — synthetic tokens minted here, NEVER stored entity inputs
(D17: no entity input is ever persisted, so replay cannot and must not reuse one).
The synthetic sample proves the template BINDS and RUNS; the injected
`WarehouseProbe` (fake in tests, no real ClickHouse) returns the `verify_result`
triple. A single green replay is not a correctness proof.

Contract note: the S4 `generalization.sql_template` is BRACE authoring form
(`{department}`) — the SAME shape the runtime binder authors — so it feeds straight
into `bind_template`/`referenced_slots` with zero placeholder translation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from data_agent.runtime.blueprint.models import ResultGrain, SlotSpec
from data_agent.runtime.blueprint.slots import slot_token_names
from data_agent.runtime.blueprint.template import (
    TemplateBindError,
    bind_template,
    referenced_slots,
)
from data_agent.runtime.blueprint.verify import VerifyOutcome, verify_result

from ..candidate.generalization import BlueprintGeneralization
from ..candidate.models import CandidateEnvelope
from .models import WarehouseProbe


@dataclass(frozen=True)
class ReplayOutcome:
    """The result of one golden replay. `passed` is the reused D56 gate verdict;
    `verify` carries the per-check breakdown (grain teeth + signature shape).
    Never carries a returned value/number (D98)."""

    passed: bool
    verify: VerifyOutcome | None
    replay_sql: str | None
    sampled_slots: tuple[str, ...]
    reason: str | None = None  # stable machine tag when not passed


def _pick_template(gen: BlueprintGeneralization) -> str | None:
    """The template whose result STRUCTURE the D56 gate verifies: a single
    blueprint's top-level template, or the TERMINAL node of a composite (highest
    `order`) — the node that produces the final result the grain/signature
    describes (the executor gates only the terminal, §2.4/D56)."""
    if gen.sql_template:
        return gen.sql_template
    if gen.node_templates:
        return max(gen.node_templates, key=lambda n: n.order).sql_template
    return None


# A fixed synthetic date for `as_of_date` / `period_range` slots (D17 — never a
# stored value).
_SAMPLE_DATE = "2020-01-01"

# A synthetic `relative_window` count. Must be a bare INT, not a string: the type's
# whole contract is that the unit lives in the template (`INTERVAL {n} MONTH`), so the
# bind site is a NUMBER literal. `1` rather than `6` because it is the only value
# guaranteed to sit inside every authorable `[min_value, max_value]` band — the
# resolver floors at 1 and a canon blueprint may narrow the ceiling to anything above
# it (`bp-hires-per-month` uses 1..36).
_SAMPLE_RELATIVE_WINDOW = 1


def _slot_types(payload: dict[str, Any]) -> dict[str, str]:
    """Map each `{token}` a slot may bind → the slot's declared `type` from
    `payload.parameterization` (role=="slot"). Used to sample a TYPE-CORRECT synthetic
    value per bind site (R7) so a date/list/windowed slot does not type-error when the
    replay hits a real warehouse.

    Keyed by TOKEN, not by slot name, and the difference is only visible for one type:
    a `period_range` occupies TWO bind sites (`{name}_start`/`{name}_end`) and every
    other type occupies one. `_sample_bindings` looks values up by what
    `referenced_slots(template)` found in the SQL — i.e. by token — so a name-keyed map
    silently missed both halves of a `period_range` and sampled them as untyped
    strings, which a real warehouse rejects as a date comparison.

    The expansion calls `slot_token_names`, the runtime's own anti-drift helper, rather
    than re-spelling the `_start`/`_end` grammar: that grammar already exists in the
    executor, the corpus loader and the binder, and a fourth hand-written copy is the
    mirror-drift failure this codebase keeps paying for. A `SlotSpec` is constructed
    directly (not via `parse`) because only `name`/`type` matter to the helper and the
    plan's slot dict is untrusted — running the full parse here would turn a malformed
    plan into a raise on the fail-closed promotion path.

    NOTE: the `period_range` half is currently AHEAD of the pipeline. S4's
    `rewrite_sql_to_template` emits one token per predicate, so the loop cannot yet
    produce a range slot at all and the extractor declines the type
    (`extractor/models.py::UNSUPPORTED_SLOT_TYPES`). It is kept, and kept correct,
    because removing it would re-arm exactly the trap that motivated it: the fake probe
    never executes the SQL, so a name-keyed map made golden replay report `passed=True`
    on SQL ClickHouse rejects. When the rewriter learns the grammar, this must not be a
    second thing to remember."""
    types: dict[str, str] = {}
    params = payload.get("parameterization")
    if not isinstance(params, list):
        return types
    for entry in params:
        if not isinstance(entry, dict):
            continue
        slot = entry.get("slot")
        if not isinstance(slot, dict) or not isinstance(slot.get("name"), str):
            continue
        name = slot["name"]
        slot_type = slot.get("type")
        slot_type = slot_type if isinstance(slot_type, str) else ""
        for token in slot_token_names(SlotSpec(name=name, type=slot_type)):
            types[token] = slot_type
    return types


def _sample_value(name: str, slot_type: str) -> Any:
    """A SYNTHETIC value typed per the slot's declared type (R7) — never a stored
    entity input (D17). `as_of_date` and each bound of a `period_range` → a fixed ISO
    date; `relative_window` → a bare INTEGER (the unit lives in the template, so the
    bind site is a number literal); `list` → a one-element set (so `IN {slot}` binds);
    every other type (string/entity/enum/period) → a synthetic string token that binds
    as a typed literal (F1)."""
    if slot_type in ("as_of_date", "period_range"):
        return _SAMPLE_DATE
    if slot_type == "relative_window":
        return _SAMPLE_RELATIVE_WINDOW
    if slot_type == "list":
        return [f"__replay_sample_{name}__"]
    return f"__replay_sample_{name}__"


def _sample_bindings(slot_names: set[str], slot_types: dict[str, str]) -> dict[str, Any]:
    """Mint a type-correct SYNTHETIC value per referenced slot — never a stored
    entity input (D17). The value proves the template binds + runs; the probe is a
    structure oracle, so only the TYPE (not the value) matters (D98/R7)."""
    return {name: _sample_value(name, slot_types.get(name, "")) for name in slot_names}


def _expected_columns(payload: dict[str, Any]) -> tuple[str, ...] | None:
    """The declared result-signature column SHAPE (D98 — the entity-free golden).
    `None` (no declared signature) makes the reused `verify_result` skip the shape
    check (a no-op), never a false pass."""
    sig = payload.get("result_signature") or {}
    shape = sig.get("shape") or []
    cols = tuple(
        c["column"]
        for c in shape
        if isinstance(c, dict) and isinstance(c.get("column"), str) and c["column"]
    )
    return cols or None


async def golden_replay(
    env: CandidateEnvelope, *, probe: WarehouseProbe
) -> ReplayOutcome:
    """Replay `env`'s frozen S4 template through the reused binder + D56 gate.

    Returns a `ReplayOutcome`; `passed=False` (with a machine `reason`) on a
    missing generalization/template, a bind failure, a failed D56 gate, OR a probe
    failure (`reason="probe_unavailable"` — the warehouse/query service is down or,
    today, the deferred stub probe is wired). NEVER raises: an unavailable probe is a
    fail-closed NON-promotion, not an exception that escapes into the cron `_guard`
    (needless traceback spam) or, worse, uncaught out of the human `approve` path.
    The scheduler treats any non-passing replay as "do not promote" / "demote", the
    D98 fail-closed posture."""
    gen_doc = env.payload.get("generalization")
    if not isinstance(gen_doc, dict):
        return ReplayOutcome(False, None, None, (), reason="no_generalization")
    try:
        gen = BlueprintGeneralization.from_doc(gen_doc)
    except (KeyError, TypeError):
        return ReplayOutcome(False, None, None, (), reason="malformed_generalization")

    template = _pick_template(gen)
    if not template:
        return ReplayOutcome(False, None, None, (), reason="no_template")

    slot_names = referenced_slots(template)
    bindings = _sample_bindings(slot_names, _slot_types(env.payload))
    sampled = tuple(sorted(slot_names))
    try:
        replay_sql = bind_template(template, bindings)
    except TemplateBindError:
        return ReplayOutcome(False, None, None, sampled, reason="bind_failed")

    # BACKSTOP (D57/D80b): the real probe mints a JWT scoped to `gen.uses`. An EMPTY
    # `uses` would mint an ALLOW-ALL (unrestricted) token, running a model/extraction-
    # derived replay SQL against live ClickHouse with no scope — the learning plane
    # must NEVER do that. Short-circuit to a clean fail-closed HOLD with an honest
    # machine reason (NOT the misleading `probe_unavailable`), BEFORE any mint/MCP call.
    if not gen.uses:
        return ReplayOutcome(False, None, replay_sql, sampled, reason="no_uses_scope")

    grain = ResultGrain(
        columns=gen.result_grain.columns, verifiable=gen.result_grain.verifiable
    )
    expected_columns = _expected_columns(env.payload)

    try:
        # `column_scope=gen.uses` is the blueprint's declared footprint (D87/OQ-1) —
        # the real probe mints a JWT scoped to EXACTLY it, so the replay reads only
        # within the declared footprint and the MCP's D57 teeth are the backstop.
        probe_result = await probe.run(
            replay_sql, grain_columns=grain.columns, column_scope=gen.uses
        )
    except Exception:  # noqa: BLE001 - a probe/warehouse failure is a fail-closed non-promotion
        # The warehouse or query service is unreachable (or the deferred stub probe
        # is wired). Degrade to a clean non-promoting outcome so BOTH the cron scan
        # and the human approve path hold cleanly — never an uncaught raise.
        return ReplayOutcome(False, None, replay_sql, sampled, reason="probe_unavailable")
    verify = verify_result(
        result_grain=grain,
        row_count=probe_result.row_count,
        distinct_grain_count=probe_result.distinct_grain_count,
        columns=probe_result.columns,
        expected_columns=expected_columns,
    )
    return ReplayOutcome(
        passed=verify.passed,
        verify=verify,
        replay_sql=replay_sql,
        sampled_slots=sampled,
        reason=None if verify.passed else (verify.reason or "verify_failed"),
    )


__all__ = ["ReplayOutcome", "golden_replay"]
