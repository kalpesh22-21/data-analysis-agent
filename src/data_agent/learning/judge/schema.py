"""The coverage judge's forced-tool schema and the guard on what comes back (plan §3b).

One tool, one call, NO retries. The judge exists to CANCEL a much larger call, so every turn it
spends eats the saving it was built to produce and a malformed response fails OPEN rather than
being re-asked — the extractor's retry budget is right for a call whose output is the product
and wrong for one whose output is an optimization.

The response is untrusted model output, and every guard is DERIVED FROM WHAT DOWNSTREAM READS
AND THE OPERATION IT PERFORMS, not from the field names: `verdict` is equality-compared against
a closed set and is the dataset's `GROUP BY` key ⇒ a MEMBER of `COVERAGE_VERDICTS`, since
anything else would silently become its own bucket in every distribution query; `covered_by` is
set-membership-tested and interpolated into a log line ⇒ str, with that test handling `""`, an
unknown id and a hallucinated id identically; `reason` is a JSON leaf and a log interpolation ⇒
str, flattened to one line and capped; `confidence` is float-compared and serialized ⇒ a real
number in `[0.0, 1.0]`, never a `bool` (an `int` subclass, so `True >= 0.9`) and never NaN,
which serializes to invalid JSON and fails every comparison, reading as a silent "never drop".

OUT-OF-RANGE IS A REJECTION, NOT A CLAMP: `5.0` is a broken response, and clamping it to `1.0`
would turn a malfunction into the most confident drop the system can express.
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

    `None` is the FAIL-OPEN signal and covers every failure shape; the caller's contract on it is
    "behave exactly as if no judge were wired", but each shape is logged distinctly because they
    mean different things to whoever tunes the prompt. Returns a partial-but-valid assessment
    where a field can degrade safely (`covered_by`/`reason`, which are only ever READ) and `None`
    where it cannot (`verdict`/`confidence`, from which the drop DECISION is computed).
    `tool_calls` is checked as a CONTAINER and as MEMBERS: `ModelClient` is a Protocol, and a
    bare string char-explodes into an `AttributeError` on `.name`.
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
    """A usable drop-gate confidence, or `None`. See the module docstring.

    Three guards in order, each catching what the previous does not: TYPE (`bool` first, since it
    passes `isinstance(x, int)` and `True >= 0.90` reads as maximal confidence), CONVERSION (an
    `int` too large to be a float raises `OverflowError` into a queue worker), and RANGE, which
    rejects NaN without a separate `isnan` test and rejects ±inf.
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

    Derived from the two things done with these fields and NOTHING else: they are written as JSON
    leaves into a KV document, and they are interpolated into log records. So every Unicode
    control/format/separator is flattened to a space — a newline in a log record forges a second
    record, and a line separator survives a naive newline check — and the length is capped,
    because the document has a TTL but no size limit of its own.

    Deliberately NOT `extractor/prior_art.py::_sanitize`, which additionally collapses runs of
    `=` so untrusted text cannot spell the PRIOR ART block's fence. There is no fence here —
    nothing on this path is composed back into a prompt — and importing a prompt guard into a
    storage guard would tie the two together such that relaxing the fence rule silently changes
    what is stored. NOT `str(raw)`: coercion turns `None` into the literal "None".
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
