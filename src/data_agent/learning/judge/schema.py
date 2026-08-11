"""The coverage judge's forced-tool schema and the guard on what comes back (plan §3b).

One tool, one call, no retries. The judge exists to CANCEL a much larger extractor
call, so every turn it spends eats the saving it was built to produce; a malformed
response therefore fails OPEN (extraction proceeds exactly as it does today) rather
than being re-asked. The extractor's three-attempt retry budget is the right posture
for a call whose output is the product — it is the wrong posture for a call whose
output is an optimization.

**The response is new untrusted model output**, and the guards below are derived from
what downstream code READS and the OPERATION it performs on it — not from the field
names, and not from the English sentence in the prompt. That distinction has cost this
repo seven rounds on the same class; the most recent (plan §Method notes) was a guard
written as `if binds_to:` to mirror an intent sentence while the downstream read was
`is not None`, which agreed on every input except `""`. So:

  field       every downstream read, and its OPERATION            ⇒ requirement
  ---------   -------------------------------------------------   -----------------
  verdict     `assessment.verdict == DROPPABLE_VERDICT`
              (equality against a closed set);
              `doc["verdict"]` → N1QL `GROUP BY verdict`
              (the dataset's grouping key)                         ⇒ str, and a MEMBER
              A value outside the set would silently become its       of COVERAGE_
              own bucket in every distribution query ever run.       VERDICTS
  covered_by  `covered_by in shown_ids` (set membership, in the
              drop gate); `f"…{covered_by}…"` into a log line;
              stored for a human to look the artifact up           ⇒ str
              The membership test is what handles `""`, an
              unknown id and a hallucinated id IDENTICALLY —
              there is no separate emptiness check, because the
              operation the value is subjected to already
              distinguishes usable from unusable.
  reason      `doc["reason"]` (a JSON leaf in a KV doc);
              `_logger.info("… %s", reason)` (one log line)        ⇒ str, flattened to
              A newline forges a log record; an unbounded value       one line, capped
              is an unbounded doc.
  confidence  `confidence >= threshold` (float comparison);
              `f"{confidence:.2f}"`; `json`-serialized into a doc  ⇒ real number in
              `bool` is an `int` subclass, so `True >= 0.9` is        [0.0, 1.0]; NOT a
              True and a perfect false positive. `float(10**400)`     bool; not NaN;
              raises OverflowError. `float("nan")` serializes to      not ±inf
              invalid JSON and fails EVERY comparison, so it
              would read as a silent "never drop" while
              poisoning the stored dataset.

`covered_by` is REQUIRED in the schema rather than nullable, with `""` as the "none"
value. A nullable field gives a model two ways to say the same thing and gives this
module two shapes to guard; the empty string is already handled by the membership test
that the value's real consumer performs.

**Out-of-range is a REJECTION, not a clamp.** A confidence of `5.0` or `-1.0` is not a
strong or weak signal, it is a broken response — and clamping `5.0` to `1.0` would turn
a malfunction into the most confident drop the system can express. The whole assessment
is discarded and extraction proceeds. Same posture as
`extractor/prior_art.py::_score`, for the same reason, on a value with far more
authority.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any

from data_agent.runtime.model.client import ModelTurnResult

from ..audit.judgement import COVERAGE_VERDICTS, CoverageAssessment

_logger = logging.getLogger(__name__)

JUDGE_TOOL_NAME = "record_coverage"

# The prompt enum is DERIVED from the stored vocabulary, never re-spelled. A model can
# therefore not be offered a verdict the record has no meaning for, and adding a fourth
# verdict is one edit in `audit/judgement.py`.
VERDICT_ENUM: list[str] = list(COVERAGE_VERDICTS)

# `reason` is a JSON leaf in a TTL'd KV doc and one line in a log. Long enough for a
# real explanation a human can act on, short enough that a runaway generation cannot
# inflate the audit bucket one document at a time.
_MAX_REASON_CHARS = 600
# An artifact id. `bp::<sha256>` is 68 characters; this leaves headroom for a knowledge
# id without letting an unbounded string into the doc.
_MAX_COVERED_BY_CHARS = 160


_JUDGE_PARAMETERS = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": VERDICT_ENUM,
            "description": (
                "'duplicate' — an artifact in the PRIOR ART block already does this "
                "session's analytical work; re-deriving it would add nothing. "
                "'existing-plus-delta' — an artifact covers most of it, but this "
                "session adds a real increment (an extra grouping, an extra filter, an "
                "extra step). 'new' — nothing listed covers it."
            ),
        },
        "covered_by": {
            "type": "string",
            "description": (
                "The id of the artifact that covers this work, copied VERBATIM from the "
                "PRIOR ART block (the `id=` field). Empty string when no listed "
                "artifact covers it. Never invent an id: an id that is not in the block "
                "is treated as no id at all."
            ),
        },
        "reason": {
            "type": "string",
            "description": (
                "One or two sentences a human reviewer can act on: what the session "
                "computed, what the named artifact computes, and where they differ. "
                "For 'existing-plus-delta', state the delta explicitly."
            ),
        },
        "confidence": {
            "type": "number",
            "description": (
                "0.0-1.0 — how sure you are of the VERDICT (not of the session's "
                "quality). A high value on 'duplicate' cancels the extraction entirely "
                "and the work is discarded, so reserve it for cases where the artifact "
                "plainly computes the same thing at the same grain."
            ),
        },
    },
    "required": ["verdict", "covered_by", "reason", "confidence"],
}


def build_judge_tool() -> dict[str, Any]:
    """The single forced tool the judge must call (the runtime's canonical flat shape,
    same as `extractor/schema.py::build_extractor_tool`)."""
    return {
        "type": "function",
        "name": JUDGE_TOOL_NAME,
        "description": (
            "Record whether the corpus already covers this work. Call this exactly "
            "once. Do not emit free text."
        ),
        "parameters": _JUDGE_PARAMETERS,
    }


def parse_assessment(result: ModelTurnResult) -> CoverageAssessment | None:
    """One judge turn → a guarded `CoverageAssessment`, or `None` when unusable.

    `None` is the FAIL-OPEN signal and it covers every failure shape: no tool call, the
    wrong tool, non-dict arguments, an unrecognized verdict, an unusable confidence. The
    caller's contract on `None` is "behave exactly as if no judge were wired", so there
    is no need for the caller to distinguish the shapes — but each one is logged
    distinctly, because they mean different things to whoever tunes the prompt.

    Returns a partial-but-valid assessment where a field can degrade safely
    (`covered_by`/`reason`) and `None` where it cannot (`verdict`/`confidence`). The
    split is not stylistic: the two fields that can degrade are only ever READ, while
    the two that cannot are the two the drop DECISION is computed from.

    **`tool_calls` is checked as a CONTAINER and as MEMBERS, and QA had to point that
    out — the eighth sighting of this class in this repo.** `ModelTurnResult` is typed,
    but `ModelClient` is a Protocol and this function is fed whatever an implementation
    returns. `next(c for c in tool_calls if c.name == ...)` char-explodes a bare string
    into `AttributeError: 'str' object has no attribute 'name'` and raises `TypeError`
    on `None`. The correct version was already in the same package —
    `prior_art.py::lookup_prior_art` performs exactly this container-AND-members check
    on its own untrusted sequence — and the guard here is derived the same way, from
    the two operations performed below (`for … in`, then `.name` per member) rather
    than from the type annotation.
    """
    calls = result.tool_calls
    if not isinstance(calls, (list, tuple)):
        _logger.warning(
            "judge: tool_calls was %s, not a sequence — failing OPEN",
            type(calls).__name__,
        )
        return None
    call = next(
        (
            c
            for c in calls
            if getattr(c, "name", None) == JUDGE_TOOL_NAME and hasattr(c, "arguments")
        ),
        None,
    )
    if call is None:
        _logger.warning(
            "judge: model did not call %s (got %r, free text: %s) — failing OPEN, "
            "extraction proceeds",
            JUDGE_TOOL_NAME,
            [getattr(c, "name", type(c).__name__) for c in calls],
            result.assistant_text is not None,
        )
        return None
    arguments = call.arguments
    if not isinstance(arguments, dict):
        _logger.warning(
            "judge: %s arguments were %s, not an object — failing OPEN",
            JUDGE_TOOL_NAME,
            type(arguments).__name__,
        )
        return None

    verdict = arguments.get("verdict")
    if verdict not in COVERAGE_VERDICTS:
        # NOT defaulted to "new". A fabricated verdict would be indistinguishable in the
        # store from one the model actually gave, and this record IS the dataset a later
        # build/don't-build decision reads.
        _logger.warning(
            "judge: verdict %r is not one of %s — failing OPEN and recording NOTHING "
            "(a fabricated verdict would poison the coverage dataset)",
            verdict,
            list(COVERAGE_VERDICTS),
        )
        return None

    confidence = _confidence(arguments.get("confidence"))
    if confidence is None:
        _logger.warning(
            "judge: confidence %r is not a usable number in [0,1] — failing OPEN",
            arguments.get("confidence"),
        )
        return None

    return CoverageAssessment(
        verdict=verdict,  # type: ignore[arg-type]
        covered_by=_clean(arguments.get("covered_by"), limit=_MAX_COVERED_BY_CHARS),
        reason=_clean(arguments.get("reason"), limit=_MAX_REASON_CHARS),
        confidence=confidence,
    )


def _confidence(raw: Any) -> float | None:
    """A usable drop-gate confidence, or `None`. See the module table.

    Three guards in order, and each one catches something the previous does not:
      1. TYPE — `bool` first, because it passes `isinstance(x, int)` and `True >= 0.90`
         is a perfect false positive that reads as maximal confidence.
      2. CONVERSION — an `int` too large to be a float raises `OverflowError` from
         `float()`, which would escape into a queue worker.
      3. RANGE — `not (0.0 <= v <= 1.0)` rejects NaN without a separate `isnan` test
         (NaN fails both comparisons) and rejects ±inf, which compares greater than
         every bar there is.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        value = float(raw)
    except (OverflowError, ValueError):
        return None
    if not (0.0 <= value <= 1.0):
        return None
    return value


def _clean(raw: Any, *, limit: int) -> str:
    """Untrusted model text as one capped, single-line string; `""` for a non-str.

    Derived from the two things done with these fields and NOTHING else: they are
    written as JSON leaves into a KV document, and they are interpolated into log
    records. So the guards are (a) flatten every Unicode control/format/separator to a
    space, because a newline in a log record forges a second record and a line separator
    survives a naive `"\\n" not in text` check, and (b) cap the length, because the
    document has a TTL but no size limit of its own.

    Deliberately NOT `extractor/prior_art.py::_sanitize`, which is the right function
    for a different job: that one additionally collapses runs of `=` so untrusted text
    cannot spell the PRIOR ART block's fence. There is no fence here — nothing on this
    path is composed back into a prompt — and importing a prompt guard into a storage
    guard would tie the two together such that relaxing the prompt's fence rule silently
    changes what is stored. Same class of transformation, different derivation, so they
    are pinned apart by their own tests rather than merged into a shared helper.

    NOT `str(raw)`: coercion turns `None` into the literal `"None"`, which then reads
    as real content to whoever queries the bucket.
    """
    if not isinstance(raw, str):
        return ""
    clipped = raw[: limit * 4]
    flattened = "".join(
        " " if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp") else ch
        for ch in clipped
    )
    collapsed = " ".join(flattened.split())
    if len(collapsed) > limit:
        return collapsed[:limit].rstrip() + "…"
    return collapsed


__all__ = [
    "JUDGE_TOOL_NAME",
    "VERDICT_ENUM",
    "build_judge_tool",
    "parse_assessment",
]
