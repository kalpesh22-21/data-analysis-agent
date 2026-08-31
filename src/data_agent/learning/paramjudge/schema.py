"""The parameterization judge's forced tool, and the guard on what comes back.

ONE tool, ONE call, NO retries — the same posture as `judge/schema.py`, for the same reason:
this judge's output is an OBSERVATION, not a product, and a malformed response is worth
recording as a failure rather than re-asking until it parses.

EVERY GUARD IS DERIVED FROM WHAT DOWNSTREAM READS AND THE OPERATION IT PERFORMS, never from
the field name:

  `verdict`    equality-compared against a closed set AND the audit dataset's `GROUP BY` key
               ⇒ a MEMBER of `PARAM_VERDICTS`; anything else becomes its own bucket in every
               distribution query ever written over these rows.
  `feedback`   a JSON leaf, a log interpolation, and (in phase D-2) the text handed to the
               reviser as a prompt ⇒ `str`, flattened to one line, capped.
  `confidence` float-compared and serialized ⇒ a real number in `[0.0, 1.0]`, never `bool` (an
               `int` subclass, so `True >= 0.9`) and never NaN, which serializes to invalid
               JSON and fails every comparison — reading as a silent "never act".
  `findings`   rendered on a card, and `class == "A"` is the phase-D-2 gate on an IRREVERSIBLE
               action ⇒ see below.

OUT-OF-RANGE IS A REJECTION, NOT A CLAMP: `5.0` is a broken response, and clamping it to `1.0`
would turn a malfunction into the most confident judgement the system can express.

⚠ **`class` is not the model's to choose freely.** Letting a model self-report the severity that
would authorize destroying a candidate is the schema equivalent of asking it for permission. Two
mitigations, both cheap and both here: the enum is described to the model in terms of
CONSEQUENCE rather than rank, and an unrecognized value is down-cast to `C` — the weakest — so
every malformed severity fails toward advisory.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any

from data_agent.runtime.model.client import ModelTurnResult

from ..audit.judgement import (
    FINDING_CLASSES,
    MAX_CRITERION_CHARS,
    MAX_FEEDBACK_CHARS,
    MAX_FINDINGS,
    MAX_NOTE_CHARS,
    PARAM_VERDICTS,
    WEAKEST_FINDING_CLASS,
    ParamAssessment,
    ParamFinding,
)

_logger = logging.getLogger(__name__)

PARAM_JUDGE_TOOL_NAME = "judge_parameterization"

# DERIVED from the stored vocabularies, never re-spelled. A model therefore cannot be offered a
# verdict the record has no meaning for, and adding one is a single edit in `models.py`.
VERDICT_ENUM: list[str] = list(PARAM_VERDICTS)
CLASS_ENUM: list[str] = list(FINDING_CLASSES)


_PARAMETERS = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": VERDICT_ENUM,
            "description": (
                "'ok' — the parameterization is sound: every frozen literal defines the "
                "metric the intent names, and every slot is genuinely a question the "
                "caller should get to ask. 'revise' — at least one role is wrong and you "
                "can say which. 'reject' — the blueprint is not repairable by re-roling "
                "literals (the intent does not describe what the template computes at "
                "all)."
            ),
        },
        "feedback": {
            "type": "string",
            "description": (
                "One or two sentences naming the specific entry and the specific change: "
                "'record_type = EARNING defines the metric named in the intent; re-role "
                "it to inline'. Empty for 'ok'. A 'revise' with no feedback is read as "
                "'ok' — a complaint nobody can act on is not a complaint."
            ),
        },
        "confidence": {
            "type": "number",
            "description": (
                "0.0-1.0 — how sure you are of the VERDICT. Reserve high values for cases "
                "where you can point at the exact literal and say what a caller would get "
                "wrong because of it."
            ),
        },
        "findings": {
            "type": "array",
            "description": (
                "One entry per objection. Omit entirely for 'ok'."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "class": {
                        "type": "string",
                        "enum": CLASS_ENUM,
                        "description": (
                            # DESCRIBED BY CONSEQUENCE, not by rank. A model asked to pick a
                            # "severity" optimizes for sounding appropriately concerned; a
                            # model asked whether a caller would get a wrong ANSWER is being
                            # asked a question about the artifact.
                            "'A' — filling this slot with a different value would answer a "
                            "DIFFERENT question while the intent still claims the original "
                            "one (a metric-defining literal was made fillable), or the "
                            "intent describes something the template does not compute. "
                            "'B' — the slot is usable but badly named or typed. "
                            "'C' — a frozen literal could reasonably have been a slot; the "
                            "blueprint is narrower than it needed to be but answers its own "
                            "question correctly."
                        ),
                    },
                    "criterion": {
                        "type": "string",
                        "description": (
                            "A short tag for the kind of problem, e.g. "
                            "'slot_should_be_inline', 'intent_mismatch', 'slot_naming'."
                        ),
                    },
                    "entry_index": {
                        "type": "integer",
                        "description": (
                            "0-based position in the PARAMETERIZATION list shown to you. "
                            "Omit when the finding is about the blueprint as a whole."
                        ),
                    },
                    "note": {
                        "type": "string",
                        "description": "What is wrong with this specific entry.",
                    },
                },
                "required": ["class", "criterion", "note"],
            },
        },
    },
    "required": ["verdict", "feedback", "confidence"],
}


def build_param_judge_tool() -> dict[str, Any]:
    """The single forced tool the judge must call (the runtime's canonical flat shape)."""
    return {
        "type": "function",
        "name": PARAM_JUDGE_TOOL_NAME,
        "description": (
            "Record whether this blueprint's parameterization is sound. Call this exactly "
            "once. Do not emit free text."
        ),
        "parameters": _PARAMETERS,
    }


def _clean(raw: Any, *, limit: int) -> str:
    """A single-line, length-capped string, or `""`.

    Control characters are stripped rather than escaped: these land in a JSON leaf and in log
    lines, and a newline in a log line is a forged second log entry.
    """
    if not isinstance(raw, str):
        return ""
    flattened = "".join(
        # `Cs` — LONE SURROGATES — is in the set and is the one that is easy to leave out:
        # its category is not `Cc`/`Cf`, it is legal in a Python str, and `json.dumps` escapes
        # it, so nothing complains until the string reaches a FastAPI response body, which is
        # UTF-8 encoded and cannot hold it. That surfaces as a 500 from a LISTING endpoint —
        # one poisoned row makes the whole review queue unreadable.
        #
        # Same five categories, same order, as `extractor/prior_art.py::_sanitize`, which
        # records the rule this follows: "the serializer saves us" is not a property to
        # depend on.
        " " if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp") else ch
        for ch in raw
    )
    return " ".join(flattened.split())[:limit]


def _confidence(raw: Any) -> float | None:
    """A usable confidence, or `None`.

    Three guards in order, each catching what the previous does not: TYPE (`bool` FIRST, since it
    passes `isinstance(x, int)` and `True >= 0.9` would read as maximal confidence), CONVERSION
    (an `int` too large for a float raises `OverflowError` into a queue worker), and RANGE, which
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


def _findings(raw: Any, *, cap: int = MAX_FINDINGS) -> tuple[ParamFinding, ...]:
    """The findings list, normalized and capped. NEVER fails the whole verdict.

    A finding is decoration on the verdict in phase D-1 and a gate in D-2, but in neither case
    is a malformed one a reason to throw away a judgement the model did give. So every degrade
    here is local: a non-list becomes no findings, a non-dict item is skipped, an unrecognized
    `class` becomes `C`, and a non-integer `entry_index` becomes `None`.
    """
    if not isinstance(raw, list):
        return ()
    out: list[ParamFinding] = []
    for item in raw[:cap]:
        if not isinstance(item, dict):
            continue
        raw_class = item.get("class")
        if raw_class not in FINDING_CLASSES:
            # DOWN-CAST, never up. See the module docstring: `A` is the class that would
            # authorize destroying a candidate, so it must never be reachable by accident.
            _logger.debug(
                "param judge: finding class %r is not one of %s — reading it as %r",
                raw_class,
                CLASS_ENUM,
                WEAKEST_FINDING_CLASS,
            )
            raw_class = WEAKEST_FINDING_CLASS
        index = item.get("entry_index")
        out.append(
            ParamFinding(
                finding_class=raw_class,  # type: ignore[arg-type]
                criterion=_clean(item.get("criterion"), limit=MAX_CRITERION_CHARS),
                note=_clean(item.get("note"), limit=MAX_NOTE_CHARS),
                entry_index=(
                    index
                    if isinstance(index, int) and not isinstance(index, bool) and index >= 0
                    else None
                ),
            )
        )
    return tuple(out)


def parse_param_assessment(
    result: ModelTurnResult, *, entry_count: int = 0, findings_cap: int = MAX_FINDINGS
) -> ParamAssessment | None:
    """One judge turn → a guarded `ParamAssessment`, or `None` when unusable.

    `None` is the FAIL-OPEN signal and covers every failure shape; the caller's contract on it is
    "behave exactly as if no judge were wired". Each shape is logged distinctly, because they
    mean different things to whoever tunes the prompt.

    A partial-but-valid assessment is returned where a field can degrade safely (`feedback`,
    `findings` — both only ever READ) and `None` where it cannot (`verdict`, `confidence` — the
    two the phase-D-2 decision would be computed from).

    `tool_calls` is checked as a CONTAINER and as MEMBERS: `ModelClient` is a Protocol, and a
    bare string char-explodes into an `AttributeError` on `.name`.

    *entry_count* bounds `entry_index`. An index past the end of the list the model was shown is
    dropped from the finding — it cannot point at anything a card could highlight — but it never
    invalidates the verdict, because miscounting a list position says nothing about whether the
    objection is real.
    """
    calls = result.tool_calls
    if not isinstance(calls, (list, tuple)):
        _logger.warning(
            "param judge: tool_calls was %s, not a sequence — failing OPEN",
            type(calls).__name__,
        )
        return None
    call = next(
        (
            c
            for c in calls
            if getattr(c, "name", None) == PARAM_JUDGE_TOOL_NAME and hasattr(c, "arguments")
        ),
        None,
    )
    if call is None:
        _logger.warning(
            "param judge: model did not call %s (got %r, free text: %s) — failing OPEN",
            PARAM_JUDGE_TOOL_NAME,
            [getattr(c, "name", type(c).__name__) for c in calls],
            result.assistant_text is not None,
        )
        return None
    arguments = call.arguments
    if not isinstance(arguments, dict):
        _logger.warning(
            "param judge: %s arguments were %s, not an object — failing OPEN",
            PARAM_JUDGE_TOOL_NAME,
            type(arguments).__name__,
        )
        return None

    verdict = arguments.get("verdict")
    if verdict not in PARAM_VERDICTS:
        # NOT defaulted to "ok". A fabricated verdict is indistinguishable in the store from
        # one the model actually gave, and these rows ARE the dataset the phase-D-2
        # ship/don't-ship decision reads.
        _logger.warning(
            "param judge: verdict %r is not one of %s — failing OPEN and recording NOTHING",
            verdict,
            VERDICT_ENUM,
        )
        return None

    confidence = _confidence(arguments.get("confidence"))
    if confidence is None:
        _logger.warning(
            "param judge: confidence %r is not a usable number in [0,1] — failing OPEN",
            arguments.get("confidence"),
        )
        return None

    feedback = _clean(arguments.get("feedback"), limit=MAX_FEEDBACK_CHARS)
    findings = tuple(
        f
        for f in _findings(arguments.get("findings"), cap=findings_cap)
        if f.entry_index is None or f.entry_index < entry_count
    )

    if verdict in ("revise", "reject") and not feedback:
        # A complaint nobody can act on is not a complaint. In phase D-2 this text IS the
        # reviser's prompt, so an empty one would spend a repair round re-deriving the same
        # objection; in D-1 it would be a row in the measurement that says only "something".
        _logger.info(
            "param judge: %r with empty feedback — reading it as 'ok' (nothing actionable)",
            verdict,
        )
        return ParamAssessment(
            verdict="ok", feedback="", confidence=confidence, findings=findings
        )

    return ParamAssessment(
        verdict=verdict,  # type: ignore[arg-type]
        feedback=feedback,
        confidence=confidence,
        findings=findings,
    )
