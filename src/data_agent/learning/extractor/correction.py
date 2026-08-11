"""The corrective turn's prompt text — what the extractor says to a model whose
candidate was well-analysed and badly packaged.

**Why this exists.** Across ten live analyst sessions the extractor mined zero
candidates, and in every case the model's ANALYSIS was right while its PACKAGING was
wrong: `gpt-4.1` flattened the envelope, `gpt-5.5` wrote `result_signature.grain` as
prose where an object belongs. Both were terminal — a shape decline was returned to the
consumer and the model was never told. This module writes the telling.

**What it must not do.** It must not argue with the model's judgement. Every message
below is scoped to a NAMED field of a NAMED candidate and to the SHAPE that field
requires; the content is never disputed, and the closing line explicitly says to omit a
candidate rather than reshape the analysis to fit. A decline for a substantive reason
never reaches here at all (`validation.py::_malformed`).

**Entity-freedom.** Every sentence is assembled from `Decline.detail`, which for a
correctable decline is a `shape.py` message: a path, a required shape and the JSON type
that arrived, never a value. The candidate TYPE is echoed only when it is one of the
four known ones, because a model that flattened the envelope can leave arbitrary
session text in `type`.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import Decline
from .schema import EXTRACTOR_TOOL_NAME
from .validation import CANDIDATE_TYPES


def build_correction_message(
    shape_declines: Sequence[tuple[int, Decline]], *, emitted: int, accepted: int
) -> str:
    """The user-visible correction for one turn's shape-declined candidates.

    *shape_declines* pairs each decline with the ZERO-BASED index of the candidate in
    the `candidates` array the model just sent, so the model can find the one being
    talked about without the extractor having to quote its content back at it.
    """
    lines = [
        f"CORRECTION. {_count(len(shape_declines), 'candidate')} of the {emitted} you "
        f"emitted could not be READ and {_was_were(len(shape_declines))} rejected. Your "
        "analysis is not in question — the JSON shape of one field is.",
        "",
    ]
    lines.extend(
        f"  - candidate {index + 1}{_type_phrase(decline)}: {decline.detail}"
        for index, decline in shape_declines
    )
    lines.append("")
    instruction = (
        f"Call {EXTRACTOR_TOOL_NAME} again with ONLY the corrected "
        f"{_count(len(shape_declines), 'candidate')} listed above."
    )
    if accepted:
        # Said out loud because the alternative — a model helpfully re-sending
        # everything — costs a duplicate candidate in the review queue for each
        # accepted one, and the extractor cannot tell a re-send from a new proposal.
        instruction += (
            f" Do NOT repeat the {_count(accepted, 'candidate')} that "
            f"{_was_were(accepted)} accepted; {_they(accepted)} already kept."
        )
    lines.append(instruction)
    lines.append(
        "Change only the SHAPE of the named field. Do not change your analysis to make "
        "a shape fit: if one of these cannot be expressed in the required shape, omit "
        "that candidate entirely."
    )
    return "\n".join(lines)


def _type_phrase(decline: Decline) -> str:
    """` (type "blueprint")`, or nothing when the type is not one of the four.

    Derived from the closed set rather than from truthiness: the decline whose type is
    unusable is exactly the flattened-envelope one, where `type` is whatever the model
    happened to leave at the top level."""
    if decline.type in CANDIDATE_TYPES:
        return f' (type "{decline.type}")'
    return ""


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _was_were(n: int) -> str:
    return "was" if n == 1 else "were"


def _they(n: int) -> str:
    return "it is" if n == 1 else "they are"
