"""The reviser's forced tool, and the guard on what comes back.

⚠ **THE TOOL HAS NO `sql_template` FIELD, AND A RESPONSE CARRYING ONE IS REJECTED.**

That is the load-bearing line in this package. `sql_template` is not authored — it is DERIVED,
by AST rewrite from the accepted SQL that actually ran (`generalize/rewrite.py`), with the
parameterization array saying which literals become holes. That derivation is what makes
`explain_ok`, `binds_to_subset_uses` and `read_only_select` mean anything: they check a
provenance chain back to a query the warehouse really answered. If a model writes the template,
all five checks silently change subject and start validating model prose.

REJECTED rather than IGNORED, and the difference is not pedantic: ignoring it would let a model
believe it had changed something it had not, and would let a reviewer read a rationale about a
template edit that never happened. A response carrying the field is a response written against a
contract this system does not have, and the honest handling is to refuse it.

Every other guard is derived from what downstream READS:

  `entries`  walked by `to_candidate`'s D97 totality check, each item indexed by `locator`/
             `role` ⇒ a `list` of `dict`, length-capped. Per-FIELD validation is deliberately
             NOT re-implemented here: `to_candidate` already owns it, it is the same validator
             the extractor's output faces, and a second vocabulary for the same mistake is
             exactly what `ui/server.py::_proxy_inbox` warns against.
  `replace`  selects `_merged_parameterization`'s append-vs-replace branch ⇒ a STRICT bool,
             defaulting to False (append) — the branch that leaves already-valid
             classifications intact.
  `rationale` rendered to the reviewer and logged ⇒ `str`, one line, capped.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any

from data_agent.runtime.model.client import ModelTurnResult

_logger = logging.getLogger(__name__)

REVISE_TOOL_NAME = "propose_parameterization"

# The reviewer reads the rationale beside a diff; it is an explanation, not an essay.
MAX_RATIONALE_CHARS = 600
# A blueprint's parameterization is one entry per literal predicate of a single SELECT.
# Far above anything legitimate, low enough that a runaway generation cannot be persisted.
MAX_ENTRIES = 64

# Keys that would mean the model thinks it is editing the SQL. Swept at EVERY depth of the
# response (see `_forbidden_keys_anywhere`) — nothing in a proposal may carry a template, at
# the top level or nested inside an entry. Note these are KEY names: an entry legitimately
# carries a `locator.value` lifted from the query, and that is not what this looks at.
FORBIDDEN_KEYS = frozenset({"sql_template", "template", "sql", "canonical_ast_norm"})
# How deep the forbidden-key sweep walks. A parameterization entry is two levels of nesting
# (`entries[i].slot.name`); this leaves headroom and still bounds an adversarial payload.
_MAX_SWEEP_DEPTH = 8


# The parameterization-entry array, PUBLIC because the minting tool (`learning/mint`) emits
# entries for the same `to_candidate` validator and a second copy of this shape would drift from
# it silently — the two would disagree about a field the validator reads, and only one of the two
# callers would start failing.
ENTRIES_SCHEMA: dict[str, Any] = {
            "type": "array",
            "description": (
                "The parameterization entries to add (or, with replace=true, the COMPLETE "
                "replacement list). Each entry classifies ONE literal predicate of the "
                "accepted SQL."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "locator": {
                        "type": "object",
                        "description": (
                            "Which literal this is about: {table, column, value}, copied "
                            "from the accepted SQL. Never invent a column."
                        ),
                        "properties": {
                            "table": {"type": "string"},
                            "column": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["table", "column", "value"],
                    },
                    "role": {
                        "type": "string",
                        "enum": ["slot", "inline", "rule"],
                        "description": (
                            "'slot' — the caller fills this in; it was this session's "
                            "question. 'inline' — it stays frozen; it is part of what the "
                            "blueprint MEANS, and you must say why. 'rule' — a catalog rule "
                            "accounts for it; cite the rule_id."
                        ),
                    },
                    "slot": {
                        "type": "object",
                        "description": "Required when role='slot', omitted otherwise.",
                        "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string"},
                            "binds_to": {
                                "type": "string",
                                "description": "database.table.column this slot constrains.",
                            },
                            "required": {"type": "boolean"},
                        },
                    },
                    "rule_id": {
                        "type": "string",
                        "description": "Required when role='rule'; a rule id you were shown.",
                    },
                    "why": {
                        "type": "string",
                        "description": (
                            "Required when role='inline': what this frozen value MEANS. "
                            "'defines the metric earnings', not 'it was in the query'."
                        ),
                    },
                },
                "required": ["locator", "role"],
            },
}

_PARAMETERS = {
    "type": "object",
    "properties": {
        "entries": ENTRIES_SCHEMA,
        "replace": {
            "type": "boolean",
            "description": (
                "false (default) — ADD these entries to the existing ones. Correct when "
                "entries are MISSING. true — these entries REPLACE the whole list. Correct "
                "when an existing entry is WRONG. Prefer false."
            ),
        },
        "rationale": {
            "type": "string",
            "description": (
                "One or two sentences for the human reviewer: what you changed and why. "
                "They will see a diff beside this."
            ),
        },
    },
    "required": ["entries", "rationale"],
}


def build_revise_tool() -> dict[str, Any]:
    """The single forced tool the reviser must call.

    Note what is absent: any way to express a change to the SQL. The template is regenerated
    from these entries by the deterministic rewrite, so a model that wants a different template
    gets one by classifying literals differently — which is the only kind of change the
    provenance chain can survive.
    """
    return {
        "type": "function",
        "name": REVISE_TOOL_NAME,
        "description": (
            "Propose the parameterization entries for this blueprint. Call this exactly "
            "once. Do not emit free text. You cannot change the SQL — only the ROLES of "
            "literals already in it."
        ),
        "parameters": _PARAMETERS,
    }


class ForbiddenTemplateEditError(ValueError):
    """The model tried to write SQL.

    Its own type because the CALLER must be able to tell it from an unusable response: this one
    is a model working against a contract the system does not have, and it is worth surfacing to
    the reviewer as such rather than as "the reviser had no suggestion".
    """


def _clean(raw: Any, *, limit: int) -> str:
    """A single-line, length-capped string, or `""`.

    Control characters are stripped rather than escaped: this text reaches a log line, and a
    newline in a log line is a forged second entry.
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


def _forbidden_keys_anywhere(value: Any, *, depth: int = 0) -> set[str]:
    """Every `FORBIDDEN_KEYS` name appearing at ANY depth in the proposal.

    Swept recursively, not just at the top level, and the reason is §C.1's own: the guard
    exists to REJECT rather than IGNORE. A nested `entries[0].sql_template` never becomes the
    template — the derivation holds, and `to_candidate` would carry it into
    `payload["parameterization"]` as inert junk — but it would leave the model believing it had
    changed something it had not, and would put a "template" nobody authored into the raw-JSON
    block a reviewer reads. A guard that catches the obvious placement and misses the nested one
    is a guard that reports success on the case it was written for.

    Depth-bounded because the input is untrusted: a deeply nested object is a cheap way to blow
    the stack inside a request handler. Past the bound the sweep stops rather than raising —
    nothing that deep can be a parameterization entry, so it will fail the totality walk anyway.
    """
    if depth > _MAX_SWEEP_DEPTH:
        return set()
    if isinstance(value, dict):
        found = FORBIDDEN_KEYS.intersection(value)
        for nested in value.values():
            found |= _forbidden_keys_anywhere(nested, depth=depth + 1)
        return found
    if isinstance(value, list):
        found = set()
        for item in value:
            found |= _forbidden_keys_anywhere(item, depth=depth + 1)
        return found
    return set()


# The ONLY keys placeholder-stripping may remove. Deliberately a closed set of the OPTIONAL
# role fields — never `locator` or `role`, which are STRUCTURAL: `to_candidate` must see a
# malformed one to report its own error, and silently deleting it turns a legible decline into
# a KeyError about a field the reviewer never saw.
_OPTIONAL_ROLE_KEYS = frozenset({"slot", "rule_id", "why", "enum_values", "optional_pattern"})

# Depth bound for the placeholder walk, matching the forbidden-key sweep. Untrusted input can
# nest arbitrarily, and a bare recursion here blew the stack inside a request handler.
_MAX_PLACEHOLDER_DEPTH = 8


def _is_information_free(value: Any, *, depth: int = 0) -> bool:
    """Is *value* a live-model PLACEHOLDER rather than a stated value?

    ⚠ A MODEL EMITS EVERY DECLARED PROPERTY. Given a schema with `slot`, `rule_id` and `why`,
    it fills all three on every entry — so a `role="inline"` entry arrives carrying
    `slot: {"name": "", "type": "", "binds_to": ""}`. That is not a slot; it is the shape of the
    question, echoed back. Downstream it is worse than absent: `to_candidate` reads a present
    `slot` as a declaration and the entry fails validation for a field the model never meant to
    set. Observed live on 2026-08-28 against gpt-5.5, on the first real revise of the first real
    declined candidate.

    NOT per-field validation — `to_candidate` still owns that, and this deliberately does not
    know what a slot means. It only decides what counts as UNSAID, which is a property of the
    transport rather than of the domain.

    Past the depth bound the answer is FALSE (not information-free), so anything too deep to
    inspect is CARRIED rather than deleted. Deleting on a failure to look would silently drop
    content; carrying it hands the whole entry to the validator that owns it.
    """
    if depth > _MAX_PLACEHOLDER_DEPTH:
        return False
    if value is None or value == "":
        return True
    if isinstance(value, dict):
        return all(_is_information_free(v, depth=depth + 1) for v in value.values())
    if isinstance(value, list):
        return not value or all(_is_information_free(v, depth=depth + 1) for v in value)
    return False


def _is_nameless_slot(value: Any) -> bool:
    """Is *value* a `slot` object the model never actually filled in?

    ⚠ THE GENERIC EMPTY-VALUES RULE IS NOT ENOUGH, and this is the second half of the same live
    finding. gpt-5.5 emitted, on a `role="inline"` entry:

        {"name": "", "type": "", "binds_to": "", "required": false}

    `required: false` is a boolean, so the object is not "all information-free" and survived the
    generic sweep — the placeholder came back one round later wearing a default.

    So the test is the ONE domain fact that settles it: A SLOT WITHOUT A NAME IS NOT A SLOT.
    The name is what the template's `{token}` is written from; an unnamed slot cannot be
    rendered, bound or recalled, so its presence carries no claim. This is deliberately the only
    domain knowledge in this module — `to_candidate` still owns whether a NAMED slot is valid,
    and a slot carrying a name is passed through untouched however wrong the rest of it is.
    """
    return isinstance(value, dict) and not str(value.get("name") or "").strip()


def _normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """One proposed entry with role placeholders dropped and the locator value unquoted.

    TWO normalizations, both of the model's TRANSCRIPTION rather than of its judgement:

      * an OPTIONAL ROLE FIELD that is information-free becomes absent. Restricted to
        `_OPTIONAL_ROLE_KEYS` — a structural field stays exactly as sent, however malformed, so
        the validator that owns it can say so;
      * a `locator.value` is the BARE literal the predicate compares against. Models copy it out
        of the SQL with its quotes still on (`"'N'"` for `employee_status != 'N'`), and the
        totality walk then matches nothing and reports the predicate still uncovered — a decline
        whose message names a predicate the reviewer can plainly see they addressed. Stripping
        ONE matched pair of surrounding quotes is transcription, not interpretation; an
        apostrophe inside a value (`O'Brien`) is content and is left alone.
    """
    out = {
        k: v
        for k, v in entry.items()
        if not (k in _OPTIONAL_ROLE_KEYS and _is_information_free(v))
        and not (k == "slot" and _is_nameless_slot(v))
    }
    locator = out.get("locator")
    if isinstance(locator, dict):
        value = locator.get("value")
        if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == "'":
            out["locator"] = {**locator, "value": value[1:-1]}
    return out


def coerce_entries(raw: Any) -> list[dict[str, Any]]:
    """A model's `entries` array, normalized and capped. Shared with `learning/mint`.

    PUBLIC because the minting tool emits entries for the SAME `to_candidate` validator, and the
    normalizations here are not cosmetic — they are the difference between a proposal that
    validates and one that comes back declined naming a predicate the author plainly addressed.
    A second implementation would re-learn the quoted-locator and placeholder-slot bugs
    independently, and only in whichever caller a live model happened to hit first.

    Returns `[]` for anything unusable; the caller decides whether that is a degrade or an error.
    """
    if not isinstance(raw, list):
        return []
    return [_normalize_entry(e) for e in raw[:MAX_ENTRIES] if isinstance(e, dict)]


def forbidden_keys_anywhere(value: Any) -> set[str]:
    """Public alias of the template-edit sweep — see `FORBIDDEN_KEYS`."""
    return _forbidden_keys_anywhere(value)


def parse_proposal(result: ModelTurnResult) -> tuple[list[dict[str, Any]], bool, str] | None:
    """One reviser turn → `(entries, replace, rationale)`, or `None` when unusable.

    Raises `ForbiddenTemplateEditError` for the one failure that is NOT a degrade — see the module
    docstring. Everything else returns `None`, which the caller surfaces as "no proposal", the
    same outcome as the reviser not being wired.
    """
    calls = result.tool_calls
    if not isinstance(calls, (list, tuple)):
        _logger.warning(
            "reviser: tool_calls was %s, not a sequence — no proposal", type(calls).__name__
        )
        return None
    call = next(
        (
            c
            for c in calls
            if getattr(c, "name", None) == REVISE_TOOL_NAME and hasattr(c, "arguments")
        ),
        None,
    )
    if call is None:
        _logger.warning(
            "reviser: model did not call %s (got %r) — no proposal",
            REVISE_TOOL_NAME,
            [getattr(c, "name", type(c).__name__) for c in calls],
        )
        return None
    arguments = call.arguments
    if not isinstance(arguments, dict):
        _logger.warning(
            "reviser: %s arguments were %s, not an object — no proposal",
            REVISE_TOOL_NAME,
            type(arguments).__name__,
        )
        return None

    trespass = _forbidden_keys_anywhere(arguments)
    if trespass:
        raise ForbiddenTemplateEditError(
            f"the proposal carried {sorted(trespass)}, but the SQL template is DERIVED from "
            "the accepted query by AST rewrite and is never model-authored; re-run with "
            "feedback about the parameterization roles instead"
        )

    entries = arguments.get("entries")
    if not isinstance(entries, list):
        _logger.warning(
            "reviser: entries was %s, not a list — no proposal", type(entries).__name__
        )
        return None
    clean_entries = coerce_entries(entries)
    if not clean_entries:
        _logger.info("reviser: the proposal contained no usable entries — no proposal")
        return None

    # STRICT bool. A truthy string ("false"!) must not select the destructive branch, so
    # anything that is not literally True reads as append.
    replace = arguments.get("replace") is True
    return clean_entries, replace, _clean(arguments.get("rationale"), limit=MAX_RATIONALE_CHARS)
