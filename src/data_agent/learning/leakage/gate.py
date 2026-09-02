"""LeakageGateStage — the S5 leakage gate (`CandidateStage`, GUARDRAIL, D58/D17).

The hard guarantee that GLOBAL (entity-free) stores stay entity-free. For each freshly
extracted global candidate it scans the entity-relevant payload fields through two layers —
the `entities.py` regex battery and the INJECTED semantic scan — and writes the authoritative
`LeakageVerdict` into `envelope.entity_scan`, overwriting S3's `pending` self-check. It is the
WRITER of the settled verdict and never READS the incoming one.

The gate STAMPS the verdict but is NOT the routing authority — the S7 writer is, so a
near-miss can never be stranded. `pass`, `quarantine` AND `reroute` therefore all flow on with
`control="continue"` at `status=extracted`; only a hard `reject` terminates here
(`status=rejected`, `route_inbox`). A `reroute` additionally COMMITS the entity as a per-user
fact into the injected store, scoped to the session's authenticated user. Entity-BEARING
targets (`user_knowledge`) and human-gated `schema_edit` are not in the gate's remit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from ..candidate.models import CandidateEnvelope, CandidateStatus
from ..candidate.store import CandidateStore
from ..candidate.verdicts import EntityHit, LeakageVerdict
from ..stage import StageContext, StageResult
from ..user.models import UserKnowledgeRecord, mint_record_id
from ..user.store import UserKnowledgeStore
from . import entities
from .scanner import (
    NullSemanticEntityScanner,
    SemanticEntityScanner,
    SemanticScanRequest,
)
from .spans import leakage_span

# The GLOBAL, entity-free targets the gate is authoritative over.
_GLOBAL_TYPES = frozenset({"blueprint", "global_knowledge"})
# The subset that is entity-free by CONTRACT (a hard entity => terminal reject,
# never a mere quarantine): global_knowledge lands in the RAG index (D58a).
_HARD_REJECT_TYPES = frozenset({"global_knowledge"})


# The entity-free CONTENT surfaces per target, in scan order (QA-Q1). Every
# text-bearing field a leak could hide in must reach BOTH scan layers — the earlier
# gate scanned only `statement` (knowledge) / `intent` (blueprint) and starved even
# a perfect semantic backstop of any other field. Blueprint raw-value carriers
# (`parameterization`/`resolves`) are DELIBERATELY excluded: they hold the
# pre-generalization slot values by design (they are templated out downstream), so
# scanning them would false-positive on every candidate.
_ENTITY_FREE_SURFACES: dict[str, tuple[str, ...]] = {
    # This tuple IS the intake contract: `_global_knowledge_payload` closes the payload
    # key set over exactly these surfaces (plus nothing), so every key intake permits is
    # a surface this gate scans. `knowledge_type` is a short label the mapper never
    # lands, scanned anyway so the invariant is exact rather than exact-minus-one.
    "global_knowledge": (
        "statement",
        "knowledge_type",
        "structured",
        "related_terms",
        "scope",
    ),
    # `notes` is extractor free text that lands with the artifact — an entity there
    # with a clean `intent` must NOT slip through as `pass` (Q1 rework).
    "blueprint": ("intent", "result_signature", "notes"),
}
# `result_signature` has a defined schema (D56) — scan it as ONE canonically
# serialized field so a hit is attributed to `result_signature` (not a leaf path).
_WHOLE_SERIALIZED_FIELDS = frozenset({"result_signature"})


def _generalization_templates(payload: dict) -> dict[str, str]:
    """The AST-rewritten SQL template(s) from S4's `generalization`.

    A role=inline literal survives verbatim into the landed global artifact, so the template
    string itself must be scanned. Single blueprint: `generalization.sql_template`; composite:
    each node template. A legit metric-defining literal will not trip the entity regex, and an
    entity hit is at worst a quarantine.
    """
    gen = payload.get("generalization")
    if not isinstance(gen, dict):
        return {}
    out: dict[str, str] = {}
    top = gen.get("sql_template")
    if isinstance(top, str) and top:
        out["generalization.sql_template"] = top
    nodes = gen.get("node_templates")
    if isinstance(nodes, (list, tuple)):
        for idx, node in enumerate(nodes):
            if isinstance(node, dict):
                tmpl = node.get("sql_template")
                if isinstance(tmpl, str) and tmpl:
                    out[f"generalization.node_templates.{idx}.sql_template"] = tmpl
    return out


def _collect_text(prefix: str, value: object, out: dict[str, str]) -> None:
    """Flatten a payload value into `{dotted_field: text}`.

    EVERY text leaf becomes a distinct, individually-attributable field handed to both scan
    layers, and a nested dict or list is walked into, so an entity buried in a sub-field cannot
    starve the scanners.
    """
    if isinstance(value, str):
        if value:
            out[prefix] = value
    elif isinstance(value, dict):
        for key, sub in value.items():
            _collect_text(f"{prefix}.{key}", sub, out)
    elif isinstance(value, (list, tuple)):
        for idx, sub in enumerate(value):
            _collect_text(f"{prefix}.{idx}", sub, out)


def _scanned_fields(candidate_type: str, payload: dict) -> dict[str, str]:
    """Select EVERY entity-relevant text field per target (Contract B).

    `result_signature` is serialized canonically as one field; all other content surfaces are
    flattened so a leak in any leaf reaches both scan layers.
    """
    fields: dict[str, str] = {}
    for name in _ENTITY_FREE_SURFACES.get(candidate_type, ()):
        value = payload.get(name)
        if value is None:
            continue
        if name in _WHOLE_SERIALIZED_FIELDS:
            fields[name] = json.dumps(value, sort_keys=True, ensure_ascii=False)
        else:
            _collect_text(name, value, fields)
    if candidate_type == "blueprint":
        # Scan the AST-rewritten template(s) too — role=inline literals survive here
        # into the landed artifact (Q1 rework).
        fields.update(_generalization_templates(payload))
    return fields


def _primary_statement(text_by_field: dict[str, str]) -> str:
    """The entity-bearing text to carry onto a rerouted `user_knowledge` fact —
    the first scanned field's text (intent / statement)."""
    for text in text_by_field.values():
        return text
    return ""


# How the S5 gate is RECOGNISED inside a stage tuple. One spelling, because two callers now ask
# the same question of a pipeline they did not build — "is the scanner in here" — and a literal
# at each of them is a way for one to stop finding a gate the other still does.
LEAKAGE_STAGE_ID = "leakage"


@dataclass(frozen=True)
class LeakageGateStage:
    """The injected S5 write-router stage.

    The reroute path commits the per-user fact through the injected `user_store` — the SAME store
    S8 uses, scoped to the session user. The semantic scanner defaults to the null (regex-only)
    scanner; the tracer is optional.
    """

    candidate_store: CandidateStore
    semantic_scanner: SemanticEntityScanner = NullSemanticEntityScanner()
    user_store: UserKnowledgeStore | None = None
    tracer: object | None = None
    stage_id: str = LEAKAGE_STAGE_ID

    async def process(
        self, env: CandidateEnvelope, ctx: StageContext
    ) -> StageResult:
        # Not the gate's remit: user_knowledge is entity-bearing by design, and
        # schema_edit is human-gated — pass both through untouched.
        if env.type not in _GLOBAL_TYPES:
            return StageResult(envelope=env, control="continue")

        verdict, text_by_field = await self.scan(env)
        return await self._apply(env, ctx, verdict, text_by_field)

    async def scan(
        self, env: CandidateEnvelope, *, extra_fields: dict[str, str] | None = None
    ) -> tuple[LeakageVerdict, dict[str, str]]:
        """SCAN ONLY: both layers, the decision, the span — and NOTHING that writes.

        Split out of `process` because a second caller needs the verdict WITHOUT the consequences
        (the declined-candidate and completion paths, which stamp a settled verdict on a candidate
        that has not passed validation and is not flowing toward a landing). `process` is this method
        plus `_apply`, so the two callers cannot drift about what a leak IS.

        WHY THIS ONE MUST BE PURE: `_apply` COMMITS a per-user knowledge record on a `reroute`. On the
        validated path that is the gate doing its job; on the declined path it would commit a fact
        scraped out of a candidate that failed validation, may never be completed, and may be
        rejected outright, with nothing to retract it.

        `env.type` is NOT re-checked: a non-global type simply scans no fields and comes back `pass`,
        which is the same answer `process` gives by skipping.

        `extra_fields` are surfaces that are NOT in the payload's own scanned set but are about to
        be persisted beside it. There is one caller and one reason: a DECLINED completion has no
        `generalization.sql_template` — the stages never ran — so the surface through which an
        inline literal normally reaches this scanner does not exist yet. See
        `inbox/completion.py::_inline_literal_fields` for what it passes and why that is the one
        honest substitute. Merged UNDER the payload's own fields, so a caller cannot shadow a real
        surface with a fabricated one.
        """
        text_by_field = {**(extra_fields or {}), **_scanned_fields(env.type, env.payload)}
        regex_hits = entities.scan_fields(text_by_field)

        semantic = await self.semantic_scanner.scan(
            SemanticScanRequest(candidate_type=env.type, text_by_field=text_by_field)
        )
        scanner_label = (
            "regex+ner+llm"
            if not isinstance(self.semantic_scanner, NullSemanticEntityScanner)
            else "regex+ner"
        )

        hits = _dedup_hits((*regex_hits, *semantic.hits))
        result = self._decide(env.type, hits, semantic.classification)
        verdict = LeakageVerdict(
            result=result,
            hits=hits,
            scanned_fields=tuple(text_by_field.keys()),
            scanner=scanner_label,
        )

        if self.tracer is not None:
            with leakage_span(
                self.tracer,
                candidate_id=env.candidate_id,
                result=result,
                hit_count=len(hits),
                scanned_fields=verdict.scanned_fields,
                scanner=scanner_label,
            ):
                pass
        return verdict, text_by_field

    @staticmethod
    def _decide(
        candidate_type: str,
        hits: tuple[EntityHit, ...],
        classification: str,
    ) -> str:
        if not hits and classification == "clean":
            return "pass"
        # An entity that the semantic scan judged a legitimate per-user fact is
        # rerouted regardless of the (global) source type.
        if classification == "user_fact":
            return "reroute"
        # A hard entity in a contract-entity-free target is terminal.
        if candidate_type in _HARD_REJECT_TYPES:
            return "reject"
        # A blueprint with an entity is a suspected leak -> hold for human.
        return "quarantine"

    async def _apply(
        self,
        env: CandidateEnvelope,
        ctx: StageContext,
        verdict: LeakageVerdict,
        text_by_field: dict[str, str],
    ) -> StageResult:
        # NOTE: the gate WRITES the verdict; it never `from_doc`s the inbound
        # `env.entity_scan` (S3's un-settled `pending` sentinel — parsing it
        # without `is_settled` would fail loud by design).
        entity_scan_doc = verdict.to_doc()

        # reject is the ONLY terminal stop at S5 (hard entity in a contract-
        # entity-free target): persist + stop.
        if verdict.result == "reject":
            rejected = replace(
                env, entity_scan=entity_scan_doc, status=CandidateStatus.REJECTED
            )
            return StageResult(envelope=rejected, control="route_inbox")

        # reroute: land the legitimate per-user fact into the per-user store NOW
        # (the reroute path commits directly — no orphaned spawned candidate), then
        # let the residual global candidate flow on to the writer.
        if verdict.result == "reroute":
            await self._commit_user_fact(env, ctx, text_by_field)

        # pass / quarantine / reroute: the WRITER is the routing authority — flow on
        # with the settled verdict stamped so S7 can route (pass → auto-land;
        # quarantine/reroute residual → in_review near-miss).
        return StageResult(
            envelope=replace(env, entity_scan=entity_scan_doc),
            control="continue",
        )

    async def _commit_user_fact(
        self,
        env: CandidateEnvelope,
        ctx: StageContext,
        text_by_field: dict[str, str],
    ) -> None:
        """Commit the entity-bearing fact into the injected per-user store.

        Scoped to the SESSION's authenticated `user_id`, never a payload-supplied id (R6). Fail-SAFE:
        with no user store wired, or an empty session user_id, the fact cannot be safely SCOPED, so
        this is a no-op — no unscoped record — and the residual near-miss is left for the human inbox.
        """
        user_id = ctx.summary.user_id
        if self.user_store is None or not user_id:
            return
        record_id = mint_record_id(user_id, f"{env.candidate_id}::rerouted-userk")
        record = UserKnowledgeRecord(
            record_id=record_id,
            user_id=user_id,
            statement=_primary_statement(text_by_field),
            fact_type="frequent_entity",
            scope="user",
            structured=None,
            source_session=env.source_session,
            source_trace=env.source_trace,
            evidence_refs=env.evidence_refs,
        )
        await self.user_store.commit(record)


# The S3 sentinel a candidate carries before the gate has settled anything. Written here
# rather than at each caller so "unsettled" has one spelling: every downstream guard
# (`writer/routing.py::_entity_scan_unsettled`, `promotion/scheduler.py::
# _entity_scan_is_actionable`, `inbox/models.py::_leakage_cleared`) fails CLOSED on it.
PENDING_ENTITY_SCAN: dict = {
    "result": "pending",
    "hits": [],
    "self_check_contains_entities": False,
}

def leakage_stage(stages: tuple) -> Any:
    """The wired S5 gate inside *stages*, found by `stage_id`, or `None`.

    ⚠ THE CAPABILITY QUESTION, ANSWERED IN ONE PLACE. `settle_entity_scan` uses it to decide
    whether to scan or to stamp the sentinel, and `KnowledgeEditor.can_scan` uses it to decide
    whether a caller whose input is entity-bearing BY CONSTRUCTION may proceed at all
    (`knowledge-edit-and-user-promotion-design.md` §F.1.a). The two must never disagree: a
    caller told "you can scan" that then received the sentinel would put an unscanned
    entity-bearing fact onto a shared queue, which is exactly the posture §F.1.a closes.
    """
    return next((s for s in stages if getattr(s, "stage_id", "") == LEAKAGE_STAGE_ID), None)


async def settle_entity_scan(
    stages: tuple,
    env: CandidateEnvelope,
    ctx: StageContext,
    *,
    extra_fields: dict[str, str] | None = None,
) -> dict:
    """The `entity_scan` doc for an envelope on a NON-LANDING path.

    The wired gate's settled verdict, or the `pending` sentinel when no gate is wired. THE ONE
    PLACE the two non-landing callers share, so the rule cannot fork: both stamp a verdict on a
    candidate that has NOT passed validation, so both need the gate's judgement and neither may
    have its consequences (see `LeakageGateStage.scan`). The gate is found by `stage_id` in the
    pipeline the caller was BUILT with, never constructed here — a privately-built scanner would
    be a second definition of what a leak is. NO GATE WIRED ⇒ the sentinel, never a fabricated
    `pass`: the resulting row is degraded (detail withheld at the wire, approvable by nobody),
    which is the correct shape for a deployment that scanned nothing.

    `extra_fields` is passed straight through — see `LeakageGateStage.scan`.
    """
    stage = leakage_stage(stages)
    if stage is None:
        return dict(PENDING_ENTITY_SCAN)
    verdict, _text_by_field = await stage.scan(env, extra_fields=extra_fields)
    return verdict.to_doc()


def _dedup_hits(hits: tuple[EntityHit, ...]) -> tuple[EntityHit, ...]:
    """Order-preserving de-dup, so regex and semantic overlap does not double-count a hit."""
    seen: set[tuple[str, str, str]] = set()
    out: list[EntityHit] = []
    for hit in hits:
        key = (hit.field, hit.kind, hit.span)
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return tuple(out)
