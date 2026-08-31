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
import unicodedata
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


# --- The S4 parameterization judge's assessment (design §D) -------------------
# HERE, not in `candidate/verdicts.py` where the envelope's other stage stamps live, for
# the same reason `CoverageAssessment` is here: the ENVELOPE imports this module, so this
# module may not import the envelope's package. `verdicts.py` is the right home by theme
# and the wrong one by import direction, and the direction wins.

# The closed verdict vocabulary. Equality-compared and used as the audit dataset's `GROUP BY`
# key, so it is a MEMBER test at the parse boundary: anything else would silently become its own
# bucket in every distribution query anybody writes over these rows.
ParamVerdict = Literal["ok", "revise", "reject"]
PARAM_VERDICTS: tuple[ParamVerdict, ...] = ("ok", "revise", "reject")

# The severity ladder. `A` is the only one that could ever authorize a destructive action, which
# is exactly why the parse boundary down-casts anything unrecognized to `C`.
FindingClass = Literal["A", "B", "C"]
FINDING_CLASSES: tuple[FindingClass, ...] = ("A", "B", "C")
WEAKEST_FINDING_CLASS: FindingClass = "C"

# Bounds. Every one of these is a JSON leaf in a retained document and an interpolation in a log
# line, so each is capped: a runaway generation must not be able to inflate the audit bucket one
# row at a time.
MAX_FEEDBACK_CHARS = 600
MAX_NOTE_CHARS = 300
MAX_CRITERION_CHARS = 80
MAX_FINDINGS = 12


@dataclass(frozen=True)
class ParamFinding:
    """One objection, with the parameterization entry it is about.

    `entry_index` points into `payload["parameterization"]`. It is advisory: an out-of-range
    index drops the FINDING and never the verdict, because a model miscounting a list position
    says nothing about whether its objection is real.
    """

    finding_class: FindingClass
    criterion: str
    note: str
    entry_index: int | None = None

    def to_doc(self) -> dict[str, Any]:
        return {
            "class": self.finding_class,
            "criterion": self.criterion,
            "note": self.note,
            "entry_index": self.entry_index,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> ParamFinding:
        raw_class = doc.get("class")
        index = doc.get("entry_index")
        return cls(
            finding_class=(
                raw_class if raw_class in FINDING_CLASSES else WEAKEST_FINDING_CLASS
            ),
            # Same rehydration guards as the assessment's `feedback`: these are rendered on
            # a card and serialized in a listing response, so a stored surrogate here is the
            # same 500 one field over.
            criterion=_rehydrated_text(doc.get("criterion"), limit=MAX_CRITERION_CHARS),
            note=_rehydrated_text(doc.get("note"), limit=MAX_NOTE_CHARS),
            entry_index=(
                index if isinstance(index, int) and not isinstance(index, bool) else None
            ),
        )


def _rehydrated_text(raw: Any, *, limit: int) -> str:
    """A store string that is SAFE TO SERIALIZE, capped. See `ParamAssessment.from_doc`.

    Two hazards a plain `str(...)[:n]` does not survive, both reachable from a document a human
    edited through cbq and neither caught by the model-output parse boundary (which never sees
    a stored doc):

      * a LONE SURROGATE (an unpaired UTF-16 half) — legal in a Python str, and json.dumps
        will emit it, but encoding the response body raises `UnicodeEncodeError`, surfacing
        as a 500 from a LISTING endpoint. One poisoned row makes the whole queue unreadable.
      * CONTROL CHARACTERS — this text reaches log lines, where a newline is a forged entry.

    Surrogates are dropped rather than escaped: the character carries no meaning to recover,
    and the reviewer-facing question is whether the row renders at all.
    """
    if not isinstance(raw, str):
        return ""
    cleaned = "".join(
        " " if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp") else ch
        for ch in raw
    )
    return " ".join(cleaned.split())[:limit]


def _rehydrated_confidence(raw: Any) -> float:
    """A stored confidence as a usable float, or `0.0`.

    ⚠ `float(raw)` RAISES `OverflowError` for an int too large to convert — and the range test
    that was supposed to reject it called `float()` to do so, so the guard was the crash. The
    parse boundary (`paramjudge/schema.py::_confidence`) already wraps the conversion; this path
    did not, and this is the path that reads what a human can type into the store.

    Same three guards, same order: TYPE (`bool` first — it passes `isinstance(x, int)`),
    CONVERSION, then RANGE, which rejects NaN without a separate test.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    try:
        value = float(raw)
    except (OverflowError, ValueError):
        return 0.0
    return value if 0.0 <= value <= 1.0 else 0.0


@dataclass(frozen=True)
class ParamAssessment:
    """What the judge said about one blueprint's parameterization."""

    verdict: ParamVerdict
    feedback: str
    confidence: float
    findings: tuple[ParamFinding, ...] = ()

    @property
    def has_class_a(self) -> bool:
        """Is there a finding that says the blueprint is WRONG, not merely narrow?

        Phase D-2's discard gate (design §D.4 precondition 6) and the card's marker both read
        this. It lives here rather than at either call site so the two can never disagree about
        what "serious" means.
        """
        return any(f.finding_class == "A" for f in self.findings)

    def to_doc(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "feedback": self.feedback,
            "confidence": self.confidence,
            "findings": [f.to_doc() for f in self.findings],
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> ParamAssessment:
        """Rehydrate, NORMALIZING rather than trusting.

        Every field here has been through a KV store a human can write to directly, so this
        mirrors the parse boundary's posture: an unusable verdict reads as `ok` (the inert one)
        and an unusable confidence as 0.0, because the alternative — raising inside a listing
        endpoint — turns one bad document into an unreadable queue.

        ⚠ THIS PATH NEEDS GUARDS THE PARSE BOUNDARY DOES NOT, and the first draft assumed the
        reverse. `paramjudge/schema.py` reads MODEL output; this reads STORED output, which has
        strictly more failure shapes — an int too large to convert to a float, and a lone
        surrogate that serializes to an unencodable response body. Both 500 a listing endpoint
        from one poisoned row. See `_rehydrated_text` / `_rehydrated_confidence`.
        """
        verdict = doc.get("verdict")
        confidence = doc.get("confidence")
        raw_findings = doc.get("findings")
        return cls(
            verdict=verdict if verdict in PARAM_VERDICTS else "ok",  # type: ignore[arg-type]
            feedback=_rehydrated_text(doc.get("feedback"), limit=MAX_FEEDBACK_CHARS),
            confidence=_rehydrated_confidence(confidence),
            findings=tuple(
                ParamFinding.from_doc(f)
                for f in (raw_findings if isinstance(raw_findings, list) else [])
                if isinstance(f, dict)
            ),
        )


# --- The S4 PARAMETERIZATION judgement (design §D) ----------------------------
# Its own record family, deliberately not a widening of `JudgeRecord`: that row is shaped
# around coverage (covered_by, best_similarity, cards_shown, authorizing_similarity) and
# not one of those fields means anything here. Two record types in one bucket, kept apart
# by `record_type`, is what this keyspace already does.

def param_judgement_ref(content_hash: str, candidate_id: str) -> str:
    """The deterministic audit key for one candidate's parameterization judgement.

    Keyed on CONTENT plus the candidate, so a redelivery of the same session re-reads the same
    row instead of paying for a second non-idempotent model call — the same idempotency the
    coverage judge gets from `judgement_fingerprint`, at the granularity this judge works on
    (one verdict per candidate, not one per session).
    """
    return f"paramjudge::{content_hash}::{candidate_id}"


@dataclass(frozen=True)
class ParamJudgeRecord:
    """One parameterization judgement as it lands in `learning_audit`.

    ⚠ ENTITY-BEARING via `feedback`, `findings[].note` AND `template` — an inline predicate keeps
    its literal value in the template body. That is precisely why this row lives in
    `learning_audit` (access-controlled, D51) and never on a span.

    `template` is on the row deliberately, and it is the field that makes the D-1 measurement
    possible at all: a verdict without the artifact it was about cannot be graded later, and the
    candidate it points at may have been approved, rejected or mutated by a completion since.

    THE MEASUREMENT'S JOIN IS `candidate_id` → THE CANDIDATE'S TERMINAL STATUS. There is
    deliberately no "was a human going to see this anyway" flag: the judge runs before the
    leakage and dedup stages, so at judgement time NOTHING has decided where the candidate
    routes, and a field that is structurally always `False` is worse than no field. A reviewer
    approving a candidate this judge flagged is the disagreement that matters, and it is
    recoverable by joining these rows to the candidate store on the id.
    """

    judgement_ref: str
    candidate_id: str
    session_id: str
    content_hash: str
    trace_id: str
    assessment: ParamAssessment
    # The template the verdict was taken about. See the class docstring.
    template: str
    # The judge model id. Verdicts are compared across time and models change; without this
    # the dataset silently mixes two judges.
    model: str
    judged_at: str  # ISO-8601
    # WOULD this verdict have discarded the candidate, under the phase D-2 rules? In D-1
    # there is no discard code path at all, so this is the ONLY output of the whole stage —
    # the column the rollout decision is read from.
    would_discard: bool = False
    # Always True in D-1. On the row so a dataset spanning the rollout is self-describing
    # rather than silently a mix of two regimes.
    shadow: bool = True
    record_type: str = "param_judgement"

    def to_doc(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "judgement_ref": self.judgement_ref,
            "candidate_id": self.candidate_id,
            "session_id": self.session_id,
            "content_hash": self.content_hash,
            "trace_id": self.trace_id,
            "template": self.template,
            "model": self.model,
            "judged_at": self.judged_at,
            "would_discard": self.would_discard,
            "shadow": self.shadow,
            **self.assessment.to_doc(),
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> ParamJudgeRecord | None:
        """Rehydrate, or `None` for a document that is not one of these.

        `None` rather than an exception, and it means "no verdict on file, judge it again" — the
        same three-shapes-collapse-to-one posture `read_judgement` documents. A row that cannot
        be read is indistinguishable from a row that is not there, and the direction that fails
        safe is doing the work twice.
        """
        if not isinstance(doc, dict) or doc.get("record_type") != "param_judgement":
            return None
        ref = doc.get("judgement_ref")
        if not isinstance(ref, str) or not ref:
            return None
        return cls(
            judgement_ref=ref,
            candidate_id=str(doc.get("candidate_id") or ""),
            session_id=str(doc.get("session_id") or ""),
            content_hash=str(doc.get("content_hash") or ""),
            trace_id=str(doc.get("trace_id") or ""),
            assessment=ParamAssessment.from_doc(doc),
            template=str(doc.get("template") or ""),
            model=str(doc.get("model") or ""),
            judged_at=str(doc.get("judged_at") or ""),
            would_discard=bool(doc.get("would_discard", False)),
            shadow=bool(doc.get("shadow", True)),
        )
