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

Frozen-contract note: the S4 fixture's `generalization.sql_template` uses sqlglot
COLON placeholders (`:department`), while the runtime binder authors BRACE tokens
(`{department}`). We normalize colon→brace before reusing `bind_template`, so the
one frozen template style replays through the runtime binder unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from data_agent.runtime.blueprint.models import ResultGrain
from data_agent.runtime.blueprint.template import (
    TemplateBindError,
    bind_template,
    referenced_slots,
)
from data_agent.runtime.blueprint.verify import VerifyOutcome, verify_result

from ..candidate.generalization import BlueprintGeneralization
from ..candidate.models import CandidateEnvelope
from .models import WarehouseProbe

# A sqlglot colon placeholder (`:name`) that is NOT a `::` type-cast — the S4
# template's bind-site style. Rewritten to the runtime binder's `{name}` token.
_COLON_PLACEHOLDER = re.compile(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)")


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


def _to_brace(template: str) -> str:
    """Normalize S4 colon placeholders (`:name`) to the runtime binder's `{name}`
    tokens so `bind_template` (reused verbatim) can drive the replay."""
    return _COLON_PLACEHOLDER.sub(lambda m: "{" + m.group(1) + "}", template)


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


def _sample_bindings(slot_names: set[str]) -> dict[str, Any]:
    """Mint a SYNTHETIC value per referenced slot — never a stored entity input
    (D17). A synthetic string binds as a typed literal (F1) and proves the template
    binds + runs; its value is irrelevant (the probe is a structure oracle, D98)."""
    return {name: f"__replay_sample_{name}__" for name in slot_names}


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
    missing generalization/template, a bind failure, or a failed D56 gate. NEVER
    raises for an expected shape problem — the scheduler treats a non-passing
    replay as "do not promote" / "demote", the D98 fail-closed posture."""
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

    brace_template = _to_brace(template)
    slot_names = referenced_slots(brace_template)
    bindings = _sample_bindings(slot_names)
    sampled = tuple(sorted(slot_names))
    try:
        replay_sql = bind_template(brace_template, bindings)
    except TemplateBindError:
        return ReplayOutcome(False, None, None, sampled, reason="bind_failed")

    grain = ResultGrain(
        columns=gen.result_grain.columns, verifiable=gen.result_grain.verifiable
    )
    expected_columns = _expected_columns(env.payload)

    probe_result = await probe.run(replay_sql, grain_columns=grain.columns)
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
