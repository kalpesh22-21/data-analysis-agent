"""Deterministic post-emit validation of a raw candidate (D31/D34/D97).

Turns one raw structured-output candidate dict into either an `ExtractedCandidate`
(structurally valid, ready to persist) or a `Decline` (rejected before emit, with
a reason code the consumer traces). The validations are the S3 safety teeth:

  - **Evidence mandatory** (D31): `len(evidence) >= 1` — the primary guard against
    hallucinated learning; zero evidence ⇒ `no_evidence` decline.
  - **Lift-not-generate** (D34): a `blueprint` requires `accepted_signal` AND the
    session must actually have carried acceptance (`SessionSummary.accepted_signal
    is not None`); else `no_acceptance`.
  - **Totality, no drop** (D97): exactly one `ParamPlan` per literal predicate of
    the accepted SQL — a missing predicate is a silent dropped filter →
    `totality_violation` (fail-to-review). Un-parseable SQL → `unrewritable_sql`.
  - **Role consistency** (D97): slot→valid type + optional slot carries an
    `optional_pattern` (no silent drop); rule→an EXISTING catalog `rule_id`
    (missing ⇒ `missing_rule`, the §7 pairing hook); inline→a `why`.
"""

from __future__ import annotations

from typing import Any

from ..summary.models import SessionSummary
from .models import (
    SLOT_TYPES,
    BlueprintPayload,
    CandidateHeader,
    ColumnShape,
    ComposeNodePlan,
    Decline,
    EntitySelfCheck,
    EvidenceRef,
    ExtractedCandidate,
    Locator,
    ParamPlan,
    ResultGrainPlan,
    ResultSignature,
    SlotPlan,
)
from .sql_predicates import literal_predicates

# Reason codes (traced by the consumer). `fail_to_review` reasons are the D52/D97
# human-review valve; the rest are hard rejects.
REASON_NO_EVIDENCE = "no_evidence"
REASON_NO_ACCEPTANCE = "no_acceptance"
REASON_TOTALITY = "totality_violation"
REASON_UNREWRITABLE = "unrewritable_sql"
REASON_BAD_ROLE = "role_inconsistent"
REASON_MISSING_RULE = "missing_rule"
REASON_MALFORMED = "malformed_candidate"

# The D34 acceptance domain (`thumbs_up` is declared but never emitted by S2 —
# it may still legitimately appear on a candidate the extractor forwards).
_ACCEPTED_SIGNAL_DOMAIN = frozenset({"no_correction", "thumbs_up", "explicit_confirm"})


def _evidence(raw: dict[str, Any]) -> tuple[EvidenceRef, ...]:
    items = raw.get("evidence") or []
    if not isinstance(items, list):
        return ()
    refs: list[EvidenceRef] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            refs.append(
                EvidenceRef(
                    turn_ref=int(item["turn_ref"]),
                    tool_call_ref=str(item["tool_call_ref"]),
                    quote=str(item["quote"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(refs)


def _header(raw: dict[str, Any], evidence: tuple[EvidenceRef, ...]) -> CandidateHeader:
    esc = raw.get("entity_self_check") or {}
    return CandidateHeader(
        type=raw["type"],
        confidence=float(raw.get("confidence", 0.0)),
        evidence=evidence,
        rationale=str(raw.get("rationale", "")),
        proposed_action=str(raw.get("proposed_action", "new")),
        entity_self_check=EntitySelfCheck(
            contains_entities=bool(esc.get("contains_entities", False)),
            found=tuple(str(f) for f in (esc.get("found") or [])),
        ),
        depends_on=tuple(str(d) for d in (raw.get("depends_on") or [])),
    )


def _slot_plan(raw: dict[str, Any]) -> SlotPlan:
    enum_values = raw.get("enum_values")
    return SlotPlan(
        name=str(raw["name"]),
        type=str(raw["type"]),
        binds_to=str(raw["binds_to"]),
        # A real model sometimes omits `required`. Default to True: a predicate that
        # appeared in the ACCEPTED SQL is required unless the model explicitly marks
        # it optional — the safe side of the no-drop (D97) invariant (an optional slot
        # still needs an optional_pattern, enforced in `_validate_roles`).
        required=bool(raw.get("required", True)),
        optional_pattern=(
            str(raw["optional_pattern"]) if raw.get("optional_pattern") is not None else None
        ),
        enum_values=tuple(str(v) for v in enum_values) if enum_values else None,
    )


def _param_plans(raw_params: list[dict[str, Any]]) -> list[ParamPlan]:
    plans: list[ParamPlan] = []
    for rp in raw_params:
        loc = rp["locator"]
        plans.append(
            ParamPlan(
                locator=Locator(
                    table=str(loc["table"]), column=str(loc["column"]), value=str(loc["value"])
                ),
                role=rp["role"],
                slot=_slot_plan(rp["slot"]) if rp.get("slot") else None,
                rule_id=str(rp["rule_id"]) if rp.get("rule_id") is not None else None,
                why=str(rp["why"]) if rp.get("why") is not None else None,
            )
        )
    return plans


def _result_signature(raw: dict[str, Any] | None) -> ResultSignature | None:
    if not raw:
        return None
    grain = raw.get("grain") or {}
    return ResultSignature(
        shape=tuple(
            ColumnShape(column=str(s["column"]), type=str(s["type"]))
            for s in (raw.get("shape") or [])
        ),
        grain=ResultGrainPlan(
            columns=tuple(str(c) for c in (grain.get("columns") or [])),
            verifiable=bool(grain.get("verifiable", True)),
        ),
        invariants=tuple(str(i) for i in (raw.get("invariants") or [])),
    )


def _compose_nodes(raw_nodes: list[dict[str, Any]]) -> tuple[ComposeNodePlan, ...]:
    nodes: list[ComposeNodePlan] = []
    for rn in raw_nodes:
        nodes.append(
            ComposeNodePlan(
                order=int(rn["order"]),
                node_kind=str(rn.get("node_kind", "query")),
                step_intent=str(rn.get("step_intent", "")),
                feeds_from=tuple(int(f) for f in (rn.get("feeds_from") or [])),
                consumes=dict(rn.get("consumes") or {}),
                output=dict(rn.get("output") or {}),
                source_tool_call_ref=rn.get("source_tool_call_ref"),
                when=rn.get("when"),
                requires_approval=rn.get("requires_approval"),
            )
        )
    return tuple(nodes)


def _validate_roles(params: list[ParamPlan], known_rules: frozenset[str]) -> Decline | None:
    for p in params:
        if p.role == "slot":
            if p.slot is None or p.slot.type not in SLOT_TYPES:
                return Decline("blueprint", REASON_BAD_ROLE, f"slot {p.locator.column} invalid type")
            # An `enum` slot MUST carry non-empty enum_values — the runtime
            # `SlotSpec.parse` rejects an enum slot without them (un-landable). Catch
            # it here as a traceable decline rather than a crash at landing. A
            # free-text filter value should be typed `entity`/`string`, not `enum`.
            if p.slot.type == "enum" and not p.slot.enum_values:
                return Decline(
                    "blueprint", REASON_BAD_ROLE,
                    f"enum slot {p.slot.name} has no enum_values "
                    "(a free-text value should be type 'entity', not 'enum')",
                )
            # No-drop (D97): an optional slot MUST carry an optional_pattern, else
            # an absent bind silently drops the predicate.
            if not p.slot.required and not p.slot.optional_pattern:
                return Decline(
                    "blueprint", REASON_BAD_ROLE,
                    f"optional slot {p.slot.name} has no optional_pattern (would silently drop)",
                )
        elif p.role == "rule":
            if not p.rule_id:
                return Decline("blueprint", REASON_BAD_ROLE, "rule role without rule_id")
            if p.rule_id not in known_rules:
                # §7 missing-rule: a rule-shaped predicate with no catalog rule →
                # fail-to-review (Slice-6 will pair a schema_edit(add_rule)).
                return Decline("blueprint", REASON_MISSING_RULE, f"unknown rule {p.rule_id!r}")
        elif p.role == "inline":
            if not p.why:
                return Decline("blueprint", REASON_BAD_ROLE, "inline role without 'why'")
        else:
            return Decline("blueprint", REASON_BAD_ROLE, f"unknown role {p.role!r}")
    return None


def _table_compatible(plan_table: str, pred_table: str) -> bool:
    # Enforce table equality ONLY when BOTH are fully-qualified (dotted) — a bare
    # alias (`d`) or an unqualified column ("") cannot be resolved to a
    # `database.table` here, so matching falls back to (column, value).
    if not plan_table or not pred_table:
        return True
    if "." not in plan_table or "." not in pred_table:
        return True
    return (
        plan_table == pred_table
        or plan_table.endswith(f".{pred_table}")
        or pred_table.endswith(f".{plan_table}")
    )


def _validate_totality(
    payload: BlueprintPayload, summary: SessionSummary
) -> Decline | None:
    # Gather the accepted SQL of the cited source runQuery refs.
    sql_by_ref = {tc.tool_call_ref: tc.sql for tc in summary.tool_calls}
    plan_locators = [p.locator for p in payload.parameterization]
    saw_any_sql = False
    for ref in payload.source_tool_call_refs:
        sql = sql_by_ref.get(ref)
        if not sql:
            continue
        saw_any_sql = True
        predicates = literal_predicates(sql)
        if predicates is None:
            return Decline("blueprint", REASON_UNREWRITABLE, f"un-parseable SQL at {ref}")
        for pred in predicates:
            # Per-locator coverage (LOW-1): match by (column, value) so
            # `region='NA' OR region='EU'` needs a plan entry PER predicate, and
            # same-named columns on different (qualified) tables are distinguished
            # by `table`. A predicate with NO covering ParamPlan → silent dropped
            # filter → decline (fail-to-review).
            covered = any(
                loc.column.lower() == pred.column.lower()
                and loc.value == pred.value
                and _table_compatible(loc.table, pred.table)
                for loc in plan_locators
            )
            if not covered:
                return Decline(
                    "blueprint", REASON_TOTALITY,
                    f"predicate {pred.column}={pred.value!r} "
                    f"(table {pred.table or '?'}) has no parameterization entry",
                )
    if not saw_any_sql:
        return Decline("blueprint", REASON_UNREWRITABLE, "no accepted SQL found for source refs")
    return None


def _resolves(raw_resolves: Any) -> dict[str, str]:
    # `resolves` is a {term: column} map, but a real model sometimes emits it as a
    # list (e.g. of {term, column} pairs). Coerce a non-dict to an empty map rather
    # than crashing — `resolves` is advisory (not required, not totality-checked).
    if not isinstance(raw_resolves, dict):
        return {}
    return {str(k): str(v) for k, v in raw_resolves.items()}


def _blueprint_payload(raw: dict[str, Any]) -> BlueprintPayload:
    return BlueprintPayload(
        intent=str(raw["intent"]),
        kind=str(raw["kind"]),
        resolves=_resolves(raw.get("resolves")),
        source_tool_call_refs=tuple(str(r) for r in (raw.get("source_tool_call_refs") or [])),
        accepted_signal=str(raw["accepted_signal"]),
        parameterization=tuple(_param_plans(raw.get("parameterization") or [])),
        composes=_compose_nodes(raw.get("composes") or []),
        result_signature=_result_signature(raw.get("result_signature")),
        notes=str(raw.get("notes", "")),
    )


def to_candidate(
    raw: dict[str, Any], summary: SessionSummary, *, known_rules: frozenset[str]
) -> ExtractedCandidate | Decline:
    """Validate one raw candidate → `ExtractedCandidate` or `Decline`."""
    ctype = raw.get("type")
    if ctype not in ("blueprint", "global_knowledge", "user_knowledge", "schema_edit"):
        return Decline(str(ctype), REASON_MALFORMED, "unknown candidate type")

    evidence = _evidence(raw)
    if not evidence:
        # D31 primary guard — no evidence ⇒ rejected before the audit snapshot.
        return Decline(ctype, REASON_NO_EVIDENCE, "candidate cites no evidence")

    try:
        header = _header(raw, evidence)
    except (KeyError, TypeError, ValueError) as exc:
        return Decline(ctype, REASON_MALFORMED, f"bad header: {exc}")

    if ctype != "blueprint":
        # §3.3: other targets are emitted with their Locked payload as a dict in
        # S3 (depth is on blueprint). Evidence-mandatory already enforced above.
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            return Decline(ctype, REASON_MALFORMED, "payload is not an object")
        return ExtractedCandidate(header=header, payload=dict(payload))

    # --- blueprint depth path ---
    payload_raw = raw.get("payload") or {}
    # A real model can emit a field with the wrong JSON type (e.g. a list where a
    # dict is expected). ANY shape confusion here MUST become a traceable Decline —
    # never an uncaught exception that escapes to the consumer (which would skip the
    # job and route it to dead-letter instead of recording a malformed_candidate).
    if not isinstance(payload_raw, dict):
        return Decline(ctype, REASON_MALFORMED, "blueprint payload is not an object")
    try:
        payload = _blueprint_payload(payload_raw)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return Decline(ctype, REASON_MALFORMED, f"bad blueprint payload: {exc}")

    # Lift-not-generate (D34): acceptance mandatory, IN the D34 domain (LOW-2 —
    # not merely truthy; an out-of-domain value like "banana" is rejected), AND
    # the session must actually have carried acceptance.
    if payload.accepted_signal not in _ACCEPTED_SIGNAL_DOMAIN:
        return Decline(
            ctype, REASON_NO_ACCEPTANCE,
            f"accepted_signal {payload.accepted_signal!r} not in "
            f"{sorted(_ACCEPTED_SIGNAL_DOMAIN)}",
        )
    if summary.accepted_signal is None:
        return Decline(ctype, REASON_NO_ACCEPTANCE, "session carried no acceptance signal")

    role_decline = _validate_roles(list(payload.parameterization), known_rules)
    if role_decline is not None:
        return role_decline

    totality_decline = _validate_totality(payload, summary)
    if totality_decline is not None:
        return totality_decline

    return ExtractedCandidate(header=header, payload=payload)
