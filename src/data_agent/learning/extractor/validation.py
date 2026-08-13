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

**Two kinds of decline, and only one of them is re-askable.** A decline is CORRECTABLE
when the fix is a change of EXPRESSION — restating something the candidate already
decided in a form the pipeline can read — and terminal when it would be a change of
DECISION. `_correctable` is the single place the flag is set and states the rule in
full; `_malformed` and `_role_shape` are its two families. The rule is derived from
what the CHECK consults, not from the field's name: a check that reads only the
candidate is re-askable, a check that consults the catalog, the accepted SQL, the
session or this pipeline's capabilities is not.

Field-shape checks come from `shape.py`, whose readers write their own message. Nothing
in this module may hand an interpreter exception string to a `Decline`: that message is
fed back to the model on the corrective turn and recorded on the final decline.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.blueprint.template import TemplateBindError, validate_optional_pattern

from ..summary.models import SessionSummary
from ..summary.refs import sql_by_ref
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
from .shape import (
    ShapeError,
    as_array,
    as_flag,
    as_int,
    as_number,
    as_object,
    as_text,
    one_of,
    optional,
    require,
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

CANDIDATE_TYPES: tuple[str, ...] = (
    "blueprint",
    "global_knowledge",
    "user_knowledge",
    "schema_edit",
)

# Spelled out because a real model got exactly this wrong: `gpt-4.1` emitted the
# blueprint payload's fields at the TOP level with no `type` and no `payload` wrapper.
# "unknown candidate type" told it nothing about the envelope it had skipped.
_CANDIDATE_TYPE_REQUIREMENT = (
    'one of "blueprint", "global_knowledge", "user_knowledge" or "schema_edit". Each '
    "candidate is an ENVELOPE — {type, confidence, evidence, rationale, "
    "entity_self_check, payload} — and every type-specific field (intent, kind, "
    "parameterization, source_tool_call_refs, ...) goes INSIDE payload, never at the "
    "top level of the candidate"
)

_BLUEPRINT_PAYLOAD_REQUIREMENT = (
    'an object with "intent", "kind", "source_tool_call_refs", "accepted_signal" and '
    '"parameterization"'
)


def _correctable(candidate_type: str, reason: str, detail: str) -> Decline:
    """The ONE place `Decline.correctable` is set. Everything else builds a plain
    `Decline`, which is terminal.

    Correctable means the extractor will put *detail* in front of the model and let it
    re-emit (`extractor.py::_drive_turns`), so *detail* must be (a) actionable — it
    names the field and what the field must contain — and (b) ENTITY-FREE, since it
    becomes prompt text and is recorded on the final decline.

    WHERE THE LINE IS. A decline is correctable iff the fix is a change of EXPRESSION —
    restating something the candidate already decided in a form the pipeline can read —
    and never a change of DECISION. Operationally that is exactly two families, and both
    are properties of the CHECK, not of the field's name:

      (a) a JSON-shape requirement of a downstream read — `shape.py` and the `composes`
          gate. The candidate could not be turned into the typed model at all.
      (b) a REQUIRED-FIELD or CLOSED-ENUM obligation that follows mechanically from a
          role or type the candidate ITSELF declared — `_validate_roles`, via
          `_role_shape`.

    A check that consults anything OUTSIDE the candidate is never correctable: the
    catalog's rule ids (`missing_rule`), the accepted SQL's predicates
    (`totality_violation`), the session's acceptance (`no_acceptance`), a SQL parser
    (a malformed `optional_pattern`), this pipeline's own capabilities (`period_range`).
    Nor is a check that asks for content the candidate does not contain (`no_evidence`,
    the D31 primary guard — re-asking there is an invitation to invent a citation).
    Re-asking any of those is talking a model into a candidate it was right to decline,
    and in the `missing_rule` case it would destroy the §7 signal that a human needs to
    ADD a rule. `_unreadable` is excluded too — see its docstring.
    """
    return Decline(candidate_type, reason, detail, correctable=True)


def _malformed(candidate_type: str, detail: str) -> Decline:
    """Family (a) of `_correctable`: a READER failed, so the candidate could not be
    turned into the typed model. Every `malformed_candidate` in this module comes
    through here EXCEPT `_unreadable`."""
    return _correctable(candidate_type, REASON_MALFORMED, detail)


def _role_shape(detail: str) -> Decline:
    """Family (b) of `_correctable`: a required-field or closed-enum obligation of a
    role/type the candidate declared for itself. Still `role_inconsistent` — the reason
    code is what the consumer traces and it has not changed meaning — but re-askable,
    because the candidate has already made every decision the fix needs."""
    return _correctable("blueprint", REASON_BAD_ROLE, detail)


def _unreadable(candidate_type: str, where: str, exc: Exception) -> Decline:
    """The BELT: a shape that escaped every reader above and raised out of the build.

    NOT correctable, and that is the point of having it separate. It names no field,
    because if it could name one, a reader would have caught it — so feeding it to the
    model would be asking it to guess at random, and each guess costs a full extraction
    prompt. It is an EXTRACTOR bug report, not a candidate fix, and it says so.

    The exception TYPE is named (safe — it describes the interpreter's complaint) and
    its MESSAGE is not (it interpolates the offending value, which is model-authored
    from an entity-bearing session)."""
    return Decline(
        candidate_type,
        REASON_MALFORMED,
        f"the {where} could not be read, and no shape check named the field that "
        f"failed — this is an extractor gap ({type(exc).__name__}), not something the "
        "candidate can be corrected into",
    )


def _evidence(raw: dict[str, Any]) -> tuple[EvidenceRef, ...]:
    """The cited evidence, or `()` when the candidate cites none. Raises `ShapeError`
    when it cites some and NONE of them can be read.

    PARTIAL tolerance is deliberate and unchanged: an item this cannot read is skipped,
    because evidence is a `>= 1` gate and one malformed quote among three should not
    cost the candidate. What changed is the ALL-BAD case. It used to fall through to
    `no_evidence` — "the candidate cites nothing", a judgement about CONTENT — when the
    truth was that it cited three things in a shape this could not read. That
    misdiagnosis is the expensive kind: a substantive decline is never re-asked, so a
    purely mechanical mistake became terminal. The last reader error is re-raised
    instead, which routes it to the correctable side."""
    items = raw.get("evidence")
    if items is None:
        return ()
    items = as_array(
        items,
        at="candidate.evidence",
        requirement=(
            'an array of {"turn_ref": <turn index>, "tool_call_ref": <tool call id>, '
            '"quote": <text from the session>} objects'
        ),
    )
    refs: list[EvidenceRef] = []
    last_error: ShapeError | None = None
    for idx, item in enumerate(items):
        at = f"candidate.evidence[{idx}]"
        try:
            obj = as_object(
                item,
                at=at,
                requirement='an object with "turn_ref", "tool_call_ref" and "quote"',
            )
            refs.append(
                EvidenceRef(
                    turn_ref=require(
                        obj,
                        "turn_ref",
                        as_int,
                        at=at,
                        requirement="the integer turn_index the quote came from",
                    ),
                    tool_call_ref=require(
                        obj,
                        "tool_call_ref",
                        as_text,
                        at=at,
                        requirement="the tool_call_ref of the session tool call it came from",
                    ),
                    quote=require(
                        obj,
                        "quote",
                        as_text,
                        at=at,
                        requirement="the quoted text, copied from the session",
                    ),
                )
            )
        except ShapeError as exc:
            last_error = exc
    if not refs and last_error is not None:
        raise last_error
    return tuple(refs)


def _header(
    raw: dict[str, Any], candidate_type: str, evidence: tuple[EvidenceRef, ...]
) -> CandidateHeader:
    at = "candidate"
    esc_at = f"{at}.entity_self_check"
    esc = optional(
        raw,
        "entity_self_check",
        as_object,
        at=at,
        requirement=(
            'an object with a boolean "contains_entities" and a "found" array of the '
            "literal values you found"
        ),
        default={},
    )
    found = optional(
        esc,
        "found",
        as_array,
        at=esc_at,
        requirement="an array of the literal values found in this candidate (strings)",
        default=[],
    )
    depends_on = optional(
        raw,
        "depends_on",
        as_array,
        at=at,
        requirement="an array of sibling candidate ids this one is blocked on (strings)",
        default=[],
    )
    return CandidateHeader(
        # Already read through `one_of` by `to_candidate` — passed in rather than
        # re-read so there is exactly one place the candidate type is validated.
        type=candidate_type,  # type: ignore[arg-type]
        confidence=optional(
            raw,
            "confidence",
            as_number,
            at=at,
            requirement="a number between 0.0 and 1.0",
            default=0.0,
        ),
        evidence=evidence,
        # `rationale` and `proposed_action` get no reader: `str()` is total, and both
        # are advisory prose whose worst case is a useless-but-harmless string. A
        # reader here would decline candidates over a field nothing gates on.
        rationale=str(raw.get("rationale", "")),
        proposed_action=str(raw.get("proposed_action", "new")),
        entity_self_check=EntitySelfCheck(
            contains_entities=optional(
                esc,
                "contains_entities",
                as_flag,
                at=esc_at,
                requirement="true or false",
                default=False,
            ),
            found=tuple(
                as_text(
                    f,
                    at=f"{esc_at}.found[{i}]",
                    requirement="a literal value found in this candidate",
                )
                for i, f in enumerate(found)
            ),
        ),
        depends_on=tuple(
            as_text(d, at=f"{at}.depends_on[{i}]", requirement="a sibling candidate id")
            for i, d in enumerate(depends_on)
        ),
    )


def _slot_plan(raw: Any, *, at: str) -> SlotPlan:
    slot = as_object(
        raw,
        at=at,
        requirement='an object with "name", "type", "binds_to" and "required"',
    )
    return SlotPlan(
        name=require(
            slot, "name", as_text, at=at, requirement="a short identifier for the slot"
        ),
        # Presence only — `_validate_roles` owns the enum, because it is the one place
        # that also knows the type-dependent `binds_to` rule, and a decline naming the
        # wrong blocker is worse than a late one.
        type=require(
            slot,
            "type",
            as_text,
            at=at,
            requirement=f"one of {', '.join(sorted(SLOT_TYPES - UNSUPPORTED_SLOT_TYPES))}",
        ),
        # ABSENT and explicit-null both mean "this slot declares no column domain",
        # which is legal for exactly the two `WINDOWED_SLOT_TYPES` and illegal for
        # everything else — `_validate_roles` decides which. A model emitting
        # `binds_to: 123` is still coerced to "123" and keeps its existing route (a
        # bogus bind that fails S4's `binds_to ⊆ uses` check and reaches a HUMAN via
        # fail-to-review) rather than being newly hard-rejected here; a CONTAINER is
        # rejected, because `str(["a"])` lands a Python repr in the corpus.
        binds_to=optional(
            slot,
            "binds_to",
            as_text,
            at=at,
            requirement=(
                "the FULLY-QUALIFIED 'database.table.column' this slot binds to, or "
                "null for a relative_window slot"
            ),
            default=None,
        ),
        # A real model sometimes omits `required`. Default to True: a predicate that
        # appeared in the ACCEPTED SQL is required unless the model explicitly marks
        # it optional — the safe side of the no-drop (D97) invariant (an optional slot
        # still needs an optional_pattern, enforced in `_validate_roles`).
        required=optional(
            slot, "required", as_flag, at=at, requirement="true or false", default=True
        ),
        optional_pattern=optional(
            slot,
            "optional_pattern",
            as_text,
            at=at,
            requirement=(
                "a self-contained boolean SQL fragment that renders when the slot is "
                "ABSENT (usually 'TRUE'), carrying no {placeholder}, or null"
            ),
            default=None,
        ),
        enum_values=_enum_values(slot, at=at),
    )


def _enum_values(slot: dict[str, Any], *, at: str) -> tuple[str, ...] | None:
    """An `enum` slot's closed set, or `None` when it declares none.

    An EMPTY array collapses to `None` (unchanged): both say "no closed set", and
    `_validate_roles` declines either one for an `enum` slot. The `as_array` read is
    what stops `enum_values: "NA,EU"` — a string is iterable, so the old code turned it
    into seven single-character values with no error anywhere."""
    values = optional(
        slot,
        "enum_values",
        as_array,
        at=at,
        requirement="an array of the closed set of allowed values, or null",
        default=None,
    )
    if not values:
        return None
    return tuple(
        as_text(v, at=f"{at}.enum_values[{i}]", requirement="one allowed value")
        for i, v in enumerate(values)
    )


def _param_plans(raw_params: list[Any], *, at: str) -> list[ParamPlan]:
    plans: list[ParamPlan] = []
    for idx, rp in enumerate(raw_params):
        entry_at = f"{at}[{idx}]"
        entry = as_object(
            rp,
            at=entry_at,
            requirement=(
                'an object with "locator", "role" ("slot"|"rule"|"inline") and the '
                "field that role requires"
            ),
        )
        loc_at = f"{entry_at}.locator"
        loc = require(
            entry,
            "locator",
            as_object,
            at=entry_at,
            requirement=(
                'an object with "table" (the \'database.table\'), "column" (the BARE '
                'column) and "value" (the literal as it appeared)'
            ),
        )
        plans.append(
            ParamPlan(
                locator=Locator(
                    table=require(
                        loc,
                        "table",
                        as_text,
                        at=loc_at,
                        requirement="the 'database.table' the column belongs to",
                    ),
                    column=require(
                        loc,
                        "column",
                        as_text,
                        at=loc_at,
                        requirement="the BARE column name, with no table prefix",
                    ),
                    value=require(
                        loc,
                        "value",
                        as_text,
                        at=loc_at,
                        requirement="the literal exactly as it appeared in the SQL",
                    ),
                ),
                # Presence and stringness only. An unknown role string still declines
                # `role_inconsistent` in `_validate_roles`, which is where the roles and
                # their obligations are described together.
                role=require(
                    entry,
                    "role",
                    as_text,
                    at=entry_at,
                    requirement='exactly one of "slot", "rule" or "inline"',
                ),  # type: ignore[arg-type]
                slot=(
                    _slot_plan(entry["slot"], at=f"{entry_at}.slot")
                    if entry.get("slot")
                    else None
                ),
                rule_id=optional(
                    entry,
                    "rule_id",
                    as_text,
                    at=entry_at,
                    requirement="the id of an EXISTING catalog rule",
                    default=None,
                ),
                why=optional(
                    entry,
                    "why",
                    as_text,
                    at=entry_at,
                    requirement="a sentence saying why this predicate is metric-defining",
                    default=None,
                ),
            )
        )
    return plans


def _result_signature(raw: Any) -> ResultSignature | None:
    """The GROUP BY output contract, or `None` when the candidate declares none.

    `grain` is where a live `gpt-5.5` extraction died: it emitted a prose SENTENCE
    ("one row per organizational unit above the ...") where the schema wants an object,
    and `grain.get("columns")` raised `AttributeError` out of the whole payload build.
    The requirement below is written to be the answer to that mistake."""
    if not raw:
        return None
    at = "candidate.payload.result_signature"
    sig = as_object(
        raw,
        at=at,
        requirement=(
            'an object with "shape" (an array of {column, type}), "grain" (an object) '
            'and "invariants" (an array of strings), or null when the SQL has no GROUP BY'
        ),
    )
    grain_at = f"{at}.grain"
    grain = optional(
        sig,
        "grain",
        as_object,
        at=at,
        requirement=(
            'an OBJECT with a "columns" array naming the GROUP BY columns and a boolean '
            '"verifiable" — a prose description of the grain is not a grain'
        ),
        default={},
    )
    columns = optional(
        grain,
        "columns",
        as_array,
        at=grain_at,
        requirement="an array of the grouped column names (strings)",
        default=[],
    )
    shape_items = optional(
        sig,
        "shape",
        as_array,
        at=at,
        requirement='an array of {"column": <name>, "type": <type>} objects',
        default=[],
    )
    invariants = optional(
        sig,
        "invariants",
        as_array,
        at=at,
        requirement="an array of invariant statements (strings)",
        default=[],
    )
    shape: list[ColumnShape] = []
    for idx, item in enumerate(shape_items):
        item_at = f"{at}.shape[{idx}]"
        column_shape = as_object(
            item, at=item_at, requirement='an object with "column" and "type"'
        )
        shape.append(
            ColumnShape(
                column=require(
                    column_shape,
                    "column",
                    as_text,
                    at=item_at,
                    requirement="the output column name",
                ),
                type=require(
                    column_shape,
                    "type",
                    as_text,
                    at=item_at,
                    requirement="the output column's type",
                ),
            )
        )
    return ResultSignature(
        shape=tuple(shape),
        grain=ResultGrainPlan(
            columns=tuple(
                as_text(
                    c,
                    at=f"{grain_at}.columns[{i}]",
                    requirement="one grouped column name",
                )
                for i, c in enumerate(columns)
            ),
            verifiable=optional(
                grain, "verifiable", as_flag, at=grain_at, requirement="true or false", default=True
            ),
        ),
        invariants=tuple(
            as_text(inv, at=f"{at}.invariants[{i}]", requirement="one invariant statement")
            for i, inv in enumerate(invariants)
        ),
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
        return _malformed(
            "blueprint",
            "candidate.payload.composes is not a list — it must be an ARRAY of node "
            f"objects, one per step of the DAG (got {type(raw_nodes).__name__})",
        )
    for idx, rn in enumerate(raw_nodes):
        where = f"candidate.payload.composes[{idx}]"
        if not isinstance(rn, dict):
            return _malformed(
                "blueprint",
                f"{where} is not an object — each node is an object with an integer "
                "'order' and optional 'feeds_from'/'consumes'/'output' "
                f"(got {type(rn).__name__})",
            )
        if "order" not in rn:
            return _malformed(
                "blueprint",
                f"{where} has no 'order'; every node declares its integer position in "
                "the DAG",
            )
        order = _node_index(rn["order"])
        if order is None:
            # The TYPE, not the value. Every message this gate produces is fed back to
            # the model on the corrective turn and recorded on the decline, so it
            # obeys the same entity-free rule as `shape.py`: `order`, `node_kind` and a
            # `feeds_from` entry are all model-authored from an entity-bearing session.
            return _malformed(
                "blueprint",
                f"{where} 'order' is not an integer (got {type(rn['order']).__name__})",
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
            return _malformed(
                "blueprint",
                f"{where} has an unknown node_kind (got {type(node_kind).__name__}; "
                f"it must be exactly one of {sorted(NODE_KINDS)})",
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
            return _malformed(
                "blueprint",
                f"{where} 'feeds_from' is not a list (got {type(feeds).__name__})",
            )
        for src_idx, src in enumerate(feeds):
            if _node_index(src) is None:
                return _malformed(
                    "blueprint",
                    f"{where} 'feeds_from' entry {src_idx} is not an integer "
                    f"(got {type(src).__name__}); each entry is the `order` of an "
                    "upstream node",
                )
        for field_name in ("consumes", "output"):
            value = rn.get(field_name)
            if value is None:
                value = {}
            if not isinstance(value, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in value.items()
            ):
                return _malformed(
                    "blueprint",
                    f"{where} {field_name!r} must be an object of string→string "
                    f"(got {type(value).__name__})",
                )
        # `requires_approval` has no downstream gate before `Node.parse` at LANDING,
        # where a non-object raises past the promotion path — reject it here instead.
        requires_approval = rn.get("requires_approval")
        if requires_approval is not None and not isinstance(requires_approval, dict):
            return _malformed(
                "blueprint", f"{where} 'requires_approval' is not an object"
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
    """Each `ParamPlan` against the obligations of the role it declared.

    TWO KINDS OF DECLINE COME OUT OF HERE, and `_role_shape` vs `Decline(...)` is which.
    A required-field or closed-enum obligation that follows MECHANICALLY from a role or
    type the candidate itself chose (`role: rule` with no `rule_id`, `role: inline` with
    no `why`, a slot type outside the enum, a windowed slot that declared a `binds_to`)
    is a change of EXPRESSION: the candidate already decided the thing, it just did not
    say it in the field that carries it. Those are correctable.

    Everything else here consults something OUTSIDE the candidate — the catalog's rule
    ids, this pipeline's generalization capability, a SQL parser — and a corrective turn
    on one of those is asking the model to make a different DECISION. Those are not.
    See `_malformed` for the same rule stated for the shape family.

    The live case that forced the distinction: `gpt-5.5` classified a status filter as
    inline and wrote its justification under `reason` instead of `why`. It had done the
    analysis; it had used the wrong key. "inline role without 'why'" was a terminal
    `role_inconsistent`, which is a judgement about content — the wrong diagnosis for a
    key name."""
    for index, p in enumerate(params):
        at = f"candidate.payload.parameterization[{index}]"
        if p.role == "slot":
            if p.slot is None or p.slot.type not in SLOT_TYPES:
                return _role_shape(
                    f"{at}.slot.type is missing or not a known slot type; it must be "
                    f"exactly one of {sorted(SLOT_TYPES - UNSUPPORTED_SLOT_TYPES)}"
                )
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
            #
            # NOT correctable, and it is the sharpest case for the line. The decline
            # text below reads like an instruction ("express it as two slots"), so it
            # is tempting — but acting on it re-models a DATE RANGE, and getting the
            # start/end assignment wrong silently inverts a filter, which is the D56
            # wrong-answer class this withdrawal exists to avoid. A capability limit of
            # this pipeline is also not a mistake the model made.
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
                    return _role_shape(
                        f"{at}.slot: a {p.slot.type} slot must not declare binds_to "
                        "(a windowed-period slot consumes no column domain; emit null)",
                    )
            elif not p.slot.binds_to:
                return _role_shape(
                    f"{at}.slot has no binds_to; it must be the FULLY-QUALIFIED "
                    "'database.table.column' (only a "
                    f"{sorted(WINDOWED_SLOT_TYPES)} slot may omit it)",
                )
            # An `enum` slot MUST carry non-empty enum_values — the runtime
            # `SlotSpec.parse` rejects an enum slot without them (un-landable). Catch
            # it here as a traceable decline rather than a crash at landing. A
            # free-text filter value should be typed `entity`/`string`, not `enum`.
            if p.slot.type == "enum" and not p.slot.enum_values:
                return _role_shape(
                    f"{at}.slot is type 'enum' but has no enum_values; give the closed "
                    "set, or use type 'entity' for a free-text value",
                )
            # No-drop (D97): an optional slot MUST carry an optional_pattern, else
            # an absent bind silently drops the predicate.
            if not p.slot.required and not p.slot.optional_pattern:
                return _role_shape(
                    f"{at}.slot is optional (required=false) but has no "
                    "optional_pattern, so an absent bind would silently drop the "
                    "predicate; give the fragment that renders when it is absent "
                    "(usually 'TRUE'), or mark the slot required",
                )
            # Well-formedness (Slice C): a PRESENT optional_pattern must be a
            # self-contained boolean SQL fragment carrying NO placeholder — the SAME
            # gate the corpus loader/runtime apply (corpus_loader validates whenever a
            # pattern is present, regardless of `required`, so a REQUIRED slot that
            # still carries a malformed pattern is caught here too rather than only at
            # load). Catch a malformed pattern as a fail-to-review decline rather than
            # let it pass extraction and only blow up at landing/runtime.
            #
            # NOT correctable, for two independent reasons. The fix is a SQL fragment,
            # and the extractor's contract is plan-not-SQL (D35) — a corrective
            # round-trip here is asking a model to keep guessing at SQL. And the
            # message carries `TemplateBindError`, which quotes the offending fragment:
            # entity-bearing text that must not become prompt text or sit on a decline
            # that is fed back. It stays a fail-to-review for a human.
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
                return _role_shape(
                    f"{at} declares role 'rule' but has no rule_id; give the id of an "
                    "EXISTING catalog rule, or reclassify the predicate as slot/inline"
                )
            if p.rule_id not in known_rules:
                # §7 missing-rule: a rule-shaped predicate with no catalog rule →
                # fail-to-review (Slice-6 will pair a schema_edit(add_rule)).
                #
                # NOT correctable, and deliberately so even though it is a one-field
                # fix. The id is checked against the CATALOG, so the only honest
                # answers are "a different existing rule" or "none exists" — and the
                # second one is the valuable outcome this decline exists to produce.
                # Re-asking converts a request for a human to add a rule into pressure
                # on the model to name any id that passes.
                return Decline("blueprint", REASON_MISSING_RULE, f"unknown rule {p.rule_id!r}")
        elif p.role == "inline":
            if not p.why:
                return _role_shape(
                    f"{at} declares role 'inline' but has no 'why'; the field carrying "
                    "the reason the predicate is metric-defining is named `why` (not "
                    "`reason`, not `note`)"
                )
        else:
            return _role_shape(
                f"{at}.role is not a known role; it must be exactly one of "
                '"slot", "rule" or "inline"'
            )
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
    # Gather the accepted SQL of the cited source refs — `summary/refs.py`, which
    # resolves an `answerWithTable` ref to the SQL that call DESIGNATED as well as a
    # runQuery ref to the SQL it ran. Without it a candidate citing the answer's ref
    # (often the session's only ref: a designated query need never have been
    # dispatched) declines as if the session carried no SQL at all.
    #
    # EVERY SQL a ref stands for is checked, not just one. A multi-table answer puts
    # several queries behind one ref, and a predicate in the second one that no
    # parameterization entry covers is a filter this blueprint would silently drop —
    # exactly what this gate exists to catch.
    resolved = sql_by_ref(summary)
    plan_locators = [p.locator for p in payload.parameterization]
    saw_any_sql = False
    for ref in payload.source_tool_call_refs:
        for sql in resolved.get(ref, ()):
            saw_any_sql = True
            predicates = literal_predicates(sql)
            if predicates is None:
                return Decline(
                    "blueprint", REASON_UNREWRITABLE, f"un-parseable SQL at {ref}"
                )
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
    at = "candidate.payload"
    return BlueprintPayload(
        intent=require(
            raw,
            "intent",
            as_text,
            at=at,
            requirement=(
                "an ENTITY-FREE natural-language sentence describing what the query "
                "does (no literal values)"
            ),
        ),
        kind=require(
            raw, "kind", as_text, at=at, requirement='either "single" or "composite"'
        ),
        resolves=_resolves(raw.get("resolves")),
        source_tool_call_refs=tuple(
            as_text(
                r,
                at=f"{at}.source_tool_call_refs[{i}]",
                requirement="a tool_call_ref from the session",
            )
            for i, r in enumerate(
                optional(
                    raw,
                    "source_tool_call_refs",
                    as_array,
                    at=at,
                    requirement=(
                        "an array of the session tool_call_refs that produced the "
                        "accepted answer"
                    ),
                    default=[],
                )
            )
        ),
        accepted_signal=require(
            raw,
            "accepted_signal",
            as_text,
            at=at,
            requirement='one of "no_correction", "thumbs_up" or "explicit_confirm"',
        ),
        parameterization=tuple(
            _param_plans(
                optional(
                    raw,
                    "parameterization",
                    as_array,
                    at=at,
                    requirement=(
                        "an ARRAY (not an object) with exactly one entry per literal "
                        "predicate of the accepted SQL, each an object with locator, "
                        "role and the field that role requires"
                    ),
                    default=[],
                ),
                at=f"{at}.parameterization",
            )
        ),
        composes=_compose_nodes(raw.get("composes") or []),
        result_signature=_result_signature(raw.get("result_signature")),
        notes=str(raw.get("notes", "")),
    )


def to_candidate(
    raw: Any, summary: SessionSummary, *, known_rules: frozenset[str]
) -> ExtractedCandidate | Decline:
    """Validate one raw candidate → `ExtractedCandidate` or `Decline`.

    NEVER raises. An uncaught exception here leaves the consumer's queue message
    un-acked → reclaim → dead-letter, losing the whole session AND the traceable
    reason; every escape route is closed by a reader (`shape.py`), the compose gate, or
    the `_unreadable` belt."""
    try:
        envelope = as_object(
            raw,
            at="candidate",
            requirement="an object (one candidate envelope)",
        )
        ctype = require(
            envelope,
            "type",
            one_of(CANDIDATE_TYPES),
            at="candidate",
            requirement=_CANDIDATE_TYPE_REQUIREMENT,
        )
    except ShapeError as exc:
        # `"unknown"`, not the value that arrived: `Decline.type` is a label a human
        # and a metric group by, and a model that flattened the envelope can put
        # arbitrary session-derived text in `type`.
        return _malformed("unknown", str(exc))

    try:
        evidence = _evidence(envelope)
    except ShapeError as exc:
        return _malformed(ctype, str(exc))
    if not evidence:
        # D31 primary guard — no evidence ⇒ rejected before the audit snapshot.
        # Reached only when the candidate cited NOTHING: an unreadable citation raises
        # above rather than being miscounted as an absent one (see `_evidence`).
        return Decline(ctype, REASON_NO_EVIDENCE, "candidate cites no evidence")

    try:
        header = _header(envelope, ctype, evidence)
    except ShapeError as exc:
        return _malformed(ctype, str(exc))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return _unreadable(ctype, "candidate header", exc)

    if ctype != "blueprint":
        # §3.3: other targets are emitted with their Locked payload as a dict in
        # S3 (depth is on blueprint). Evidence-mandatory already enforced above.
        try:
            payload = require(
                envelope,
                "payload",
                as_object,
                at="candidate",
                requirement=f"an object carrying the {ctype} payload",
            )
        except ShapeError as exc:
            return _malformed(ctype, str(exc))
        return ExtractedCandidate(header=header, payload=dict(payload))

    # --- blueprint depth path ---
    # A real model can emit a field with the wrong JSON type (e.g. a list where a
    # dict is expected). ANY shape confusion here MUST become a traceable Decline —
    # never an uncaught exception that escapes to the consumer (which would skip the
    # job and route it to dead-letter instead of recording a malformed_candidate).
    try:
        payload_raw = as_object(
            envelope.get("payload") if envelope.get("payload") is not None else {},
            at="candidate.payload",
            requirement=_BLUEPRINT_PAYLOAD_REQUIREMENT,
        )
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
    except ShapeError as exc:
        return _malformed(ctype, str(exc))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return _unreadable(ctype, "blueprint payload", exc)

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
