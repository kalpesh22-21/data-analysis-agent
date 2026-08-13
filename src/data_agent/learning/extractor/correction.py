"""The corrective turn's prompt text — what the extractor says to a model whose
candidate was well-analysed and badly packaged.

**Why this exists.** Across ten live analyst sessions the extractor mined zero
candidates, and in every case the model's ANALYSIS was right while its PACKAGING was
wrong: `gpt-4.1` flattened the envelope, `gpt-5.5` wrote `result_signature.grain` as
prose where an object belongs. Both were terminal — a shape decline was returned to the
consumer and the model was never told. This module writes the telling.

**What it must not do.** It must not argue with the model's judgement. Every message
below is scoped to a NAMED field of a NAMED candidate and to what that field must
contain; the content is never disputed, and the closing line explicitly says to omit a
candidate rather than bend the analysis to fit. A decline for a substantive reason never
reaches here at all (`validation.py::_malformed`).

**Three families, and the header has to be true of all of them.** Most corrections are
about SHAPE — a reader failed, the field is the wrong JSON type. Two are not, and both
are candidates that were read perfectly well: a `rule`-role plan whose `rule_id` the
catalog does not declare but can name a counterpart for (`validation.py::_rule_hint`),
and a plan that left a literal predicate of the accepted SQL with no entry at all
(`validation.py::_predicate_hint`). Telling either of those that it "could not be READ"
would be false, and a model told the wrong thing about its own output is being invited to
change something nobody asked about. The opening and closing lines are therefore chosen
from what is actually in the batch, and the two invariants EVERY variant keeps are the
ones that stop a correction becoming coercion: change ONLY what the lines name, and OMIT
the candidate rather than invent something to satisfy the correction.

**Entity-freedom.** Every sentence is assembled from `Decline.detail`, which for a
shape decline is a `shape.py` message: a path, a required shape and the JSON type that
arrived, never a value. The two hinting families quote more than that — an identifier
the model itself authored a turn earlier, and a literal of the accepted SQL this same
prompt already carries in full — and `validation.py::_correctable` states the rule they
satisfy and where it stops. The candidate TYPE is echoed only when it is one of the four
known ones, because a model that flattened the envelope can leave arbitrary session text
in `type`.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import Decline
from .schema import EXTRACTOR_TOOL_NAME
from .validation import (
    CANDIDATE_TYPES,
    REASON_MISSING_RULE_HINTED,
    REASON_RULE_MISMATCH,
    REASON_TOTALITY,
)

# reason code → family. Anything not listed is the SHAPE family, which is the right
# default: `shape.py` messages are the ones a reader wrote, and a new reader added
# tomorrow lands in the family whose wording describes it.
_RULE_FAMILY = "rule"
_PREDICATE_FAMILY = "predicate"
_SHAPE_FAMILY = "shape"
_FAMILIES = {
    REASON_MISSING_RULE_HINTED: _RULE_FAMILY,
    # The same family as the unknown-id hint: both are "the rule_id you cited is not the
    # right one, and here is what the catalog says", and both are fixed by changing that
    # one field or dropping the candidate.
    REASON_RULE_MISMATCH: _RULE_FAMILY,
    REASON_TOTALITY: _PREDICATE_FAMILY,
}

# The closing instruction, per family. All four say "change only what was named" and all
# four offer the exit; what differs is WHAT was named and what "cannot be fixed" means.
_CLOSINGS = {
    _SHAPE_FAMILY: (
        "Change only the SHAPE of the named field. Do not change your analysis to make "
        "a shape fit: if one of these cannot be expressed in the required shape, omit "
        "that candidate entirely."
    ),
    _RULE_FAMILY: (
        "Change only the rule_id each line names. Do not change your analysis to make "
        "the correction fit: if the catalog rule named above is not what your plan "
        "implements, omit that candidate entirely rather than citing an id that merely "
        "passes."
    ),
    _PREDICATE_FAMILY: (
        "Add only the parameterization entries the lines above ask for, and change no "
        "SQL and no predicate. Do not change your analysis to make the correction fit: "
        "if you cannot say what a listed predicate IS — a catalog rule, a caller-supplied "
        "slot, or a metric-defining inline — omit that candidate entirely rather than "
        "inventing a classification for it."
    ),
    "mixed": (
        "Change only what each line above names. Do not change your analysis to make a "
        "correction fit: if one of these cannot be corrected as described, omit that "
        "candidate entirely."
    ),
}

_OPENINGS = {
    _SHAPE_FAMILY: (
        "could not be READ and {was_were} rejected. Your analysis is not in question — "
        "the JSON shape of one field is."
    ),
    _RULE_FAMILY: (
        "{was_were} rejected over the catalog rule cited for a predicate. Your analysis "
        "is not in question — one identifier is."
    ),
    _PREDICATE_FAMILY: (
        "{was_were} rejected for leaving a literal predicate of the accepted SQL "
        "unaccounted for. Your analysis is not in question — the accepted SQL is fixed, "
        "and every literal predicate in it needs exactly one parameterization entry."
    ),
    "mixed": (
        "{was_were} rejected, each for the one reason its line gives. Your analysis is "
        "not in question."
    ),
}


def build_correction_message(
    declines: Sequence[tuple[int, Decline]], *, emitted: int, accepted: int
) -> str:
    """The user-visible correction for one turn's correctable declines.

    *declines* pairs each decline with the ZERO-BASED index of the candidate in the
    `candidates` array the model just sent, so the model can find the one being talked
    about without the extractor having to quote its content back at it.
    """
    families = {
        _FAMILIES.get(decline.reason, _SHAPE_FAMILY) for _index, decline in declines
    }
    family = families.pop() if len(families) == 1 else "mixed"
    lines = [
        f"CORRECTION. {_count(len(declines), 'candidate')} of the {emitted} you "
        f"emitted {_OPENINGS[family].format(was_were=_was_were(len(declines)))}",
        "",
    ]
    lines.extend(
        # A multi-line detail (the predicate checklist) is indented under its candidate
        # so the list of candidates stays readable as a list.
        f"  - candidate {index + 1}{_type_phrase(decline)}: "
        + decline.detail.replace("\n", "\n    ")
        for index, decline in declines
    )
    lines.append("")
    instruction = (
        f"Call {EXTRACTOR_TOOL_NAME} again with ONLY the corrected "
        f"{_count(len(declines), 'candidate')} listed above."
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
    lines.append(_CLOSINGS[family])
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
