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

Verdict -> control mapping:
  * pass       -> the candidate is entity-free; leave `status=extracted`, emit
                  `control="continue"` so the downstream writer auto-lands it.
  * reroute    -> an entity that is a legitimate per-user fact: spawn a linked
                  `user_knowledge` candidate (`depends_on` this one) into the
                  candidate store and mark THIS global one `rejected`.
  * quarantine -> a suspected leak in a blueprint: `status=quarantined`, hold for
                  human (S7 routes the near-miss to the inbox — not this slice).
  * reject     -> a hard entity in a global_knowledge candidate: terminal
                  `status=rejected`.
For every non-`pass` verdict the enriched envelope is persisted (audit posture,
D101) and the candidate's pipeline stops — `control="route_inbox"` is purely the
consumer's "persist + stop this candidate" signal; the STATUS field (not the
control) determines inbox visibility, which S7 projects by `status`.

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


def _scanned_fields(candidate_type: str, payload: dict) -> dict[str, str]:
    """Select the entity-relevant text fields per target (design §4 / Contract B).
    `result_signature` is serialized canonically so a nested dict is scannable."""
    fields: dict[str, str] = {}
    if candidate_type == "global_knowledge":
        statement = payload.get("statement")
        if isinstance(statement, str) and statement:
            fields["statement"] = statement
        return fields
    # blueprint (and any other global carrying intent/result_signature)
    intent = payload.get("intent")
    if isinstance(intent, str) and intent:
        fields["intent"] = intent
    signature = payload.get("result_signature")
    if signature is not None:
        fields["result_signature"] = json.dumps(
            signature, sort_keys=True, ensure_ascii=False
        )
    return fields


def _primary_statement(text_by_field: dict[str, str]) -> str:
    """The entity-bearing text to carry onto a rerouted `user_knowledge` fact —
    the first scanned field's text (intent / statement)."""
    for text in text_by_field.values():
        return text
    return ""


@dataclass(frozen=True)
class LeakageGateStage:
    """The injected S5 write-router stage. `candidate_store` is used ONLY on the
    reroute path (to persist the spawned `user_knowledge` candidate); the semantic
    scanner defaults to the null (regex-only) scanner; the tracer is optional."""

    candidate_store: CandidateStore
    semantic_scanner: SemanticEntityScanner = NullSemanticEntityScanner()
    tracer: object | None = None
    stage_id: str = "leakage"

    async def process(
        self, env: CandidateEnvelope, ctx: StageContext
    ) -> StageResult:
        # Not the gate's remit: user_knowledge is entity-bearing by design, and
        # schema_edit is human-gated — pass both through untouched.
        if env.type not in _GLOBAL_TYPES:
            return StageResult(envelope=env, control="continue")

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

        return await self._apply(env, ctx, verdict, text_by_field)

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

        if verdict.result == "pass":
            return StageResult(
                envelope=replace(env, entity_scan=entity_scan_doc),
                control="continue",
            )

        if verdict.result == "reroute":
            await self._spawn_user_knowledge(env, ctx, text_by_field)
            rejected = replace(
                env, entity_scan=entity_scan_doc, status=CandidateStatus.REJECTED
            )
            return StageResult(envelope=rejected, control="route_inbox")

        if verdict.result == "quarantine":
            held = replace(
                env, entity_scan=entity_scan_doc, status=CandidateStatus.QUARANTINED
            )
            return StageResult(envelope=held, control="route_inbox")

        # reject (terminal)
        rejected = replace(
            env, entity_scan=entity_scan_doc, status=CandidateStatus.REJECTED
        )
        return StageResult(envelope=rejected, control="route_inbox")

    async def _spawn_user_knowledge(
        self,
        env: CandidateEnvelope,
        ctx: StageContext,
        text_by_field: dict[str, str],
    ) -> None:
        """Persist a NEW `user_knowledge` candidate carrying the entity-bearing
        fact, linked back to the rejected global one via `depends_on`. Keyed
        deterministically off the global id so a re-run UPSERTs (idempotent)."""
        spawned = CandidateEnvelope(
            candidate_id=f"{env.candidate_id}::rerouted-userk",
            type="user_knowledge",
            status=CandidateStatus.EXTRACTED,
            payload={
                "user_id": ctx.summary.user_id,
                "fact_type": "frequent_entity",
                "scope": "user",
                "statement": _primary_statement(text_by_field),
                "structured": None,
            },
            source_session=env.source_session,
            source_trace=env.source_trace,
            evidence_refs=env.evidence_refs,
            extractor_rationale=(
                "rerouted by the S5 leakage gate: a global candidate carried a "
                "user-specific entity, captured here as a per-user fact (D17/D58)"
            ),
            entity_scan={
                "result": "pending",
                "hits": [],
                "self_check_contains_entities": True,
            },
            confidence=env.confidence,
            proposed_action="new",
            depends_on=(env.candidate_id,),
            content_hash=env.content_hash,
        )
        await self.candidate_store.put(spawned)


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
