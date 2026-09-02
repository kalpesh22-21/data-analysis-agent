"""KnowledgeEditor — THE one write path into a `global_knowledge` candidate's payload (§B).

Two surfaces reach it: a reviewer applying an edit to a candidate under review (K1,
`apply_knowledge`), and a reviewer promoting a per-user fact into a global one (K2,
`promote_user_knowledge`). They are the same operation from here on — a payload arriving from
OUTSIDE the extractor, about to sit in front of a person who can approve it — so they get one
implementation and one order of checks:

  1. INTAKE VALIDATION through the reader the extractor itself uses
     (`extractor/validation.py::validate_payload`). Not a second vocabulary: literally the same
     function, so a payload a human typed faces the check an EXTRACTED CANDIDATE faces on the
     way into the store. This is what keeps the CLOSED KEY SET true on the human path —
     `definition`, `intent`, `user_id`, anything outside the five surfaces is a 422 naming the
     key, because a sixth key is a text surface the S5 gate does not scan and would reach the
     global index unread.

     ⚠ IT IS NOT THE REVISER'S CHECK, AND THE ASYMMETRY IS DELIBERATE (design §F.1.b). The
     knowledge reviser refuses a nested `structured` at any depth; this reader accepts one.
     They govern DIFFERENT POPULATIONS: the reviser governs what a MODEL may author into a
     payload, and intake governs what an existing candidate may CARRY — including one the
     mined path already produced and the loop already handles (the gate's `_collect_text`
     scans string leaves at any depth, and `knowledge_seed_from_candidate` lands exactly those
     scanned leaves). Tightening intake to match the tool would start declining candidates
     this system handles correctly today, so it is not tightened.
  1b. SIZE CAPS, the reviser's own (`revise/knowledge_schema.py`), applied here because this
     is the HUMAN write path and it had none: a reviewer-token caller could otherwise store a
     multi-megabyte statement that the regex+NER scanner then walks. Same numbers as the ones
     bounding what the assistant may propose, so "too long for the assistant to write" and
     "too long to store" are one answer.
  2. LEAKAGE SCAN, SCAN-ONLY (`leakage/gate.py::settle_entity_scan`) — the verdict is STAMPED,
     the gate's consequences are NOT run. §B.1 of the design owns that decision; the short
     version is below.
  3. STATUS `in_review` with a `route_reason` saying which surface did it.
  4. GUARDED PUT (`completion.py::guarded_put`) — the row must still be where it was read, or
     nothing is written.

⚠ WHY SCAN-ONLY AND NOT THE FULL GATE. `LeakageGateStage.process` would do two things that are
right for an unattended pipeline and wrong here. Its `_decide` makes a hard entity in a
`global_knowledge` payload a TERMINAL reject — which would reject every K2 promotion on
arrival, since a user fact is entity-bearing by definition, so the button could only ever
produce an archive row. And its `reroute` COMMITS a per-user fact scoped to the session user —
which on K1 means a reviewer's keystroke writing into somebody else's private store, with
nothing to retract it. On both paths a human is already holding the candidate, and
`learning-declined-candidate-review.md` made this exact call for the parameterization form:
when a person is in the loop, "hold with the verdict visible" beats "discard".

⚠ WHAT THAT DOES NOT DO IS OPEN A DOOR. The stamped verdict is what the existing guards read:
`inbox/models.py::_leakage_cleared` keeps the flagged text off the card, and
`promotion/scheduler.py::_entity_scan_is_actionable` refuses an approve whose scan never
settled. Nothing here adds an approval shortcut, and nothing here can move a candidate forward
— `in_review` is where every path through this module ends.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field, replace
from typing import Any

from data_agent.timeutil import now_iso

from ..candidate.models import CandidateEnvelope, CandidateStatus, KnowledgeEdit
from ..candidate.store import CandidateStore
from ..extractor.validation import validate_payload
from ..leakage import leakage_stage, settle_entity_scan
from ..revise.knowledge_schema import (
    MAX_LABEL_CHARS,
    MAX_STATEMENT_CHARS,
    MAX_TERM_CHARS,
    MAX_TERMS,
)
from ..stage import CandidateStage, StageContext
from ..summary.models import SessionSummary
from ..triage import TriageVerdict
from .completion import guarded_put

_logger = logging.getLogger(__name__)

# The candidate type this editor is the write path for. A CONSTANT rather than a literal at
# three call sites, because it is also the key `validate_payload` and `_ENTITY_FREE_SURFACES`
# are looked up by — the three have to be one string or the checks silently address nothing.
KNOWLEDGE_TYPE = "global_knowledge"

# `route_reason` values this editor stamps. They are the ONE durable record of which human
# surface produced the row: `derive_inbox_reason` will call both `knowledge_pre_gate` (it keys
# on the type), and a reviewer looking at the queue otherwise cannot tell an extracted fact
# from one a colleague promoted out of somebody's private store.
ROUTE_REASON_EDITED = "knowledge_edited"
ROUTE_REASON_PROMOTED = "promoted_from_user"

# The triage verdict the scan stage is handed. NOTHING reads `ctx.verdict` today (the gate
# reads `ctx.summary`, and `scan` reads neither), so this is documentation with a type —
# `COMPLETION_VERDICT` exists for the same reason and says so. `keep` because a human chose it.
KNOWLEDGE_EDIT_VERDICT = TriageVerdict(
    decision="keep",
    reason="human_edited_global_knowledge",
    target_hints=(KNOWLEDGE_TYPE,),
)


class KnowledgeEditInputError(ValueError):
    """The submitted payload is not a `global_knowledge` payload.

    Carries the INTAKE READER'S OWN SENTENCE, which is the whole value of the type: that text
    names the offending key and says what the five surfaces are, and it is the same sentence a
    model would have been shown. Rewriting it here would give the reviewer a second vocabulary
    for one mistake — the thing `ui/server.py::_proxy_inbox` warns against — and only one of the
    two would name the fix.
    """


class KnowledgeEditorUnavailableError(RuntimeError):
    """No knowledge write plane is wired in this deployment.

    Distinct from an edit that ran and was refused, which is an ordinary result. 503, the same
    answer `CompletionUnavailableError` gives for the same shape of absence — and loud, because
    the alternative (silently doing less) would leave a reviewer believing they had saved
    something.
    """


@dataclass(frozen=True)
class KnowledgeEditResult:
    """What one admit did. The row is `in_review`, whatever the scan said.

    `entity_scan` is the SETTLED doc as stamped, spans included — it lives on the result, not on
    the wire. The service projects `(field, kind)` off it; see `entity_scan_view`.
    """

    envelope: CandidateEnvelope
    entity_scan: dict[str, Any]
    # Whether the row already existed at this id and nothing was written (K2's second press).
    # On the RESULT rather than derived by the caller, because the caller cannot tell: a
    # candidate promoted an hour ago looks identical to one promoted just now.
    already: bool = False


def entity_scan_view(scan: Any) -> dict[str, Any]:
    """The scan as it may cross to a browser: `{result, hits: [{field, kind}]}`.

    ⚠ THE SPAN IS DROPPED, always. It is the entity value itself, and this projection exists
    because two response bodies now carry a verdict — `apply_knowledge` and the promote button
    — neither of which goes through `InboxItem`, whose `_leakage_view` owns the same rule for
    the listing. Two spellings of "blank the span" would be two chances to forget one.

    An UNSETTLED scan renders its own `result` verbatim (`pending`), NOT a synthesized `pass`.
    That is deliberately the opposite of `_leakage_view`, and the difference is the audience: a
    listing must not show a reviewer a phantom finding, while this is the immediate answer to
    "what happened to the thing I just saved", where "nobody scanned it" is the single most
    important fact and rendering it as `pass` would be a lie about a gate.
    """
    if not isinstance(scan, dict):
        return {"result": "pending", "hits": []}
    hits = [
        {"field": str(hit.get("field") or ""), "kind": str(hit.get("kind") or "")}
        for hit in (scan.get("hits") or [])
        if isinstance(hit, dict)
    ]
    return {"result": str(scan.get("result") or "pending"), "hits": hits}


def summary_for(env: CandidateEnvelope) -> SessionSummary:
    """The `SessionSummary` a stage is handed for *env*. Entity-free by construction.

    Comes off the envelope's own `ValidationSnapshot` when it has one — the same reason
    `completion.py` gives for reading the snapshot rather than a live session: a review item
    must not stop being editable when the session's TTL expires. A candidate with no snapshot
    gets one rebuilt from the envelope's provenance fields, whose `user_id` is EMPTY: the
    envelope does not carry a session owner, and inventing one would let a stage scope a write
    to the wrong person. Every collection is empty because nothing on this path reads one —
    filling them with plausible content would be worse than leaving them honest.
    """
    if env.revalidation is not None:
        return env.revalidation.to_summary()
    return SessionSummary(
        session_id=env.source_session,
        user_id="",
        scope_ref="",
        trace_id=env.source_trace,
        content_hash=env.content_hash,
        turns=(),
        tool_calls=(),
        blueprint_usages=(),
        askuser_exchanges=(),
        failed_fixed_sql=(),
        accepted_signal=None,
    )


def stage_scanner(stages: tuple[CandidateStage, ...]):
    """A `KnowledgeScanner` over *stages* — the hook the knowledge reviser withholds on.

    THE SAME `settle_entity_scan` THE EDITOR CALLS, over the SAME stage tuple, so "would this
    draft be clean if applied" and "was this row clean when applied" are one computation. A
    separately-constructed scanner would be a second definition of what a leak is, which is the
    mistake `settle_entity_scan`'s own docstring refuses at the layer below.

    With no gate in *stages* it returns the `pending` sentinel, which the reviser reads as "not
    a pass" and therefore withholds — fail-closed, and the correct posture for a deployment that
    scanned nothing.
    """

    async def scan(env: CandidateEnvelope) -> dict[str, Any]:
        return await settle_entity_scan(
            stages, env, StageContext(summary=summary_for(env), verdict=KNOWLEDGE_EDIT_VERDICT)
        )

    return scan


# How deep the `structured` walk descends before it refuses. Matches
# `revise/knowledge_schema.py::_MAX_SWEEP_DEPTH` for the same reason it exists there: untrusted
# input nests arbitrarily, and a bare recursion inside a request handler is a cheap way to blow
# the stack of the process holding the review queue.
_MAX_STRUCTURED_DEPTH = 8


def _refuse(sentence: str) -> KnowledgeEditInputError:
    """One refusal, phrased the way the other intake refusals are: what, and what to do.

    The reader's sentences name the offending field and state the rule; a bare "too large"
    would leave a reviewer guessing which of five fields to shorten. Same shape, so the two
    kinds of 422 this path can answer read as one vocabulary.
    """
    return KnowledgeEditInputError(sentence)


def _cap_text(value: Any, limit: int, at: str, *, why: str) -> None:
    """Refuse a string longer than *limit*. A non-string is NOT this check's business.

    Type is the intake reader's job and it runs on the same payload; objecting here as well
    would give one mistake two sentences, which is the thing this module refuses to do.
    """
    if isinstance(value, str) and len(value) > limit:
        raise _refuse(
            f"candidate.payload.{at} is {len(value)} characters — at most {limit} is stored. "
            f"{why}"
        )


def check_payload_size(payload: dict[str, Any]) -> None:
    """Refuse a `global_knowledge` payload larger than the reviser is allowed to author.

    ⚠ THE HUMAN WRITE PATH HAD NO SIZE BOUND AT ALL. `revise/knowledge_schema.py` truncates
    every field a MODEL proposes, so the assistant cannot author an oversized fact; a reviewer
    token posting straight to `apply_knowledge` (or a per-user record promoted through K2)
    faced nothing, and the payload it stores is then walked by the regex+NER scanner, rendered
    onto a card and eventually landed. The caps are the reviser's OWN numbers, imported rather
    than restated, so the two answers to "how long may a fact be" cannot drift apart.

    ⚠ NOT IN `extractor/validation.py`. That reader also governs the MINED path, and adding a
    length refusal there would start declining candidates the loop accepts today — the
    tightening §F.1.b declines to make, for the same reason.

    `structured` is walked to its leaves because intake deliberately permits nesting (§F.1.b):
    capping only the top level would leave the whole payload unbounded one key down.
    """
    _cap_text(
        payload.get("statement"),
        MAX_STATEMENT_CHARS,
        "statement",
        why=(
            "the statement becomes the whole text of one landed knowledge chunk; a fact this "
            "long is several facts, and this is the same cap the knowledge assistant writes "
            "under"
        ),
    )
    for label in ("knowledge_type", "scope"):
        _cap_text(
            payload.get(label),
            MAX_LABEL_CHARS,
            label,
            why=f"{label} is a short LABEL, not prose",
        )

    terms = payload.get("related_terms")
    if isinstance(terms, list):
        if len(terms) > MAX_TERMS:
            raise _refuse(
                f"candidate.payload.related_terms carries {len(terms)} terms — at most "
                f"{MAX_TERMS} are stored. These are recall terms for ONE fact; a fact needing "
                "more than that is several facts."
            )
        for index, term in enumerate(terms):
            _cap_text(
                term,
                MAX_TERM_CHARS,
                f"related_terms[{index}]",
                why="a related term is a phrase this fact should be recalled by, not prose",
            )

    structured = payload.get("structured")
    if isinstance(structured, dict):
        _check_structured(structured, at="structured", depth=0, budget=[MAX_TERMS])


def _check_structured(node: Any, *, at: str, depth: int, budget: list[int]) -> None:
    """Walk `structured` to its leaves, capping key length, leaf length and leaf COUNT.

    *budget* is a one-element list rather than a return value so the count is shared across
    every branch of the walk: the bound that matters is how many supporting pairs the payload
    carries in total, not how many sit under any one key.
    """
    if depth > _MAX_STRUCTURED_DEPTH:
        raise _refuse(
            f"candidate.payload.{at} nests deeper than {_MAX_STRUCTURED_DEPTH} levels — "
            "supporting detail for one fact is a flat set of pairs, and a structure this deep "
            "is not read by anything that lands it."
        )
    if isinstance(node, dict):
        for key, value in node.items():
            _cap_text(
                key,
                MAX_TERM_CHARS,
                f"{at}.{key[:40] if isinstance(key, str) else key}",
                why="a structured key names a supporting detail; it is a label, not prose",
            )
            _check_structured(value, at=f"{at}.{key}", depth=depth + 1, budget=budget)
        return
    if isinstance(node, list):
        for index, item in enumerate(node):
            _check_structured(item, at=f"{at}[{index}]", depth=depth + 1, budget=budget)
        return
    budget[0] -= 1
    if budget[0] < 0:
        raise _refuse(
            f"candidate.payload.structured carries more than {MAX_TERMS} values in total — "
            "these are supporting pairs for ONE fact, and a fact needing more than that is "
            "several facts."
        )
    _cap_text(
        node,
        MAX_TERM_CHARS,
        at,
        why="a structured value is a supporting detail; state the fact in `statement`",
    )


def _statement_digest(payload: dict[str, Any]) -> str:
    """sha256 of the payload's current statement — never the statement. See `KnowledgeEdit`."""
    statement = payload.get("statement")
    text = statement if isinstance(statement, str) else ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class KnowledgeEditor:
    """Admit a payload into a `global_knowledge` candidate, re-adjudicating on the way in.

    `stages` EMPTY is a legal but DEGRADED wiring FOR K1, exactly as it is for
    `ParameterizationCompleter`: the payload still faces intake validation, but the scan settles
    at the `pending` sentinel, which `_entity_scan_is_actionable` refuses — so the row can be
    edited and rejected and never approved. Legal because an offline dev inbox has no write
    plane; logged, because that is not visible from the 200 the reviewer gets.

    ⚠ IT IS NOT LEGAL FOR K2, and `can_scan` is how the promote path finds out (§F.1.a). The
    difference is the INPUT, not the wiring: an edited fact is text a reviewer typed and can
    re-read, while a promoted one is entity-bearing by construction. Degraded-but-honest is an
    acceptable answer for the first and not for the second.
    """

    store: CandidateStore
    stages: tuple[CandidateStage, ...] = field(default_factory=tuple)

    @property
    def can_scan(self) -> bool:
        """Is an S5 gate wired here — i.e. will `admit` settle a REAL verdict?

        ⚠ EXISTS SO A CALLER WHOSE INPUT IS ENTITY-BEARING BY CONSTRUCTION CAN REFUSE
        (design §F.1.a). `ReviewInbox.promote_user_knowledge` asks this before it builds a row:
        a per-user fact went to the private store BECAUSE it named someone, so admitting one
        with the `pending` sentinel would list raw entity-bearing text on the SHARED review
        queue. The capability is published here, rather than the inbox reading `.stages`,
        because "can this editor scan" is the editor's fact — and it is answered by
        `leakage_stage`, the same lookup `settle_entity_scan` uses, so the answer and the
        behaviour cannot disagree.

        K1 does NOT consult it: an unscanned edit is degraded but HONEST — the reviewer typed
        the text and can see what they typed — and the row stays unapprovable either way.
        """
        return leakage_stage(self.stages) is not None

    async def admit(
        self,
        env: CandidateEnvelope,
        *,
        payload: dict[str, Any],
        route_reason: str,
        before_status: str | None = None,
        expect_absent: bool = False,
    ) -> KnowledgeEditResult:
        """Validate, scan, stamp and write *payload* onto *env*. See the module docstring.

        TWO PRECONDITIONS, and the caller says which. *before_status* is the status the caller
        READ the row at (defaulting to the envelope's own, which is what K1 wants).
        *expect_absent* is K2's: the promote button builds a row at a DETERMINISTIC id, so what
        must still be true is that nobody else created it — two reviewers pressing the same
        button is exactly the collision that id makes possible. Both go through the same
        `guarded_put`; what must never exist is a third caller with no precondition at all.

        Raises `KnowledgeEditInputError` (422) for a payload intake would decline, and
        `CompletionRaceError` (409) when the row moved. Everything else is a result.
        """
        # SIZE FIRST, and cheapest first is not the reason: everything after this line WALKS
        # the payload — the intake reader, the regex+NER scanner, the card — so the bound has
        # to be established before any of them is handed an unbounded string.
        check_payload_size(payload)
        decline = validate_payload(KNOWLEDGE_TYPE, payload)
        if decline is not None:
            # THE READER'S OWN SENTENCE, verbatim — see `KnowledgeEditInputError`.
            raise KnowledgeEditInputError(decline.detail or decline.reason)

        edited = replace(
            env,
            payload=dict(payload),
            status=CandidateStatus.IN_REVIEW,
            route_reason=route_reason,
            # ⚠ STAMPED ONLY WHEN A HUMAN ACTUALLY EDITED SOMETHING. K2 CREATES the row, and
            # creation is not an edit: stamping it recorded `edits=1` against a
            # `previous_statement_sha256` of the EMPTY string (the envelope's payload is empty
            # by construction at that point), so the first real edit afterwards read "2" and
            # the badge counted one revision that never happened. A row nobody has edited
            # carries no badge, which is also what makes the badge's presence meaningful.
            knowledge_edit=(
                self._stamp(env)
                if route_reason == ROUTE_REASON_EDITED
                else env.knowledge_edit
            ),
        )
        # THE SCAN RUNS OVER THE NEW PAYLOAD, not the old one, and that is the whole point of
        # doing it here rather than trusting the verdict already on the row: the stored verdict
        # was settled about DIFFERENT CONTENT, and it is exactly what the card and the approve
        # guard consult next. `_still_declined` makes the same move for the same reason.
        edited = replace(
            edited, entity_scan=await settle_entity_scan(self.stages, edited, self._ctx(env))
        )
        if not self.stages:
            _logger.warning(
                "learning inbox: admitted a %s payload for %s with NO write-router pipeline "
                "wired — its entity scan stays at the `pending` sentinel, which every approve "
                "guard refuses. This is the offline dev posture; a deployment that means to "
                "land edited knowledge must wire the stages.",
                KNOWLEDGE_TYPE,
                env.candidate_id,
            )
        await guarded_put(
            self.store,
            replace(env, status=before_status if before_status is not None else env.status),
            edited,
            operation="knowledge edit",
            expect_absent=expect_absent,
        )
        _logger.info(
            "learning inbox: %s payload of %s admitted (%s) — status=%s, entity_scan=%s",
            KNOWLEDGE_TYPE,
            env.candidate_id,
            route_reason,
            edited.status,
            edited.entity_scan.get("result"),
        )
        return KnowledgeEditResult(envelope=edited, entity_scan=edited.entity_scan)

    def _ctx(self, env: CandidateEnvelope) -> StageContext:
        return StageContext(summary=summary_for(env), verdict=KNOWLEDGE_EDIT_VERDICT)

    @staticmethod
    def _stamp(env: CandidateEnvelope) -> KnowledgeEdit:
        """The additive edit badge, counting up from whatever is already on the row.

        The digest is of the statement BEING REPLACED, taken before the new payload is written,
        so a chain of edits leaves a chain of digests rather than a count with no subject.
        """
        previous = env.knowledge_edit
        return KnowledgeEdit(
            applied_at=now_iso(),
            previous_statement_sha256=_statement_digest(env.payload),
            edits=(previous.edits + 1) if previous is not None else 1,
        )


__all__ = [
    "KNOWLEDGE_EDIT_VERDICT",
    "KNOWLEDGE_TYPE",
    "ROUTE_REASON_EDITED",
    "ROUTE_REASON_PROMOTED",
    "KnowledgeEditInputError",
    "KnowledgeEditResult",
    "KnowledgeEditor",
    "KnowledgeEditorUnavailableError",
    "check_payload_size",
    "entity_scan_view",
    "stage_scanner",
    "summary_for",
]
