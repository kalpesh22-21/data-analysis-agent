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
  - **Role consistency** (D97): slot→valid type + a `binds_to` iff the type takes
    one (see `WINDOWED_SLOT_TYPES`) + optional slot carries an `optional_pattern`
    (no silent drop); rule→an EXISTING catalog `rule_id` (missing ⇒ `missing_rule`,
    the §7 pairing hook); inline→a `why`.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.blueprint.template import TemplateBindError, validate_optional_pattern

from ..summary.models import SessionSummary
from .models import (
    NODE_KINDS,
    SLOT_TYPES,
    UNSUPPORTED_SLOT_TYPES,
    WINDOWED_SLOT_TYPES,
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
    # ABSENT and explicit-null both mean "this slot declares no column domain", which
    # is legal for exactly the two `WINDOWED_SLOT_TYPES` and illegal for everything
    # else — `_validate_roles` decides which, because it is the only place that knows
    # the type. A PRESENT non-null value is still `str()`-coerced exactly as before,
    # so a model emitting `binds_to: 123` keeps its existing route (a bogus bind that
    # fails S4's `binds_to ⊆ uses` check and reaches a HUMAN via fail-to-review),
    # rather than being newly hard-rejected here.
    raw_binds = raw.get("binds_to")
    return SlotPlan(
        name=str(raw["name"]),
        type=str(raw["type"]),
        binds_to=None if raw_binds is None else str(raw_binds),
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


def _node_index(value: Any) -> int | None:
    """Coerce an LLM-emitted node index (`order` / a `feeds_from` entry) to an int,
    or `None` when the value is not one.

    A real model emits `0` or the string `"0"`, so a numeric string is accepted. A
    BOOL is not an index (`True` would silently become node 1) and a fractional float
    is a typo, not something to truncate — both are `None` (⇒ decline)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _validate_compose_nodes(raw_nodes: Any) -> Decline | None:
    """Structural gate over the raw `composes` DAG plan — the composite sibling of
    `_validate_roles`, run BEFORE `_compose_nodes` builds the typed plans.

    This is LLM output: every field is untrusted. The gate exists so a shape-confused
    node (`order` absent, `"order": "first"`, `feeds_from` a string or a bare index,
    `consumes` a list) becomes a Decline naming WHICH node and WHICH field, rather
    than a raw interpreter message — or, worse, an exception: an uncaught raise out of
    `to_candidate` leaves the consumer message un-acked → reclaim → dead-letter,
    losing the whole session. Nothing here may raise; `to_candidate` additionally runs
    this inside its malformed-payload try as the belt.

    Checked here: the STRUCTURE `_compose_nodes` assumes and `Node.parse` requires at
    landing. Deliberately NOT checked here: the DAG SEMANTICS (unique orders, acyclic
    edges, executable `output` kinds) — S4's `check_dag` owns those and routes them to
    `fail_to_review`, the human-review valve, which a hard `malformed` reject would
    bypass. `when` is likewise left alone: S4 declines ANY when-bearing composite
    (`when_bearing_composite`) whatever its type."""
    if raw_nodes is None:
        return None  # a single (non-composite) blueprint declares no DAG
    if not isinstance(raw_nodes, list):
        return Decline("blueprint", REASON_MALFORMED, "composes is not a list")
    for idx, rn in enumerate(raw_nodes):
        where = f"composes[{idx}]"
        if not isinstance(rn, dict):
            return Decline("blueprint", REASON_MALFORMED, f"{where} is not an object")
        if "order" not in rn:
            return Decline("blueprint", REASON_MALFORMED, f"{where} has no 'order'")
        order = _node_index(rn["order"])
        if order is None:
            return Decline(
                "blueprint", REASON_MALFORMED,
                f"{where} 'order' {rn['order']!r} is not an integer",
            )
        node_kind = rn.get("node_kind", "query")
        # `isinstance` BEFORE the frozenset test: `x not in <frozenset>` HASHES x, so
        # `"node_kind": ["query"]` — ordinary model output — would raise TypeError
        # straight out of `to_candidate` and out of `LearningExtractor.extract` (no
        # try/except there), costing the whole session's extraction. That is precisely
        # the failure this gate exists to prevent, so it must not be the gate's own
        # crash site. `node_kind` was the ONLY untrusted field here fed raw to a
        # membership test; every other one is isinstance-checked or `str()`-coerced.
        if not isinstance(node_kind, str) or node_kind not in NODE_KINDS:
            return Decline(
                "blueprint", REASON_MALFORMED,
                f"{where} has unknown node_kind {node_kind!r} (allowed: {sorted(NODE_KINDS)})",
            )
        # ABSENT means "none"; every other wrong type declines, INCLUDING the falsy
        # ones. `x or []` would have normalized `0`, `""` and `{}` alike to no-edges:
        # `feeds_from: 0` ("feeds from node 0", the likeliest scalar-for-list slip
        # since node 0 is always first) silently lost an edge, leaving a `consumes`
        # whose source is absent from `feeds_from` — a CorpusLoadError at landing. The
        # falsy CONTAINERS lose no information, but "the model sent a string where a
        # list belongs" is the signal you want in a decline reason when tuning the
        # prompt, and one type violation treated two ways is a rule nobody remembers.
        # Same rule for the two maps below.
        feeds = rn.get("feeds_from")
        if feeds is None:
            feeds = []
        if not isinstance(feeds, list):
            return Decline(
                "blueprint", REASON_MALFORMED,
                f"{where} 'feeds_from' is not a list (got {type(feeds).__name__})",
            )
        for src in feeds:
            if _node_index(src) is None:
                return Decline(
                    "blueprint", REASON_MALFORMED,
                    f"{where} 'feeds_from' entry {src!r} is not an integer",
                )
        for field_name in ("consumes", "output"):
            value = rn.get(field_name)
            if value is None:
                value = {}
            if not isinstance(value, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in value.items()
            ):
                return Decline(
                    "blueprint", REASON_MALFORMED,
                    f"{where} {field_name!r} must be an object of string→string "
                    f"(got {type(value).__name__})",
                )
        # `requires_approval` has no downstream gate before `Node.parse` at LANDING,
        # where a non-object raises past the promotion path — reject it here instead.
        requires_approval = rn.get("requires_approval")
        if requires_approval is not None and not isinstance(requires_approval, dict):
            return Decline(
                "blueprint", REASON_MALFORMED, f"{where} 'requires_approval' is not an object"
            )
    return None


def _compose_nodes(raw_nodes: list[dict[str, Any]]) -> tuple[ComposeNodePlan, ...]:
    """Build the typed `ComposeNodePlan`s. PRECONDITION (load-bearing):
    `_validate_compose_nodes` has already passed on `raw_nodes`, which is what makes
    every coercion below total — `int(...)` here is exactly `_node_index` given that
    gate (bools and fractional floats are already declined)."""
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
                # Coerced (as `_param_plans` coerces `rule_id`): S4 looks the ref up in
                # a dict, so an unhashable model-emitted value must never reach it.
                source_tool_call_ref=(
                    str(rn["source_tool_call_ref"])
                    if rn.get("source_tool_call_ref") is not None
                    else None
                ),
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
            # A type the RUNTIME executes but this pipeline cannot GENERALIZE. Declined
            # FIRST, before the `binds_to` rules below, so the reason names the actual
            # blocker rather than a consequence of it.
            #
            # Reached only by a replayed candidate, a hand-fed payload or a
            # non-enforcing model — the prompt enum does not offer these
            # (`schema.py::SLOT_TYPE_ENUM`). It exists because the alternative is the
            # failure this whole rule family is about: `period_range` extracts and
            # generalizes into a template whose tokens no slot declares, dies at the
            # landing gates, and golden replay says `passed=True` on the way there
            # (the fake probe never executes the SQL). An honest decline beats a
            # silent dead end four stages downstream.
            if p.slot.type in UNSUPPORTED_SLOT_TYPES:
                return Decline(
                    "blueprint", REASON_BAD_ROLE,
                    f"slot {p.slot.name} has type {p.slot.type!r}, which the runtime "
                    "executes but S4 cannot yet generalize (rewrite_sql_to_template "
                    "emits ONE token per predicate; this type binds two, "
                    "{name}_start/{name}_end). Express the filter as two separate "
                    "as_of_date/period slots instead.",
                )
            # `binds_to` presence is TYPE-DEPENDENT, and the two directions are
            # DIFFERENT tests on purpose — each mirrors the operation its own
            # downstream reader performs, not the English sentence "must/must not
            # declare a binding target":
            #
            #   windowed  → `is not None`. `SlotSpec.parse` (models.py:170) refuses on
            #     `binds_to is not None`, so `""` is a REFUSAL there. A truthiness test
            #     here agreed with it on every input except that one — and `""` is
            #     exactly what an "emit null" instruction routinely produces, and what
            #     the schema's `["string","null"]` permits. It passed this validator,
            #     passed S4 (`builder.py:75` also skips falsy), and raised
            #     `BlueprintParseError` at LANDING out of `blueprint_seed_from_candidate`
            #     — reintroducing the extraction-clean-then-landing-raise failure this
            #     rule exists to eliminate. Seventh sighting of the derive-the-guard
            #     class; the guard was written from the INTENT rather than the read.
            #
            #   non-windowed → truthiness. Nothing downstream raises on an absent
            #     `binds_to` here; the harm is silent (the slot lands with no domain, so
            #     the DISTINCT-domain probe that makes a value checkable never fires and
            #     S4's `binds_to ⊆ uses` assertion is vacuous), and `""` is just as
            #     unusable as absent. So the empty string belongs on the REJECT side of
            #     this branch and on the ACCEPT side of the one above — which is why
            #     they cannot share a predicate.
            #     (Before this slice the field was mandatory in `_slot_plan`, so an
            #     absent one was a KeyError ⇒ `malformed_candidate`; it is now a named
            #     role decline, the same hard reject with a reason a prompt-tuner can
            #     act on.)
            if p.slot.type in WINDOWED_SLOT_TYPES:
                if p.slot.binds_to is not None:
                    return Decline(
                        "blueprint", REASON_BAD_ROLE,
                        f"{p.slot.type} slot {p.slot.name} must not declare binds_to "
                        "(a windowed-period slot consumes no column domain; emit null)",
                    )
            elif not p.slot.binds_to:
                return Decline(
                    "blueprint", REASON_BAD_ROLE,
                    f"slot {p.slot.name} has no binds_to (only a "
                    f"{sorted(WINDOWED_SLOT_TYPES)} slot may omit it)",
                )
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
            # Well-formedness (Slice C): a PRESENT optional_pattern must be a
            # self-contained boolean SQL fragment carrying NO placeholder — the SAME
            # gate the corpus loader/runtime apply (corpus_loader validates whenever a
            # pattern is present, regardless of `required`, so a REQUIRED slot that
            # still carries a malformed pattern is caught here too rather than only at
            # load). Catch a malformed pattern as a fail-to-review decline rather than
            # let it pass extraction and only blow up at landing/runtime.
            if p.slot.optional_pattern:
                try:
                    validate_optional_pattern(p.slot.name, p.slot.optional_pattern)
                except TemplateBindError as exc:
                    return Decline(
                        "blueprint", REASON_BAD_ROLE,
                        f"optional slot {p.slot.name} has a malformed optional_pattern: {exc}",
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
        # Gate the composite DAG BEFORE building the payload: `_compose_nodes` coerces
        # (`int(rn["order"])`) on the assumption this passed. INSIDE the try as well:
        # the gate is the thing standing between malformed model output and the
        # dead-letter path, so its OWN bugs must degrade to a generic malformed
        # decline rather than become the escape it exists to close (it shipped once
        # with an unhashable-`node_kind` TypeError doing exactly that).
        compose_decline = _validate_compose_nodes(payload_raw.get("composes"))
        if compose_decline is not None:
            return compose_decline
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
