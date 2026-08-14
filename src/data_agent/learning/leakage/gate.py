"""LeakageGateStage — the S5 leakage gate (`CandidateStage`, GUARDRAIL, D58/D17).

The hard guarantee that GLOBAL (entity-free) stores stay entity-free. For every
freshly-extracted GLOBAL candidate (a `blueprint` or `global_knowledge`), the gate
scans `payload.intent` + `payload.result_signature` (blueprint) or
`payload.statement` (global_knowledge) for entities via two layers — the
`entities.py` regex/NER battery + the INJECTED semantic scan (`scanner.py`,
scripted in tests) — and writes the authoritative `LeakageVerdict` into
`envelope.entity_scan`, overwriting S3's preliminary `pending` self-check.

The gate is the WRITER of the settled verdict, so it never READS the incoming
`entity_scan` (which is S3's un-settled `pending` sentinel — parsing it as a
`LeakageVerdict` without `is_settled` would fail loud by design). It only writes.

The gate STAMPS the verdict but is NOT the routing authority — the S7 writer is
(the single routing seam, so a near-miss can never be stranded, D-frozen). The gate
therefore lets `pass`, `quarantine`, AND `reroute` flow ON to S6→S7 with
`control="continue"`, leaving `status=extracted`; only a hard `reject` terminates
here (`status=rejected`, `control="route_inbox"` = persist + stop):

  * pass       -> entity-free; `control="continue"`, writer auto-lands it.
  * quarantine -> suspected leak in a blueprint; `control="continue"`, the writer
                  routes it to `in_review` (reason `leakage_near_miss`).
  * reroute    -> an entity that is a legitimate per-user fact: the fact is
                  COMMITTED directly into the injected per-user `UserKnowledgeStore`
                  (scoped to the session's authenticated `user_id`), and the global
                  candidate flows on with `control="continue"` (the writer routes
                  the residual near-miss to `in_review`).
  * reject     -> a hard entity in a global_knowledge candidate: terminal
                  `status=rejected`, `control="route_inbox"` (persist + stop).

Entity-BEARING targets (`user_knowledge`) and human-gated `schema_edit` are NOT
in the gate's remit — it passes them through untouched (`control="continue"`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

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
    "global_knowledge": ("statement", "structured", "related_terms", "scope"),
    # `notes` is extractor free text that lands with the artifact — an entity there
    # with a clean `intent` must NOT slip through as `pass` (Q1 rework).
    "blueprint": ("intent", "result_signature", "notes"),
}
# `result_signature` has a defined schema (D56) — scan it as ONE canonically
# serialized field so a hit is attributed to `result_signature` (not a leaf path).
_WHOLE_SERIALIZED_FIELDS = frozenset({"result_signature"})


def _generalization_templates(payload: dict) -> dict[str, str]:
    """The AST-rewritten SQL template(s) from S4's `generalization` (Q1 rework). A
    role=inline literal (e.g. a hardcoded `region = 'EMEA'`) survives verbatim into
    the landed global artifact, so the template string itself must be scanned. Single
    blueprint: `generalization.sql_template`; composite (top-level template is None):
    each `node_templates[*].sql_template`. Legit metric-defining literals (`'EARNING'`)
    won't trip the entity regex; an entity hit is at worst a quarantine → human."""
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
    """Flatten a payload value into `{dotted_field: text}` so EVERY text leaf is a
    distinct, individually-attributable field handed to both scan layers. A nested
    dict/list is walked into (`structured.example_employee`), so an entity buried in
    a sub-field cannot starve the scanners (QA-Q1)."""
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
    """Select EVERY entity-relevant text field per target (design §4 / Contract B,
    QA-Q1). `result_signature` is serialized canonically as one field; all other
    content surfaces are flattened so a leak in any leaf reaches both scan layers."""
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


@dataclass(frozen=True)
class LeakageGateStage:
    """The injected S5 write-router stage. `candidate_store` is retained for the
    stage's audit-store wiring parity; the reroute path commits the per-user fact
    through the injected `user_store` (the SAME store S8 uses, scoped to the
    session user). The semantic scanner defaults to the null (regex-only) scanner;
    the tracer is optional."""

    candidate_store: CandidateStore
    semantic_scanner: SemanticEntityScanner = NullSemanticEntityScanner()
    user_store: UserKnowledgeStore | None = None
    tracer: object | None = None
    stage_id: str = "leakage"

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
        self, env: CandidateEnvelope
    ) -> tuple[LeakageVerdict, dict[str, str]]:
        """SCAN ONLY: both layers, the decision, the span — and NOTHING that writes.

        Split out of `process` because a second caller needs the verdict WITHOUT the
        consequences (`consumer.py::_scan_declined` and `inbox/completion.py`, which stamp
        a settled verdict on a candidate that has not passed validation and is not flowing
        toward a landing). The split is a refactor, not a new policy: `process` is this
        method plus `_apply`, so the two callers cannot drift about what a leak IS —
        which is the whole reason the declined path runs the WIRED gate instead of
        assembling its own scanner.

        WHY IT MATTERS THAT THIS ONE IS PURE. `_apply` has a side effect: a `reroute`
        verdict COMMITS a per-user knowledge record (`_commit_user_fact`). On the
        validated path that is the gate doing its job — the fact was lifted from a
        candidate that passed every check. On the DECLINED path the same call would
        commit a per-user fact scraped out of a candidate that failed validation and may
        never be completed or may be rejected outright, and nothing would ever retract
        it. So the declined path takes the verdict and leaves the consequences here.

        `env.type` is NOT re-checked: the caller is either `process` (which checked) or a
        caller that wants a verdict for a global candidate it already knows the type of.
        A non-global type simply scans no fields and comes back `pass`, which is the same
        answer `process` gives by skipping."""
        text_by_field = _scanned_fields(env.type, env.payload)
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
        """Commit the entity-bearing fact into the injected per-user store, scoped
        to the SESSION's authenticated `user_id` (`ctx.summary.user_id`, never a
        payload-supplied id — R6). Fail-SAFE: if no user store is wired OR the session
        user_id is empty (`job.user_id or ""` can be blank), the fact cannot be safely
        SCOPED — this is a no-op (no unscoped `userknow::::` record) and the residual
        near-miss is left for the human inbox (S2 refusal, mirroring
        `UserKnowledgeRecord.from_candidate`'s non-empty guard)."""
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


async def settle_entity_scan(
    stages: tuple, env: CandidateEnvelope, ctx: StageContext
) -> dict:
    """Return the `entity_scan` doc for an envelope on a NON-LANDING path: the wired
    gate's settled verdict, or the `pending` sentinel when no gate is wired.

    THE ONE PLACE the two non-landing callers share, so the rule cannot fork:
    `consumer.py::_persist_declined_for_review` (a decline being parked for review) and
    `inbox/completion.py::_still_declined` (a reviewer's merged payload going back into
    the queue). Both stamp a verdict on a candidate that has NOT passed validation, so
    both need the gate's judgement and neither may have its consequences — see
    `LeakageGateStage.scan`.

    The gate is found by `stage_id` in the pipeline the caller was BUILT with, never
    constructed here: a privately-built scanner would be a second definition of what a
    leak is, and the two would drift on the first threshold change.

    NO GATE WIRED ⇒ the sentinel, deliberately, and never a fabricated `pass`. The
    resulting row is degraded — its decline detail is withheld at the wire and it can be
    approved by nobody — which is the correct shape for a deployment that scanned
    nothing, and is loudly logged by the caller."""
    stage = next((s for s in stages if getattr(s, "stage_id", "") == "leakage"), None)
    if stage is None:
        return dict(PENDING_ENTITY_SCAN)
    verdict, _text_by_field = await stage.scan(env)
    return verdict.to_doc()


def _dedup_hits(hits: tuple[EntityHit, ...]) -> tuple[EntityHit, ...]:
    """Order-preserving de-dup so regex + semantic overlap does not double-count
    the same (field, kind, span)."""
    seen: set[tuple[str, str, str]] = set()
    out: list[EntityHit] = []
    for hit in hits:
        key = (hit.field, hit.kind, hit.span)
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return tuple(out)
