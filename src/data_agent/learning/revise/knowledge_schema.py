"""The knowledge reviser's forced tool, and the guard on what comes back (design §C.1).

⚠ **THE TOOL HAS EXACTLY FIVE CONTENT FIELDS, AND A RESPONSE CARRYING A SIXTH IS REJECTED.**

That is this module's load-bearing line, and it is the KNOWLEDGE analogue of
`schema.py`'s "the tool never has a `sql_template` field" — but the reason is different, and
the difference is worth stating because it decides how strict the guard has to be.

A blueprint's forbidden field is dangerous because it would DERAIL A DERIVATION: a
model-authored template leaves five checks passing about the wrong subject. A knowledge
payload has no derivation to derail. What it has is a CLOSED KEY SET that exists for a leakage
reason: `leakage/gate.py::_ENTITY_FREE_SURFACES["global_knowledge"]` names the five surfaces
the S5 gate scans, `validation.py::_GLOBAL_KNOWLEDGE_KEYS` closes intake over exactly those
five, and a test pins the two together. A sixth key is therefore not merely unread — it is
TEXT NOBODY SCANNED, on its way to a reviewer's card and into the global, scope-bypassed
knowledge index. The stuck candidate that motivated the intake reader carried two of them
(`definition`, `intent`) and the gate reported `pass` on a payload it had never read.

So the property list here is the SAME five, spelled out rather than imported (importing the
gate would drag the semantic scanner and the user store into a tool schema for one tuple —
`validation.py` makes the identical trade and says so). `test_knowledge_tool_properties_are_
the_gate_surfaces` pins them, so a surface added to the gate fails a test here, and vice versa.

REJECTED rather than IGNORED, for `schema.py`'s reason verbatim: ignoring an off-contract key
would let the model believe it had set something it had not, and would let the reviewer read a
rationale about a field that does not exist. `ForbiddenKnowledgeEditError` is a `ValueError`
surfaced as a 422 carrying the sentence, so what the reviewer learns is true about the system.

WHERE THE SWEEP IS NOT A KEY CHECK: `structured`'s OWN keys are CONTENT — it is declared as
an object of string→string supporting detail, and a fact about "overtime_multiplier" may
legitimately use that word as a key. So the sweep skips `structured`'s keys and walks its
VALUES, which must be scalars; a dict nested inside `structured` is a model writing a shape
the contract does not have, and its keys are named in the refusal like any other trespass.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any

from data_agent.runtime.model.client import ModelTurnResult

_logger = logging.getLogger(__name__)

KNOWLEDGE_TOOL_NAME = "propose_knowledge"

# ⚠ THE FIVE ENTITY-FREE SURFACES, IN GATE ORDER. Pinned by test to
# `leakage/gate.py::_ENTITY_FREE_SURFACES["global_knowledge"]` and, through it, to
# `validation.py::_GLOBAL_KNOWLEDGE_KEYS`. Three things must agree about this list — what the
# gate scans, what intake permits, and what the assistant is offered — and only a test can hold
# three modules together without one importing the other two.
KNOWLEDGE_SURFACES: tuple[str, ...] = (
    "statement",
    "knowledge_type",
    "structured",
    "related_terms",
    "scope",
)

# The one non-content field: an explanation for the human, never stored on the candidate.
_RATIONALE_KEY = "rationale"
_ALLOWED_TOP: frozenset[str] = frozenset({*KNOWLEDGE_SURFACES, _RATIONALE_KEY})

# The one surface whose OWN KEYS are content rather than schema — see the module docstring.
_FREE_KEY_SURFACE = "structured"

# The reviewer reads the rationale beside a per-field diff; it is an explanation, not an essay.
MAX_RATIONALE_CHARS = 600
# A knowledge statement is the whole text of the landed chunk — one or two sentences by
# contract. Far above anything legitimate, low enough that a runaway generation cannot be
# persisted or put on a review card.
MAX_STATEMENT_CHARS = 2_000
# `knowledge_type` and `scope` are short LABELS: a kind of fact, and what the fact is about
# (the latter titles the landed chunk).
MAX_LABEL_CHARS = 200
# One `related_terms` entry, and one `structured` key or value. Recall terms, not prose.
MAX_TERM_CHARS = 200
# How many recall terms / supporting pairs one fact may carry. A fact needing more than this
# is several facts.
MAX_TERMS = 32
# How deep the off-contract-key sweep walks. Untrusted input can nest arbitrarily and a bare
# recursion inside a request handler is a cheap way to blow the stack. Matches
# `schema.py::_MAX_SWEEP_DEPTH`.
_MAX_SWEEP_DEPTH = 8
# How many off-contract keys one refusal names, matching `validation.py::_MAX_LISTED_KEYS`: a
# response with more than this has a systemic problem and the message is prompt-adjacent text.
_MAX_LISTED_KEYS = 6


class ForbiddenKnowledgeEditError(ValueError):
    """The model wrote a field this contract does not have.

    Its own type because the CALLER must tell it from an unusable response, exactly as
    `ForbiddenTemplateEditError` is: this one is a model working against a system that does not
    exist, and the reviewer is better served by the sentence saying so than by "the assistant
    had no suggestion". A `ValueError` so a caller that only knows the base classes still
    handles it as bad input rather than as an outage.
    """


def _statement_property() -> dict[str, Any]:
    return {
        "type": "string",
        "description": (
            "The fact, as ONE self-contained, ENTITY-FREE sentence. It becomes the whole "
            "text of the landed knowledge chunk, so it has to make sense to a reader who "
            "has none of this context. Never name a person, an employee code, a department "
            "code, a customer or a date from the session."
        ),
    }


def build_knowledge_tool() -> dict[str, Any]:
    """The single forced tool the knowledge reviser must call.

    Note what is ABSENT, which is the same observation `build_revise_tool` invites: any way to
    express a field this system does not have. The five properties are the five surfaces the
    leakage gate scans, so anything the model can say here is something a scanner will read.
    """
    return {
        "type": "function",
        "name": KNOWLEDGE_TOOL_NAME,
        "description": (
            "Propose the corrected content of this piece of global knowledge. Call this "
            "exactly once. Do not emit free text. Return the COMPLETE fact you want stored "
            "— every field you leave out is stored empty, not carried over."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "statement": _statement_property(),
                "knowledge_type": {
                    "type": "string",
                    "description": (
                        'A short label for the kind of fact, e.g. "business_rule", '
                        '"definition", "data_caveat". Lower case, no spaces.'
                    ),
                },
                "structured": {
                    "type": "object",
                    "description": (
                        "Supporting entity-free detail as flat key/value STRINGS, e.g. "
                        '{"unit": "usd", "grain": "per employee per period"}. Values are '
                        "strings — never nested objects or lists. Omit it if the statement "
                        "says everything."
                    ),
                    "additionalProperties": {"type": "string"},
                },
                "related_terms": {
                    "type": "array",
                    "description": (
                        "Other entity-free words this fact should be recalled by — synonyms "
                        "and the vocabulary a user would actually type. One term per item."
                    ),
                    "items": {"type": "string"},
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "What this fact is ABOUT, in a few words. It titles the landed "
                        'chunk, e.g. "payroll earnings" — not a person and not a department.'
                    ),
                },
                _RATIONALE_KEY: {
                    "type": "string",
                    "description": (
                        "One or two sentences for the human reviewer: what you changed and "
                        "why. They will see a per-field diff beside this."
                    ),
                },
            },
            "required": ["statement", _RATIONALE_KEY],
        },
    }


def _clean(raw: Any, *, limit: int) -> str:
    """A single-line, length-capped string, or `""`.

    The same five-category flatten `schema.py::_clean` applies, for the same two reasons: this
    text reaches a log line (where a newline is a forged second entry) and a FastAPI response
    body (which is UTF-8 encoded and cannot hold a lone surrogate — the `Cs` category, which is
    the easy one to leave out, since Python and `json.dumps` both tolerate it and only the
    socket does not).
    """
    if not isinstance(raw, str):
        return ""
    flattened = "".join(
        " " if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp") else ch
        for ch in raw
    )
    return " ".join(flattened.split())[:limit]


def _keys_anywhere(value: Any, *, depth: int = 0) -> set[str]:
    """Every dict KEY appearing at or below *value*.

    Used for the four surfaces that are declared as strings or arrays of strings: none of them
    may contain an object at all, so any key found beneath one is off-contract by construction
    and gets named. Depth-bounded like `schema.py`'s sweep, and past the bound it STOPS rather
    than raising — nothing that deep survives the coercion below anyway.
    """
    if depth > _MAX_SWEEP_DEPTH:
        return set()
    if isinstance(value, dict):
        found = {str(key) for key in value}
        for nested in value.values():
            found |= _keys_anywhere(nested, depth=depth + 1)
        return found
    if isinstance(value, list):
        found: set[str] = set()
        for item in value:
            found |= _keys_anywhere(item, depth=depth + 1)
        return found
    return set()


def forbidden_knowledge_keys(arguments: Any) -> set[str]:
    """Every off-contract key in a knowledge proposal, at any depth.

    THREE RULES, and the middle one is the one a reader will want explained:

      * at the TOP LEVEL, a key outside the five surfaces plus `rationale`;
      * inside `structured`, the keys are CONTENT and are skipped — but its VALUES are declared
        as strings, so any key found INSIDE a value is off-contract;
      * inside any other surface, ANY key at all: they are declared as strings and arrays of
        strings, so an object under one of them is a shape this system has no reader for.
    """
    if not isinstance(arguments, dict):
        return set()
    found = set(arguments) - _ALLOWED_TOP
    for key, value in arguments.items():
        if key == _FREE_KEY_SURFACE:
            if isinstance(value, dict):
                for nested in value.values():
                    found |= _keys_anywhere(nested, depth=1)
            else:
                found |= _keys_anywhere(value, depth=1)
            continue
        found |= _keys_anywhere(value, depth=1)
    return found


def _terms(raw: Any) -> list[str]:
    """`related_terms`, cleaned and capped — or `[]` for anything unusable.

    Information-free items are DROPPED rather than carried: a live model emits every declared
    property, so an empty string in this array is the shape of the question echoed back, and an
    empty term recalls nothing while failing intake (`_non_empty_text`) for the whole payload.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw[:MAX_TERMS]:
        term = _clean(item, limit=MAX_TERM_CHARS)
        if term and term not in out:
            out.append(term)
    return out


def _structured(raw: Any) -> dict[str, str]:
    """`structured`, flattened to string→string and capped — or `{}` for anything unusable.

    A non-string VALUE is dropped rather than coerced. `str()` on a dict is a Python repr, which
    intake would accept as a string and the mapper would land verbatim: not a rejection but a
    corruption, which is the distinction `shape.py::as_text` draws and this follows.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if len(out) >= MAX_TERMS:
            break
        name = _clean(key, limit=MAX_TERM_CHARS)
        text = _clean(value, limit=MAX_TERM_CHARS)
        if name and text:
            out[name] = text
    return out


def coerce_knowledge_payload(arguments: dict[str, Any]) -> dict[str, Any]:
    """The model's arguments → a `global_knowledge` payload, or `{}` when unusable.

    ⚠ AN INFORMATION-FREE FIELD BECOMES ABSENT, which is the live-model lesson
    `schema.py::_is_information_free` records: a model emits EVERY declared property, so a
    proposal that means "leave the type alone" arrives as `knowledge_type: ""`. Carrying it
    would store an empty label as though the assistant had chosen one.

    NOT the intake validator — `validation.py::validate_payload` still owns whether the result
    is a legal payload, and it runs on the way IN through `KnowledgeEditor.admit`. This is
    transport: what the model actually said, in the shapes the surfaces are declared as.

    `{}` when there is no usable `statement`, because a knowledge payload with no statement is
    not a partial proposal — it is nothing. The caller turns that into "no suggestion".
    """
    statement = _clean(arguments.get("statement"), limit=MAX_STATEMENT_CHARS)
    if not statement:
        return {}
    payload: dict[str, Any] = {"statement": statement}
    knowledge_type = _clean(arguments.get("knowledge_type"), limit=MAX_LABEL_CHARS)
    if knowledge_type:
        payload["knowledge_type"] = knowledge_type
    structured = _structured(arguments.get("structured"))
    if structured:
        payload["structured"] = structured
    terms = _terms(arguments.get("related_terms"))
    if terms:
        payload["related_terms"] = terms
    scope = _clean(arguments.get("scope"), limit=MAX_LABEL_CHARS)
    if scope:
        payload["scope"] = scope
    return payload


def parse_knowledge_proposal(
    result: ModelTurnResult,
) -> tuple[dict[str, Any], str] | None:
    """One knowledge-reviser turn → `(payload, rationale)`, or `None` when unusable.

    Raises `ForbiddenKnowledgeEditError` for the one failure that is NOT a degrade — see the
    module docstring. Everything else returns `None`, which the caller surfaces as "no
    suggestion", the same outcome as the reviser not being wired at all.
    """
    calls = result.tool_calls
    if not isinstance(calls, (list, tuple)):
        _logger.warning(
            "knowledge reviser: tool_calls was %s, not a sequence — no proposal",
            type(calls).__name__,
        )
        return None
    call = next(
        (
            c
            for c in calls
            if getattr(c, "name", None) == KNOWLEDGE_TOOL_NAME and hasattr(c, "arguments")
        ),
        None,
    )
    if call is None:
        _logger.warning(
            "knowledge reviser: model did not call %s (got %r) — no proposal",
            KNOWLEDGE_TOOL_NAME,
            [getattr(c, "name", type(c).__name__) for c in calls],
        )
        return None
    arguments = call.arguments
    if not isinstance(arguments, dict):
        _logger.warning(
            "knowledge reviser: %s arguments were %s, not an object — no proposal",
            KNOWLEDGE_TOOL_NAME,
            type(arguments).__name__,
        )
        return None

    trespass = forbidden_knowledge_keys(arguments)
    if trespass:
        listed = sorted(trespass)[:_MAX_LISTED_KEYS]
        raise ForbiddenKnowledgeEditError(
            f"the proposal carried {listed}, but a global_knowledge fact has ONLY "
            "statement, knowledge_type, related_terms, structured and scope. Those five are "
            "exactly the surfaces the entity scanner reads, so a sixth field would reach the "
            "global knowledge index having been checked for entities by nothing at all — it "
            "is refused rather than dropped. State the fact in `statement` and put supporting "
            "detail in `related_terms` or `structured` (flat string values)."
        )

    payload = coerce_knowledge_payload(arguments)
    if not payload:
        _logger.info(
            "knowledge reviser: the proposal carried no usable statement — no proposal"
        )
        return None
    return payload, _clean(arguments.get(_RATIONALE_KEY), limit=MAX_RATIONALE_CHARS)


__all__ = [
    "KNOWLEDGE_SURFACES",
    "KNOWLEDGE_TOOL_NAME",
    "MAX_RATIONALE_CHARS",
    "MAX_STATEMENT_CHARS",
    "ForbiddenKnowledgeEditError",
    "build_knowledge_tool",
    "coerce_knowledge_payload",
    "forbidden_knowledge_keys",
    "parse_knowledge_proposal",
]
