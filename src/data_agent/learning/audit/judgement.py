"""The coverage judge's DURABLE contract — the verdict vocabulary + the audit record
(plan §3b).

This module lives under `audit/` rather than under `judge/` on purpose, and the
placement is the design statement: **the verdict is a record first and a branch
second.** Its primary consumer is not the `if` that cancels an extraction — it is the
query, months from now, that answers "do the loop's re-derivations skew to
`existing-plus-delta`?", which is the documented trigger for building atomic
composable blueprints at all (plan §Decisions). A field shaped only to drive a branch
would have been a `bool`.

It is also what makes the drop AUDITABLE. The user's decision to let the judge drop
candidates outright was taken with the risk stated out loud: a wrong DROP is invisible
in a way a wrong KEEP is not — nothing downstream ever sees the thing that did not
happen. The agreed mitigation is that every drop leaves a durable, queryable row
carrying the session, the verdict, the reason, the artifact it was deemed covered by,
and the confidence, so "we dropped four thousand candidates last quarter" is a query
rather than a guess.

**Three placement consequences, each deliberate:**

  1. `learning_audit`, not the candidate store. A dropped candidate has NO envelope —
     that is the entire point of dropping before extraction — so the candidate store
     has nowhere to put it. `learning_audit` is also the bucket already provisioned for
     entity-bearing content, which `reason` is: it is free model prose about a real
     session and may name a department or a person. The same reason evidence quotes
     live here and not on a span.
  2. No import cycle. `candidate/models.py` stamps a `CoverageAssessment` on the
     envelope and `judge/judge.py` writes a `JudgeRecord`; if both types lived under
     `judge/`, importing `judge` from `candidate` would close a loop through
     `judge → audit → …`. Nothing in this module imports anything from the learning
     package, so it can be imported from anywhere in it.
  3. The verdict vocabulary is defined ONCE, here, next to the doc shape that
     persists it. The prompt's enum is derived from it (`judge/schema.py`), so the
     model can never be offered a value the store has no meaning for.

**The KV key is DETERMINISTIC, and that is the idempotency mechanism.** An LLM call is
not idempotent, so a judgement is keyed on the CONTENT it was rendered about
(`judgement_fingerprint`) and read back before the model is asked. The hash-based dedup
key in `DedupStage` is untouched and remains the race-safe first layer; this is a cache
in front of a model call, not a dedup key.

**Which re-runs it actually catches — corrected, because an earlier draft named the
wrong one.** That draft said "a crash between `processing` and `done` puts the message
back in the PEL", implying the reclaim path re-judges. QA traced it and it does not:
there is no `processing → processing` edge in the state machine, so the reclaimed
delivery CAS-mismatches in `_process` and returns `skip` without ever reaching
`_do_work`. The key earns its keep on the paths that DO re-run a judgement — a session
re-enqueued after a sweep, a peer racing the same work, a pipeline re-run over an
existing envelope, and (post-extraction) a re-extraction that mints fresh envelopes for
the same content. Worth stating precisely: "we tested the scenario in the comment" is a
false reassurance when the comment names a scenario that cannot happen.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from data_agent.untrusted import as_float

# --- the verdict vocabulary -------------------------------------------------------
#
# THE dataset. Three values, chosen so the distribution answers one question:
#
#   duplicate            — the corpus already carries this work. Nothing to learn.
#   existing-plus-delta  — an existing artifact covers MOST of it; this session adds a
#                          genuine increment (another dimension, another filter, one
#                          more step).
#   new                  — nothing in the corpus covers it.
#
# A skew to `existing-plus-delta` means the loop keeps re-deriving near-misses of what
# it owns, which is exactly the case atomic/composable blueprints would fix. A skew to
# `duplicate` means the RECALL path is failing (the agent had the artifact and did not
# offer it). A skew to `new` means the corpus is simply young. Three different projects
# hang off which one it is, so the field must distinguish all three — a
# `covered: bool` would have collapsed the first two, which are the two that matter.
CoverageVerdict = Literal["duplicate", "existing-plus-delta", "new"]

COVERAGE_VERDICTS: tuple[CoverageVerdict, ...] = ("duplicate", "existing-plus-delta", "new")

# The ONLY verdict that may cancel work. `existing-plus-delta` says by construction
# that something is left to learn, so dropping on it would discard the increment while
# recording that we knew it was there. Pinned in the judge's drop gate and asserted in
# the unit suite.
DROPPABLE_VERDICT: CoverageVerdict = "duplicate"

# Which side of the extractor call the judgement was taken on. Load-bearing for
# READING the dataset later: the two see materially different evidence and are held to
# different bars, so pooling them would average a well-informed verdict with a
# poorly-informed one.
#
#   pre_extraction  — sees the session summary: the question, the raw SQL WITH its
#                     literals, the tool trail. No generalization, no parameterization,
#                     no result grain. Cheapest place to drop, least informed.
#   post_extraction — sees the extracted candidate: the entity-free intent, the
#                     parameterized template, the grain, the rule ids.
JudgeStage = Literal["pre_extraction", "post_extraction"]

# What the loop DID with the verdict. Separate from the verdict itself because the bar
# is configuration: the same `duplicate @ 0.82` is a drop under one deployment and a
# proceed under another, and a stored verdict with no record of what was done with it
# cannot be re-read after a retune.
JudgeOutcome = Literal["dropped", "proceeded"]

# The N1QL discriminator. `learning_audit` is a single default collection holding
# EvidenceSnapshot docs and these, so every judgement query starts
# `WHERE record_type = 'judge_verdict'`. EvidenceSnapshot docs carry no `record_type`
# at all (they predate this and are never rewritten), so the predicate is exact rather
# than merely selective — a missing key can never match.
JUDGE_RECORD_TYPE = "judge_verdict"

_PRE_PREFIX = "judgement::pre::"
_POST_PREFIX = "judgement::post::"


def judgement_fingerprint(*parts: str) -> str:
    """A content fingerprint over everything the judge was SHOWN.

    **The key must bind CONTENT, not position, and an earlier cut of this module got
    that wrong on the post-extraction side.** It keyed on `candidate_id`, which is
    `candidate::<content_hash>::<ordinal>` — and `consumer.py::_run_extractor` says in
    its own comment that a re-extraction can emit "a different count/order". So:
    delivery 1 extracts ordinal-0 = candidate B, judged `duplicate`, dropped; ordinal-1
    = candidate A, novel. Crash before `done`. The redelivery re-extracts and now
    ordinal-0 is A — which would read B's stored verdict under B's old key. The gate is
    re-applied, so A only drops if A's own card union happens to contain the artifact
    that covered B at an in-band score; but when it does, A is discarded on a verdict
    rendered about different content and the audit `reason` describes B. That is
    precisely the unauditable drop `judge.py::_resolve_covered_by` refuses to allow,
    arriving through the cache instead of through the model.

    The equivalence class that makes reusing a model's answer legitimate is therefore
    "the judge would be shown exactly this again" — the rendered brief, plus the session
    content hash, plus the stage. Hashing the brief rather than picking out fields also
    means a change to `prompt.py`'s rendering invalidates every cached verdict, which is
    correct: a verdict about a differently-shaped question is not this question's
    verdict.

    A content-keyed MISS costs one small judge call. A position-keyed false HIT costs a
    session. Only one of those directions is acceptable here.
    """
    digest = hashlib.sha256()
    for part in parts:
        # Length-prefixed so `("ab", "c")` and `("a", "bc")` cannot collide — the
        # ordinary concatenation-ambiguity bug, and this input is partly attacker-shaped
        # (a session brief carries user text).
        digest.update(f"{len(part)}:".encode())
        digest.update(part.encode("utf-8", errors="replace"))
    return digest.hexdigest()


def pre_extraction_ref(fingerprint: str) -> str:
    """The KV key for one session's PRE-extraction judgement.

    *fingerprint* comes from `judgement_fingerprint` — see there for why this is content
    and not position. The pre-extraction side was never exposed to the ordinal bug (the
    session `content_hash` is content-derived and is the loop's own idempotency key),
    but it is keyed the same way so there is ONE rule rather than two, and so a change to
    the pre-extraction brief invalidates its cache too."""
    return f"{_PRE_PREFIX}{fingerprint}"


def post_extraction_ref(fingerprint: str) -> str:
    """The KV key for one candidate's POST-extraction judgement. See
    `judgement_fingerprint` — this key binds the candidate's CONTENT, deliberately not
    its `candidate_id`."""
    return f"{_POST_PREFIX}{fingerprint}"


@dataclass(frozen=True)
class CoverageAssessment:
    """One judgement, as the model gave it — the parsed, guarded model output.

    Separate from `JudgeRecord` because this is only the four fields the MODEL is
    responsible for. Everything else on the record (which threshold was in force, what
    we did about it, which artifacts were shown) is the CALLER's knowledge, and mixing
    the two would make it impossible to tell later whether a field was asserted by a
    language model or computed by the loop.

    Every field here has already passed `judge/schema.py::parse_assessment`, whose
    guards are derived from the operations performed below and downstream — see that
    function's table. In particular `confidence` is guaranteed a real float in
    `[0.0, 1.0]` (never a bool, never NaN, never an int too large to convert), because
    the only thing anyone ever does with it is compare it to a configured bar.

    It is stamped on `CandidateEnvelope.judge` as well as written to the audit store, so
    a human reading a candidate in the review inbox can see the machine already had an
    opinion about whether it was novel.
    """

    verdict: CoverageVerdict
    # The prior-art artifact id the model says covers this work, VERBATIM from the
    # block it was shown, or "" when it named none. Never trusted as a real id by the
    # drop gate — see `judge/judge.py::_drop_allowed`, which requires membership in the
    # set of ids actually shown, because an id nobody can look up makes the audit record
    # unauditable and the drop therefore unreviewable.
    covered_by: str = ""
    # The card's trust tier, resolved by the CALLER from `covered_by`, not asserted by
    # the model. "" when `covered_by` matched nothing shown.
    covered_by_tier: str = ""
    reason: str = ""
    confidence: float = 0.0

    def to_doc(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "covered_by": self.covered_by,
            "covered_by_tier": self.covered_by_tier,
            "reason": self.reason,
            "confidence": self.confidence,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> CoverageAssessment | None:
        """Rehydrate from a stored doc, or `None` when the stored verdict is not one of
        ours.

        **`None`, not a coerced `new`, and an earlier cut of this method had it wrong.**
        Coercing landed the value INSIDE the vocabulary, which the drop gate would then
        refuse only by accident (it tests equality against `duplicate`, not membership)
        — and, far worse, `judge.py::_judge` re-persists a reused assessment, so a
        corrupted stored verdict would be rewritten as a `new` no model ever gave,
        permanently replacing the original under the same key. That breaks this
        module's own never-fabricate rule at the one place it matters most. A
        rehydration failure is "no record on file", the caller re-judges, and one small
        model call is the entire cost.

        The OTHER fields stay NORMALIZING, and the asymmetry is deliberate: `verdict` is
        the field the record exists to carry and there is no safe default for it, while
        `reason`/`covered_by` are only ever read and a junk value there must not throw
        away a usable verdict. Every consumer treats `confidence` as a float (`>=`
        against a bar) and the text fields as strings (formatted into logs and docs), so
        a non-conforming stored value is coerced rather than raising inside a queue
        worker.
        """
        raw_verdict = doc.get("verdict")
        if raw_verdict not in COVERAGE_VERDICTS:
            return None
        confidence = doc.get("confidence")
        return cls(
            verdict=raw_verdict,  # type: ignore[arg-type]
            covered_by=doc.get("covered_by") if isinstance(doc.get("covered_by"), str) else "",
            covered_by_tier=(
                doc.get("covered_by_tier")
                if isinstance(doc.get("covered_by_tier"), str)
                else ""
            ),
            reason=doc.get("reason") if isinstance(doc.get("reason"), str) else "",
            confidence=(
                float(confidence)
                if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                else 0.0
            ),
        )


@dataclass(frozen=True)
class JudgeRecord:
    """One judgement as it lands in `learning_audit` — the durable, queryable row.

    Flat scalars only, deliberately. The queries this exists to serve are counts and
    distributions (`GROUP BY verdict`, `WHERE dropped = true AND judged_at >= …`), and
    a nested shape would make every one of them a nested-path expression against a
    bucket with one GSI.

    **`threshold` and `best_similarity` are on the row for a reason.** Both bars are
    operator-tunable, so a stored verdict with no record of the bar it was compared
    against cannot be re-interpreted after a retune — "how many of last quarter's drops
    would still drop at 0.95?" is answerable only if the row says what 0.90 meant at the
    time. `best_similarity` is the retrieval score that decided the judge was worth
    calling, which is what makes the band itself tunable from evidence.

    ENTITY-BEARING via `reason` (free model prose about a real session), which is
    exactly why this record lives in `learning_audit` and never on a span. The
    `learning.judge` span carries the shape — verdict, confidence, tier, dropped — and
    no prose.
    """

    judgement_ref: str
    stage: JudgeStage
    session_id: str
    content_hash: str
    trace_id: str
    assessment: CoverageAssessment
    outcome: JudgeOutcome
    # The drop bar in force when this verdict was taken (see the class docstring).
    threshold: float
    # The best prior-art confidence at the moment the judge was called — the retrieval
    # score that gated the CALL, NOT the judge's own confidence, and NOT necessarily the
    # score of the artifact the verdict names. See `authorizing_similarity`.
    best_similarity: float
    # How many prior-art cards the judge was actually shown. A `duplicate` asserted
    # against one card and against five are different levels of evidence.
    cards_shown: int
    # The judge model id. Verdicts are compared across time and the model changes;
    # without this the dataset silently mixes two judges.
    model: str
    judged_at: str  # ISO-8601
    # Present only for `post_extraction` (a pre-extraction drop has no candidate — that
    # is the point of it).
    candidate_id: str | None = None
    # Did the id the model named actually appear in the block it was shown? A `False`
    # here with a `duplicate` verdict is a CONFABULATION and is never allowed to drop;
    # it is recorded rather than discarded because the rate of it is the honest measure
    # of how much the judge can be trusted.
    covered_by_known: bool = False
    # Where the covering card came from — `graph` (landed / canon) or `corpus` (an
    # unlanded in-flight sibling in `learning_corpus`). On the row because it is one of
    # the five drop conditions and the ONLY one that is invisible from the other fields:
    # a reader looking at a `duplicate @ 0.99 / tier=learning / covered_by_known=true`
    # that did NOT drop can otherwise see no reason why.
    covered_by_origin: str = ""
    # The retrieval score of the artifact the verdict actually NAMES, `0.0` when it
    # named none. Distinct from `best_similarity`, and QA is the reason: the band gates
    # on the BEST card, but the drop is authorized by whichever card the model cites, so
    # a drop can be taken on an artifact scoring 0.05 while the row reports an unrelated
    # 0.85. The row stayed auditable — `covered_by` always named the real artifact — but
    # the number stored beside the decision was not the number behind it, which is
    # exactly the kind of quiet mismatch a threshold retune would later be reasoned from.
    authorizing_similarity: float = 0.0
    # The content fingerprint this judgement was keyed on (`judgement_fingerprint`).
    # Belt-and-braces on top of the key itself: the reuse path re-checks it, so a hand
    # written doc, a key collision or a future key-format change degrades to "no record
    # on file" and a re-judgement rather than to a verdict about different content.
    fingerprint: str = ""
    # WOULD this verdict have dropped, under the bar in force? Distinct from `dropped`
    # for exactly one reason: SHADOW MODE. It is the column the safe rollout reads —
    # "run for a week, record everything, discard nothing, then look at what we would
    # have thrown away". Outside shadow mode the two are always equal.
    would_drop: bool = False
    # Was the judge running in shadow mode when this row was written? Makes the row
    # self-describing, so a dataset spanning the rollout is not silently a mix of two
    # regimes.
    shadow: bool = False

    @property
    def dropped(self) -> bool:
        return self.outcome == "dropped"

    def to_doc(self) -> dict[str, Any]:
        doc = {
            "record_type": JUDGE_RECORD_TYPE,
            "judgement_ref": self.judgement_ref,
            "stage": self.stage,
            "session_id": self.session_id,
            "content_hash": self.content_hash,
            "trace_id": self.trace_id,
            "outcome": self.outcome,
            # Denormalized alongside `outcome` so the headline query
            # (`WHERE dropped = true`) needs no string comparison and reads the way the
            # question is asked.
            "dropped": self.dropped,
            "would_drop": self.would_drop,
            "shadow": self.shadow,
            "threshold": self.threshold,
            "best_similarity": self.best_similarity,
            "authorizing_similarity": self.authorizing_similarity,
            "cards_shown": self.cards_shown,
            "covered_by_known": self.covered_by_known,
            "covered_by_origin": self.covered_by_origin,
            "fingerprint": self.fingerprint,
            "model": self.model,
            "judged_at": self.judged_at,
            "candidate_id": self.candidate_id,
        }
        # FLATTENED, not nested: `GROUP BY verdict` is the whole point of the record.
        doc.update(self.assessment.to_doc())
        return doc

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> JudgeRecord | None:
        """Rehydrate a stored judgement, or `None` when the stored VERDICT is not one of
        ours.

        `None` propagates `CoverageAssessment.from_doc`'s refusal all the way to
        `AuditStore.read_judgement`, where the caller reads it as "no record on file"
        and re-judges. Every other field is normalizing — the read-through idempotency
        path feeds this straight into a drop decision, so a hand-edited doc must degrade
        rather than raise inside a queue worker — but the verdict has no safe default and
        inventing one would put a value in the dataset that no model ever gave."""
        assessment = CoverageAssessment.from_doc(doc)
        if assessment is None:
            return None
        stage = doc.get("stage")
        outcome = doc.get("outcome")
        return cls(
            judgement_ref=str(doc.get("judgement_ref", "")),
            stage=stage if stage in ("pre_extraction", "post_extraction") else "pre_extraction",  # type: ignore[arg-type]
            session_id=str(doc.get("session_id", "")),
            content_hash=str(doc.get("content_hash", "")),
            trace_id=str(doc.get("trace_id", "")),
            assessment=assessment,
            outcome=outcome if outcome in ("dropped", "proceeded") else "proceeded",  # type: ignore[arg-type]
            threshold=_float(doc.get("threshold")),
            best_similarity=_float(doc.get("best_similarity")),
            authorizing_similarity=_float(doc.get("authorizing_similarity")),
            cards_shown=int(doc["cards_shown"]) if isinstance(doc.get("cards_shown"), int) else 0,
            model=str(doc.get("model", "")),
            judged_at=str(doc.get("judged_at", "")),
            candidate_id=(
                doc["candidate_id"] if isinstance(doc.get("candidate_id"), str) else None
            ),
            covered_by_known=doc.get("covered_by_known") is True,
            covered_by_origin=(
                doc["covered_by_origin"]
                if isinstance(doc.get("covered_by_origin"), str)
                else ""
            ),
            # A non-str fingerprint reads as `""`, which can never equal a real
            # `judgement_fingerprint` — so a corrupt value degrades to a re-judgement,
            # the same safe direction as an unknown verdict.
            fingerprint=(
                doc["fingerprint"] if isinstance(doc.get("fingerprint"), str) else ""
            ),
            would_drop=doc.get("would_drop") is True,
            shadow=doc.get("shadow") is True,
        )


def _float(raw: Any) -> float:
    """A stored numeric as a float, or 0.0. `bool` is excluded because it is an `int`
    subclass and a stored `true` would read as a threshold of 1.0.

    DELIBERATELY UNBOUNDED, unlike `priorart/neo4j_index.py::_float`'s `[0, 1]`. The
    three fields this reads (`threshold`, `best_similarity`, `authorizing_similarity`)
    are the FORENSIC record of a judgement that already happened — nothing re-ranks or
    re-thresholds on them; they are read back for audit, telemetry and QA. Replacing a
    stored out-of-range number with a plausible in-range 0.0 would make the audit row
    say something that never happened, which is the opposite of what an audit row is
    for. The ranking path, where a broken score DOES change an outcome, passes bounds.
    """
    return as_float(raw)


__all__ = [
    "COVERAGE_VERDICTS",
    "DROPPABLE_VERDICT",
    "JUDGE_RECORD_TYPE",
    "CoverageAssessment",
    "CoverageVerdict",
    "JudgeOutcome",
    "JudgeRecord",
    "JudgeStage",
    "judgement_fingerprint",
    "post_extraction_ref",
    "pre_extraction_ref",
]
