"""ReviewInbox — the `in_review` projection + the human transitions (§4).

A PROJECTION over `learning_candidates` (D101) adding NO second store: `list()` reads by
status and maps each envelope to an `InboxItem`, and a human's action advances `status`.

    approve  → in_review → validated
    reject   → in_review | needs_parameterization → rejected   (archived as a NEGATIVE
                                                                training signal, NOT a delete)
    complete → needs_parameterization → extracted → (the write router decides)
    retract  → validated → retired          (a post-promotion pull-from-index)
    verify   → validated → validated        (Phase-3: flip `verified` on node + envelope)
    promote  → validated → promoted         (Phase-3: emit the MCP YAML for a MANUAL PR)

ONE APPROVE IMPLEMENTATION (R4): the inbox does NOT re-implement the invariants — the D17
entity strip, the `depends_on` guard, the static/replay guards — it guards the current status
fail-loud and DELEGATES to `PromotionScheduler.apply_human_decision`. An illegal transition
raises `InboxTransitionError`, so a mis-routed action never silently mutates a candidate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Literal

from data_agent.runtime.blueprint.models import (
    SCALAR_CONSUME_REF,
    TABLE_CONSUME_REF,
    ResultGrain,
)
from data_agent.runtime.blueprint.template import (
    TemplateBindError,
    bind_template,
    referenced_slots,
)
from data_agent.runtime.blueprint.verify import verify_result
from data_agent.timeutil import now_iso

from ..candidate.decline import ValidationSnapshot
from ..candidate.models import (
    CandidateEnvelope,
    CandidateStatus,
    mint_promoted_candidate_id,
    promoted_record_digest,
)
from ..candidate.store import CandidateStore
from ..candidate.verdicts import (
    LeakageAttestation,
    LeakageVerdict,
    leakage_fingerprint,
)
from ..promotion.mcp_export import PromotionEmit, build_promotion_emit
from ..promotion.models import ProbeResult, PromotionPolicy
from ..promotion.scheduler import PromotionScheduler
from ..revise import (
    BlueprintReviser,
    KnowledgeProposal,
    KnowledgeReviser,
    ReviseProposal,
    ReviserUnavailableError,
)
from ..user.models import UserKnowledgeRecord
from ..user.store import UserKnowledgeStore
from .completion import CompletionResult, ParameterizationCompleter, sql_rewrite_of
from .knowledge_edit import (
    KNOWLEDGE_TYPE,
    ROUTE_REASON_EDITED,
    ROUTE_REASON_PROMOTED,
    KnowledgeEditor,
    KnowledgeEditorUnavailableError,
    KnowledgeEditResult,
    entity_scan_view,
)
from .models import InboxItem, _leakage_cleared
from .ranking import rank_key

_logger = logging.getLogger(__name__)


class InboxTransitionError(Exception):
    """Raised when a human transition is requested from an illegal current status."""


class UserKnowledgeUnavailableError(RuntimeError):
    """No per-user knowledge store is wired in this deployment.

    A 503, and its own type for the reason every other absence on this surface has one: it is a
    fact about the DEPLOYMENT, not about the request, so mapping it to a 404 ("no such user") or
    a 200-with-an-empty-list would tell a reviewer that a user has no facts when the truth is
    that nobody looked. `_build_inbox_from_env` always wires a store — the in-memory one in the
    offline posture — so this is reachable only from a directly-constructed inbox.
    """


class _NoOpProbe:
    """A no-op warehouse probe for an UNWIRED inbox (no scheduler injected).

    Only reached if an approve replays a blueprint template; a production inbox injects the real
    scheduler and probe.
    """

    async def run(
        self,
        sql: str,
        *,
        grain_columns: tuple[str, ...],
        column_scope: tuple[str, ...] = (),
    ) -> ProbeResult:
        return ProbeResult(row_count=0, distinct_grain_count=None, columns=())


class _ZeroHitCounts:
    async def hit_count(self, canonical_key: str) -> int:
        return 0


@dataclass(frozen=True)
class UserKnowledgeView:
    """One per-user fact as the reviewer's User Knowledge tab shows it (§D.1).

    `promoted` is the CANDIDATE this record has already been lifted into, or `None`. Carried as
    the envelope rather than pre-flattened so `to_wire` owns exactly what crosses — the card
    needs an id and a status, and every other field on a candidate is a different kind of thing
    that happens to be in hand.
    """

    record: UserKnowledgeRecord
    promoted: CandidateEnvelope | None = None

    def to_wire(self) -> dict[str, Any]:
        """The record verbatim + the promotion pointer.

        ⚠ ENTITY-BEARING AND DELIBERATELY UNREDACTED. This is the one listing on this surface
        that carries raw per-user facts, because a redacted per-user fact is not a fact — the
        reviewer is deciding whether the sentence can be generalized, which they cannot do
        without reading it. What bounds it is the guard in front (`list_user_knowledge`: one
        named user, never all) rather than a projection here.

        The provenance is the SESSION AND TRACE IDS ONLY — the evidence refs are KV keys into
        `learning_audit` and have no reader on this page, and a pointer offered with nothing to
        follow it with is an invitation to add one.
        """
        record = self.record
        return {
            "record_id": record.record_id,
            "user_id": record.user_id,
            "statement": record.statement,
            "fact_type": record.fact_type,
            "scope": record.scope,
            "structured": record.structured,
            "committed_at": record.committed_at,
            "provenance": {
                "source_session": record.source_session,
                "source_trace": record.source_trace,
            },
            "promotion": (
                {
                    "candidate_id": self.promoted.candidate_id,
                    "status": self.promoted.status,
                }
                if self.promoted is not None
                else None
            ),
        }


@dataclass(frozen=True)
class PromotedUserKnowledge:
    """What one press of "promote to global knowledge" produced (§D.2).

    `already` is the SECOND-PRESS answer and it is a success, not an error: the deterministic id
    means the row is the same row, so the honest response is the one that already exists. The
    caller renders it as "in review as <id>" either way, which is why the two cases share a
    shape instead of one of them being a 409.
    """

    candidate_id: str
    status: str
    already: bool
    entity_scan: dict[str, Any]

    def to_wire(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "status": self.status,
            "already": self.already,
            # `{result, hits:[{field, kind}]}` — the span is dropped by `entity_scan_view`,
            # which owns that rule for every non-`InboxItem` response on this surface.
            "entity_scan": self.entity_scan,
        }


# What a promoted fact's `extractor_rationale` says. There was no extractor: a person read a
# private fact and decided the organisation should hold it, and the field is what a later
# reader consults to understand where the row came from.
PROMOTED_RATIONALE = "promoted from user knowledge by a reviewer"
# `proposed_action` for the same row. A value outside the extractor's vocabulary ON PURPOSE, so
# a query for "candidates a human introduced" is one predicate rather than a join through the
# route reason.
PROMOTED_ACTION = "promote_user_knowledge"
# ⚠ ITS OWN CONTENT-HASH NAMESPACE, and this is load-bearing rather than tidy. `content_hash`
# is what `CandidateStore.supersede` sweeps on: a re-extraction of the session this fact came
# from would delete every candidate carrying that session's hash, and a promoted row sharing it
# would vanish under a reviewer mid-review, with no trace and no way to tell it had existed.
PROMOTED_HASH_PREFIX = "userpromote::"


def promoted_content_hash(record_id: str) -> str:
    """The `content_hash` a promoted user fact carries: the namespace, then a DIGEST.

    ⚠ HASHED FOR THE REASON `mint_promoted_candidate_id` IS HASHED. This field used to
    interpolate the record id — `userpromote::userknow::<user_id>::<candidate_id>` — which put
    the owner's user id in cleartext on the candidate doc, in a field that is stored, exported
    to the MCP canon and read back by anything auditing the store. The candidate id beside it
    hashes precisely so a user id never rides along in a candidate field; the content hash had
    the same exposure and now uses the same digest.

    THE NAMESPACE SURVIVES, which is the property the field exists for: the prefix keeps this
    row outside every session's hash, so `supersede(content_hash)` on a re-extraction of the
    source session cannot sweep a promotion out from under the reviewer holding it.
    """
    return f"{PROMOTED_HASH_PREFIX}{promoted_record_digest(record_id)}"


# A HUMAN CHOSE IT. Confidence on this row is not a model's estimate of anything, and any other
# value would be a fabricated measurement — the ranking reads it, so a made-up 0.7 would order
# the queue by a number nobody produced.
PROMOTED_CONFIDENCE = 1.0


def _promoted_payload(record: UserKnowledgeRecord) -> dict[str, Any]:
    """The `global_knowledge` payload a user fact becomes (§D.2).

    ⚠ `scope` IS NOT CARRIED, and the reason is that the two fields with that name mean
    different things. On a user record `scope` is the literal string `"user"` (it is what makes
    the record per-user); on a knowledge chunk it becomes the node TITLE
    (`knowledge_seed_from_candidate`). Copying it across would title a shared corpus entry
    "user", which is both meaningless and the exact opposite of what the field now claims.

    `structured` is carried ONLY when it is a non-empty OBJECT — the record's is typed
    `dict | None` but is rehydrated JSON, and intake declines a non-object, so a list there
    would turn a promotion into a 422 about a field the reviewer never saw.

    ⚠ AN OBJECT, NOT AN OBJECT OF STRINGS, and the distinction is checked nowhere on purpose
    (design §F.1.b). `validate_payload` checks that `structured` IS an object and does not type
    its values, so a nested one survives this hop; the knowledge reviser's tool would have
    refused it, but a promoted record never went through the reviser. What must hold is that
    the text inside is still SCANNED, and it is: the gate's `_collect_text` walks dicts and
    lists to any depth, so a buried entity is attributed to a dotted field and flagged, and
    `knowledge_seed_from_candidate` lands exactly those scanned leaves. Flattening or refusing
    a nested value here would lose supporting detail the loop already handles correctly.

    `fact_type` becomes `knowledge_type`, defaulting to `user_fact`: the label says what KIND of
    thing this is, and "a fact one user taught the agent" is the truest available answer for a
    record whose own type was never set.
    """
    payload: dict[str, Any] = {
        "statement": record.statement,
        "knowledge_type": record.fact_type or "user_fact",
    }
    if isinstance(record.structured, dict) and record.structured:
        payload["structured"] = dict(record.structured)
    return payload


def _promoted_envelope(record: UserKnowledgeRecord, candidate_id: str) -> CandidateEnvelope:
    """The candidate a promoted user fact enters as. NOT yet written — `admit` does that.

    The PAYLOAD is deliberately empty here: `KnowledgeEditor.admit` writes it, and seeding it in
    both places would mean the same content arrived by two routes with the losing one silent —
    the argument `mint/engine.py` makes about its own empty `parameterization`.

    The `revalidation` snapshot is MINIMAL and carries exactly three facts: the owner's
    `user_id`, the source session and the content hash. It is there so
    `StageContext.summary.user_id` names the OWNER on every later re-run — a stage that scopes
    anything to a user must scope it to the person whose fact this was, not to the reviewer who
    pressed the button and not to nobody. The field is entity-free at the wire by construction:
    it is never projected (`_inbox_item_to_wire` does not carry it).
    """
    return CandidateEnvelope(
        candidate_id=candidate_id,
        type=KNOWLEDGE_TYPE,
        # `extracted` is the status the row is BUILT at and never the one it is stored at:
        # `admit` moves it to `in_review` and uses this value as its race guard (see the call
        # site). It is also the honest description of the instant — a candidate nothing has
        # adjudicated yet.
        status=CandidateStatus.EXTRACTED,
        payload={},
        source_session=record.source_session,
        source_trace=record.source_trace,
        # THE EVIDENCE TRAIL SURVIVES THE HOP. These are the same KV keys into `learning_audit`
        # the original candidate carried, so the quotes behind a promoted fact are still
        # resolvable — which is the whole difference between a promotion and a retyping.
        evidence_refs=record.evidence_refs,
        extractor_rationale=PROMOTED_RATIONALE,
        # The S3 sentinel. `admit` overwrites it with the settled verdict a moment later; what
        # matters is that the value in between is the one every guard fails closed on.
        entity_scan={"result": "pending", "hits": [], "self_check_contains_entities": False},
        confidence=PROMOTED_CONFIDENCE,
        proposed_action=PROMOTED_ACTION,
        # NOTHING TO WAIT FOR. A `depends_on` would be a claim about an artifact this fact needs
        # to land first, and a promoted sentence needs none.
        depends_on=(),
        content_hash=promoted_content_hash(record.record_id),
        revalidation=ValidationSnapshot(
            session_id=record.source_session,
            user_id=record.user_id,
            trace_id=record.source_trace,
            content_hash=promoted_content_hash(record.record_id),
            accepted_signal=None,
        ),
    )


@dataclass(frozen=True)
class TrialRunResult:
    """What one reviewer-driven trial run produced. STRUCTURE ONLY — never rows."""

    ok: bool
    reason: str = ""
    detail: str = ""
    missing: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    row_count: int = 0
    distinct_grain_count: int | None = None
    verify_passed: bool = False
    verify_reason: str | None = None
    # Was `row_count` above actually READ from the warehouse, or is it the 0 placeholder?
    # DEFAULTS FALSE, because every field on this dataclass except `ok` defaults to the
    # "nothing was established" value and every REFUSAL path builds the result without
    # touching this one. Defaulting True made a refusal claim it had measured a row count
    # it never went near — observed live, a `scalar_shape` refusal reported
    # `row_count: 0, row_count_measured: true`.
    row_count_measured: bool = False

    @property
    def inconclusive(self) -> bool:
        """Did the run PROVE anything about the shape?

        ⚠ `verify_passed` IS TRUE ON AN EMPTY RESULT, and reporting that as a green tick would
        be the worst possible answer to "does it function as we think". The D56 gate passes
        vacuously when there is nothing to check: the grain teeth are satisfied by zero rows,
        and the signature check is SKIPPED when the candidate declares no `result_signature.shape`
        — which most do.

        Observed immediately on the live stack: the dev warehouse's row-level grant returns
        zero rows to the replay tenant (`SELECT count()` came back 0 against 33 real rows), so
        every trial would have shown "✓ verified" while proving only that the SQL parses and is
        authorized. That is a true and much weaker claim, and the reviewer has to be told which
        one they got.
        """
        return self.ok and self.row_count == 0 and not self.columns

    def to_wire(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "detail": self.detail,
            "missing": list(self.missing),
            "columns": list(self.columns),
            "row_count": self.row_count,
            "row_count_measured": self.row_count_measured,
            "distinct_grain_count": self.distinct_grain_count,
            "verify_passed": self.verify_passed,
            "verify_reason": self.verify_reason,
            "inconclusive": self.inconclusive,
        }


def _result_grain_columns(payload: dict[str, Any]) -> tuple[str, ...]:
    """The declared result grain, read defensively (the payload is rehydrated JSON)."""
    generalization = payload.get("generalization")
    grain = generalization.get("result_grain") if isinstance(generalization, dict) else None
    if not isinstance(grain, dict):
        signature = payload.get("result_signature")
        grain = signature.get("grain") if isinstance(signature, dict) else None
    columns = grain.get("columns") if isinstance(grain, dict) else None
    return tuple(str(c) for c in columns) if isinstance(columns, list) else ()


def _expected_columns(payload: dict[str, Any]) -> list[str] | None:
    """The declared result column names, or `None` to skip the signature check."""
    signature = payload.get("result_signature")
    shape = signature.get("shape") if isinstance(signature, dict) else None
    if not isinstance(shape, list) or not shape:
        return None
    names = [c.get("column") for c in shape if isinstance(c, dict) and c.get("column")]
    return [str(n) for n in names] or None


def _trial_verdict(
    payload: dict[str, Any], grain: tuple[str, ...], result: ProbeResult
) -> TrialRunResult:
    """The D56 gate over a trial's FINAL result → the reviewer-facing `TrialRunResult`.

    ONE implementation for both the single-template and the composite path — the composite runs
    it over its TERMINAL node, exactly as `executor._finalize` does. Written once because the
    two would otherwise be free to disagree about what "verified" means on the same card.
    """
    verdict = verify_result(
        result_grain=ResultGrain(columns=grain, verifiable=bool(grain)),
        row_count=result.row_count,
        distinct_grain_count=result.distinct_grain_count,
        columns=list(result.columns),
        expected_columns=_expected_columns(payload),
    )
    return TrialRunResult(
        ok=True,
        columns=tuple(result.columns),
        row_count=result.row_count,
        # DERIVED FROM THE MEASUREMENT, not from the declaration. The two candidate
        # sources are both untruthful here: `bool(grain)` and `VerifyOutcome.grain_checked`
        # (which is `bool(result_grain.columns) and verifiable`, i.e. the same thing given
        # the `verifiable=bool(grain)` above) say only that a grain was DECLARED. A grain
        # can be declared and still unprobeable — `map_grain_columns` finds no matching
        # output column, or `unpack_grain_probe` cannot read the counts — and on both of
        # those `MCPWarehouseProbe.run` returns `row_count=0, distinct_grain_count=None`
        # while the declaration still says True. `distinct_grain_count is not None` is the
        # one signal that only the COUNT(DISTINCT) round-trip can set: every path in the
        # probe that leaves it None also returns the `row_count=0` placeholder.
        row_count_measured=result.distinct_grain_count is not None,
        distinct_grain_count=result.distinct_grain_count,
        verify_passed=verdict.passed,
        verify_reason=verdict.reason,
    )


# What replaces an upstream warehouse cell anywhere a message would otherwise render it.
_CELL_REDACTED = "[upstream value redacted]"

# Below this many characters a cell's rendering cannot be substituted out of a message without
# corrupting unrelated text: `5` or `42` occurs inside error codes, byte offsets, dates and
# column names, so replacing every run of it would produce a sentence that says something else.
# Such a message is WITHHELD WHOLE instead (`_cell_safe` returns `None`) — fail-closed, and the
# reviewer is told why rather than shown a mangled string.
#
# FOUR, not one and not ten, and the trade is deliberate. Withholding is always safe and always
# costs the diagnostic, which on this path is the whole point of the reason string; substituting
# is safe for values specific enough that an incidental match is not worth insuring against. A
# 4+ character aggregate, date or id is in that class; a 1-3 character count is not. Nothing
# leaks either way — the choice is only between "the message, redacted" and "no message".
_MIN_REDACTABLE_CELL = 4


def _cell_safe(text: str, cells: Any) -> str | None:
    """*text* with every upstream cell's rendering replaced, or `None` if it cannot be made safe.

    The cells are warehouse values this trial read to run the DAG. They are bound into SQL and
    discarded; nothing may put them on a wire or in a log (`trial_run`'s "structure, never
    values"). This is the read-back guard for the one path that can carry them out — a warehouse
    error quoting the query it failed on.
    """
    for cell in cells:
        rendered = str(cell)
        if len(rendered) < _MIN_REDACTABLE_CELL:
            return None
        text = text.replace(rendered, _CELL_REDACTED)
    return text


def _declared_footprint(generalization: Any) -> tuple[tuple[str, ...], str]:
    """The candidate's declared `uses` as a column-scope tuple, or `((), why)`.

    ⚠ TYPE-GUARDED, and it is the ONE read on this path that decides what a query is allowed to
    touch. `tuple(generalization.get("uses") or [])` accepted anything iterable, so a rehydrated
    STRING footprint — `"payroll.payroll_fact.gross_pay"` — was exploded into one
    single-character "column" per letter and handed to the probe as `column_scope`. The trial
    then reported `ok=True`, having proved something about a scope nobody declared. Harmless
    under the trial's own `SuppliedTokenMinter` (which ignores scope and returns the reviewer's
    token verbatim) and NOT harmless on the offline fallback to the scheduler's probe, whose
    minter posts that list to the IdP.

    Every OTHER read in this walk is type-guarded (`_trial_nodes` refuses a non-list
    `node_templates`, a non-dict `consumes`, a non-integer `order`); this one now is too. Damage
    and emptiness share `no_uses_scope`, because the refusal and the reviewer's next step are the
    same — there is no usable footprint to run inside — and the `detail` says which it was.
    """
    if not isinstance(generalization, dict):
        return (), "this candidate carries no generalization to read a footprint from"
    declared = generalization.get("uses")
    if declared is None or declared == []:
        return (), ""
    if not isinstance(declared, list) or not all(isinstance(c, str) and c for c in declared):
        return (), "the declared footprint ('uses') is not a list of column names"
    return tuple(declared), ""


@dataclass(frozen=True)
class _TrialNode:
    """One step of a composite, as the trial walks it.

    Its two halves live in two places on the candidate and are joined by `order` here:
    `generalization.node_templates` carries the AST-rewritten SQL, and the S3 plan's
    `composes` carries the wiring (`consumes`, `output`). A composite's top-level
    `sql_template` is `None` BY CONSTRUCTION (`generalize/builder.py`), so the node templates
    are the only SQL such a candidate has.
    """

    order: int
    sql_template: str
    consumes: dict[str, str]  # {placeholder: "$N.name"} — SCALAR refs only (see `_trial_nodes`)


# A composite whose shape this trial refuses to guess at. The payload is rehydrated JSON, so
# every field below is read defensively rather than trusted: `check_dag` proved these shapes
# at S4, but a store doc can carry anything and a projection must not raise.
_MALFORMED_COMPOSITE = "malformed_composite"
# EXACT STRING — the review card maps it to an explanation. A table intermediate needs the
# scratch materialization the probe has no side-channel for, and it is the same shape that
# cannot be promoted yet (`_provenance_uses` stamps `explain_ok=False` on it), so
# honestly-unsupported is the truthful answer rather than a degraded run.
_TABLE_INTERMEDIATE = "table_intermediate_unsupported"


def _trial_nodes(payload: dict[str, Any]) -> tuple[tuple[_TrialNode, ...], str, str]:
    """A composite's nodes IN DAG ORDER, or `((), reason, detail)` when it cannot be walked.

    DAG order IS `order` order: `check_dag` refuses a `feeds_from` edge pointing at a HIGHER
    order ("orders are a topological index"), which is the same invariant the executor's
    `_topo_order` computes. Sorting is therefore the whole topological sort, not an
    approximation of one.

    A TABLE intermediate is refused here rather than half-run — see `_TABLE_INTERMEDIATE`. Both
    spellings are caught: a bare `$N` consume, and a non-terminal node declaring a `table`
    output (the shape the loader forbids but a poisoned record could carry).

    ⚠ THE TWO HALVES MUST NAME THE SAME STEPS, and the check is derived from what the producer
    can emit rather than from what looks tidy. `_generalize_composite` walks `sorted(composes)`
    and appends EXACTLY ONE `NodeTemplate` per entry (and `check_dag` has already proved the
    orders unique), so a candidate carrying node templates has a BIJECTION between the two
    order sets — roots included, whose `composes` entry exists with an empty `consumes`. Nothing
    downstream removes it either: `redact_payload` rewrites string leaves and drops no keys.

    So every desync below is damage, and each has a wrong-answer to its name:

      * a `composes` order with no template — the missing step may be the TERMINAL one, and the
        walk would then gate an INTERMEDIATE's result and report `verify_passed` about the wrong
        node;
      * a template order with no `composes` entry — its `{total}` stops being an edge and
        becomes an ordinary slot, so the card grows a box and a reviewer who types a number gets
        a GREEN two-step run whose second step never saw the first one's output. That is the
        "green for a query nobody ran" shape `no_scalar_probe` refuses;
      * duplicate `composes` orders — the join key stops being a key, and a dict comprehension
        would silently take the last one. Refused exactly as duplicate TEMPLATE orders are; the
        two halves of one join cannot hold different standards.
    """
    generalization = payload.get("generalization")
    raw_templates = (
        generalization.get("node_templates") if isinstance(generalization, dict) else None
    )
    if not isinstance(raw_templates, list) or not raw_templates:
        return (), "no_template", ""
    templates: dict[int, str] = {}
    for doc in raw_templates:
        if not isinstance(doc, dict):
            return (), _MALFORMED_COMPOSITE, "a node template is not an object"
        order, sql = doc.get("order"), doc.get("sql_template")
        if not isinstance(order, int) or isinstance(order, bool):
            return (), _MALFORMED_COMPOSITE, "a node template has no integer 'order'"
        if not isinstance(sql, str) or not sql.strip():
            return (), _MALFORMED_COMPOSITE, f"step {order} carries no SQL template"
        if order in templates:
            return (), _MALFORMED_COMPOSITE, f"two node templates claim step {order}"
        templates[order] = sql
    raw_composes = payload.get("composes")
    if not isinstance(raw_composes, list):
        return (), _MALFORMED_COMPOSITE, "the composite carries no 'composes' wiring"
    plans: dict[int, dict[str, Any]] = {}
    for node in raw_composes:
        if not isinstance(node, dict):
            return (), _MALFORMED_COMPOSITE, "a 'composes' entry is not an object"
        order = node.get("order")
        if not isinstance(order, int) or isinstance(order, bool):
            return (), _MALFORMED_COMPOSITE, "a 'composes' entry has no integer 'order'"
        if order in plans:
            return (), _MALFORMED_COMPOSITE, f"two 'composes' entries claim step {order}"
        plans[order] = node
    if set(plans) != set(templates):
        missing_plan = sorted(set(templates) - set(plans))
        missing_sql = sorted(set(plans) - set(templates))
        return (
            (),
            _MALFORMED_COMPOSITE,
            "the DAG's wiring and its SQL disagree about which steps exist: "
            + (f"step(s) {missing_plan} have SQL but no wiring; " if missing_plan else "")
            + (f"step(s) {missing_sql} have wiring but no SQL" if missing_sql else ""),
        )
    terminal = max(templates)
    nodes: list[_TrialNode] = []
    for order in sorted(templates):
        plan = plans[order]
        output = plan.get("output")
        if output is not None and not isinstance(output, dict):
            return (), _MALFORMED_COMPOSITE, f"step {order} has a malformed 'output'"
        # `== "table"` per value rather than a set membership test: a rehydrated `output`
        # value can be an unhashable list, and hashing it would raise out of a reader whose
        # contract is a reason string.
        if isinstance(output, dict) and order != terminal:
            if any(kind == "table" for kind in output.values()):
                return (), _TABLE_INTERMEDIATE, f"step {order} passes a whole table downstream"
        raw_consumes = plan.get("consumes") or {}
        if not isinstance(raw_consumes, dict):
            return (), _MALFORMED_COMPOSITE, f"step {order} has a malformed 'consumes'"
        consumes: dict[str, str] = {}
        for placeholder, ref in raw_consumes.items():
            if not isinstance(placeholder, str) or not isinstance(ref, str):
                return (), _MALFORMED_COMPOSITE, f"step {order} has a malformed consume ref"
            if TABLE_CONSUME_REF.match(ref) is not None:
                return (), _TABLE_INTERMEDIATE, f"step {order} consumes step {ref[1:]} as a table"
            if SCALAR_CONSUME_REF.match(ref) is None:
                return (
                    (),
                    _MALFORMED_COMPOSITE,
                    f"step {order} consumes {ref!r}, which is neither consume grammar",
                )
            consumes[placeholder] = ref
        nodes.append(_TrialNode(order=order, sql_template=templates[order], consumes=consumes))
    return tuple(nodes), "", ""


# The statuses a parameterization revision may be proposed for and applied to.
#
# `needs_parameterization` is the form: entries are MISSING and the decline names which.
# `in_review` is a candidate a human is adjudicating and can see is wrong — a slot that should
# be frozen, a `why` that explains nothing. Both re-run the SAME `to_candidate` re-validation
# and the SAME write-router stages, so neither is a way around a check; the second one was
# impossible until kept candidates started carrying a `ValidationSnapshot`
# (`build_candidate_envelope`), because there was no accepted SQL to re-validate against.
#
# ⚠ NOT `validated` or `promoted`. Those have passed golden replay and may already be landed,
# so editing one means re-verifying and re-landing an artifact the corpus is serving — a
# different operation with a different blast radius, and `retract` is how that is expressed.
REVISABLE_STATUSES: tuple[str, ...] = (
    CandidateStatus.NEEDS_PARAMETERIZATION,
    CandidateStatus.IN_REVIEW,
)


# What replaces a reviewer's token anywhere it would otherwise be rendered or logged.
_REDACTED = "[token redacted]"


def _explain(exc: BaseException) -> str:
    """The most specific message inside *exc*, unwrapping exception groups.

    An `ExceptionGroup` stringifies as "unhandled errors in a TaskGroup (1 sub-exception)",
    which is what a reviewer saw for an expired or rejected token: nothing about the token, the
    warehouse, or what to do. The transport raises inside a task group, so the real cause — the
    403, the connection refusal — is one level down and is the only part worth showing.

    Walks nested groups, keeps the leaves, and de-duplicates: a fan-out that failed the same way
    five times should say so once.
    """
    leaves: list[str] = []

    def _walk(node: BaseException) -> None:
        inner = getattr(node, "exceptions", None)
        if inner:
            for child in inner:
                _walk(child)
            return
        text = f"{type(node).__name__}: {node}" if str(node) else type(node).__name__
        if text not in leaves:
            leaves.append(text)

    _walk(exc)
    return " / ".join(leaves) if leaves else str(exc)


def _scrub(text: str, token: str) -> str:
    """*text* with every recognisable run of the reviewer's token removed.

    ANY RUN, not just a leading one. The first version walked prefixes only (`token[:size]`),
    which handles the truncated-header case and misses the one the docstring actually named:
    a client that LINE-WRAPS the header quotes the prefix on one line and the REST on the next,
    and that remainder is the JWT's signature — the part worth protecting. A prefix is mostly
    algorithm boilerplate.

    Runs shorter than `_MIN_SECRET_RUN` are left alone: below that a "match" is header
    boilerplate every token shares (`eyJhbGciOi…`), so redacting it would blank harmless text
    without protecting anything.

    ⚠ WHAT THIS CANNOT DO is reach a re-encoded token — base64-of-base64, percent-encoding — and
    no string scrub can. That limit is accepted deliberately: the transports on this path quote
    headers verbatim, and the defence against the rest is that nothing here logs request bodies.
    """
    token = (token or "").strip()
    if not token:
        return text
    text = text.replace(token, _REDACTED)
    size = len(token)
    if size < _MIN_SECRET_RUN:
        return text
    # Slide a minimum-length window over the token; on a hit, grow the match as far as the
    # token keeps agreeing, replace it, and rescan — several fragments can appear separately.
    scanning = True
    while scanning:
        scanning = False
        for offset in range(size - _MIN_SECRET_RUN + 1):
            window = token[offset : offset + _MIN_SECRET_RUN]
            at = text.find(window)
            if at == -1:
                continue
            stop = offset + _MIN_SECRET_RUN
            while stop < size and text.startswith(token[offset : stop + 1], at):
                stop += 1
            text = text[:at] + _REDACTED + text[at + (stop - offset) :]
            scanning = True
            break
    return text


# Below this length a "prefix" of a JWT is header boilerplate shared by every token
# (`eyJ...`), so scrubbing it would redact harmless text without protecting anything.
_MIN_SECRET_RUN = 24


class ReviewInbox:
    """The `in_review` projection over a `CandidateStore` + the human transitions."""

    def __init__(
        self,
        store: CandidateStore,
        *,
        scheduler: PromotionScheduler | None = None,
        policy: PromotionPolicy | None = None,
        completer: ParameterizationCompleter | None = None,
        reviser: BlueprintReviser | None = None,
        minter: Any = None,
        mcp_client: Any = None,
        probe_factory: Any = None,
        knowledge_editor: KnowledgeEditor | None = None,
        knowledge_reviser: KnowledgeReviser | None = None,
        user_store: UserKnowledgeStore | None = None,
    ) -> None:
        self._store = store
        # The fail-to-review completion plane. OPTIONAL and default-absent, so an inbox
        # built without one behaves exactly as it did before the slice — except that a
        # completion attempt is refused LOUDLY (503) instead of silently doing nothing.
        # It is not folded into the scheduler: the scheduler owns transitions of a
        # candidate that already passed validation, and this one owns re-running the
        # validation itself.
        self._completer = completer
        # The LLM typing aid for that same form (design §C). OPTIONAL and default-absent,
        # like the completer — and, unlike the completer, absent changes NOTHING about what a
        # reviewer can accomplish: the raw-entries form still works. It writes nothing; see
        # `propose_revision`.
        self._reviser = reviser
        # The hand-authoring plane (`learning/mint`). OPTIONAL and default-absent like the two
        # above. Absent removes an ENTRY POINT rather than degrading one: with no minter an
        # expert cannot author a blueprint from a question, but nothing about reviewing the
        # mined ones changes. It writes through the completer, so it cannot exist without one.
        self._minter = minter
        # The runQuery transport for the reviewer-driven trial. The probe itself is built PER
        # REQUEST around the reviewer's own token (`_probe_for`) — only the transport is shared,
        # because it holds no authority.
        self._mcp_client = mcp_client
        # THE ONE WRITE PATH into a `global_knowledge` candidate's payload (K1/K2). OPTIONAL
        # and default-absent like the completer, and refusing LOUDLY on use for the same
        # reason: a reviewer who typed a correction and got a 200 that wrote nothing would
        # believe the corpus had been fixed. It is NOT folded into the completer — the
        # completer re-runs the BLUEPRINT validation and the write router, and this re-runs
        # intake validation and the scan; the two have neither a check nor a status in common.
        self._knowledge_editor = knowledge_editor
        # The LLM typing aid for that same edit. OPTIONAL and, like the blueprint reviser,
        # absent changes nothing about what a reviewer can ACCOMPLISH: the five fields are
        # still editable directly. What it costs is the one case it exists for — a fact whose
        # entity the reviewer would otherwise have to remove by hand while the card is
        # withholding the text they need to see.
        self._knowledge_reviser = knowledge_reviser
        # The per-user knowledge store, READ-ONLY from here (D17's deliberate exception —
        # design §D.1). The inbox never commits to it: `promote_user_knowledge` reads one
        # record and writes a CANDIDATE, so a promotion cannot alter the user's own facts.
        self._user_store = user_store
        # HOW A PROBE IS MADE FROM A TOKEN. Injectable because the real one needs a live MCP
        # transport, and the alternative — falling back to the scheduler's own probe when none
        # is wired — is exactly the silent substitution this plane refuses everywhere else: a
        # reviewer would see the same shape whether their token or the service's was used.
        self._probe_factory = probe_factory
        # The SINGLE approve/reject implementation (R4). Defaulted for an unwired
        # inbox; production injects the wired scheduler.
        self._scheduler = scheduler or PromotionScheduler(
            store,
            probe=_NoOpProbe(),
            hit_counts=_ZeroHitCounts(),
        )
        # Plan §4: the inbox needs exactly ONE knob off the promotion policy
        # (`review_score_cutoff`).
        #
        # It defaults to the SCHEDULER's policy, never to a fresh `PromotionPolicy()`.
        # The fresh-default version re-created, one level down, the exact defect this
        # slice was written to remove: a caller who built a correctly-configured
        # scheduler and passed it here would silently get cutoff 0.0 — a knob turned in
        # the environment and ignored at the surface it governs. `build_promotion_plane`
        # passes it explicitly, but a direct `ReviewInbox(store, scheduler=...)` is a
        # supported construction (both demo scripts and every test in this suite use it),
        # and correctness must not depend on the caller remembering.
        #
        # `self._scheduler` is always set by the line above, and an unwired inbox's
        # default scheduler carries a default policy — so this changes nothing for the
        # unwired case and removes the split for the wired one.
        self._policy = policy if policy is not None else self._scheduler.policy

    async def list(
        self,
        *,
        status: str = CandidateStatus.IN_REVIEW,
        limit: int = 100,
        order: Literal["asc", "desc"] = "asc",
    ) -> list[InboxItem]:
        """The inbox projection for *status* (default `in_review`, the review queue).

        The archive view passes `status=rejected` so the SAME projection serves the Archived tab,
        with `order="desc"` chosen explicitly by the caller — never inferred from the status string
        inside the store — so the LIMIT trims OLD history rather than present rejects.

        ONLY the review queue is RANKED (plan §4), by `novelty × groundedness² × session-quality`
        descending, MEASURED rows first, arrival order breaking ties, filtered by
        `review_score_cutoff`. The other listings are left alone on purpose: `rejected` is an ARCHIVE
        whose newest-first contract the LIMIT depends on; `validated` is the verify/promote worklist,
        whose rows have already been through a human once; `needs_parameterization` is a WORK list
        whose entry condition already answered "is this worth extracting".

        THE LIMIT IS APPLIED BY THE STORE, BEFORE THE RANKING — a real limitation rather than an
        oversight: the ranking inputs live inside the candidate document, so ranking the whole
        `in_review` population would mean fetching it all. Past 100 rows the caller ranks the oldest
        100, not the best 100; fixing it properly means ranking server-side, which needs the score
        materialized.
        """
        envelopes = await self._store.list_by_status(status, limit=limit, order=order)
        if status != CandidateStatus.IN_REVIEW:
            return [InboxItem.from_envelope(env) for env in envelopes]
        # Rank the ENVELOPES, then project — rather than projecting and then sorting the
        # items by a re-derived key. Two reasons, and the second is the one that bites:
        # the order and the projected score then come from ONE computation and cannot
        # disagree, and there is no id-keyed map to join the two lists back together (a
        # `candidate_id` read off a rehydrated doc with no type check need not even be
        # hashable, and building that map would have been the crash site).
        ranked = sorted(envelopes, key=rank_key)
        items = [InboxItem.from_envelope(env) for env in ranked]
        return self._apply_cutoff(items)

    def _apply_cutoff(self, items: list[InboxItem]) -> list[InboxItem]:
        """Drop review-queue rows below `review_score_cutoff` — MEASURED rows only.

        A row whose novelty or groundedness could not be measured carries a NEUTRAL 1.0 on that axis,
        and novelty's measured ceiling against a real corpus is ~0.47 — so filtering both groups on
        one number would hide the candidates we know most about and keep the ones we know nothing
        about, the knob doing the exact opposite of what its name says. A cutoff is a judgement about
        a score, and an unmeasured row has no score to judge; it is never in the way either, because
        the sort has already put every unmeasured row below every measured one.

        The consequence, stated rather than hidden: a queue dominated by unmeasured rows cannot be
        trimmed with this knob. That is a signal, not a defect — the fix is to wire the prior-art
        index. An operator who empties their own inbox is TOLD.
        """
        cutoff = self._policy.review_score_cutoff
        if cutoff <= 0.0 or not items:
            return items
        kept = [
            item for item in items if item.score.measured is False or item.score.score >= cutoff
        ]
        if not kept:
            _logger.warning(
                "review_score_cutoff=%.3f hid ALL %d row(s) of a non-empty review queue. "
                "The score is an ORDERING, not a percentage, and it does not use the top "
                "of its range: sentence-embedding cosines floor around 0.53, so novelty "
                "lives in roughly [0, 0.47] and a PERFECT candidate scores about 0.26. A "
                "moderate-looking cutoff hides everything. Set it from the scores you "
                "actually observe (they are on every item as `score`), or back to 0.0.",
                cutoff,
                len(items),
            )
        return kept

    async def _require(self, candidate_id: str, expected: str) -> CandidateEnvelope:
        env = await self._store.get(candidate_id)
        if env is None:
            raise InboxTransitionError(f"candidate {candidate_id!r} not found")
        if env.status != expected:
            raise InboxTransitionError(
                f"candidate {candidate_id!r} is {env.status!r}, expected {expected!r}"
            )
        return env

    async def approve(
        self,
        candidate_id: str,
        *,
        token: str = "",
        acknowledge_date_warnings: bool = False,
    ) -> CandidateEnvelope:
        """Human approve: `in_review → validated`, delegating to `apply_human_decision` (D17/R4).

        A guard that HOLDS — an unresolved `depends_on`, a missing generalization, a failed replay —
        leaves the candidate `in_review`. That is NOT a success, so a held approve surfaces as an
        `InboxTransitionError` carrying the hold reason rather than as an unchanged envelope.

        ⚠ THE REVIEWER'S OWN TOKEN IS REQUIRED FOR BLUEPRINTS. Their approval runs the golden
        replay against the live warehouse, and this surface never mints authority for that.
        Global knowledge and schema edits do not query the warehouse, so asking those reviewers
        for a warehouse credential would expand authority without using it — those types are
        approved with NO probe at all, rather than with the deployment principal's, because
        handing them that one would re-create exactly the fallback `_probe_for` refuses.

        NO FALLBACK to the deployment principal on a blank token, for the reason the trial gives:
        both paths return the same shape, so a reviewer would believe the blueprint had been
        proven against their access when it had been proven against somebody else's.
        """
        env = await self._require(candidate_id, CandidateStatus.IN_REVIEW)
        static = (env.payload.get("generalization") or {}).get("static_validation") or {}
        date_warnings = static.get("date_literal_warnings") or []
        if date_warnings and not acknowledge_date_warnings:
            raise InboxTransitionError(
                "approve_needs_date_warning_ack: this blueprint contains ambiguous "
                "date-like literals; inspect and acknowledge them before approval"
            )
        probe = self._probe_for(token) if env.type == "blueprint" else None
        if env.type == "blueprint" and probe is None:
            raise InboxTransitionError(
                f"approve_needs_token: approving {candidate_id!r} replays the blueprint against "
                "the live warehouse, and this surface mints no tokens — paste one you already "
                "hold"
            )
        decision = await self._scheduler.apply_human_decision(env, "approve", probe=probe)
        if decision.action != "approve":
            raise InboxTransitionError(f"approve held for {candidate_id}: {decision.reason}")
        return await self._store.get(candidate_id)

    async def reject(self, candidate_id: str) -> CandidateEnvelope:
        """Human reject: `in_review | needs_parameterization → rejected`. A NEGATIVE signal, not a delete.

        The row is retained for the S9 learner (D29). `needs_parameterization` is accepted because
        reject writes no content, so gating it would leave a row with NO terminal action at all —
        completable only by a human who may have decided the form has no honest answer, and otherwise
        clearable only by waiting out a 180-day TTL.

        RACE, stated because it is real and unclosed here: the guard reads the envelope and
        `apply_human_decision` writes it back, so a completion finishing in between is overwritten by
        this reject. That direction is the SAFE one — the human's "no" wins and nothing lands — where
        the opposite is refused by `completion.py::_guarded_put`. Both windows close properly only
        with a CAS on the candidate store.
        """
        env = await self._require_one_of(
            candidate_id,
            (CandidateStatus.IN_REVIEW, CandidateStatus.NEEDS_PARAMETERIZATION),
        )
        await self._scheduler.apply_human_decision(env, "reject")
        return await self._store.get(candidate_id)

    async def _require_one_of(
        self, candidate_id: str, expected: tuple[str, ...]
    ) -> CandidateEnvelope:
        env = await self._store.get(candidate_id)
        if env is None:
            raise InboxTransitionError(f"candidate {candidate_id!r} not found")
        if env.status not in expected:
            raise InboxTransitionError(
                f"candidate {candidate_id!r} is {env.status!r}, expected one of "
                f"{sorted(expected)!r}"
            )
        return env

    async def complete_parameterization(
        self,
        candidate_id: str,
        *,
        entries: list,
        replace_all: bool = False,
        rewritten_sql: str = "",
    ) -> CompletionResult:
        """FILL IN the form of a `needs_parameterization` candidate and re-run the pipeline.

        Guarded on `needs_parameterization` specifically — NOT on `in_review` — so this can never be
        used as a second, unvalidated route into an ordinary review item's payload; approve is
        guarded the other way round, so the two surfaces cannot be crossed. Returns the
        `CompletionResult` rather than an envelope, because "the form is still incomplete" is an
        outcome the caller must be able to SHOW, not an error to map to a status code.

        ⚠ The APPEND mode is why this guard stays an equality. `_merged_parameterization`
        concatenates, so a second submission — a double-click, a stale tab, a redelivery —
        would double the entries of a candidate that has already moved on. The status is what
        makes that unreachable. Editing an `in_review` candidate is a DIFFERENT operation with
        a different safety argument: see `apply_revision`, which is replace-only and therefore
        idempotent.

        ⚠ A `rewritten_sql` (§C.5) FORCES `replace_all`, whatever the request asked for, and that
        is not the surface being lenient — it is the surface agreeing with the completer, which
        refuses a rewrite without it. Every existing entry describes the OLD query, so appending
        to them can only produce a parameterization half about a string nobody has. Forcing it
        also restores the idempotence the append mode costs this verb, so the double-click the
        equality guard above protects against is harmless on this path for the ordinary reason
        rather than by luck.
        """
        env = await self._require(candidate_id, CandidateStatus.NEEDS_PARAMETERIZATION)
        if self._completer is None:
            raise InboxTransitionError(
                f"completion_unavailable: no validation plane is wired for "
                f"{candidate_id!r}, so the completed parameterization cannot be "
                "re-validated against the accepted SQL"
            )
        return await self._completer.complete(
            env,
            entries=entries,
            replace_all=replace_all or bool(sql_rewrite_of(env, rewritten_sql)),
            rewritten_sql=rewritten_sql,
        )

    async def propose_revision(
        self, candidate_id: str, *, feedback: str, allow_sql: bool = False
    ) -> ReviseProposal:
        """Ask the reviser for parameterization entries. WRITES NOTHING.

        `allow_sql` is the reviewer's §C.5 opt-in, passed straight through. It changes what the
        assistant may RETURN, never what this method does with it: the proposal still comes back
        for the reviewer to apply, and the caution the engine attaches is what tells them the
        result can no longer auto-land. The leakage refusal below stays IN FRONT of it — a
        candidate whose scan did not clear cannot have its literals quoted at a model whether or
        not the model would be allowed to rewrite them.

        Guarded on `REVISABLE_STATUSES` — the SAME guard as `complete_parameterization`, so the
        assistant is never offered toward an operation the completer would then refuse.

        Both statuses became reachable once `build_candidate_envelope` started stamping a
        `ValidationSnapshot` on kept candidates. Before that, `in_review` was structurally
        impossible rather than merely disallowed: the snapshot was written at DECLINE time
        only, so such a candidate had no accepted SQL to propose against. That was design
        §C.4's deferral, and stamping it is what §C.4 named as the unlock.

        ⚠ A candidate carrying no snapshot at all (extracted before that change) still degrades
        cleanly — `BlueprintReviser.propose` returns a proposal with a reason saying so, rather
        than raising.

        The proposal is returned, not applied. The reviewer applies it through `complete`,
        which is and remains the only write path into a candidate's payload.
        """
        env = await self._require_one_of(candidate_id, REVISABLE_STATUSES)
        if self._reviser is None:
            raise ReviserUnavailableError(
                f"revise_unavailable: no reviser is wired for {candidate_id!r}; the "
                "parameterization entries can still be supplied directly"
            )
        # ⚠ THE SAME WITHHOLDING RULE THE DECLINE DETAIL OBEYS (`InboxItem.decline_view`).
        #
        # A proposal is entity-BEARING BY CONSTRUCTION and cannot be redacted the way the
        # payload view and the judge verdict are: every entry carries a `locator.value` lifted
        # verbatim from the accepted SQL, the diff interpolates those values into its rows, and
        # the rationale is model prose about the unredacted query. Redacting them would also
        # destroy them — an entry whose value is `[redacted]` matches no literal and applies to
        # nothing.
        #
        # So this surface REFUSES where the others redact. Without it, a candidate whose scan
        # settled `quarantine` renders a card that withholds its decline detail and then hands
        # back the same literals the moment the reviewer clicks the assistant — through a route
        # added to make that form easier, past the one rule the form is careful about. An
        # UNSETTLED scan refuses for the stronger reason: nobody looked.
        if not _leakage_cleared(env):
            scan = env.entity_scan if isinstance(env.entity_scan, dict) else {}
            flagged = ", ".join(
                sorted(
                    {
                        f"{hit.get('field')} ({hit.get('kind')})"
                        for hit in (scan.get("hits") or [])
                        if isinstance(hit, dict) and hit.get("field")
                    }
                )
            )
            # ⚠ SAY ONLY WHAT IS TRUE AND ACTIONABLE. The first version of this message ended
            # "or clear the scan first", which named an operation that DOES NOT EXIST anywhere
            # in the plane — there is no re-scan or override action on any surface. It also
            # said "supply the entries directly", which is only possible on the FORM: a review
            # queue card has no entries box, so on `in_review` it advised a control that is not
            # on the page either. Both readings sent a reviewer looking for a button.
            #
            # What is true: the scan verdict is the gate, a reviewer cannot change it, and the
            # only way it is re-evaluated is a re-extraction of the session. Naming the flagged
            # FIELD AND KIND (never the span — that is the value being withheld) is what turns
            # this from a refusal into something a human can judge: `sql_template (person)` on
            # a query full of enum literals reads as the false positive it usually is.
            supply = (
                " You can still type the entries into the form below."
                if env.status == CandidateStatus.NEEDS_PARAMETERIZATION
                else ""
            )
            return self._reviser.refuse_withheld(
                env,
                reason=(
                    "the assistant is unavailable for this candidate: its entity scan settled "
                    f"{scan.get('result') or 'unsettled'}"
                    + (f" on {flagged}" if flagged else "")
                    + ". A proposal necessarily quotes the accepted SQL's literal values — the "
                    "same ones this row is withholding — so it is refused rather than redacted "
                    "(a redacted literal matches no predicate). The verdict is not something "
                    "this surface can change; it is re-evaluated only when the session is "
                    "re-extracted." + supply
                ),
            )
        return await self._reviser.propose(env, feedback=feedback, allow_sql=allow_sql)

    async def apply_revision(
        self, candidate_id: str, *, entries: list, rewritten_sql: str = ""
    ) -> CompletionResult:
        """Apply a revision to a candidate ALREADY under review (`in_review`).

        A SEPARATE verb from `complete_parameterization`, and the split is the safety argument
        rather than tidiness:

          * `complete` fills in a FORM. Its entries APPEND, because the reviewer is supplying
            what is missing and must not have to retype the model's valid classifications. That
            makes it non-idempotent, which is exactly why its guard is an equality against
            `needs_parameterization` — a double-click on a row that has moved on would double
            the entries.
          * this REPLACES the parameterization of a candidate whose form is already complete.
            The reviewer is correcting a role, not filling a gap, so `replace_all=True` is both
            the right semantics AND idempotent: applying the same entries twice yields the same
            payload. A stale tab or a double-click costs one redundant re-validation, never a
            corrupted array.

        `replace_all` is therefore NOT a parameter. Offering it would reintroduce the append
        mode on the one status whose guard cannot protect it.

        Everything else is the completer's usual path — `to_candidate` (totality walk included)
        then the full write-router stages — so the revision is RE-ADJUDICATED, not admitted: a
        payload whose leakage scan still fails routes back to review, and one whose dedup
        verdict changes re-routes on the writer's rules. Nothing here moves a candidate
        FORWARD; `approve` remains the only thing that does, and it stays `in_review`-only.

        `rewritten_sql` (§C.5) needs no special handling here for once: this verb is already
        replace-only, which is exactly what a rewrite requires. What it does change is the
        SUBJECT of the re-adjudication — the accepted SQL becomes assistant-authored, the
        snapshot is stamped `authored=True`, and the router's `hand_authored` rule holds the
        result at `in_review` rather than letting the writer's other rules decide.
        """
        env = await self._require(candidate_id, CandidateStatus.IN_REVIEW)
        if self._completer is None:
            raise InboxTransitionError(
                f"completion_unavailable: no validation plane is wired for "
                f"{candidate_id!r}, so the revised parameterization cannot be "
                "re-validated against the accepted SQL"
            )
        return await self._completer.complete(
            env, entries=entries, replace_all=True, rewritten_sql=rewritten_sql
        )

    # --- K1: editing a `global_knowledge` candidate under review ---------------

    async def _require_knowledge(self, candidate_id: str) -> CandidateEnvelope:
        """The row, or an `InboxTransitionError`. `in_review` AND `global_knowledge`.

        TWO GUARDS, and the second is not redundant with the first. `in_review` holds a
        blueprint, a schema edit and a knowledge fact, and the knowledge editor writes a payload
        checked ONLY by `validate_payload("global_knowledge", ...)` — pointed at a blueprint it
        would replace a generalization, a template and a parameterization with five text fields
        and call it valid, because it never read the blueprint reader. So the type guard is what
        keeps this from being a second, unvalidated write path into every other target, which is
        exactly what `complete_parameterization`'s own status guard exists to prevent in the
        other direction.
        """
        env = await self._require(candidate_id, CandidateStatus.IN_REVIEW)
        if env.type != KNOWLEDGE_TYPE:
            raise InboxTransitionError(
                f"candidate {candidate_id!r} is a {env.type!r}, expected {KNOWLEDGE_TYPE!r}; "
                "the knowledge edit surface writes a five-field fact and cannot check any "
                "other kind of payload"
            )
        return env

    async def propose_knowledge_revision(
        self, candidate_id: str, *, feedback: str
    ) -> KnowledgeProposal:
        """Ask the assistant for a corrected fact. WRITES NOTHING.

        ⚠ NO LEAKAGE REFUSAL IN FRONT OF THIS, and the asymmetry with `propose_revision` is
        deliberate rather than an omission. That method refuses when the scan did not clear,
        because a parameterization proposal necessarily QUOTES the flagged literals and a
        redacted literal matches no predicate. Here the flagged text is the thing being removed,
        so refusing on a dirty scan would switch the assistant off in precisely the case it
        exists for — a knowledge card whose statement names somebody, which today has exactly
        one action (`reject`).

        The withholding rule is kept; it just moves to the OUTPUT. `KnowledgeReviser` scans its
        own draft and returns a reason instead of a payload when the draft still trips the
        scanner, so nothing entity-bearing crosses to the browser either way — see
        `revise/knowledge.py`.

        The proposal is returned, not applied. `apply_knowledge_edit` remains the only write.
        """
        env = await self._require_knowledge(candidate_id)
        if self._knowledge_reviser is None:
            raise ReviserUnavailableError(
                f"revise_unavailable: no knowledge assistant is wired for {candidate_id!r}; "
                "the five fields can still be edited directly"
            )
        return await self._knowledge_reviser.propose(env, feedback=feedback)

    async def apply_knowledge_edit(
        self, candidate_id: str, *, payload: dict[str, Any]
    ) -> KnowledgeEditResult:
        """Write a corrected fact onto a `global_knowledge` candidate under review (§B/§C.2).

        THE ONLY WRITE into this payload, whether the reviewer pasted the assistant's draft,
        hand-edited it, or typed one from scratch — the two-step the rework doc's §C.3
        establishes for blueprints, kept here for the same reason: a model's output must face
        the identical checks a human's does, and the way to guarantee that is to give them one
        door.

        It is RE-ADJUDICATION, not admission: intake validation runs (so the closed key set
        holds), the leakage scan re-settles over the NEW text (so the card's withholding and
        the approve guard are reading a verdict about what is actually stored), and the row
        stays `in_review`. Nothing here moves a candidate forward — `approve` remains the only
        thing that does.
        """
        env = await self._require_knowledge(candidate_id)
        if self._knowledge_editor is None:
            raise KnowledgeEditorUnavailableError(
                f"knowledge_edit_unavailable: no knowledge write plane is wired for "
                f"{candidate_id!r}, so the edited fact cannot be re-validated or re-scanned"
            )
        # ⚠ `editor.can_scan` IS DELIBERATELY NOT CHECKED HERE, and the promote path below
        # deliberately does check it (design §F.1.a). The difference is the INPUT. An edit that
        # nobody scanned is DEGRADED BUT HONEST: the text is what the reviewer just typed into
        # a form they were looking at, so the queue shows them their own sentence, and the
        # `pending` sentinel keeps the row unapprovable until a deployment with a gate settles
        # a verdict on it. Refusing the edit as well would take away the one action available
        # on an offline dev inbox in exchange for nothing.
        return await self._knowledge_editor.admit(
            env, payload=payload, route_reason=ROUTE_REASON_EDITED
        )

    # --- K2: a user fact, promoted --------------------------------------------

    async def list_user_knowledge(
        self, user_id: str, *, limit: int = 100
    ) -> tuple[UserKnowledgeView, ...]:
        """ONE named user's private facts, plus whether each is already promoted (§D.1).

        ⚠ THIS IS THE D17 AMENDMENT, recorded rather than hidden. `learning/user/store.py` says
        a per-user fact is "surfaced only in that user's context", and the reviewer role now
        sees a named user's — on the same privileged, token-guarded surface that already shows
        audit quotes, trial-run rows and unredacted SQL. The narrowing that keeps it an
        exception rather than a hole: the store's PER-USER read is the only one used, no
        cross-user listing is added to the Protocol, and the reviewer must NAME the user. An
        empty id is refused, never read as "all users" — that single default is the difference
        between an exception and a bulk export.

        The records come back AS THEY ARE: they are entity-bearing by contract, so there is
        nothing to redact toward (a redacted per-user fact is not a fact). They are never
        indexed, scored or written back by this path.
        """
        if not (user_id or "").strip():
            raise ValueError(
                "user_id is required to list per-user knowledge: these records are "
                "entity-bearing and are readable one named user at a time, never in bulk"
            )
        if self._user_store is None:
            raise UserKnowledgeUnavailableError(
                "user_knowledge_unavailable: no per-user knowledge store is wired in this "
                "deployment, so a user's facts cannot be read here"
            )
        records = await self._user_store.list_for_user(user_id, limit=limit)
        return tuple([await self._with_promotion(record) for record in records])

    async def _with_promotion(self, record: UserKnowledgeRecord) -> UserKnowledgeView:
        """One record plus its promoted candidate, if it has one.

        A `get` on the DETERMINISTIC id rather than a scan — which is the whole reason the id is
        deterministic (`mint_promoted_candidate_id`). It costs one KV read per row and answers
        the only question the card needs: is this button still live.
        """
        env = await self._store.get(mint_promoted_candidate_id(record.record_id))
        return UserKnowledgeView(record=record, promoted=env)

    async def promote_user_knowledge(self, user_id: str, record_id: str) -> PromotedUserKnowledge:
        """Lift one user's private fact into a `global_knowledge` candidate under review (§D.2).

        ⚠ BOTH IDS MUST AGREE. A record id is guessable — `userknow::<user>::<candidate>` — and
        the `user_id` in the request is the one the reviewer was LOOKING AT, so a record whose
        owner differs is a 404 rather than a promotion. Without that check the surface would
        promote any user's fact from a URL, which is a much wider read than the named-user
        listing this is supposed to be a button on.

        IDEMPOTENT BY CONSTRUCTION. The candidate id is derived from the record id, so a second
        press finds the existing row and returns it with `already=True` — no duplicate, no
        status move, whatever status it has reached. That matters more than it sounds: this is
        a button on a list, and the alternatives (a random id, or a scan for a matching payload)
        would either file a second review row per click or need a search this store cannot do.

        The row lands `in_review` with its verdict stamped, and the verdict will usually NOT be
        `pass` — the fact was rerouted into the user store in the first place BECAUSE it carried
        an entity. That is the designed outcome, not a failure: the card withholds the flagged
        text, and the knowledge assistant is the next click.

        ⚠ AND IF NOTHING CAN SCAN IT, THERE IS NO PROMOTION (§F.1.a). "Usually not `pass`" is
        the whole reason: a deployment with no S5 gate would stamp the `pending` sentinel, the
        listing renders that as a green `pass`, and the fact this button moves is entity-bearing
        by construction. So an editor that cannot scan is a 503 here, while the same editor
        still serves K1 — see the comment in `apply_knowledge_edit`.
        """
        if not (user_id or "").strip() or not (record_id or "").strip():
            raise ValueError("both user_id and record_id are required to promote a fact")
        if self._user_store is None:
            raise UserKnowledgeUnavailableError(
                "user_knowledge_unavailable: no per-user knowledge store is wired in this "
                "deployment, so there is nothing to promote from"
            )
        if self._knowledge_editor is None:
            raise KnowledgeEditorUnavailableError(
                "knowledge_edit_unavailable: no knowledge write plane is wired in this "
                "deployment, so a promoted fact could not be validated or scanned"
            )
        if not self._knowledge_editor.can_scan:
            # ⚠ REFUSED, NOT DEGRADED (design §F.1.a). This posture is reachable in a real
            # deployment — real `USER_KNOWLEDGE_*` credentials give the inbox a live per-user
            # store, while an unreadable catalog gives it no write-router pipeline, so the
            # editor is built with no stages beside a store full of real facts. Admitting
            # through it would stamp the `pending` sentinel, which `inbox/models.py::
            # _leakage_view` renders to the card as a green `pass`, and a raw per-user fact
            # would sit on the SHARED review queue under a verdict nobody reached.
            raise KnowledgeEditorUnavailableError(
                "knowledge_scan_unavailable: this fact is entity-bearing by construction — it "
                "was written to a private per-user store BECAUSE it named someone — and no "
                "leakage scanner is wired in this deployment, so it cannot be put on the "
                "shared review queue. Promote it from a deployment whose write-router "
                "pipeline includes the S5 gate; editing an existing knowledge candidate still "
                "works here."
            )
        record = await self._user_store.get(record_id)
        if record is None or record.user_id != user_id:
            # ONE message for both, on purpose: distinguishing "no such record" from "not your
            # user's record" would turn this endpoint into an oracle for which record ids exist
            # in other users' stores, which is the read the owner check exists to prevent.
            raise InboxTransitionError(
                f"user knowledge record {record_id!r} for user {user_id!r} not found"
            )

        candidate_id = mint_promoted_candidate_id(record_id)
        existing = await self._store.get(candidate_id)
        if existing is not None:
            return PromotedUserKnowledge(
                candidate_id=existing.candidate_id,
                status=existing.status,
                already=True,
                entity_scan=entity_scan_view(existing.entity_scan),
            )
        result = await self._knowledge_editor.admit(
            _promoted_envelope(record, candidate_id),
            payload=_promoted_payload(record),
            route_reason=ROUTE_REASON_PROMOTED,
            # ⚠ THE RACE GUARD FOR A ROW THAT DOES NOT EXIST YET. `guarded_put` re-reads the id
            # and requires it to be STILL ABSENT, so "somebody else promoted this between the
            # check above and now" is a 409 rather than a silent overwrite of their row — the
            # collision the deterministic id makes possible the moment two reviewers are
            # looking at the same user.
            expect_absent=True,
        )
        return PromotedUserKnowledge(
            candidate_id=result.envelope.candidate_id,
            status=result.envelope.status,
            already=False,
            entity_scan=entity_scan_view(result.entity_scan),
        )

    def mint_schema(self) -> dict[str, Any]:
        """What the minting form offers: the tables an expert may pick, and their columns.

        The COLUMNS are included so the page can show what a table actually has before the
        expert commits to it. They are catalog metadata — names and nothing else, no rows and no
        values — so this crosses to a browser on the same footing as the rest of the catalog,
        which the runtime already puts in a prompt.
        """
        if self._minter is None:
            return {"available": False, "tables": [], "columns": []}
        return {
            "available": True,
            "tables": list(self._minter.tables),
            "columns": list(self._minter.catalog_columns),
        }

    async def mint_prior_art(self, question: str) -> tuple[dict[str, Any], ...]:
        """Artifacts that may already answer *question*. Reads only; mints nothing."""
        if self._minter is None:
            return ()
        return await self._minter.find_prior_art(question)

    async def mint_blueprint(self, request: Any) -> Any:
        """Draft one hand-authored blueprint onto the review queue. See `learning/mint`.

        The inbox owns this rather than the page calling the minter directly, for the reason
        every other write here is owned: the review queue is access-controlled, and a second
        door into it that did not go through the same guard would be a second thing to keep
        true. Nothing is promoted; the result is a candidate the reviewer then works on with
        the surfaces that already exist.
        """
        if self._minter is None:
            # `MintUnavailableError`, NOT `InboxTransitionError`. The two map to different
            # statuses and only one of them is true here: a transition error is a 409, meaning
            # "the row is in the wrong state", and there is no row. This is a 503 — the
            # deployment has no minting plane — which is the same answer the completer gives
            # for the same shape of absence. Imported locally because `learning.mint` imports
            # this package's completer, and a module-level import would close the cycle.
            from ..mint import MintUnavailableError

            raise MintUnavailableError(
                "mint_unavailable: no hand-authoring plane is wired in this deployment, so a "
                "blueprint cannot be drafted from a question here"
            )
        return await self._minter.mint(request)

    def _probe_for(self, token: str) -> Any:
        """A probe bound to the REVIEWER'S token, or `None` when they supplied none.

        Built per request and thrown away, because the credential is. It deliberately does NOT
        fall back to the scheduler's own probe when the token is blank: that fallback would make
        an empty box silently run as the deployment principal, so a reviewer would believe they
        had tested their own access when they had tested somebody else's — and the failure is
        invisible, because both paths return the same shape.
        """
        if not (token or "").strip():
            # THE RULE THAT MATTERS, and it never degrades: no token, no run. A blank box must
            # not quietly execute as the deployment principal, because both paths return the
            # same shape and the reviewer would believe they had proven something about their
            # own access.
            return None
        if self._probe_factory is not None:
            return self._probe_factory(token)
        if self._mcp_client is None:
            # NO WAREHOUSE TRANSPORT IN THIS PROCESS — an offline dev inbox or a test. There is
            # nothing to build a probe from, so the wired probe is used instead. This is the one
            # place a reviewer's token does not reach the warehouse, and it is bounded to
            # deployments that have no warehouse: `_build_inbox_from_env` always passes a
            # `RealMCPClient`, so the branch is unreachable in production. Logged at WARNING
            # because it is not visible from the 200 the reviewer gets.
            _logger.warning(
                "review inbox: no MCP transport is wired, so the reviewer's token cannot be "
                "used — falling back to the configured probe. This is an offline/dev wiring; "
                "in a real deployment the token is what runs the query."
            )
            return self._scheduler.probe
        from ..promotion.token_minter import SuppliedTokenMinter
        from ..promotion.warehouse_probe import MCPWarehouseProbe

        return MCPWarehouseProbe(
            mcp_client=self._mcp_client, token_minter=SuppliedTokenMinter(token)
        )

    async def trial_run(
        self, candidate_id: str, *, bindings: dict[str, Any], token: str = ""
    ) -> TrialRunResult:
        """Run this blueprint with REVIEWER-CHOSEN slot values and report what came back.

        The question a reviewer actually has before approving — "does it still run, and does it
        produce the shape I expect, with values I chose" — which no existing surface answered.
        The promotion gate replays with SYNTHETIC samples, and that is exactly how a badly-typed
        `period` sample went unnoticed until it blocked every approve.

        ⚠ STRUCTURE, NEVER VALUES. Columns, a row count and a distinct-grain count come back;
        rows do not. That is `replay.py`'s rule verbatim — "there is no value oracle here, and
        adding one would breach D17" — and it holds here for the same reason: this surface is
        access-controlled for CANDIDATES, which are redacted artifacts, and returning warehouse
        rows would quietly turn it into a data-browsing surface. Whether it should become one is
        a separate decision, not a side effect of adding a trial button.

        ⚠ THE REVIEWER SUPPLIES THE TOKEN, AND THIS SURFACE NEVER MINTS ONE. `token` is a
        credential the reviewer already holds, pasted into the page and used for exactly this
        request. A review surface that could mint warehouse authority would be a privilege
        escalation dressed as a convenience: reaching the inbox would become a way to obtain a
        warehouse token, which is not what being allowed to review candidates is supposed to
        grant. So the trial borrows authority rather than creating it.

        WHAT THAT COSTS, stated plainly: the token carries whatever scope its holder was given
        rather than this blueprint's `uses`, so the MCP's D57 column teeth do not bite during a
        trial. The footprint is still enforced — statically, at LANDING, by
        `_assert_template_reads_within_uses`, which no pasted token can influence. A trial
        therefore proves "this SQL runs and returns this shape for this principal", and NOT
        "the declared footprint is honest". See `SuppliedTokenMinter`.

        The transport is otherwise the replay's own: the same `MCPWarehouseProbe` over the same
        `runQuery`, so a trial still predicts the real gate's structural verdict.

        Allowed on `in_review` and `validated`: the two states where a human is deciding whether
        this artifact should go further. A `needs_parameterization` candidate has no template to
        bind, and says so rather than failing obscurely.

        A COMPOSITE takes the second branch. Its top-level `sql_template` is `None` by
        construction, so reading only that field answered `no_template` for every DAG the loop
        or the minting page has ever produced — a review card with no inputs and a button that
        could not work. See `_trial_run_composite`.
        """
        env = await self._require_one_of(
            candidate_id, (CandidateStatus.IN_REVIEW, CandidateStatus.VALIDATED)
        )
        generalization = env.payload.get("generalization")
        template = generalization.get("sql_template") if isinstance(generalization, dict) else None
        if not isinstance(template, str) or not template.strip():
            return await self._trial_run_composite(env, bindings=bindings, token=token)

        required = sorted(referenced_slots(template))
        missing = [name for name in required if not str(bindings.get(name, "")).strip()]
        if missing:
            return TrialRunResult(ok=False, reason="missing_bindings", missing=tuple(missing))
        try:
            sql = bind_template(template, {k: bindings[k] for k in required})
        except TemplateBindError as exc:
            return TrialRunResult(ok=False, reason=f"bind_failed:{exc}")

        uses, uses_detail = _declared_footprint(generalization)
        if not uses:
            # The same backstop `golden_replay` carries: an empty `uses` would mint an
            # UNRESTRICTED token, running reviewer-supplied input against live ClickHouse with
            # no column scope. Refuse with an honest reason rather than widen the scope.
            return TrialRunResult(ok=False, reason="no_uses_scope", detail=uses_detail)

        grain = _result_grain_columns(env.payload)
        probe = self._probe_for(token)
        if probe is None:
            return TrialRunResult(ok=False, reason="no_token")
        try:
            result = await probe.run(sql, grain_columns=grain, column_scope=uses)
        except Exception as exc:  # noqa: BLE001 — a trial is diagnostic; it may not 500 a review
            # ⚠ SCRUBBED BEFORE IT GOES ANYWHERE. The reviewer's bearer token is on this request,
            # and an HTTP client's exception text routinely quotes the request it failed on —
            # URL, headers, body. Relaying that verbatim put the token in the JSON the browser
            # renders AND in this process's log, which is exactly how a credential outlives the
            # one request it was borrowed for. The message is still useful; it just cannot carry
            # the secret. Applied to the log line too, for the same reason.
            safe = _scrub(_explain(exc), token)
            _logger.info("trial run for %s failed: %s", candidate_id, safe[:400])
            return TrialRunResult(ok=False, reason="warehouse_error", detail=safe[:400])

        return _trial_verdict(env.payload, grain, result)

    async def _trial_run_composite(
        self, env: CandidateEnvelope, *, bindings: dict[str, Any], token: str
    ) -> TrialRunResult:
        """Trial one SCALAR-PASSING composite: walk the DAG, node by node, in order.

        The runtime `BlueprintExecutor` is the oracle this mirrors — bind slots plus upstream
        scalar `consumes`, run each node, and gate the TERMINAL node's result on D56 — with two
        deliberate narrowings, because a trial has a probe rather than the tool dispatcher:

          * A TABLE intermediate is refused (`table_intermediate_unsupported`). Passing a whole
            result downstream needs the D93 scratch side-channel the probe does not have, and
            that same shape cannot be promoted today either, so an honest refusal is the whole
            truth rather than a degraded run.
          * Each upstream node is read as ONE CELL, through the `ScalarCellProbe` port. A node
            that returns anything else fails `scalar_shape` — the same fail-closed rule as
            `executor._extract_scalar_output`, and for the same reason: the D56 gate guards only
            the terminal node, so an arbitrary cell bound from a fanned-out intermediate would
            return a "verified" wrong answer.

        NOT the promotion replay's `_pick_template`, which runs the terminal node ALONE. That is
        right for a structure oracle over synthetic samples and wrong here: with no upstream run,
        the terminal template's `{total}` is unbound, so the reviewer would either be asked to
        type a value the blueprint computes for itself or get a bind failure.
        """
        nodes, reason, detail = _trial_nodes(env.payload)
        if reason:
            return TrialRunResult(ok=False, reason=reason, detail=detail)

        # What each node needs FROM THE REVIEWER, and what it gets from upstream. A declared
        # consume its template does not reference is IGNORED (`executor._node_bindings`' rule),
        # so an unreferenced edge neither demands a value nor costs a warehouse read.
        required: set[str] = set()
        consumed: dict[int, set[str]] = {}
        for node in nodes:
            refs = referenced_slots(node.sql_template)
            filled = {ph for ph in node.consumes if ph in refs}
            required |= refs - filled
            for placeholder in filled:
                ref = node.consumes[placeholder]
                match = SCALAR_CONSUME_REF.match(ref)
                consumed.setdefault(int(match.group(1)), set()).add(ref)
        for order in sorted(consumed):
            if len(consumed[order]) > 1:
                # The runtime binds several scalars off one wide row; this trial reads ONE
                # cell per step, so it would have to bind that cell to both names. Refuse
                # rather than pass the same value twice under different names.
                return TrialRunResult(
                    ok=False,
                    reason="scalar_shape",
                    detail=(
                        f"step {order} is consumed as {len(consumed[order])} separate scalars; "
                        "a trial reads one cell per step"
                    ),
                )

        missing = sorted(name for name in required if not str(bindings.get(name, "")).strip())
        if missing:
            return TrialRunResult(ok=False, reason="missing_bindings", missing=tuple(missing))

        uses, uses_detail = _declared_footprint(env.payload.get("generalization"))
        if not uses:
            # The same backstop the single path and `golden_replay` carry: an empty `uses`
            # would mint an UNRESTRICTED token. Refuse rather than widen the scope.
            return TrialRunResult(ok=False, reason="no_uses_scope", detail=uses_detail)
        probe = self._probe_for(token)
        if probe is None:
            return TrialRunResult(ok=False, reason="no_token")
        if consumed and not hasattr(probe, "run_cell"):
            # An offline/dev inbox falls back to the scheduler's probe, which is a structure
            # oracle only. Say so: the alternative is binding a synthetic value into the
            # consumer and reporting green for a query nobody ran.
            return TrialRunResult(
                ok=False,
                reason="no_scalar_probe",
                detail=(
                    "this deployment's warehouse probe cannot read an intermediate value, so a "
                    "multi-step blueprint cannot be trialled here"
                ),
            )

        grain = _result_grain_columns(env.payload)
        terminal = nodes[-1].order
        values: dict[str, Any] = {}  # "$N.name" → the cell that step returned
        result: ProbeResult | None = None
        for node in nodes:
            refs = referenced_slots(node.sql_template)
            node_bindings: dict[str, Any] = {}
            for placeholder, ref in node.consumes.items():
                if placeholder not in refs:
                    continue
                if ref not in values:
                    return TrialRunResult(
                        ok=False,
                        reason=_MALFORMED_COMPOSITE,
                        detail=f"step {node.order} consumes {ref}, which no earlier step produced",
                    )
                node_bindings[placeholder] = values[ref]
            for name in refs - set(node_bindings):
                node_bindings[name] = bindings[name]
            try:
                sql = bind_template(node.sql_template, node_bindings)
            except TemplateBindError:
                # ⚠ THE EXCEPTION TEXT IS WITHHELD HERE, where the single path quotes it. The
                # difference is whose data is in it: on that path every binding is a value the
                # reviewer typed, and on this one a binding can be a warehouse CELL this trial
                # read from an upstream step (`bind_template` renders an offending value into
                # its message). This surface returns structure, never values.
                return TrialRunResult(
                    ok=False,
                    reason="bind_failed",
                    detail=f"step {node.order} could not be bound",
                )
            try:
                if node.order == terminal:
                    result = await probe.run(sql, grain_columns=grain, column_scope=uses)
                elif node.order in consumed:
                    cell = await probe.run_cell(sql, column_scope=uses)
                    if cell is None:
                        return TrialRunResult(
                            ok=False,
                            reason="scalar_shape",
                            detail=(
                                f"step {node.order} is consumed as a single value but did not "
                                "return exactly one non-empty cell"
                            ),
                        )
                    values[next(iter(consumed[node.order]))] = cell
                else:
                    # Neither terminal nor consumed — nothing downstream needs its value, but
                    # the executor still runs it, so a trial that skipped it would report green
                    # on a DAG containing a step that does not execute.
                    await probe.run(sql, grain_columns=(), column_scope=uses)
            except Exception as exc:  # noqa: BLE001 — a trial is diagnostic; it may not 500 a review
                # TWO scrubs, and the second is what a composite added. The first is the single
                # path's: the reviewer's bearer token is on this request and a client's exception
                # text quotes the request it failed on. The second is the same rule applied to
                # the OTHER secret this walk handles — the SQL a consumer runs has an upstream
                # warehouse cell rendered into it as a literal, and a warehouse routinely quotes
                # the failing query back ("Cannot parse Date from String '2026-01-05'"), so the
                # governed value the `bind_failed` branch above withholds would walk out through
                # the error message instead. Applied to the LOG for the same reason: a value that
                # reaches a log line has outlived the request it was read for.
                safe = _cell_safe(_scrub(_explain(exc), token), values.values())
                if safe is None:
                    safe = (
                        f"step {node.order} failed at the warehouse. The message is withheld: it "
                        "may quote the query, which carries a value read from an earlier step, "
                        "and that value is too short to remove without destroying the text."
                    )
                _logger.info(
                    "trial run for %s failed at step %s: %s",
                    env.candidate_id,
                    node.order,
                    safe[:400],
                )
                return TrialRunResult(ok=False, reason="warehouse_error", detail=safe[:400])

        if result is None:  # unreachable: the terminal is the last node walked
            return TrialRunResult(
                ok=False, reason=_MALFORMED_COMPOSITE, detail="no terminal step ran"
            )
        return _trial_verdict(env.payload, grain, result)

    async def attest_scan(self, candidate_id: str, *, note: str) -> CandidateEnvelope:
        """Record a reviewer's statement that a leakage finding is a FALSE POSITIVE.

        Motivated by a real case: an NER scanner flagged an 8-character leave-type enum inside
        `event_type = '<...> Request'` as a `person`, quarantining a time-off blueprint with no
        way to unblock it. Nothing in the plane could clear a verdict, so a false positive
        blocked the assistant permanently.

        WHAT IT DOES NOT DO, and each is deliberate:

          * it does NOT rewrite `entity_scan`. The scanner's finding is the record of what a
            machine saw; a human disagreeing is a second fact stored beside it, so both survive
            and the disagreement itself is queryable;
          * it does NOT make the candidate auto-promotable. `_entity_scan_is_clean` — the
            AUTOMATIC edge, which exists for the path where "nobody is looking there" — never
            consults it;
          * it does NOT stop redaction. The strip costs nothing if the attestation is right.

        The one gate it clears is `inbox/models.py::_leakage_cleared`: the assistant and the
        decline-detail display, both of which a human is looking at when it happens.

        REQUIRES A SETTLED, NON-PASS VERDICT. Attesting to an unsettled scan would be a human
        vouching for content NOBODY has looked at, which is the opposite of the point; a clean
        pass has nothing to attest to. And the attestation binds to THESE findings, so it
        lapses the moment the scan re-settles differently.
        """
        env = await self._require_one_of(candidate_id, REVISABLE_STATUSES)
        scan = env.entity_scan if isinstance(env.entity_scan, dict) else {}
        if not LeakageVerdict.is_settled(scan):
            raise InboxTransitionError(
                f"candidate {candidate_id!r} has no SETTLED leakage verdict to attest to — "
                "attesting to an unscanned candidate would vouch for content nobody has "
                "looked at"
            )
        if scan.get("result") == "pass":
            raise InboxTransitionError(
                f"candidate {candidate_id!r} already has a clean leakage pass; there is "
                "nothing to override"
            )
        hits = scan.get("hits") if isinstance(scan.get("hits"), list) else []
        attestation = LeakageAttestation(
            scan_fingerprint=leakage_fingerprint(scan),
            attested_at=now_iso(),
            note=note,
            hit_count=len(hits),
        )
        _logger.warning(
            "leakage OVERRIDE on %s: a reviewer attested %d finding(s) (%s) are false "
            "positives — reason: %s. The verdict itself is unchanged and the candidate is "
            "still not auto-promotable.",
            candidate_id,
            len(hits),
            ", ".join(
                sorted({f"{h.get('field')}/{h.get('kind')}" for h in hits if isinstance(h, dict)})
            )
            or "none",
            note,
        )
        await self._store.put(replace(env, leakage_attestation=attestation))
        return await self._store.get(candidate_id)

    async def retract(self, candidate_id: str) -> CandidateEnvelope:
        """Retract a promoted artifact: `validated → retired` (a leak/drift pull).

        DELEGATES to the single `apply_retract` path, so the leak PULL stamps the landed neo4j node
        `retired` (fail-open) BEFORE the store retire and the recall filter excludes it immediately.
        The physical index removal and the D25 exposure trace remain S10; the STAMP is what closes
        the recall exposure here and now.
        """
        env = await self._require(candidate_id, CandidateStatus.VALIDATED)
        await self._scheduler.apply_retract(env)
        return await self._store.get(candidate_id)

    async def verify(self, candidate_id: str) -> tuple[CandidateEnvelope, bool]:
        """VERIFY an auto-landed learning node (Phase-3): flip `verified` on node AND envelope.

        Requires the current status to be `validated` — every validated candidate in the store is
        `source='learning'` by construction. DELEGATES to `apply_verify`. Returns
        `(env, node_stamped)`, where `node_stamped` is False when the neo4j write did not land (no
        writer, never landed, or a fail-open error) so the caller can prompt a re-verify; the
        envelope reads `verified=true` regardless, and a re-verify converges the node.
        """
        env = await self._require(candidate_id, CandidateStatus.VALIDATED)
        _decision, node_stamped = await self._scheduler.apply_verify(env)
        return await self._store.get(candidate_id), node_stamped

    async def promote(
        self,
        candidate_id: str,
        *,
        doc_id: str | None = None,
        title: str | None = None,
    ) -> PromotionEmit:
        """PROMOTE a verified learning node (Phase-3): emit the MCP-format YAML for a MANUAL PR.

        The FIRST promote (from `validated`, requiring `verified`) also moves the candidate to
        `promoted`; a re-promote RE-EMITS the same YAML with NO status move, so an abandoned or lost
        PR can always be regenerated. Any other status is a fail-loud transition error. The YAML `id`
        is the landing id VERBATIM so a later reseed flips THAT SAME node instead of duplicating it;
        `doc_id`/`title` are optional knowledge refinements and `id` can NEVER be overridden. The
        move is OPTIMISTIC: the actual `learning → mcp` reseed happens only when the human MERGES,
        and until then the node stays excluded from recall.
        """
        env = await self._store.get(candidate_id)
        if env is None:
            raise InboxTransitionError(f"candidate {candidate_id!r} not found")
        # Idempotent re-emit for an already-promoted candidate: regenerate the YAML with
        # NO status move (pure build), so a lost/abandoned PR can always be recreated.
        if env.status == CandidateStatus.PROMOTED:
            return build_promotion_emit(env, doc_id=doc_id, title=title)
        if env.status != CandidateStatus.VALIDATED:
            raise InboxTransitionError(
                f"candidate {candidate_id!r} is {env.status!r}, expected 'validated' or 'promoted'"
            )
        if not env.verified:
            raise InboxTransitionError(
                f"candidate {candidate_id!r} is not verified; verify it before promoting"
            )
        # Emit FIRST (pure — raises on a malformed/non-landable candidate before any
        # store move), then move to the terminal `promoted` state via the single writer.
        emit = build_promotion_emit(env, doc_id=doc_id, title=title)
        decision = await self._scheduler.apply_promote(env)
        if decision.action != "promote_emit":
            # A held promote (e.g. a racing status change) must NOT return 200 + YAML with
            # the candidate left validated — surface the hold reason fail-loud (mirrors
            # `approve`).
            raise InboxTransitionError(f"promote held for {candidate_id}: {decision.reason}")
        return emit
