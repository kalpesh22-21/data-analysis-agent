"""promotion/replay.py — golden replay (D29/D36/D98), the STRUCTURE oracle.

A candidate does not promote on replay alone (D98 layer iii), but replay is a hard GATE on
the `candidate → validated` edge: it proves the frozen S4 template still executes and still
produces a result whose STRUCTURE (grain-integrity teeth + result_signature column shape) is
intact. Structure, NOT values — there is no value oracle here, and adding one would breach
D17. REUSE, not reimplement: the D56 `verify_result` gate and the runtime template binder
verbatim, so the replay SQL is built exactly as a live `runBlueprint` would build it. Slot
values are SAMPLED synthetic tokens, never stored entity inputs. A single green replay is
not a correctness proof.
"""

from __future__ import annotations

import logging
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
    """The result of one golden replay.

    `passed` is the reused D56 gate verdict and `verify` carries the per-check breakdown. Never
    carries a returned value (D98).
    """

    passed: bool
    verify: VerifyOutcome | None
    replay_sql: str | None
    sampled_slots: tuple[str, ...]
    reason: str | None = None  # stable machine tag when not passed


_logger = logging.getLogger(__name__)


def _pick_template(gen: BlueprintGeneralization) -> str | None:
    """The template whose result STRUCTURE the D56 gate verifies.

    A single blueprint's top-level template, or the TERMINAL node of a composite (highest
    `order`) — the node that produces the final result the grain/signature describes.
    """
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
    """Map each `{token}` a slot may bind → the slot's declared `type` (role=="slot").

    Keyed by TOKEN, not by slot name, and the difference is visible for exactly one type: a
    `period_range` occupies TWO bind sites (`{name}_start`/`{name}_end`). `_sample_bindings`
    looks values up by what `referenced_slots(template)` found in the SQL, so a name-keyed map
    silently missed both halves and sampled them as untyped strings, which a real warehouse
    rejects. The expansion calls the runtime's own `slot_token_names` rather than re-spelling
    the grammar a fourth time, and a `SlotSpec` is constructed directly rather than parsed,
    because the plan's slot dict is untrusted and a full parse would raise on the fail-closed
    promotion path.

    NOTE: the `period_range` half is AHEAD of the pipeline — S4 cannot yet emit a range slot —
    and is kept correct because the fake probe never executes the SQL, so the bug it prevents
    reports `passed=True` on SQL ClickHouse rejects.
    """
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
    entity input (D17). `as_of_date`, `period`, and each bound of a `period_range` → a
    fixed ISO date; `relative_window` → a bare INTEGER (the unit lives in the template,
    so the bind site is a number literal); `list` → a one-element set (so `IN {slot}`
    binds); every other type (string/entity/enum) → a synthetic string token that binds
    as a typed literal (F1).

    ⚠ `period` MOVED into the date branch on 2026-08-28, from live evidence. The gloss in
    `runtime/blueprint/models.py` calls a period "a warehouse pay-period key, NOT a calendar
    date", and on that reading a string token looked right — but S4 emits `period` for
    date-window slots and binds them straight into `toDate({slot})`. Every live blueprint
    carrying a period slot (3 of 3 in the review queue) did exactly that, so the replay sent
    `toDate('__replay_sample_pay_period_end_start__')` to ClickHouse and got
    `Code: 38 ... Cannot parse Date from String`. `golden_replay` catches that into
    `probe_unavailable`, so EVERY period-windowed blueprint was unapprovable and unpromotable,
    and the reviewer saw only `approve_blocked_replay:probe_unavailable` with no clue why.

    An ISO date is correct under BOTH readings: it parses inside a date function, and it is
    still a plain string where a period is a key column. Matching zero rows is fine either way
    — the probe is a STRUCTURE oracle (columns + grain counts), not a data one, which is the
    same reason `as_of_date`'s fixed 2020-01-01 has always been acceptable."""
    if slot_type in ("as_of_date", "period", "period_range"):
        return _SAMPLE_DATE
    if slot_type in ("relative_window", "positive_integer"):
        return _SAMPLE_RELATIVE_WINDOW
    if slot_type == "list":
        return [f"__replay_sample_{name}__"]
    return f"__replay_sample_{name}__"


def _sample_bindings(slot_names: set[str], slot_types: dict[str, str]) -> dict[str, Any]:
    """Mint a type-correct SYNTHETIC value per referenced slot — never a stored entity input (D17).

    The value proves the template binds and runs; the probe is a structure oracle, so only the
    TYPE matters (D98/R7).
    """
    return {name: _sample_value(name, slot_types.get(name, "")) for name in slot_names}


def _expected_columns(payload: dict[str, Any]) -> tuple[str, ...] | None:
    """The declared result-signature column SHAPE (D98 — the entity-free golden).

    `None` (no declared signature) makes the reused `verify_result` skip the shape check, never
    a false pass.
    """
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

    `passed=False` with a machine `reason` on a missing generalization or template, a bind
    failure, a failed D56 gate, or a probe failure (`probe_unavailable`). NEVER raises: an
    unavailable probe is a fail-closed NON-promotion, not an exception escaping into the cron
    `_guard` or, worse, out of the human approve path.
    """
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
        #
        # ⚠ LOGGED, and it was not. `probe_unavailable` is the reason a reviewer sees on a
        # refused approve, and it names the CATEGORY while discarding the only thing that
        # identifies the fault — one badly-typed sample value produced a ClickHouse
        # `Cannot parse Date from String` that reached nobody, and the queue simply stopped
        # promoting. A fail-closed path still has to say what it closed on.
        _logger.warning(
            "golden replay: the probe raised for %s — holding at probe_unavailable. "
            "Replay SQL: %s",
            getattr(env, "candidate_id", "<unknown>"),
            replay_sql,
            exc_info=True,
        )
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
