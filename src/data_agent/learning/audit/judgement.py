"""The coverage judge's DURABLE contract — the verdict vocabulary + the audit record.

This module lives under `audit/` rather than `judge/` on purpose, and the placement is the
design statement: THE VERDICT IS A RECORD FIRST AND A BRANCH SECOND. Its primary consumer is
the query, months from now, that answers whether the loop's re-derivations skew to
`existing-plus-delta`; a field shaped only to drive a branch would have been a `bool`. It is
also what makes the drop AUDITABLE — a wrong DROP is invisible in a way a wrong KEEP is not,
and the agreed mitigation is that every drop leaves a durable, queryable row.

Three placement consequences, each deliberate: (1) `learning_audit`, not the candidate store,
because a dropped candidate has NO envelope and because `reason` is free model prose about a
real session; (2) no import cycle — nothing here imports anything from the learning package,
so `candidate/models.py` and `judge/judge.py` can both use it; (3) the verdict vocabulary is
defined ONCE, next to the doc shape that persists it, and the prompt's enum is derived from
it, so the model can never be offered a value the store has no meaning for.

THE KV KEY IS DETERMINISTIC, and that is the idempotency mechanism: an LLM call is not
idempotent, so a judgement is keyed on the CONTENT it was rendered about
(`judgement_fingerprint`) and read back before the model is asked. This is a cache in front of
a model call, not a dedup key — `DedupStage`'s hash key remains the race-safe first layer.
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

    THE KEY MUST BIND CONTENT, NOT POSITION. Keying on `candidate_id` —
    `candidate::<content_hash>::<ordinal>` — was wrong, because a re-extraction can emit a
    different count and order: ordinal-0 on the redelivery may be a different candidate, which
    would then read the first one's stored verdict. The gate is re-applied, so it only drops when
    the new candidate's own card union happens to contain the covering artifact at an in-band
    score — but when it does, a candidate is discarded on a verdict rendered about different
    content and the audit `reason` describes the wrong one.

    The equivalence class that makes reusing a model's answer legitimate is "the judge would be
    shown exactly this again": the rendered brief, the session content hash, and the stage.
    Hashing the brief also means a change to `prompt.py`'s rendering invalidates every cached
    verdict, which is correct. A content-keyed MISS costs one small judge call; a position-keyed
    false HIT costs a session.
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

    *fingerprint* comes from `judgement_fingerprint`. The pre-extraction side was never exposed
    to the ordinal bug, but it is keyed the same way so there is ONE rule rather than two, and so
    a change to the pre-extraction brief invalidates its cache too.
    """
    return f"{_PRE_PREFIX}{fingerprint}"


def post_extraction_ref(fingerprint: str) -> str:
    """The KV key for one candidate's POST-extraction judgement.

    This key binds the candidate's CONTENT, deliberately not its `candidate_id` — see
    `judgement_fingerprint`.
    """
    return f"{_POST_PREFIX}{fingerprint}"


@dataclass(frozen=True)
class CoverageAssessment:
    """One judgement, as the model gave it — the parsed, guarded model output.

    Separate from `JudgeRecord` because this is only the four fields the MODEL is responsible
    for; everything else on the record is the CALLER's knowledge, and mixing the two would make it
    impossible to tell later whether a field was asserted by a language model or computed by the
    loop. Every field here has already passed `judge/schema.py::parse_assessment`, so
    `confidence` is guaranteed a real float in `[0.0, 1.0]`. It is stamped on
    `CandidateEnvelope.judge` as well as written to the audit store, so a human reading the inbox
    can see the machine already had an opinion.
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
        """Rehydrate from a stored doc, or `None` when the stored verdict is not one of ours.

        `None`, NOT a coerced `new`: coercing would land the value INSIDE the vocabulary, which the
        drop gate refuses only by accident (it tests equality against `duplicate`, not membership) —
        and `judge.py::_judge` re-persists a reused assessment, so a corrupted stored verdict would be
        rewritten as a `new` no model ever gave, permanently replacing the original under the same
        key. A rehydration failure is "no record on file" and costs one small model call.

        The OTHER fields stay NORMALIZING, deliberately: `verdict` is the field the record exists to
        carry and has no safe default, while `reason`/`covered_by` are only ever read and a junk value
        there must not throw away a usable verdict.
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

    Flat scalars only, because the queries this exists to serve are counts and distributions, and
    a nested shape would make every one of them a nested-path expression against a bucket with one
    GSI. `threshold` and `best_similarity` are on the row because both bars are operator-tunable:
    a stored verdict with no record of the bar it was compared against cannot be re-interpreted
    after a retune. ENTITY-BEARING via `reason`, which is exactly why this record lives in
    `learning_audit` and never on a span — the span carries the shape and no prose.
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
        """Rehydrate a stored judgement, or `None` when the stored VERDICT is not one of ours.

        `None` propagates the refusal to `AuditStore.read_judgement`, where the caller reads it as "no
        record on file" and re-judges. Every other field is normalizing — the read-through idempotency
        path feeds this straight into a drop decision, so a hand-edited doc must degrade rather than
        raise inside a queue worker — but the verdict has no safe default, and inventing one would put
        a value in the dataset that no model ever gave.
        """
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
    """A stored numeric as a float, or 0.0; `bool` is excluded (a stored `true` would read as 1.0).

    DELIBERATELY UNBOUNDED, unlike the ranking path's `[0, 1]`. The three fields this reads are
    the FORENSIC record of a judgement that already happened — nothing re-ranks on them — so
    replacing a stored out-of-range number with a plausible 0.0 would make the audit row say
    something that never happened.
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
