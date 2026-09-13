"""ProgressSummarizer — a cheap side LLM that turns one tool CALL (name + args, never
results) into a natural-language UI progress line. Opt-in behind
`progress_summary_enabled`, awaited before dispatch with a bounded per-call timeout.

D25 relaxation for this channel only: the line MAY carry BUSINESS values (a period, a
department) but never internal database structure. It reaches the UI VERBATIM as the
progress event's `step`, bypassing the `shape` allowlist, so the guard is at the INPUT
and is default-deny — `_project_args` passes only per-tool allowlisted arguments, and
nothing at all for an unlisted tool. Two deterministic checks then drop a line back to
the tool's static phrasing: `_looks_structural` and `_leaks_identifiers`.

Fail-soft (load-bearing): `summarize` returns `None` on any error, timeout or empty
output — a flaky summarizer must never break a turn nor delay a tool.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from data_agent.runtime.model.client import ModelClient, begin_turn_client

_logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You write a single short present-tense progress line (max ~12 words) for a "
    "data-analysis assistant's UI, describing in plain business English what the "
    "assistant is doing. Write for a business user who has never seen the database: "
    "Describe the user's business goal, never the implementation. The line MUST NOT "
    "mention SQL, queries, databases, schemas, tables, columns, blueprints, tools, "
    "capabilities, widgets, retrieval, or analysis machinery. You MAY include "
    "business-level parameters that "
    "appear in the arguments you are given (a department, a period, a search "
    "phrase). No preamble, no quotes, no trailing period, no fluff. Output only "
    "the line."
)

# --- the input guard: which arguments a tool may show the summarizer --------
#
# DEFAULT-DENY. A tool absent from this map contributes its NAME ONLY, so a tool
# added later leaks nothing until someone decides what of it is safe to narrate.
# Every entry below is an argument whose VALUE is business language or a corpus
# identifier — never a database, table or column name, and never SQL.
#
# Deliberately absent (name-only), with the physical identifiers they carry:
#   runQuery / explainQuery   -> `sql`
#   listTables / getTableSchema / sampleRows -> `database`, `table`
#   listDatabases             -> nothing to say beyond "what data is available"
#   answerWithTable           -> `tables[].sql`, plus the whole written answer
#   updateAnalysisState       -> intent bookkeeping, nothing to narrate
_ARG_ALLOWLIST: dict[str, frozenset[str]] = {
    # The D25 relaxation's intended case: slot VALUES are business values (a
    # department, a period) and the blueprint id is a corpus identifier authored
    # in business language — neither names anything physical.
    "runBlueprint": frozenset({"id", "slot_bindings"}),
    "getBlueprint": frozenset({"id"}),
    # The model's own search phrase, written in the user's language.
    "searchBlueprints": frozenset({"query"}),
    "searchKnowledge": frozenset({"query"}),
    # `concept` is the natural-language concept being resolved; `table`/`column`
    # are withheld by omission.
    "resolveValues": frozenset({"concept"}),
    "askUser": frozenset({"question"}),
    "recordAssumptions": frozenset({"assumptions"}),
}

# The deterministic fallback when the produced line is rejected by
# `_leaks_identifiers` — same register as `progress.py`'s `_STEP_LABELS`, but
# phrased for a business reader and carrying no tool name.
_STATIC_LINES: dict[str, str] = {
    "runQuery": "finding the requested information",
    "explainQuery": "checking the requested information",
    "sampleRows": "reviewing the available information",
    "listDatabases": "checking what information is available",
    "listTables": "checking what information is available",
    "getTableSchema": "checking what information is available",
    "runBlueprint": "calculating the requested result",
    "getBlueprint": "preparing the requested calculation",
    "searchBlueprints": "finding the best way to answer",
    "searchKnowledge": "looking up relevant background",
    "resolveValues": "matching your wording to the available choices",
    "askUser": "putting a question back to you",
    "recordAssumptions": "noting the assumptions behind the answer",
    "answerWithTable": "putting the answer together",
    "answerWithText": "putting the answer together",
    "searchHelpCenter": "looking up relevant product guidance",
    "getHelpCenterDocument": "reviewing the relevant product guidance",
    "searchCapabilityTools": "looking for a useful next step",
    "getCapabilityTool": "preparing a useful next step",
    "updateAnalysisState": "tracking the parts of your question",
}
_GENERIC_STATIC_LINE = "working on your question"
_ALWAYS_STATIC = frozenset(
    {
        "searchHelpCenter",
        "getHelpCenterDocument",
        "searchCapabilityTools",
        "getCapabilityTool",
        "answerWithText",
        "answerWithTable",
        "updateAnalysisState",
    }
)
_MODEL_SUMMARIZED_TOOLS = frozenset(_ARG_ALLOWLIST) | frozenset(
    {
        "runQuery",
        "explainQuery",
        "sampleRows",
        "listDatabases",
        "listTables",
        "getTableSchema",
        "answerWithTable",
        "updateAnalysisState",
    }
)

_MECHANICAL_LANGUAGE = re.compile(
    r"\b(?:sql|quer(?:y|ies|ying)|database|schema|tables?|columns?|blueprints?|"
    r"tools?|capabilit(?:y|ies)|widgets?|mcp|retriev(?:al|ing)|rerank(?:ing)?|"
    r"hydrat(?:e|ing|ion)|warehouse|data analysis|system|features?|business insights?|"
    r"reporting options?)\b",
    re.IGNORECASE,
)

# Identifier-looking material extracted from the WITHHELD raw arguments. Simple
# substring matching by design (not a SQL parser): the projection above is the
# real guard, this is a cheap second layer, and a false positive only costs the
# static line.
_DOTTED_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)+")
_SQL_SOURCE_IDENTIFIER = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+[`\"]?([A-Za-z_][A-Za-z0-9_$.]*)", re.IGNORECASE
)
_BARE_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_$]*\Z")
# Short tokens ("id", "n", "sum") collide with ordinary English, and a false
# positive silently downgrades every line for that tool — so only tokens this
# long or longer are matched.
_MIN_TOKEN_CHARS = 4

# --- the UNCONDITIONAL structural check ------------------------------------
#
# The two layers above are both provenance-based: the projection withholds args,
# and `_forbidden_tokens` only looks for what was WITHHELD. That leaves a real
# hole, because the allowlisted values are authored by the schema-aware MAIN
# model: a `resolveValues` concept, a `searchBlueprints` query, a slot value or a
# recorded assumption can itself name `dbpcm_warehouse.employee`, pass the
# projection (it is allowlisted), be echoed by the summarizer, and be exempt from
# the withheld-token scan (it was not withheld). So the SHAPE of the produced
# line is checked too, whatever its provenance — a progress line for a business
# reader never contains a qualified identifier, a quoted identifier, or SQL.
#
# Both sides of the dot must be 2+ chars, which is what keeps "e.g."/"i.e."/"U.S."
# out of it; a digit-leading right side ("Q1.2026") is not an identifier either.
# Quoting is covered separately because backticks defeat the dotted pattern
# entirely (`db`.`table`) — and a backtick has no business in a progress line.
_STRUCTURAL_DOTTED = re.compile(
    r"[`\"]?[A-Za-z_][A-Za-z0-9_$]+[`\"]?\s*\.\s*[`\"]?[A-Za-z_][A-Za-z0-9_$]+"
)
_QUOTED_IDENTIFIER = re.compile(r"[`\"][A-Za-z_][A-Za-z0-9_$]*[`\"]|`")
# A SQL source keyword AS WRITTEN IN SQL (upper case) is structural on its own —
# it catches `... FROM employee`, whose target is an ordinary-looking word. The
# same keyword in LOWER case is ordinary English ("pulling headcount from last
# month") and is deliberately NOT matched; a lower-case fragment that really is
# SQL ("joining employee_master") is caught by the standalone-token rule instead.
_SQL_SOURCE_KEYWORDS = ("FROM", "JOIN", "INTO", "UPDATE", "TABLE")
_UPPERCASE_SQL_SOURCE = re.compile(rf"\b(?:{'|'.join(_SQL_SOURCE_KEYWORDS)})\b\s+\S")
_WORD_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")

# Per-argument-value truncation bound (characters) applied BEFORE serialization,
# so a huge `sql` string or a long list argument cannot blow up the prompt token
# count. The line only needs the gist of the call, never the full payload.
_MAX_ARG_VALUE_CHARS = 300
# Overall cap on the compact-JSON arguments blob handed to the model (defense in
# depth over the per-value truncation, e.g. an args dict with very many keys).
_MAX_ARGS_BLOB_CHARS = 1200


def _truncate(value: Any) -> Any:
    """Bound one argument value's serialized size: strings are cut, lists/tuples are
    element-truncated then capped, dicts are recursively value-truncated, scalars pass
    through unchanged.
    """
    if isinstance(value, str):
        return value if len(value) <= _MAX_ARG_VALUE_CHARS else value[:_MAX_ARG_VALUE_CHARS] + "…"
    if isinstance(value, (list, tuple)):
        return [_truncate(item) for item in list(value)[:20]]
    if isinstance(value, dict):
        return {str(k): _truncate(v) for k, v in list(value.items())[:20]}
    return value


def _compact_args(arguments: dict[str, Any]) -> str:
    """Serialize *arguments* to a compact, token-bounded JSON blob for the prompt."""
    try:
        bounded = {str(k): _truncate(v) for k, v in list(arguments.items())[:20]}
        blob = json.dumps(bounded, ensure_ascii=False, default=str)
    except Exception:
        blob = str(arguments)[:_MAX_ARGS_BLOB_CHARS]
    if len(blob) > _MAX_ARGS_BLOB_CHARS:
        blob = blob[:_MAX_ARGS_BLOB_CHARS] + "…"
    return blob


def _project_args(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """The arguments the summarizer prompt may see — allowlisted per tool, EMPTY for any
    tool not in `_ARG_ALLOWLIST` (default-deny).
    """
    allowed = _ARG_ALLOWLIST.get(tool_name)
    if not allowed:
        return {}
    return {key: value for key, value in arguments.items() if key in allowed}


def _collect_strings(value: Any, out: list[str]) -> None:
    """Flatten every string reachable in *value* into *out*, keys included — a dict KEY
    can itself be a column name (e.g. a `slot_bindings` entry).
    """
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                out.append(key)
            _collect_strings(item, out)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _collect_strings(item, out)


def _forbidden_tokens(tool_name: str, arguments: dict[str, Any]) -> set[str]:
    """Identifier-looking tokens drawn from the arguments this tool WITHHELD.

    Withheld ones only: an allowlisted value (a department, a search phrase) is exactly
    what the line is allowed to repeat, so scanning for it would reject every good line.
    """
    allowed = _ARG_ALLOWLIST.get(tool_name, frozenset())
    withheld = [value for key, value in arguments.items() if key not in allowed]
    strings: list[str] = []
    for value in withheld:
        _collect_strings(value, strings)

    tokens: set[str] = set()
    for text in strings:
        for dotted in _DOTTED_IDENTIFIER.findall(text):
            tokens.add(dotted)
            tokens.update(dotted.split("."))
        for source in _SQL_SOURCE_IDENTIFIER.findall(text):
            tokens.add(source)
            tokens.update(source.split("."))
        # A whole value that IS an identifier — `listTables(database=...)`,
        # `getTableSchema(table=...)`, `sampleRows(table=...)`.
        if _BARE_IDENTIFIER.match(text):
            tokens.add(text)
    return {token.lower() for token in tokens if len(token) >= _MIN_TOKEN_CHARS}


def _leaks_identifiers(line: str, forbidden: set[str]) -> bool:
    """True when *line* repeats any withheld identifier (case-insensitive)."""
    lowered = line.lower()
    return any(token in lowered for token in forbidden)


def _is_identifier_shaped(token: str) -> bool:
    """True when *token* looks like a physical identifier rather than a word.

    An underscore, a dot, or mixed camel/Pascal casing — BOTH cases must be present, so
    a capitalised word ("January") and an acronym ("OPEX") are not matched, and a plain
    word is never identifier-shaped. Accepted false positive: a genuinely
    inner-capitalised business value ("McKinsey") costs that line its static fallback,
    which is the cheap side of catching PascalCase column names.
    """
    token = token.strip('`"')
    if len(token) < _MIN_TOKEN_CHARS:
        return False
    if "_" in token or "." in token:
        return True
    has_upper = any(char.isupper() for char in token[1:])
    has_lower = any(char.islower() for char in token)
    return has_upper and has_lower


def _looks_structural(line: str) -> bool:
    """True when *line* is itself shaped like database structure — UNCONDITIONAL,
    independent of where the material came from.

    This is the layer covering the allowlisted FREE-TEXT arguments (a `concept`, a
    search `query`, a slot value): written by the schema-aware main model, they reach
    the summarizer legitimately and are invisible to the withheld-token scan.
    """
    if _QUOTED_IDENTIFIER.search(line):
        return True
    if _STRUCTURAL_DOTTED.search(line):
        return True
    if _UPPERCASE_SQL_SOURCE.search(line):
        return True
    # Any standalone token that is identifier-shaped — `payroll_detail`,
    # `EmployeeMaster` — with no SQL keyword needed in front of it.
    return any(_is_identifier_shaped(token) for token in _WORD_TOKEN.findall(line))


def _static_line(tool_name: str) -> str:
    """The safe, deterministic line for *tool_name*, used when the model's line is
    rejected. Never contains the tool name — the instant template label already does.
    """
    return _STATIC_LINES.get(tool_name, _GENERIC_STATIC_LINE)


class ProgressSummarizer:
    """One-shot tool-call → progress-line summarizer over a cheap `ModelClient`."""

    def __init__(self, model_client: ModelClient, *, timeout_seconds: float = 3.0) -> None:
        self._model_client = model_client
        self._timeout_seconds = timeout_seconds

    async def summarize(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        """Return a short present-tense progress line for *tool_name*(*arguments*),
        or `None` on any error/timeout/empty output (fail-soft — never raises)."""
        try:
            return await asyncio.wait_for(
                self._summarize(tool_name, arguments), timeout=self._timeout_seconds
            )
        except TimeoutError:
            # The dominant drop cause whenever the budget is set near the model's
            # own latency: the line is abandoned in flight and that tool call gets
            # NO line at all, which reads as a random UI flake. Logged rather than
            # swallowed silently so the drop rate is visible in the logs.
            _logger.debug(
                "progress summary timed out after %.1fs for %s (dropped)",
                self._timeout_seconds,
                tool_name,
            )
            return None
        except Exception:
            # Fail-soft: a transport error or any other failure drops the summary —
            # the instant template label already streamed, so the UI degrades to
            # "running <tool>…".
            _logger.debug("progress summary failed for %s (dropped)", tool_name, exc_info=True)
            return None

    async def _summarize(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        # A name-only call gives the side model no business context and encourages it
        # to translate internal names into vague mechanical prose. Use the reviewed,
        # deterministic wording for those calls instead.
        if tool_name in _ALWAYS_STATIC or tool_name not in _MODEL_SUMMARIZED_TOOLS:
            return _static_line(tool_name)
        # THE GUARD, before anything is built: the model only ever sees the
        # allowlisted arguments for this tool (nothing at all for an unlisted one).
        projected = _project_args(tool_name, arguments)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Tool: {tool_name}. Arguments: {_compact_args(projected)}",
            },
        ]
        # B3: never mutate the shared client's fallback stickiness directly.
        turn_client = begin_turn_client(self._model_client)
        result = await turn_client.send_turn(messages, tools=[])
        text = (result.assistant_text or "").strip()
        # Strip a wrapping pair of quotes the model sometimes adds despite the prompt.
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            text = text[1:-1].strip()
        if not text:
            return None
        # Second layer, deterministic, two independent checks — either one replaces
        # the line with this tool's static phrasing rather than emitting it:
        #   - SHAPE: the line itself looks like database structure, whatever its
        #     provenance (this is what covers the allowlisted free-text args, which
        #     the schema-aware main model authored);
        #   - PROVENANCE: the line repeats an identifier out of the WITHHELD args.
        if (
            _MECHANICAL_LANGUAGE.search(text)
            or _looks_structural(text)
            or _leaks_identifiers(text, _forbidden_tokens(tool_name, arguments))
        ):
            return _static_line(tool_name)
        return text


__all__ = ["ProgressSummarizer"]
