"""The parameterization judge's operating config.

THE RECORD IS THE PRODUCT. Phase D-1 discards nothing and repairs nothing — it observes,
records, and marks the reviewer card. Everything here therefore exists to answer one question a
week from now: *when this judge says a blueprint is wrong, is it right?* A verdict that is not
durably recorded, alongside the payload it was about, is a verdict nobody can grade.

`docs/decisions/learning-blueprint-review-rework-design.md` §D.

FINDING CLASSES, and the ranking is the design's whole point — the OBVIOUS reading of "not
enough slots for flexibility" is inverted here:

  A — a SLOT that should be `inline`, or an `intent` the template does not compute. The literal
      defines the metric, so filling it with another value answers a different question under an
      unchanged intent. This is the only class that may ever authorize a discard (phase D-2),
      because it is the only one where the blueprint is WRONG rather than merely narrower.
  B — naming and typing (`x1`, `entity` where `period` was meant). Unusable at recall, not wrong.
  C — an `inline` that could have been a slot. The ORIGINAL requirement, and the WEAKEST finding
      in the file: an over-inlined blueprint is narrow, an over-slotted one is wrong, so the
      repair for C pushes toward the failure mode A describes. Advisory, never grounds for
      anything destructive.

An unrecognized class reads as C — the weakest — never as A. See `schema.py`.

⚠ NEITHER THE ASSESSMENT NOR THE RECORD LIVES HERE, and both moves are forced by import
direction rather than by taste:

  `ParamAssessment`/`ParamFinding` → `audit/judgement.py`, beside `CoverageAssessment`. The
  ENVELOPE imports that module for its coverage stamp, so the assessment must live somewhere
  the envelope's own package does not import back. `candidate/verdicts.py` is the right home
  by theme — it is where `LeakageVerdict` and `DedupVerdict` sit — and the wrong one by
  import direction, and the direction wins.

  `ParamJudgeRecord`/`param_judgement_ref` → `audit/judgement.py`, beside `JudgeRecord`.
  `audit/store.py` types its port methods on the record, and this package imports the
  store — so a record defined here would close the loop `store → paramjudge → store`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..audit.judgement import MAX_FINDINGS


@dataclass(frozen=True)
class ParamJudgeConfig:
    """The judge's operating parameters, resolved once at the composition root.

    ⚠ NO `max_rounds` AND NO `discard_confidence`. Both belong to phase D-2 and both were
    deliberately left unset by the design: the first draft justified them with an anecdote about
    a different loop and a position on a number line. They are OUTPUTS of the D-1 measurement,
    not inputs to it, and a default here would mean tuning the data to numbers picked before it
    existed.
    """

    model: str = ""
    timeout_seconds: float = 30.0
    # Phase D-1 hard-wires this True and there is no code path that reads it as False. It
    # exists so D-2 does not have to invent the flag, and so the record is self-describing.
    shadow: bool = True
    findings_cap: int = MAX_FINDINGS
    metadata: dict[str, Any] = field(default_factory=dict)
