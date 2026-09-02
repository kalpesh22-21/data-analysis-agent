"""The reviser's forced tool, and the guard on what comes back.

⚠ **THE TOOL NEVER HAS A `sql_template` FIELD, AND A RESPONSE CARRYING ONE IS REJECTED.**

That is still the load-bearing line in this package. `sql_template` is not authored — it is
DERIVED, by AST rewrite from the ACCEPTED SQL, with the parameterization array saying which
literals become holes (`generalize/rewrite.py`). That derivation is what makes `explain_ok`,
`binds_to_subset_uses` and `read_only_select` mean anything: they check that the template still
IS the accepted query. A model-authored template would leave all five checks passing about a
different subject, and nothing anywhere would notice.

⚠ **WHAT CHANGED (§C.5): WITH `allow_sql=True` THE TOOL GAINS A TOP-LEVEL `sql` FIELD.**

That is a different claim from the one above and it is worth stating precisely. A rewrite does
not author the TEMPLATE; it authors the ACCEPTED SQL, and then every derivation runs again from
there — the same AST rewrite, the same totality walk, the same five checks. What it gives up is
the provenance chain: the accepted SQL is no longer a query the warehouse answered on a real
session. The argument that replaces it is `learning/mint`'s, verbatim — a hand-authored blueprint
enters the SAME completer, faces a STRICTER totality walk (the SQL came from outside the entries,
so the walk cannot be circular), and is stamped `authored=True` on its `ValidationSnapshot`, which
`writer/routing.py::_is_authored` turns into a forced `in_review`. It can never auto-land.

The reviewer opts in per request and is shown a caution saying exactly that. `sql_template` stays
forbidden at every depth, and `sql` stays forbidden ANYWHERE BUT THE TOP LEVEL: an entry carrying
one is still a model working against a contract this system does not have.

REJECTED rather than IGNORED, and the difference is not pedantic: ignoring it would let a model
believe it had changed something it had not, and would let a reviewer read a rationale about a
template edit that never happened. A response carrying a forbidden field is a response written
against a contract this system does not have, and the honest handling is to refuse it.

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
  `sql`      (rewrite mode only) parsed by `sqlglot`, walked by the totality check and rewritten
             into a template ⇒ `str`, stripped, capped at the SAME limit a minted blueprint's SQL
             is capped at. Whether it is a legal read-only SELECT is decided by the check that
             will READ it (`generalize/validate.py::check_read_only_select`), in the engine, so
             an unusable rewrite degrades to a reason rather than a 4xx.
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
# The ONE key `allow_sql=True` un-forbids, and ONLY at the top level of the arguments object.
# Nested is still refused: an `entries[i].sql` is not a rewrite by any reading — nothing would
# ever read it — so it is a model writing against a contract that does not exist, which is the
# case this sweep was built for. `sql_template`/`template`/`canonical_ast_norm` stay forbidden
# in BOTH modes, at every depth: a rewrite authors the accepted SQL, never the template.
_REWRITE_KEY = "sql"
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


def max_rewrite_sql_chars() -> int:
    """The cap on a rewritten query, which is MINT'S cap — imported, never restated.

    A rewrite and a minted draft are the same object by the time anything downstream looks: an
    accepted SQL that a model wrote and a human is about to review. Two caps for one thing would
    drift, and the direction of the drift decides whether a reviewer sees "too long" from the
    boundary or a truncated query from the store.

    Imported LAZILY, and that is a cycle, not a style choice: `mint/schema.py` imports
    `ENTRIES_SCHEMA` and `coerce_entries` from THIS module (deliberately — one entries shape for
    one validator), and `learning.mint`'s package init reaches the inbox completer, which reaches
    this package again. A module-level import here would close that loop at interpreter start.
    """
    from ..mint.models import MAX_SQL_CHARS

    return MAX_SQL_CHARS


def _rewrite_sql_property() -> dict[str, Any]:
    """The `sql` field, offered ONLY in rewrite mode. See the module docstring for the trade."""
    return {
        "type": "string",
        "description": (
            "OPTIONAL, and a LAST RESORT. Leave it out unless the reviewer's feedback cannot "
            "be met by re-roling literals. When you do return it, return a COMPLETE "
            "replacement query — one read-only SELECT, ClickHouse dialect, reading only the "
            "tables and columns you were shown — and write `entries` against the NEW query "
            "with replace=true, because every existing entry describes the old one."
        ),
    }


def build_revise_tool(allow_sql: bool = False) -> dict[str, Any]:
    """The single forced tool the reviser must call.

    With `allow_sql=False` (the default, and every path that has not opted in) note what is
    absent: any way to express a change to the SQL. The template is regenerated from these
    entries by the deterministic rewrite, so a model that wants a different template gets one by
    classifying literals differently — which is the only kind of change the provenance chain can
    survive.

    With `allow_sql=True` the tool gains a top-level `sql`. The reviewer asked for it, having
    been told what it costs; see the module docstring. It is still not a TEMPLATE field — what
    comes back is an accepted SQL, and the template is derived from it exactly as before.
    """
    parameters = _PARAMETERS
    if allow_sql:
        parameters = {
            **_PARAMETERS,
            "properties": {**_PARAMETERS["properties"], "sql": _rewrite_sql_property()},
        }
    return {
        "type": "function",
        "name": REVISE_TOOL_NAME,
        "description": (
            "Propose the parameterization entries for this blueprint. Call this exactly "
            "once. Do not emit free text. "
            + (
                "You MAY return a complete replacement query in `sql` when the feedback "
                "cannot be met by re-roling literals; if you do, write the entries against "
                "the NEW query and set replace=true. Otherwise change only the ROLES of "
                "literals already in the query."
                if allow_sql
                else "You cannot change the SQL — only the ROLES of literals already in it."
            )
        ),
        "parameters": parameters,
    }


class ForbiddenTemplateEditError(ValueError):
    """The model wrote a field this request had no contract for.

    Its own type because the CALLER must be able to tell it from an unusable response: this one
    is a model working against a contract the system does not have, and it is worth surfacing to
    the reviewer as such rather than as "the reviser had no suggestion".

    Still raised in rewrite mode. What `allow_sql=True` un-forbids is exactly one key in exactly
    one position; a `sql_template`, or a `sql` inside an entry, is the same mistake it always was.
    """


# ⚠ ONE REASON, TWO GUARDS, TWO NEXT STEPS. The reviser refuses a composite rewrite before the
# model call; the completer refuses one at the write boundary. Separate checks, because only the
# second is on a door that writes — but the same RULE, so the sentence explaining WHY is shared
# and neither copy can drift into describing a different limitation.
#
# What is NOT shared is the advice, because the two callers reach a reviewer in different places:
# the reviser is answering a ticked CHECKBOX ("untick it"), and the completer may be answering a
# stale form or a direct POST, where there is no checkbox to untick. A single string would have
# had to give one of them an instruction about a control that is not on their page — the failure
# `ReviewInbox.propose_revision`'s withheld-scan message already records.
#
# Lives here because `inbox/completion.py` already imports this module for
# `max_rewrite_sql_chars`, so sharing it adds no dependency.
COMPOSITE_REWRITE_REASON = (
    "SQL rewrite is not offered for composite blueprints yet: a composite has one accepted "
    "query per node, so a single replacement query would replace all of them with one"
)


class SqlRewriteUnsupportedError(ForbiddenTemplateEditError):
    """A rewrite was asked for on a candidate whose shape cannot take one (composite, today).

    A SUBCLASS so the route that already maps `ForbiddenTemplateEditError` to 422-with-the-reason
    keeps working without a second handler: both are "this request asked for something this
    contract does not offer", and the reviewer's next step in both cases is to read the sentence.
    Its own type all the same, because the two need different fixes — one is the model's mistake
    and this one is the surface offering a control it cannot honour.
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


def _forbidden_keys_anywhere(
    value: Any, *, depth: int = 0, allow_top_level: frozenset[str] = frozenset()
) -> set[str]:
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
        # `allow_top_level` applies at DEPTH 0 ONLY — the arguments object itself. Everything
        # below it is swept against the full set, so a rewrite mode that permits `sql` still
        # refuses `entries[i].sql`, which no reader would ever consult.
        forbidden = FORBIDDEN_KEYS - allow_top_level if depth == 0 else FORBIDDEN_KEYS
        found = forbidden.intersection(value)
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
    """Public alias of the template-edit sweep — see `FORBIDDEN_KEYS`.

    No rewrite mode here: this is the alias `learning/mint` calls, and neither minting tool ever
    permits a model-authored `sql` in the position this sweeps.
    """
    return _forbidden_keys_anywhere(value)


def parse_proposal(
    result: ModelTurnResult, *, allow_sql: bool = False
) -> tuple[list[dict[str, Any]], bool, str, str] | None:
    """One reviser turn → `(entries, replace, rationale, sql)`, or `None` when unusable.

    `sql` is `""` unless `allow_sql` is set AND the model returned a non-empty top-level one. It
    is returned RAW-but-stripped: whether it is usable SQL is decided by the check that will read
    it, in the engine, so an unusable rewrite becomes a reason on a 200 rather than an error.

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

    trespass = _forbidden_keys_anywhere(
        arguments,
        allow_top_level=frozenset({_REWRITE_KEY}) if allow_sql else frozenset(),
    )
    if trespass:
        raise ForbiddenTemplateEditError(
            f"the proposal carried {sorted(trespass)}, but the SQL template is DERIVED from "
            "the accepted query by AST rewrite and is never model-authored; re-run with "
            "feedback about the parameterization roles instead"
            + (
                " (a whole-query rewrite goes in the top-level `sql` field, not in an entry "
                "and never as a template)"
                if allow_sql
                else ""
            )
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
    return (
        clean_entries,
        replace,
        _clean(arguments.get("rationale"), limit=MAX_RATIONALE_CHARS),
        _rewritten_sql(arguments) if allow_sql else "",
    )


def _rewritten_sql(arguments: dict[str, Any]) -> str:
    """The model's replacement query, stripped and capped — or `""` for "no rewrite".

    An EMPTY string is the same answer as an absent field on purpose: the tool describes `sql`
    as optional and a live model emits every declared property, so `""` is the shape of the
    question echoed back rather than a claim that the query should become nothing.

    OVER-LONG IS EMPTY, not truncated. A truncated query would be a syntactically different one
    that might still parse, which is the one way this could hand a reviewer something to approve
    that no model ever wrote. It degrades to "no rewrite", and the entries — written against a
    query nobody now has — will fail the totality walk and say so.
    """
    raw = arguments.get(_REWRITE_KEY)
    if not isinstance(raw, str):
        return ""
    if len(raw) > max_rewrite_sql_chars():
        _logger.warning(
            "reviser: the proposed rewrite was %d characters (cap %d) — treated as no rewrite",
            len(raw),
            max_rewrite_sql_chars(),
        )
        return ""
    return raw.strip()
