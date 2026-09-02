"""KnowledgeReviser — a reviewer's sentence in, a corrected FACT out (design §C.1).

WHAT IT IS NOT: a writer. Mechanically it mirrors `BlueprintReviser` — one forced-tool model
turn, an `asyncio.timeout`, fail-soft to a proposal carrying a `reason`, a span on every return
path — and it keeps the same split for the same reason: the proposal comes back for the
reviewer to APPLY through `apply_knowledge`, which is the one write path into a knowledge
candidate's payload and the one place intake re-validation and the leakage scan run.

TWO THINGS ARE DIFFERENT FROM THE BLUEPRINT REVISER, and both come from the same fact — that a
knowledge payload is TEXT, where a parameterization is a classification of literals that already
exist in a query.

⚠ 1. THE PROPOSAL IS SCANNED BEFORE IT LEAVES. The blueprint reviser is REFUSED on a candidate
whose scan did not clear (`ReviewInbox.propose_revision`), because its output necessarily quotes
the accepted SQL's literals and a redacted literal matches no predicate. Here the opposite
holds: the assistant is offered PRECISELY BECAUSE the scan did not clear — removing the entity
is the job. So the refusal moves from the input to the OUTPUT. The reviser runs the gate's own
scan over a throwaway envelope carrying the proposed payload, and a verdict that is not `pass`
means the draft is NOT returned: the 200 carries a `reason` naming `field (kind)` and nothing
else. That is the withholding rule the card already obeys (`inbox/models.py::_leakage_cleared`),
applied to the one surface that could otherwise hand a browser FRESH entity-bearing text that
no stored verdict covers.

NO SCANNER WIRED ⇒ NOTHING IS RETURNED, and that is deliberate rather than an oversight. The
scan is the only thing standing between a model's prose about an unredacted payload and a
response body, so "we could not check" has to read as "we do not send". It is the same
fail-closed posture `settle_entity_scan` takes when no gate is wired, one layer out.

⚠ 2. THE DIFF REDACTS ITS `before`. Every field this proposal is about may currently carry the
entity the scan flagged, so a per-field before/after would ship the flagged text under the
label "before" — through the surface added to help remove it. A flagged field's `before` is
`[withheld]`; the `after` is the model's own text, which has just been scanned clean.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from data_agent.runtime.model.client import ModelClient, begin_turn_client

from ..candidate.models import CandidateEnvelope
from ..candidate.verdicts import LeakageVerdict
from ..observability import revise_span
from .engine import MAX_FEEDBACK_CHARS
from .knowledge_prompt import knowledge_brief, system_prompt
from .knowledge_schema import (
    KNOWLEDGE_SURFACES,
    ForbiddenKnowledgeEditError,
    build_knowledge_tool,
    parse_knowledge_proposal,
)

_logger = logging.getLogger(__name__)

# ⚠ `MAX_FEEDBACK_CHARS` is IMPORTED from `engine.py` above rather than restated: it is the cap
# on a reviewer's free text before it reaches a prompt, and two surfaces disagreeing about how
# much a human may type into "the same box" is a difference nobody would ever find deliberately.

# What a redacted `before` renders as. DISTINCT from `redaction.py::_REDACTED` ("[redacted]") on
# purpose: that marker means "an entity value was removed from this text", and this one means
# "the whole previous value is being withheld from you". A reviewer who sees `[withheld]` in a
# before/after knows there is content they are not being shown, which "[redacted]" — which they
# see all over the payload view — would not tell them.
WITHHELD = "[withheld]"

# What is returned when a scanner is not wired, or when the draft came back still carrying an
# entity. A NON-EMPTY payload can never accompany either.
_NO_SCANNER_REASON = (
    "the assistant is unavailable for this candidate: this deployment has no entity scanner "
    "wired, and a proposed fact is model prose about an unredacted payload — it is refused "
    "rather than returned unchecked. The five fields can still be edited directly in the form."
)

# The scan hook. A plain async callable rather than a Protocol because there is exactly one
# question — "what does the gate say about this envelope's payload" — and the answer is the
# `entity_scan` doc shape every guard on this plane already reads. `inbox/knowledge_edit.py`
# builds one from the SAME write-router stage tuple the editor scans with, so the check that
# withholds a draft and the check that stamps the applied row are one implementation.
KnowledgeScanner = Callable[[CandidateEnvelope], Awaitable[dict[str, Any]]]

KnowledgeDiffKind = Literal["unchanged", "changed", "added", "removed"]


@dataclass(frozen=True)
class KnowledgeFieldDiff:
    """One field's before/after, as the card renders it.

    Keyed on the FIELD NAME, which is the whole identity of a knowledge surface — unlike a
    parameterization entry, whose identity is a locator and whose position is meaningless. So
    this diff needs none of `diff.py`'s conflict machinery: there are five rows at most, always
    the same five names, and "append vs replace" is not a mode this operation has.
    """

    field: str
    kind: KnowledgeDiffKind
    before: str = ""
    after: str = ""

    def to_doc(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "kind": self.kind,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True)
class KnowledgeProposal:
    """What one knowledge-revise attempt produced. NOTHING has been written.

    `payload` empty means no proposal; `reason` then says why in words a reviewer can read —
    including the case that matters most here, a draft that came back still carrying an entity,
    where the reason names the field and the kind and NEVER the span.
    """

    payload: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    reason: str = ""
    diff: tuple[KnowledgeFieldDiff, ...] = ()

    @property
    def has_proposal(self) -> bool:
        return bool(self.payload)

    def to_wire(self) -> dict[str, Any]:
        """The response body (design §C.1). Deliberately WITHOUT a candidate status: nothing
        moved, and a status here would imply otherwise.

        ⚠ THE FIVE SURFACES ARE ALWAYS PRESENT when there is a proposal, filled with their
        empty value where the assistant said nothing. The form on the other side prefills from
        this object, and a key that appears only sometimes makes "the assistant cleared this
        field" and "an older server could not produce it" indistinguishable to the browser —
        the same argument `ReviseProposal.to_wire` makes for always emitting `sql_changed`.

        NO SWEEP FOR TRANSPORT IS NEEDED HERE, unlike `ReviseProposal.to_wire`: every string in
        this payload came through `knowledge_schema._clean`, whose flatten includes the `Cs`
        (lone surrogate) category, so nothing unencodable can reach the socket. That is the same
        guard by a different route — done at the parse boundary rather than the wire one,
        because unlike a parameterization entry this text is not applied verbatim.
        """
        payload = (
            {name: self.payload.get(name, _empty_for(name)) for name in KNOWLEDGE_SURFACES}
            if self.payload
            else {}
        )
        return {
            "payload": payload,
            "rationale": self.rationale,
            "reason": self.reason,
            "diff": [row.to_doc() for row in self.diff],
        }


def _empty_for(name: str) -> Any:
    """The empty value of one surface, by its declared shape."""
    if name == "related_terms":
        return []
    if name == "structured":
        return {}
    return ""


def _render(value: Any) -> str:
    """One field value as the single line a card shows.

    Rendered SERVER-SIDE for the reason `diff.py` gives: showing a reviewer two JSON blobs and
    asking them to spot the difference would reproduce the original complaint inside the fix.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in sorted(value.items()))
    return str(value)


def flagged_fields(scan: Any) -> frozenset[str]:
    """The top-level surfaces the SETTLED scan flagged, from its hits' dotted field paths.

    The gate flattens a payload into `{dotted_field: text}` before scanning, so a hit inside
    `structured` arrives as `structured.unit` and one inside `related_terms` as
    `related_terms.0`. The redaction rule is about the SURFACE the reviewer sees a row for, so
    the prefix is what is matched — a hit on `structured.unit` withholds the whole `structured`
    row, because that row renders the value the hit is inside.
    """
    if not isinstance(scan, dict) or not LeakageVerdict.is_settled(scan):
        return frozenset()
    names: set[str] = set()
    for hit in scan.get("hits") or []:
        if not isinstance(hit, dict):
            continue
        raw = hit.get("field")
        if isinstance(raw, str) and raw:
            names.add(raw.split(".", 1)[0])
    return frozenset(names)


def scan_hits(scan: Any) -> tuple[tuple[str, str, str], ...]:
    """`(field, kind, span)` triples off a settled verdict, in verdict order.

    ⚠ CARRIES THE SPAN. Server-side only: it feeds the model's brief (which has to SEE the
    entity to remove it) and nothing else. Every caller that puts a finding on the wire uses
    `_scan_summary` or the `(field, kind)` pair below instead.
    """
    if not isinstance(scan, dict) or not LeakageVerdict.is_settled(scan):
        return ()
    out: list[tuple[str, str, str]] = []
    for hit in scan.get("hits") or []:
        if not isinstance(hit, dict):
            continue
        field_name = str(hit.get("field") or "")
        if not field_name:
            continue
        out.append((field_name, str(hit.get("kind") or ""), str(hit.get("span") or "")))
    return tuple(out)


def _flagged_summary(scan: Any) -> str:
    """`field (kind)` pairs, deduped and sorted — NEVER the span.

    The exact projection `propose_revision`'s refusal message uses, for the same reason: naming
    the field and the kind is what turns a refusal into something a human can judge, while the
    span is the value being withheld.
    """
    return ", ".join(
        sorted({f"{field} ({kind})" for field, kind, _span in scan_hits(scan) if field})
    )


def knowledge_diff(
    current: dict[str, Any], proposed: dict[str, Any], *, withhold: frozenset[str] = frozenset()
) -> tuple[KnowledgeFieldDiff, ...]:
    """What applying *proposed* would change, one row per surface.

    ALWAYS FIVE ROWS, unchanged ones included. A reviewer deciding whether to apply needs to see
    that the two fields they already trusted are still there, and a diff showing only deltas
    cannot say so — the same call `diff_parameterization` makes.

    *withhold* names the surfaces whose CURRENT value the scan flagged; their `before` renders
    as `[withheld]`. Note what is NOT withheld: the `after`, which is the model's own text and
    has been scanned clean by the caller before this is ever built.
    """
    rows: list[KnowledgeFieldDiff] = []
    for name in KNOWLEDGE_SURFACES:
        before_raw = current.get(name)
        after_raw = proposed.get(name)
        before = _render(before_raw)
        after = _render(after_raw)
        if not before and not after:
            kind: KnowledgeDiffKind = "unchanged"
        elif not before:
            kind = "added"
        elif not after:
            kind = "removed"
        elif before == after:
            kind = "unchanged"
        else:
            kind = "changed"
        rows.append(
            KnowledgeFieldDiff(
                field=name,
                kind=kind,
                before=WITHHELD if (before and name in withhold) else before,
                after=after,
            )
        )
    return tuple(rows)


@dataclass(frozen=True)
class KnowledgeReviser:
    """One forced-tool model turn that proposes a corrected `global_knowledge` fact."""

    model_client: ModelClient
    # THE OUTPUT GATE, not an optional enhancement — see the module docstring. `None` means
    # every proposal is withheld, which is what "we could not check it" has to mean here.
    scanner: KnowledgeScanner | None = None
    timeout_seconds: float = 30.0
    model: str = ""
    # Injected like every other collaborator on this plane; `None` = no tracing.
    tracer: object | None = None
    # The D25 gate, decided at the composition root. ⚠ The verbose payload is a human's free
    # text plus model prose about an UNREDACTED knowledge payload, so it is never defaulted on.
    trace_verbose: bool = False

    async def propose(
        self, env: CandidateEnvelope, *, feedback: str
    ) -> KnowledgeProposal:
        """Propose a corrected fact. NEVER raises except on a forbidden field.

        Takes the payload off the envelope UNREDACTED, for the reason the blueprint brief does:
        a model asked to remove `E10842` while being shown `[redacted]` has nothing to reason
        about. What crosses back to the browser is decided by the scan below, not by this input.
        """
        brief = knowledge_brief(
            env.payload,
            surfaces=KNOWLEDGE_SURFACES,
            hits=scan_hits(env.entity_scan),
            scan_result=str((env.entity_scan or {}).get("result") or ""),
            feedback=(feedback or "")[:MAX_FEEDBACK_CHARS],
        )

        try:
            async with asyncio.timeout(self.timeout_seconds):
                messages = [
                    {"role": "system", "content": system_prompt()},
                    # The brief is a SEPARATE user message with the reviewer's own words inside
                    # it, clearly labelled — never spliced into the instruction message. Same
                    # placement, same reasoning, as the blueprint brief.
                    {"role": "user", "content": brief},
                ]
                client = begin_turn_client(self.model_client)
                result = await client.send_turn(messages, [build_knowledge_tool()])
                parsed = parse_knowledge_proposal(result)
        except ForbiddenKnowledgeEditError:
            self._emit(env, "refused_knowledge_edit", feedback=feedback)
            raise
        except TimeoutError:
            _logger.warning(
                "knowledge reviser: model call exceeded %.1fs — no proposal",
                self.timeout_seconds,
            )
            self._emit(env, "timeout", feedback=feedback)
            return KnowledgeProposal(reason="the assistant timed out; try again")
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            # The three that must still propagate — cancellation is how a shutdown reaches an
            # awaiting request handler, and the other two are the operator stopping the process.
            raise
        except BaseException:  # noqa: BLE001 — a typing aid may not 500 the review surface
            # WIDER THAN `Exception`, for the reason `engine.py` records from QA: a provider SDK
            # raising a bare `BaseException` escaped every narrower handler. An optional
            # convenience must not be able to take down the surface it is attached to.
            _logger.warning(
                "knowledge reviser: model call raised — no proposal", exc_info=True
            )
            self._emit(env, "failed", feedback=feedback)
            return KnowledgeProposal(reason="the assistant could not be reached; try again")

        if parsed is None:
            self._emit(env, "unusable", feedback=feedback)
            return KnowledgeProposal(
                reason=(
                    "the assistant did not return a usable statement; try rephrasing what "
                    "you want the fact to say"
                )
            )
        payload, rationale = parsed

        # ⚠ THE OUTPUT GATE. Everything above this line is model prose about an unredacted
        # payload; nothing below it may reach a response body without a verdict.
        if self.scanner is None:
            self._emit(env, "no_scanner", feedback=feedback)
            return KnowledgeProposal(reason=_NO_SCANNER_REASON)
        scan = await self._scan(env, payload)
        if not (isinstance(scan, dict) and scan.get("result") == "pass"):
            flagged = _flagged_summary(scan)
            settled = str(scan.get("result") or "nothing") if isinstance(scan, dict) else "nothing"
            self._emit(env, "withheld_dirty_draft", feedback=feedback, reason=flagged or None)
            return KnowledgeProposal(
                reason=(
                    "the assistant's draft still trips the entity scanner"
                    + (f" on {flagged}" if flagged else "")
                    + f" (it settled {settled!r}), so it was withheld rather than shown to "
                    "you — showing it would have put the flagged text back on this page. Say "
                    "more specifically what should replace it, or edit the fields below "
                    "yourself."
                )
            )

        rows = knowledge_diff(
            env.payload, payload, withhold=flagged_fields(env.entity_scan)
        )
        self._emit(
            env,
            "proposed",
            feedback=feedback,
            fields=len(payload),
            rationale=rationale,
        )
        return KnowledgeProposal(payload=payload, rationale=rationale, diff=rows)

    async def _scan(
        self, env: CandidateEnvelope, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """The gate's verdict on a THROWAWAY envelope carrying *payload*. Never raises.

        A throwaway rather than a store write, because this candidate has not been edited — the
        reviewer has not decided anything yet, and a scan that MOVED the row would make asking
        the assistant a mutation. The envelope is otherwise the real one, so the gate scans the
        same type through the same surfaces it will scan on apply.

        FAIL-CLOSED on an exception: a scanner that raised has told us nothing, and the branch
        above turns "not a pass" into "withheld", which is the answer that cannot leak.
        """
        assert self.scanner is not None  # guarded by the caller
        try:
            return await self.scanner(replace(env, payload=dict(payload)))
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001 — an unchecked draft must never be returned
            _logger.warning(
                "knowledge reviser: the proposal scan raised — the draft is withheld",
                exc_info=True,
            )
            return {"result": "pending", "hits": []}

    def refuse(self, env: CandidateEnvelope, *, reason: str) -> KnowledgeProposal:
        """A caller-side refusal, as a proposal — and as a SPAN.

        Lives here rather than at the caller so every `learning.revise` outcome is emitted from
        one place, exactly as `BlueprintReviser.refuse_withheld` does: a refusal that produced
        no span would be indistinguishable from the assistant never having been asked.
        """
        self._emit(env, "withheld_scan")
        return KnowledgeProposal(reason=reason)

    def _emit(self, env: CandidateEnvelope, outcome: str, **attrs: Any) -> None:
        """One `learning.revise` span with `subject="knowledge"`, or nothing when untraced.

        ONE span name for both revisers, because every operational question about them is the
        same question — see `observability.py::revise_span`. The outcomes that are new here are
        the two the blueprint path cannot have: `no_scanner` (this deployment cannot check a
        draft, so none is ever shown — a WIRING signal) and `withheld_dirty_draft` (the model
        answered and the answer still leaked — a PROMPT signal, and the one number that says
        whether this whole path is working).
        """
        if self.tracer is None:
            return
        with revise_span(
            self.tracer,  # type: ignore[arg-type]
            candidate_id=env.candidate_id,
            outcome=outcome,
            status=env.status,
            model=self.model,
            subject="knowledge",
            verbose=self.trace_verbose,
            **attrs,
        ):
            return


__all__ = [
    "WITHHELD",
    "ForbiddenKnowledgeEditError",
    "KnowledgeDiffKind",
    "KnowledgeFieldDiff",
    "KnowledgeProposal",
    "KnowledgeReviser",
    "KnowledgeScanner",
    "flagged_fields",
    "knowledge_diff",
    "scan_hits",
]
