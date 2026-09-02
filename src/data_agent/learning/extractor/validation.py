"""Deterministic post-emit validation of a raw candidate (D31/D34/D97).

One raw structured-output candidate dict → an `ExtractedCandidate` or a `Decline`. The S3
safety teeth: EVIDENCE MANDATORY (D31, `len(evidence) >= 1`); LIFT-NOT-GENERATE (D34, a
blueprint needs `accepted_signal` AND a session that actually carried acceptance);
TOTALITY (D97, exactly one `ParamPlan` per literal predicate of the accepted SQL — a
missing one is a silently dropped filter); ROLE CONSISTENCY (D97). A decline is CORRECTABLE
iff the fix is a change of EXPRESSION, never of DECISION; `_correctable` is the single
place that flag is set. Nothing here may hand an interpreter exception string to a
`Decline` — that message is fed back to the model and recorded on the final decline.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, get_args

import sqlglot
import sqlglot.expressions as exp

from data_agent.runtime.blueprint.template import TemplateBindError, validate_optional_pattern

from ..summary.models import AcceptedSignal, SessionSummary
from ..summary.refs import sql_by_ref
from .grounding import CatalogRule, RuleIndex
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
from .rule_match import (
    nearest_known_rule,
    rule_contradicts_predicate,
    rules_for_predicate,
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
from .sql_predicates import LiteralPredicate, literal_predicates

# How many uncovered predicates one totality correction names. A model that skipped
# more than this has a systemic problem the next round will re-report; a message that
# listed forty would stop being a checklist.
_MAX_LISTED_PREDICATES = 6

# Reason codes (traced by the consumer). `fail_to_review` reasons are the D52/D97
# human-review valve; the rest are hard rejects.
REASON_NO_EVIDENCE = "no_evidence"
REASON_NO_ACCEPTANCE = "no_acceptance"
REASON_TOTALITY = "totality_violation"
REASON_UNREWRITABLE = "unrewritable_sql"
REASON_BAD_ROLE = "role_inconsistent"
REASON_MISSING_RULE = "missing_rule"
# The HINTED variant of `missing_rule`: the cited id does not exist AND the catalog
# names exactly one rule it can only have meant, so the decline is correctable and the
# re-ask carries that id (`_rule_hint`).
#
# A SEPARATE code rather than a flag on `missing_rule`, and the reason is what the codes
# are FOR. `missing_rule` is the §7 signal that a human must ADD a rule to the catalog;
# a deployment watching that count needs it to mean exactly that. Folding in the cases
# where the rule already exists under another name would inflate the one number the
# pairing work is prioritized from, and no amount of detail text on the decline recovers
# a count. The two codes also answer different questions when they are the FINAL outcome:
# `missing_rule` says nobody was asked, `missing_rule_hinted` says the model was handed
# the id and did not take it — which points at the prompt, not at the catalog.
REASON_MISSING_RULE_HINTED = "missing_rule_hinted"
# A rule-role entry citing a rule that EXISTS and that the catalog proves declares a
# different filter from the predicate the entry covers (`_rule_mismatch`).
#
# Its own code, by the same test the hinted variant was given: does an existing count
# change meaning? `role_inconsistent` means "the entry does not satisfy the obligations
# of the role it declared" — a shape-ish family, every member of which is answerable
# from the candidate alone. This one is a DISAGREEMENT between the catalog and the
# accepted SQL about what a filter means, it is the only check here that can catch a
# mis-cited rule before it lands, and its rate is the number an operator wants when
# asking whether hinting is steering models wrong. Folded into `role_inconsistent` it
# would be unfindable.
REASON_RULE_MISMATCH = "rule_predicate_mismatch"
REASON_MALFORMED = "malformed_candidate"

# The D34 acceptance domain (`thumbs_up` is declared but never emitted by S2 —
# it may still legitimately appear on a candidate the extractor forwards).
#
# DERIVED from the `AcceptedSignal` Literal that defines it, not re-typed: this was the
# fourth copy of the same three strings, and a hand-written mirror of a closed set is a
# gate that silently stops matching the moment the set gains a member.
_ACCEPTED_SIGNAL_DOMAIN = frozenset(get_args(AcceptedSignal))

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
    """The ONE place `Decline.correctable` is set; everything else builds a terminal `Decline`.

    Correctable means the extractor puts *detail* in front of the model and lets it re-emit, so
    *detail* must be actionable AND carry NOTHING THE MODEL DOES NOT ALREADY HOLD — it becomes
    prompt text and is recorded on the final decline. The line: correctable iff the fix is a
    change of EXPRESSION and never of DECISION, judged by what the CHECK consults rather than
    by the field's name. Exactly three families — (a) a JSON-shape requirement of a downstream
    read (`shape.py`, the `composes` gate); (b) a required-field or closed-enum obligation that
    follows mechanically from a role or type the candidate ITSELF declared (`_role_shape`);
    (c) a check that consults something outside the candidate AND can NAME the unique fix
    deterministically (`_rule_hint`, `_predicate_hint`), whose re-ask CARRIES that fix.
    Everything else is terminal — the session's acceptance, a SQL parser, this pipeline's own
    capabilities, and `no_evidence` (re-asking there invites an invented citation). An
    open-ended re-ask on the catalog or the accepted SQL remains forbidden.
    """
    return Decline(candidate_type, reason, detail, correctable=True)


def _malformed(candidate_type: str, detail: str) -> Decline:
    """Family (a) of `_correctable`: a READER failed, so the candidate could not be typed.

    Every `malformed_candidate` in this module comes through here EXCEPT `_unreadable`.
    """
    return _correctable(candidate_type, REASON_MALFORMED, detail)


def _role_shape(detail: str) -> Decline:
    """Family (b) of `_correctable`: an obligation of a role/type the candidate declared itself.

    Still `role_inconsistent` — the reason code the consumer traces has not changed meaning —
    but re-askable, because the candidate has already made every decision the fix needs.
    """
    return _correctable("blueprint", REASON_BAD_ROLE, detail)


def _rule_hint(at: str, cited: str, hint: str) -> Decline:
    """Family (c) of `_correctable`: an unknown `rule_id` the catalog can uniquely name.

    The message NAMES the fix. It never says "cite an existing rule" — the model has no list of
    them and inviting a guess is the coercion terminal `missing_rule` exists to prevent — and it
    never asserts the hint IS the plan's rule. *cited* is model-authored and goes through
    `_quoted`: being model-authored is not a safety property, and a newline in it could forge a
    hint line. *hint* is a catalog id this module read out of the deployment's own YAML.
    """
    return _correctable(
        "blueprint",
        REASON_MISSING_RULE_HINTED,
        f"{at}.rule_id names {_quoted(cited)}, which the catalog does not declare — the "
        f"catalog names this concept {hint!r}; if your plan implements that rule, cite it "
        "by its catalog id",
    )


def _rule_mismatch(at: str, wrong: CatalogRule, pred: LiteralPredicate) -> Decline:
    """Family (c) of `_correctable`: the entry cites a REAL rule declaring a different filter.

    The check holds both halves of the disagreement and prints them, so the model is not asked
    to search for anything — only which of two statements it made it meant. The rule's declared
    predicate is CATALOG text and needs no sanitizing; the session's goes through
    `_render_predicate`.
    """
    return _correctable(
        "blueprint",
        REASON_RULE_MISMATCH,
        f"{at} cites rule {wrong.id!r}, which the catalog declares on {wrong.table} as "
        f"`{wrong.predicate}` — but the predicate this entry covers is "
        f"{_render_predicate(pred)}. Those are different filters, and the rule is what "
        "future runs will execute. Cite the rule that declares THIS predicate, or "
        "classify the predicate as slot/inline instead.",
    )


def _predicate_hint(uncovered: list[LiteralPredicate], rule_index: RuleIndex | None) -> Decline:
    """Family (c) of `_correctable`, predicate side: each uncovered predicate, named.

    Carries any catalog rule that IS that filter plus the three legal ways to cover it. KEEPS
    its `totality_violation` reason code, unlike the rule-id hint next door: the code still
    means "a predicate of the accepted SQL has no entry", so no existing count changes meaning
    (`missing_rule` would have — hence `missing_rule_hinted`). Budget-exhausted is TERMINAL;
    the hint remains useful in the inbox even when it cannot be re-asked. Corrective turns
    consume the SQL-family budget independently of shape and semantic repairs, until the
    overall correction ceiling is reached.
    """
    listed = uncovered[:_MAX_LISTED_PREDICATES]
    unlisted = len(uncovered) - len(listed)
    # NEWLINE-separated, and the FIRST line is entity-free on purpose. A checklist of
    # predicates wrapped into one paragraph is materially harder to act on, and two
    # readers depend on the split: `correction.py` indents the continuation lines under
    # the candidate they belong to, and `extractor.py::_finish` logs the first line
    # ALONE, which keeps a SQL literal out of the operational log while still saying
    # what happened. (`consumer.py::_decline_details` flattens whitespace for the span,
    # so the attribute is unaffected either way.)
    lines = [
        "candidate.payload.parameterization has no entry for "
        f"{len(uncovered)} literal predicate(s) of the accepted SQL, so this blueprint "
        "would silently drop them (D97: exactly one entry per literal predicate; there "
        "is NO drop role):",
        *(_uncovered_line(pred, rule_index) for pred in listed),
    ]
    if unlisted:
        lines.append(
            f"  - (and {unlisted} more, not listed here; fix these first and the rest "
            "will be named if any remain)"
        )
    lines.append(
        "Add exactly ONE parameterization entry per predicate listed, choosing the role "
        "that says what the predicate IS: role 'rule' with rule_id set to a catalog id "
        "named above; or role 'slot' with name/type/binds_to (an optional slot also "
        "needs an optional_pattern); or role 'inline' with a 'why' saying why the "
        "predicate is metric-defining. Change no predicate and no entry you already "
        "wrote."
    )
    return _correctable("blueprint", REASON_TOTALITY, "\n".join(lines))


def _uncovered_line(pred: LiteralPredicate, rule_index: RuleIndex | None) -> str:
    """One predicate's line: what it is, and what the catalog calls it (if anything).

    A predicate NO rule matches is reported plainly, with no rule attached — that is the §7
    signal in its predicate-level form, and the model still has two legal ways to cover it.
    """
    matches = rules_for_predicate(pred, rule_index)
    if not matches:
        return f"  - {_render_predicate(pred)} — no catalog rule declares this predicate"
    # The rule id and its table are CATALOG-authored (a human wrote them into a YAML the
    # deployment ships), so they are not sanitized — only `match.value`, which is the
    # literal this run lifted out of the analyst's SQL, is.
    named = ", ".join(
        f"{_quoted(match.value)} as rule {match.rule.id!r} on {match.rule.table}"
        for match in matches
    )
    return f"  - {_render_predicate(pred)} — the catalog declares {named}"


# --- sanitizing the one class of untrusted text these messages carry ----------------

# Everything a terminal can act on plus everything that ends a line: C0, DEL and the C1
# block. Collapsed to a space rather than dropped, so removing them cannot silently
# join two tokens into a third.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]+")

# Per rendered literal. Long enough for any real filter value (a department, a status
# code, a date), short enough that a pathological one cannot dominate a message that is
# both prompt text and a log line. Applied PER `IN` MEMBER, so a 20-member list is
# bounded by the message's own predicate cap rather than by one product.
_MAX_LITERAL_CHARS = 120
_TRUNCATION_MARKER = "...[truncated]"


def _flattened(text: str, *, limit: int = _MAX_LITERAL_CHARS) -> str:
    """Untrusted text, reduced to one bounded line. Never quoted — see `_quoted`."""
    flat = _CONTROL_CHARS.sub(" ", text)
    if len(flat) > limit:
        return flat[:limit] + _TRUNCATION_MARKER
    return flat


def _quoted(value: str) -> str:
    """ONE session-derived literal, rendered as a single visibly-delimited token.

    Every string here reaches a model as prompt text on a corrective turn, formatted as a
    checklist of lines the CHECKER authored — and the values are literals out of the analyst's
    accepted SQL, i.e. text an end user can choose. A literal containing a line break plus a
    forged checklist entry used to be interpolated verbatim, reading exactly like a line this
    module produced. Three properties close it: NO LINE BREAKS (control characters, including
    the C1 block, become spaces), BOUNDED (120 chars with a visible marker), and ONE VISIBLE
    TOKEN (the delimiters are the checker's, and an embedded quote is doubled). This is NOT an
    escaping scheme — nothing downstream parses these messages; the STRUCTURAL defence is
    `rule_match.rule_contradicts_predicate`, which re-checks what the model finally cited.
    """
    return "'" + _flattened(value).replace("'", "''") + "'"


def _render_predicate(pred: LiteralPredicate) -> str:
    """The predicate as it reads in the SQL, from the enumerator's decomposition.

    Rendered rather than quoted from the source because the enumerator is what the totality
    check actually compared — a message showing the original SQL text could describe a different
    predicate from the one that declined. EVERY session-derived part is sanitized: the literal
    through `_quoted`, the column and table identifiers through `_flattened` (a quoted
    identifier is as attacker-shaped as a literal). The operator is from a closed set.
    """
    where = f" [on {_flattened(pred.table, limit=64)}]" if pred.table else ""
    column = _flattened(pred.column, limit=64)
    if pred.operator == "IN":
        members = ", ".join(_quoted(value) for value in pred.value.split(","))
        return f"{column} IN ({members}){where}"
    if pred.operator == "BETWEEN":
        low, _, high = pred.value.partition(",")
        return f"{column} BETWEEN {_quoted(low)} AND {_quoted(high)}{where}"
    return f"{column} {pred.operator} {_quoted(pred.value)}{where}"


def _unreadable(candidate_type: str, where: str, exc: Exception) -> Decline:
    """The BELT: a shape that escaped every reader above and raised out of the build.

    NOT correctable, and that is the point of keeping it separate: it names no field, because
    if it could name one a reader would have caught it, so re-asking would be asking the model
    to guess at the price of a full extraction prompt. The exception TYPE is named (it
    describes the interpreter's complaint); its MESSAGE is not (it interpolates a
    model-authored value from an entity-bearing session).
    """
    return Decline(
        candidate_type,
        REASON_MALFORMED,
        f"the {where} could not be read, and no shape check named the field that "
        f"failed — this is an extractor gap ({type(exc).__name__}), not something the "
        "candidate can be corrected into",
    )


def _evidence(raw: dict[str, Any]) -> tuple[EvidenceRef, ...]:
    """The cited evidence, or `()` when the candidate cites none.

    Raises `ShapeError` when it cites some and NONE of them can be read. PARTIAL tolerance is
    deliberate — an unreadable item is skipped, because evidence is a `>= 1` gate. The ALL-BAD
    case re-raises rather than falling through to `no_evidence`, which would misdiagnose a
    mechanical shape failure as a judgement about CONTENT and so make it terminal.
    """
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


def read_evidence(raw: dict[str, Any]) -> tuple[EvidenceRef, ...]:
    """The citations of a raw candidate, or `()` when none can be read. NEVER raises.

    The public face of `_evidence`, for the one caller that needs the citations of a candidate
    that never became an `ExtractedCandidate`: `consumer.py` snapshotting a fail-to-review
    candidate's quotes. It goes through THIS reader rather than a second hand-written one, so
    the audit record covers precisely the set validation accepted — an audit trail with fewer
    citations than the candidate is worse than none, because it looks complete.
    """
    try:
        return _evidence(raw)
    except ShapeError:
        # The all-bad case, which `_evidence` re-raises so the extractor can route it to
        # the correctable side. Here there is nothing to correct: the candidate has
        # already declined, and a review item with no auditable citation is a degraded
        # row, not a failed request.
        return ()


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
        name=require(slot, "name", as_text, at=at, requirement="a short identifier for the slot"),
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

    An EMPTY array collapses to `None`; both say "no closed set". The `as_array` read is what
    stops `enum_values: "NA,EU"` turning into seven single-character values with no error.
    """
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


def _bare_locator_value(value: str) -> str:
    """Normalize a model-copied standalone SQL literal to the enumerator's bare value.

    The extractor contract asks for a semantic value, but live models commonly copy the SQL
    token including quotes. Only normalize when the whole value parses as one literal; names,
    comma lists, and arbitrary text remain untouched rather than being guessed at.
    """
    try:
        parsed = sqlglot.parse_one(
            value, dialect="clickhouse", error_level=sqlglot.ErrorLevel.RAISE
        )
    except Exception:
        return value
    if isinstance(parsed, exp.Literal):
        return parsed.name
    if isinstance(parsed, exp.Boolean):
        return "TRUE" if parsed.this else "FALSE"
    return value


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
                    value=_bare_locator_value(
                        require(
                            loc,
                            "value",
                            as_text,
                            at=loc_at,
                            requirement=(
                                "the bare semantic literal without surrounding SQL quotes"
                            ),
                        )
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
                    _slot_plan(entry["slot"], at=f"{entry_at}.slot") if entry.get("slot") else None
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

    `grain` is where a live `gpt-5.5` extraction died: it emitted a prose SENTENCE where the
    schema wants an object, and `grain.get("columns")` raised `AttributeError` out of the whole
    payload build.
    """
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
    normalizations: list[str] = []
    if "shape" not in sig and "columns" in sig:
        shape_items = as_array(
            sig["columns"],
            at=f"{at}.columns",
            requirement=(
                'an array of output column objects using either {"column", "type"} '
                'or the recognized aliases {"name", "semantic_type"}'
            ),
        )
        normalizations.append("columns_to_shape")
    else:
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
        column_shape = as_object(item, at=item_at, requirement='an object with "column" and "type"')
        column_key = "column" if "column" in column_shape else "name"
        type_key = "type" if "type" in column_shape else "semantic_type"
        if column_key == "name":
            normalizations.append("name_to_column")
        if type_key == "semantic_type":
            normalizations.append("semantic_type_to_type")
        shape.append(
            ColumnShape(
                column=require(
                    column_shape,
                    column_key,
                    as_text,
                    at=item_at,
                    requirement="the output column name",
                ),
                type=require(
                    column_shape,
                    type_key,
                    as_text,
                    at=item_at,
                    requirement="the output column's type",
                ),
            )
        )
    verifiable = optional(
        grain, "verifiable", as_flag, at=grain_at, requirement="true or false", default=True
    )
    if verifiable and not shape:
        raise ShapeError(
            f"{at}.shape",
            "a non-empty array of output columns when grain.verifiable is true",
            shape_items,
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
            verifiable=verifiable,
        ),
        invariants=tuple(
            as_text(inv, at=f"{at}.invariants[{i}]", requirement="one invariant statement")
            for i, inv in enumerate(invariants)
        ),
        normalizations=tuple(dict.fromkeys(normalizations)),
    )


def _node_index(value: Any) -> int | None:
    """Coerce an LLM-emitted node index (`order` / a `feeds_from` entry) to an int, else `None`.

    A real model emits `0` or the string `"0"`, so a numeric string is accepted. A BOOL is not
    an index (`True` would silently become node 1) and a fractional float is a typo rather than
    something to truncate — both are `None` (⇒ decline).
    """
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
    """Structural gate over the raw `composes` DAG plan, run BEFORE `_compose_nodes` builds it.

    This is LLM output: every field is untrusted, so a shape-confused node becomes a Decline
    naming WHICH node and WHICH field rather than a raw interpreter message — or, worse, an
    exception, which would leave the consumer's message un-acked → reclaim → dead-letter.
    Nothing here may raise. Checked: the STRUCTURE `_compose_nodes` assumes and `Node.parse`
    requires at landing. Deliberately NOT checked: DAG SEMANTICS — S4's `check_dag` owns those
    and routes them to `fail_to_review`, which a hard `malformed` reject would bypass.
    """
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
                f"{where} has no 'order'; every node declares its integer position in the DAG",
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
            return _malformed("blueprint", f"{where} 'requires_approval' is not an object")
    return None


def _validate_kind_composes(kind: str, composes: tuple[ComposeNodePlan, ...]) -> Decline | None:
    """Require the discriminator and DAG payload to describe the same shape."""
    if kind == "composite" and not composes:
        return _role_shape(
            "candidate.payload.kind is 'composite' but composes is empty; WITH/CTEs are "
            "one query, while a composite requires one DAG node per executed SQL call"
        )
    if kind == "single" and composes:
        return _role_shape(
            "candidate.payload.kind is 'single' but composes contains DAG nodes; use "
            "'composite' for a multi-query DAG or remove the nodes"
        )
    return None


def _compose_nodes(raw_nodes: list[dict[str, Any]]) -> tuple[ComposeNodePlan, ...]:
    """Build the typed `ComposeNodePlan`s.

    PRECONDITION (load-bearing): `_validate_compose_nodes` has already passed on `raw_nodes`,
    which is what makes every coercion here total — bools and fractional floats are already
    declined.
    """
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


def _validate_roles(
    params: list[ParamPlan],
    known_rules: frozenset[str],
    rule_index: RuleIndex | None = None,
) -> Decline | None:
    """Each `ParamPlan` against the obligations of the role it declared.

    TWO KINDS OF DECLINE come out of here. A required-field or closed-enum obligation that
    follows MECHANICALLY from a role or type the candidate itself chose (`role: rule` with no
    `rule_id`, `role: inline` with no `why`, a windowed slot that declared a `binds_to`) is a
    change of EXPRESSION and is correctable via `_role_shape`. Everything else here consults
    something OUTSIDE the candidate — the catalog's rule ids, this pipeline's generalization
    capability, a SQL parser — and is terminal, with the single exception the unknown-`rule_id`
    branch documents. See `_correctable` for the line in full.
    """
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
                    "blueprint",
                    REASON_BAD_ROLE,
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
            # gate the blueprint compiler/runtime apply (`validate_blueprint_dag`
            # validates whenever a
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
                        "blueprint",
                        REASON_BAD_ROLE,
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
                # TERMINAL, unless the catalog can name the fix. The id is checked
                # against the CATALOG, so the only honest answers are "a different
                # existing rule" or "none exists" — and the second is the valuable
                # outcome this decline exists to produce. An open-ended re-ask would
                # convert a request for a human to add a rule into pressure on the model
                # to name any id that passes, so there is none.
                #
                # `nearest_known_rule` closes the first answer WITHOUT opening the
                # second. It is deterministic, it reads only this plan's own id and
                # locator plus the catalog, and it returns a counterpart only when
                # exactly one is unambiguous — so either the decline carries the id the
                # model should have cited (correctable, `_rule_hint`), or nothing has
                # changed at all. The live case it was written for: `earnings_only`,
                # quoted off a PRIOR ART card whose blueprint `uses_rules` namespace had
                # drifted from the catalog, where the catalog says `gross_earnings`.
                hint = nearest_known_rule(p.rule_id, p, known_rules, rule_index)
                if hint is not None:
                    return _rule_hint(at, p.rule_id, hint)
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
    payload: BlueprintPayload,
    summary: SessionSummary,
    rule_index: RuleIndex | None = None,
) -> Decline | None:
    """Every literal predicate of the accepted SQL against the plan's coverage.

    COLLECTS the uncovered predicates instead of returning on the first, which is what makes
    the decline correctable inside a 2-round budget. Un-parseable SQL returns IMMEDIATELY and
    terminally: the check cannot enumerate what it could not parse, so it has nothing to name —
    exactly the property that keeps `unrewritable_sql` on the terminal side of the line while
    its neighbour crosses it.
    """
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
    plans = list(payload.parameterization)
    saw_any_sql = False
    uncovered: list[LiteralPredicate] = []
    mismatch: Decline | None = None
    duplicate_coverage: tuple[LiteralPredicate, list[int]] | None = None
    for ref in payload.source_tool_call_refs:
        for sql in resolved.get(ref, ()):
            saw_any_sql = True
            predicates = literal_predicates(sql)
            if predicates is None:
                return Decline("blueprint", REASON_UNREWRITABLE, f"un-parseable SQL at {ref}")
            for pred in predicates:
                # Per-locator coverage (LOW-1): match by (column, value) so
                # `region='NA' OR region='EU'` needs a plan entry PER predicate, and
                # same-named columns on different (qualified) tables are distinguished
                # by `table`. A predicate with NO covering ParamPlan → silent dropped
                # filter → decline.
                #
                # OPERATOR-BLIND, unchanged and deliberately so: a plan entry accounts
                # for a predicate however it compares, and tightening the COVERAGE test
                # would newly decline plans that are fine. The operator matters only to
                # the decline's hint, which must not offer a rule for an inverted filter
                # (`rule_match.rules_for_predicate`).
                covering = [
                    (index, plan)
                    for index, plan in enumerate(plans)
                    if plan.locator.column.lower() == pred.column.lower()
                    and plan.locator.value == pred.value
                    and _table_compatible(plan.locator.table, pred.table)
                ]
                if len(covering) > 1 and duplicate_coverage is None:
                    duplicate_coverage = (pred, [index for index, _plan in covering])
                # Deduplicated: the same predicate reached through two refs (a
                # multi-table answer re-running one query) is ONE thing for the model to
                # fix, and a correction listing it twice reads like two.
                if not covering:
                    if pred not in uncovered:
                        uncovered.append(pred)
                    continue
                # COVERED IS NOT THE SAME AS CORRECTLY COVERED. This is the landing
                # gate: an entry may claim a predicate with the WRONG catalog rule, and
                # a rule id is not decoration — the kept predicate stays correct SQL
                # (S4 keeps role=rule predicates verbatim since 2026-08-18), but a
                # wrong citation ships a false ANNOTATION: `gross_earnings` declared
                # on a `register_type = 'DDUCT'` predicate misleads every reviewer
                # and keys the blueprint against the wrong governance concept, with
                # nothing downstream to catch it (S4 checks columns, not rule
                # semantics). Checked HERE rather than in `_validate_roles`
                # because this is the only place the cited rule and the predicate it
                # covers are both in hand.
                #
                # Only a DISPROVED citation declines: an unparseable/complex rule
                # predicate, an id the index does not carry, or no index at all all
                # return None and the citation is accepted exactly as before this check
                # existed (`rule_match.rule_contradicts_predicate` states why
                # can't-verify must not become reject).
                if mismatch is None:
                    mismatch = _first_rule_mismatch(covering, pred, rule_index)
    if not saw_any_sql:
        return Decline("blueprint", REASON_UNREWRITABLE, "no accepted SQL found for source refs")
    # UNCOVERED FIRST: a plan that skipped predicates is a bigger, cheaper-to-state
    # problem than one that mis-labelled a covered one, and the two messages ask for
    # different edits. Whichever is not reported this round is reported the next.
    if uncovered:
        return _predicate_hint(uncovered, rule_index)
    if duplicate_coverage is not None:
        pred, indexes = duplicate_coverage
        return Decline(
            "blueprint",
            REASON_TOTALITY,
            f"literal predicate {pred.column} {pred.operator} {pred.value!r} is covered by "
            f"multiple parameterization entries {indexes}; exactly one entry may own a SQL site",
        )
    return mismatch


def _first_rule_mismatch(
    covering: list[tuple[int, ParamPlan]],
    pred: LiteralPredicate,
    rule_index: RuleIndex | None,
) -> Decline | None:
    """The first `rule`-role entry among *covering* whose cited rule the catalog proves
    declares a different filter, as a correctable decline.
    """
    for index, plan in covering:
        if plan.role != "rule" or not plan.rule_id:
            continue
        wrong = rule_contradicts_predicate(plan.rule_id, pred, rule_index)
        if wrong is not None:
            return _rule_mismatch(f"candidate.payload.parameterization[{index}]", wrong, pred)
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
        kind=require(raw, "kind", as_text, at=at, requirement='either "single" or "composite"'),
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
                        "an array of the session tool_call_refs that produced the accepted answer"
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


# ⚠ `sql_rewrite` IS DROPPED HERE, and silently, which is the right handling for exactly this
# key. It is §C.5's badge saying "the assistant rewrote this query, not the session" — an
# attestation about PROVENANCE — and everything this function reads is model-authored JSON. A
# mined candidate that emitted one would render that claim on a review card while `authored=False`
# let it auto-land: the card and the router disagreeing about one row.
#
# `BlueprintPayload` therefore has no such field, so the key simply does not survive
# `payload_to_doc()`. The badge is stamped SERVER-SIDE after this function returns
# (`inbox/completion.py::_stamped`), from a record derived from the `ValidationSnapshot` — the
# one structure a model cannot write to. Dropped rather than DECLINED because the key is inert
# junk, not a lie a blueprint should die for: the rest of the payload may be perfectly good.


# --- the NON-blueprint payloads (§3.3) ----------------------------------------------
#
# WHY THESE EXIST. Until this branch checked anything, `to_candidate` asked of a
# non-blueprint payload only that it BE an object — so a `global_knowledge` candidate
# whose payload read {definition, fact_type, intent, scope} passed intake, passed the S5
# leakage gate (which scans a FIXED four-field list and therefore never looked at
# `definition` or `intent` at all), reached a reviewer, was approved — and only THEN
# died, inside `knowledge_seed_from_candidate`, on a `statement` that had never been
# there. The scheduler could report that only as a landing failure, i.e. as INFRA, so the
# approve answered 503 and retried forever without ever being able to succeed. Intake is
# the one place where the same defect is still a CORRECTABLE decline the model can
# re-emit against, instead of a hold no human action can clear.
#
# WHAT THEY CHECK IS DERIVED FROM WHAT LANDING READS — never from a hand-kept list of
# plausible field names, which is the rule this plane keeps re-learning. Each reader
# below mirrors, field for field, the one function that consumes its payload:
#
#   global_knowledge → `generalize/mapping.py::knowledge_seed_from_candidate`
#   user_knowledge   → `user/models.py::UserKnowledgeRecord.from_candidate`
#   schema_edit      → `schema_edit/models.py::SchemaEditPatch.from_payload`
#
# Two of those three DEFAULT every field they read, which is worse than raising: a
# malformed `user_knowledge` payload commits a record with a BLANK statement, and a
# malformed `schema_edit` opens a PR carrying an empty patch against no catalog. Neither
# fails; both are silent. So REQUIRED here means "the consumer would otherwise default
# this into nothing", not merely "the consumer raises".

# The COMPLETE key set a `global_knowledge` payload may carry: the four content surfaces
# the S5 gate scans (`leakage/gate.py::_ENTITY_FREE_SURFACES["global_knowledge"]`) plus
# the `knowledge_type` label 05 §global_knowledge declares (no code reads it; the docs
# name it, and a payload that carries it is not thereby malformed).
#
# Spelled here rather than imported, because importing the gate would drag the semantic
# scanner + the user store into the validator for one tuple; `test_global_knowledge_keys_
# match_the_leakage_gate_surfaces` pins the two together instead — the same arrangement
# `schema.py::_SEARCH_KIND_ENUM` has with `prior_art.py::KNOWN_KINDS`.
_GLOBAL_KNOWLEDGE_KEYS = frozenset(
    {"statement", "knowledge_type", "related_terms", "structured", "scope"}
)

# How many off-contract keys one decline names. A payload with more than this has a
# systemic problem, and the message is prompt text before it is a log line.
_MAX_LISTED_KEYS = 6


def _non_empty_text(value: Any, *, at: str, requirement: str) -> str:
    """We are about to store this as the artifact's ONLY identifying text.

    NOT `as_text`: that coerces a number, and `knowledge_seed_from_candidate` rejects a
    non-`str` `statement` outright — a coercion here would merely move the same failure to
    the far side of a human approve. Whitespace-only is rejected for the same reason it is
    there: a blank statement recalls nothing and only pollutes the index.
    """
    if not isinstance(value, str) or not value.strip():
        raise ShapeError(at, requirement, value)
    return value


def _first_non_empty(candidates: tuple[tuple[str, Any], ...], *, requirement: str) -> str:
    """The first usable value among several ALIAS keys, or a `ShapeError` naming them all.

    `SchemaEditPatch.from_payload` reads three of its fields as `payload.get(a) or
    payload.get(b) or ""` — the Locked names and the fixture names both work — so the intake
    check has to accept the same disjunction on the same precedence. It also has to name
    EVERY alias when none is usable: declining on `patch` alone would ask a model that
    already sent `proposed_yaml` to add a field it had not omitted.

    *candidates* is (dotted path, value) in the downstream `or`-chain's order. A wrong-TYPE
    alias declines immediately rather than falling through, because `or` would skip it
    silently and the model would never learn which of the two names it got wrong.
    """
    for path, value in candidates:
        if isinstance(value, str) and value.strip():
            return value
        if value is not None and not isinstance(value, str):
            raise ShapeError(path, requirement, value)
    raise ShapeError(" or ".join(path for path, _ in candidates), requirement, None, absent=True)


def _global_knowledge_payload(raw: dict[str, Any]) -> Decline | None:
    """Check a `global_knowledge` payload against what LANDING reads.

    `statement` is the seed's whole first line and the ONLY field
    `knowledge_seed_from_candidate` refuses to default — it raises on an absent or non-`str`
    one, which is the exact defect this reader was written for.

    The other four are optional, and their TYPE checks are the one thing here not forced by a
    downstream raise: the mapper flattens `related_terms`/`structured` through `_collect_text`,
    which walks a dict, a list or a bare string without complaint. It is not indifferent to
    which, though — it just fails QUIETLY. `related_terms: "active, headcount"` lands as ONE
    term rather than two, and a list-shaped `structured` lands leaves keyed by index. So the
    types are checked against the DECLARED contract (the same one `schema.py` puts in front of
    the model) rather than against the raise, on the rule that a landed artifact should be the
    shape the reviewer approving it believes it is.

    UNIQUELY AMONG THE THREE, the key set is CLOSED, and that is a leakage rule rather than a
    tidiness one: this payload lands in the GLOBAL, scope-bypassed knowledge index, and the S5
    gate scans exactly the surfaces named in `_GLOBAL_KNOWLEDGE_KEYS` and nothing else. A key
    outside that set is therefore an UNSCANNED TEXT SURFACE — text that reaches a reviewer's
    card and (if the mapper ever grows to read it) the global index, having been checked for
    entities by nothing at all. The stuck candidate carried two of them, `definition` and
    `intent`, and the gate reported `pass` on a payload it had never read.
    """
    at = "candidate.payload"
    unknown = sorted(set(raw) - _GLOBAL_KNOWLEDGE_KEYS)
    if unknown:
        # NOT a `ShapeError`: nothing about the SHAPE of these keys is wrong — the objection
        # is that they exist at all, which is not the sentence `ShapeError` writes. The exit
        # is the same one (`_malformed`, correctable), so the corrective turn is unchanged.
        # The key names are MODEL-AUTHORED text on their way into prompt text, so they go
        # through `_quoted` exactly like a session literal does.
        listed = ", ".join(_quoted(key) for key in unknown[:_MAX_LISTED_KEYS])
        return _malformed(
            "global_knowledge",
            f"{at} carries {listed} — a global_knowledge payload may carry ONLY "
            "statement, knowledge_type, related_terms, structured and scope. The leakage "
            "gate scans exactly those five surfaces, so text "
            "under any other key would reach the GLOBAL, scope-bypassed knowledge index "
            "having been scanned for entities by nothing at all. State the fact in "
            "`statement` (supporting terms in `related_terms` or `structured`) and drop "
            "the remaining keys.",
        )
    require(
        raw,
        "statement",
        _non_empty_text,
        at=at,
        requirement=(
            "a non-empty, ENTITY-FREE sentence stating the fact being learned — it "
            "becomes the whole text of the landed knowledge chunk"
        ),
    )
    optional(
        raw,
        "knowledge_type",
        as_text,
        at=at,
        requirement='a short label for the kind of fact (e.g. "business_rule")',
        default=None,
    )
    for index, term in enumerate(
        optional(
            raw,
            "related_terms",
            as_array,
            at=at,
            requirement="an ARRAY of entity-free terms this fact should also be recalled by",
            default=[],
        )
    ):
        _non_empty_text(
            term,
            at=f"{at}.related_terms[{index}]",
            requirement="a non-empty term, as a string",
        )
    optional(
        raw,
        "structured",
        as_object,
        at=at,
        requirement="an OBJECT of supporting entity-free detail",
        default={},
    )
    optional(
        raw,
        "scope",
        as_text,
        at=at,
        requirement="a string naming what this fact is about (it titles the landed chunk)",
        default=None,
    )
    return None


def _user_knowledge_payload(raw: dict[str, Any]) -> Decline | None:
    """Check a `user_knowledge` payload against `UserKnowledgeRecord.from_candidate`.

    `statement` is required for the reason the reader's default hides: it is `payload.get(
    "statement", "")`, so a payload without one COMMITS — a blank per-user fact that recalls
    nothing, with no error anywhere to say so.

    NO closed key set here, deliberately. This target is entity-BEARING by contract (the S5
    gate's remit stops at the global types), it lands in a per-user store recalled only under
    that user's scope, and the extra key the reader is most likely to meet is `user_id` — which
    `from_candidate` reads and DISCARDS in favour of the session's authenticated user (R6/D17).
    Rejecting unknown keys would decline candidates the consumer already handles safely.
    """
    at = "candidate.payload"
    require(
        raw,
        "statement",
        _non_empty_text,
        at=at,
        requirement="a non-empty sentence stating the per-user fact being remembered",
    )
    optional(
        raw,
        "fact_type",
        as_text,
        at=at,
        requirement='a short label for the kind of fact (e.g. "preference")',
        default=None,
    )
    optional(
        raw,
        "scope",
        as_text,
        at=at,
        requirement='a string naming the fact\'s scope (defaults to "user")',
        default=None,
    )
    optional(
        raw,
        "structured",
        as_object,
        at=at,
        requirement="an OBJECT of supporting detail",
        default={},
    )
    return None


def _schema_edit_payload(raw: dict[str, Any]) -> Decline | None:
    """Check a `schema_edit` payload against `SchemaEditPatch.from_payload`.

    Every field that reader touches is defaulted, and the defaults are the danger: a payload
    missing its patch opens a PR whose body is an empty string against a catalog path derived
    from `""`. The bot never auto-commits (D18), so nothing is corrupted — but a reviewer is
    sent an empty PR to judge, which is the most expensive possible way to say "malformed".

    The three alias pairs are the reader's own `or`-chains, in its order, via `_first_non_empty`.
    No closed key set: a schema_edit is human-gated and entity-bearing (it quotes catalog YAML),
    so an extra key is a reviewer's problem rather than a leak.
    """
    at = "candidate.payload"
    require(
        raw,
        "statement",
        _non_empty_text,
        at=at,
        requirement="a non-empty sentence stating what the catalog edit changes and why",
    )
    _first_non_empty(
        ((f"{at}.edit_kind", raw.get("edit_kind")), (f"{at}.edit_type", raw.get("edit_type"))),
        requirement=(
            'a non-empty string naming the kind of catalog edit (e.g. "add_rule") — under '
            "either name; the writer reads edit_kind first, then edit_type"
        ),
    )
    _first_non_empty(
        ((f"{at}.proposed_yaml", raw.get("proposed_yaml")), (f"{at}.patch", raw.get("patch"))),
        requirement=(
            "a non-empty string carrying the proposed catalog YAML — under either name; "
            "the writer reads proposed_yaml first, then patch, and an absent patch opens "
            "an EMPTY pull request"
        ),
    )
    target = optional(
        raw,
        "target",
        as_object,
        at=at,
        requirement='an OBJECT naming what the edit targets, as {"database": ...}',
        default={},
    )
    _first_non_empty(
        (
            (f"{at}.target_catalog", raw.get("target_catalog")),
            (f"{at}.target.database", target.get("database")),
        ),
        requirement=(
            "a non-empty string naming the catalog database the edit targets — under "
            "either shape; the writer reads target_catalog first, then target.database"
        ),
    )
    return None


# Per non-blueprint type, the reader that checks its payload. `to_candidate` looks up
# rather than branches, and `test_every_non_blueprint_type_has_a_payload_reader` pins the
# table against `CANDIDATE_TYPES` — so adding a fifth target FAILS A TEST rather than
# quietly re-opening the unchecked-payload hole this table exists to close.
_PAYLOAD_READERS: dict[str, Callable[[dict[str, Any]], Decline | None]] = {
    "global_knowledge": _global_knowledge_payload,
    "user_knowledge": _user_knowledge_payload,
    "schema_edit": _schema_edit_payload,
}


def validate_payload(candidate_type: str, raw: Any) -> Decline | None:
    """Check one NON-BLUEPRINT payload against what its landing reads. `None` ⇒ usable.

    THE INTAKE READER, MADE REACHABLE FROM OUTSIDE `to_candidate`. It exists because a second
    write path into a candidate payload now exists — a reviewer editing a `global_knowledge`
    candidate under review, and a user fact promoted into one
    (`docs/decisions/knowledge-edit-and-user-promotion-design.md` §B) — and the ONE thing that
    must not fork is what a legal payload IS. The closed `global_knowledge` key set is a LEAKAGE
    rule, not a tidiness one (see `_global_knowledge_payload`): a key outside the five surfaces
    is text the S5 gate never scans, so a human path that skipped this reader would reopen
    exactly the hole the reader was written to close, on the path where a person's approval is
    about to carry the payload into the global index.

    `to_candidate` DELEGATES here rather than keeping its own copy of the dispatch, so the two
    cannot answer differently for the same payload. The exception handling is the same shape it
    has always had, for the same reason: this function is documented never to raise, so a
    reader's own bug becomes a traceable decline rather than an escape.

    An UNKNOWN type reads as `None` (no opinion) — the `.get`-not-`[...]` posture
    `to_candidate` already had, kept loud by `test_every_non_blueprint_type_has_a_payload_reader`.
    """
    reader = _PAYLOAD_READERS.get(candidate_type)
    if reader is None:
        return None
    try:
        payload = as_object(
            raw,
            at="candidate.payload",
            requirement=f"an object carrying the {candidate_type} payload",
        )
        return reader(payload)
    except ShapeError as exc:
        return _malformed(candidate_type, str(exc))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return _unreadable(candidate_type, f"{candidate_type} payload", exc)


def to_candidate(
    raw: Any,
    summary: SessionSummary,
    *,
    known_rules: frozenset[str],
    rule_index: RuleIndex | None = None,
) -> ExtractedCandidate | Decline:
    """Validate one raw candidate → `ExtractedCandidate` or `Decline`.

    *known_rules* decides whether a cited `rule_id` EXISTS; the optional *rule_index* only ever
    affects what a decline for a non-existent one can SAY. NEVER raises — an uncaught exception
    here leaves the consumer's queue message un-acked → reclaim → dead-letter, losing the whole
    session AND the traceable reason.
    """
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
            # ...and its FIELDS, against what the type's landing actually reads, through
            # the SHARED reader (`validate_payload`) rather than a private dispatch here.
            # The human edit path calls the same function, so the two paths into a
            # non-blueprint payload cannot disagree about what a legal one is; the
            # exception handling below is retained because it is this function's contract
            # (never raise), not because it is the only place the readers are guarded.
            payload_decline = validate_payload(ctype, payload)
            if payload_decline is not None:
                return payload_decline
        except ShapeError as exc:
            return _malformed(ctype, str(exc))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            # The same BELT the blueprint path has, for the same reason: a reader's own
            # bug must become a traceable decline, not an un-acked queue message that
            # dead-letters the whole session.
            return _unreadable(ctype, f"{ctype} payload", exc)
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
            ctype,
            REASON_NO_ACCEPTANCE,
            f"accepted_signal {payload.accepted_signal!r} not in {sorted(_ACCEPTED_SIGNAL_DOMAIN)}",
        )
    if summary.accepted_signal is None:
        return Decline(ctype, REASON_NO_ACCEPTANCE, "session carried no acceptance signal")

    kind_decline = _validate_kind_composes(payload.kind, payload.composes)
    if kind_decline is not None:
        return kind_decline

    role_decline = _validate_roles(list(payload.parameterization), known_rules, rule_index)
    if role_decline is not None:
        return role_decline

    totality_decline = _validate_totality(payload, summary, rule_index)
    if totality_decline is not None:
        return totality_decline

    return ExtractedCandidate(header=header, payload=payload)


def validate_parameterization_totality(
    payload_doc: dict[str, Any],
    summary: SessionSummary,
    *,
    rule_index: RuleIndex | None = None,
) -> Decline | None:
    """Validate that a proposed blueprint plan accounts for every SQL predicate.

    This is the read-only proposal-time seam used by the review assistant. The eventual
    apply still runs ``to_candidate`` in full; this narrower check prevents presenting a
    proposal as applicable when its replacement SQL and entries are already known to be
    inconsistent.
    """
    try:
        payload = _blueprint_payload(payload_doc)
    except ShapeError as exc:
        return _malformed("blueprint", str(exc))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return _unreadable("blueprint", "blueprint payload", exc)
    return _validate_totality(payload, summary, rule_index)
