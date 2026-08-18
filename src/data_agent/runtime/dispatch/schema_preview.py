"""schema_preview — fit a WIDE `getTableSchema` result under the per-result token
cap without hiding the existence of columns (ISSUES C5).

WHAT WAS WRONG. `_cap_nontabular_result`'s columns branch kept the HEAD of the
column list: on `dbpcm_warehouse.employee` (124 emitted columns, ~11.5k estimated
tokens) a 4,000-token cap kept the first ~36 columns WHOLE and dropped the other
~88 ENTIRELY — names included. `annual_salary` sits at index ~87, so the model
was never told the column exists, let alone its median-on-even-rows guidance, and
the marker's advice ("re-fetch getTableSchema or narrow if you need an omitted
column") was unfollowable: `getTableSchema` takes no narrowing argument and the
repeated-read guard declines an identical re-fetch. The truncation was, in
effect, silent.

WHAT THIS DOES INSTEAD — degrade DETAIL, not EXISTENCE, in a fixed order:

  1. COMPACT every column entry (LOSSLESS). The MCP emits a fixed 10-key entry
     per column (`clickhouse-api` `semantic_catalog/overlay.py::_merge_columns`:
     name, type, comment, description, synonyms, unit, values, observed_values,
     client_defined, sensitive) and defaults the overlay half to `None`/`False`
     for any column the catalog does not document. On the employee schema ~38%
     of the column payload is that null/false boilerplate. A key is dropped ONLY
     when its value is null/False/""/[]/{} — a truthy field is never lost, and a
     numeric `0` is NOT empty (`_is_empty`).

  2. SKELETON TIER: every column is present as `{name, type}` (~2.0k tokens for
     all of employee's). The model may not know what a column MEANS, but it
     always knows the column EXISTS and what it is typed as.

  3. DETAIL TIER: columns are upgraded back to their full (compacted) entry while
     the trial fit holds, in question-relevance order when a *question* is given
     (`_rank_columns`) and in list order otherwise.

  4. THE MARKER IS INSIDE THE FIT. It is part of every trial render, so the
     returned payload — marker included — is what was measured against the cap
     (the old marker was appended AFTER the fit, unbudgeted). Its text makes no
     promise the runtime cannot keep: it does NOT say "re-fetch or narrow"; it
     says a name/type-only column has documentation that was withheld and must
     not be guessed at from the name.

  5. BASE KEYS ARE NOT FREE. `database, table, catalogued, description, grain,
     temporal, primary_key, join_keys, measures, rules, ambiguities` ride beside
     `columns` and cost ~1.2k tokens on employee — `rules`/`ambiguities`/
     `join_keys` are most of it. They are kept whenever the skeleton fits beside
     them (they are exactly the semantics the prompt tells the model to read),
     and dropped LARGEST-FIRST only when the skeleton would not otherwise fit,
     never the identity keys (`database`/`table`). The marker names the dropped
     sections, so a degraded base is never a silent one.

D25 (load-bearing): *question* is RAW USER TEXT. It may ONLY influence the ORDER
in which columns are upgraded. It is never written into the returned schema, the
marker, the report, or anything derived from them — `fit_schema_under_cap` never
puts it anywhere; a test scans the whole output for it.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
from dataclasses import dataclass
from typing import Any

# The marker key the dispatcher has always used for a size-degraded preview.
MARKER_KEY = "_truncated"

# Never dropped from the base: without them the model cannot tell WHICH table it
# is looking at, and they cost ~6 tokens together.
_IDENTITY_KEYS = frozenset({"database", "table"})


def _estimate_tokens(text: str) -> int:
    """chars/4 token estimate — THE single home for the dispatch-side estimator
    (`tool_dispatcher.py` imports this name; `context/budget.py::_estimate_tokens`
    is the deliberate copy that cannot be imported without an import cycle, and a
    parity test in `tests/runtime/dispatch/test_tool_dispatcher.py` pins the two
    together so a tokenizer swap must update both)."""
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class SchemaFitReport:
    """What the fit did — COUNTS AND BOOLEANS ONLY (D25). No column names, no
    question text, no schema content: this is safe to log, to put on an observer
    event, and to assert on."""

    total_columns: int
    # Present in the returned schema at all (skeleton or detail).
    listed_columns: int
    # Listed WITH everything the schema had for them (a column the catalog never
    # documented is `detailed` the moment its name/type are shown — nothing about
    # it was withheld).
    detailed_count: int
    # Listed as `{name, type}` only, with documentation withheld to fit.
    reduced_count: int
    # Not listed at all (only when even the skeleton does not fit).
    omitted_columns: int
    # At least one column entry lost a null/False/empty key (lossless).
    compacted: bool
    # Every non-`columns` top-level key survived.
    base_kept: bool
    base_dropped_count: int
    marker_added: bool
    # The returned payload measures at or under the cap. False only for a cap so
    # small that even `{database, table, columns: [], marker}` exceeds it.
    fits: bool


# ---------------------------------------------------------------------------
# 1. Compaction (lossless)
# ---------------------------------------------------------------------------


def _is_empty(value: Any) -> bool:
    """True for the information-free values the MCP defaults absent overlay
    fields to. `0`/`0.0` are NOT empty (a real measurement), and `False` is
    checked as a bool BEFORE the numeric fallthrough (`0 == False` in Python)."""
    if value is None:
        return True
    if isinstance(value, bool):
        return value is False
    if isinstance(value, str | list | tuple | dict | set):
        return len(value) == 0
    return False


def _compact_column(column: Any) -> Any:
    """Drop the information-free keys of one column entry. A non-dict entry (no
    shape this module can reason about) is returned untouched."""
    if not isinstance(column, dict):
        return column
    return {key: value for key, value in column.items() if not _is_empty(value)}


def _skeleton_of(column: Any) -> Any:
    """`{name, type}` — the tier-2 form. A non-dict entry, or one carrying
    neither key, has no skeleton form and is returned as-is (it is already as
    small as this module can make it)."""
    if not isinstance(column, dict):
        return column
    skeleton = {key: column[key] for key in ("name", "type") if key in column}
    return skeleton or column


# ---------------------------------------------------------------------------
# 2. Question-relevance ranking (ordering ONLY — D25)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Question filler and analytics verbs. Deliberately small: the IDF weighting
# below already demotes anything that occurs in most columns, so this list only
# has to remove words that appear in the QUESTION but in no column at all.
_STOPWORDS = frozenset(
    """a all an and any are as at be been by can compare could did do does each find for
    from get give had has have how i in into is it its list many me most much my of on
    only or our over per please show showing so some than that the their them then there
    these they this those to top total us was we were what when where which who whom why
    will with within would you your""".split()
)

# `name` and `synonyms` ARE the column's identity; `description`/`comment` are
# prose in which a query word is much weaker evidence (the naive prototype that
# weighted description equally floated badge_number and emergency-contact columns
# for a salary question, because their prose repeats common words).
_NAME_WEIGHT = 3.0
_SYNONYM_WEIGHT = 3.0
_PROSE_WEIGHT = 0.6


def _stem(word: str) -> str:
    """Crude plural fold. `ies -> y` is checked BEFORE the trailing-`s` strip
    because the strip alone turns "salaries" into "salarie", which matches
    NOTHING — not `annual_salary`, not the word "salary" in any column's prose —
    so the single most common phrasing of the question this ranker exists for
    ("what are the average salaries by department") scored every column zero and
    fell back to list order. Same for "ambiguities"/"cities"/"policies".

    The length guards keep short words whose `s`/`ies` is not a plural intact
    ("lies" -> "lie", not "ly"; "gas" stays "gas"). This is not a real stemmer and
    does not need to be: it is applied IDENTICALLY to the question and to the
    columns, so the only thing that matters is that the two sides agree."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s"):
        return word[:-1]
    return word


def _tokens(text: Any) -> set[str]:
    """Fold `AnnualSalary` / `annual_salary` / "annual salary" to the same tokens,
    drop stopwords, and stem a plural (`_stem`) so "employees" matches "employee"
    and "salaries" matches "salary". Applied identically to the question and to
    the columns, so the crude stemming cannot make the two sides disagree."""
    if not isinstance(text, str):
        return set()
    words = _TOKEN_RE.findall(_CAMEL_BOUNDARY_RE.sub(" ", text).lower())
    out: set[str] = set()
    for word in words:
        if word in _STOPWORDS:
            continue
        out.add(_stem(word))
    return out


def _column_fields(column: Any) -> tuple[set[str], set[str], set[str]]:
    """(name tokens, synonym tokens, prose tokens) for one column entry."""
    if not isinstance(column, dict):
        return set(), set(), set()
    name = _tokens(column.get("name"))
    synonyms: set[str] = set()
    raw_synonyms = column.get("synonyms")
    if isinstance(raw_synonyms, list):
        for entry in raw_synonyms:
            synonyms |= _tokens(entry)
    elif isinstance(raw_synonyms, str):
        synonyms = _tokens(raw_synonyms)
    prose = _tokens(column.get("description")) | _tokens(column.get("comment"))
    return name, synonyms, prose


def _rank_columns(columns: list[Any], question: str | None) -> list[int]:
    """Column INDICES in the order detail should be restored to them.

    No question (or no usable token in it) -> list order, unchanged. Otherwise
    columns are scored by the question's tokens, weighted by field and by
    INVERSE DOCUMENT FREQUENCY over this table's own columns: a token that
    occurs in most of the table's columns ("employee", "date", "code") carries
    almost no signal, while a rare one ("salary") carries most of it. Ties fall
    back to list order, so the ranking is total and deterministic.
    """
    query = _tokens(question) if question else set()
    if not query:
        return list(range(len(columns)))

    fields = [_column_fields(column) for column in columns]
    total = len(columns) or 1
    document_frequency: dict[str, int] = {}
    for name, synonyms, prose in fields:
        for token in name | synonyms | prose:
            document_frequency[token] = document_frequency.get(token, 0) + 1

    scored: list[tuple[float, int]] = []
    for index, (name, synonyms, prose) in enumerate(fields):
        score = 0.0
        # `sorted`, not the set itself: float addition is not associative, so a
        # set's per-process iteration order (hash-randomized for str) could give
        # two processes different scores for the same column and, on a tie,
        # different orderings. Sorted iteration makes the sum reproducible.
        for token in sorted(query):
            weight = 0.0
            if token in name:
                weight += _NAME_WEIGHT
            if token in synonyms:
                weight += _SYNONYM_WEIGHT
            if token in prose:
                weight += _PROSE_WEIGHT
            if weight:
                idf = math.log(1.0 + total / document_frequency.get(token, 1))
                score += weight * idf
        scored.append((-score, index))
    scored.sort()
    return [index for _, index in scored]


# ---------------------------------------------------------------------------
# 3. Assembly + the marker
# ---------------------------------------------------------------------------


def _marker_text(report: SchemaFitReport, dropped_sections: list[str]) -> str:
    """The model-facing note. Every clause is a fact about THIS payload; there is
    no instruction the runtime cannot honour (notably NOT "re-fetch or narrow" —
    `getTableSchema` has no narrowing argument and an identical re-fetch is
    declined by the repeated-read guard; a real narrowing mechanism is queued)."""
    parts = [
        f"…[this schema was reduced to fit the tool-result cap. "
        f"{report.listed_columns} of {report.total_columns} columns are listed"
    ]
    if report.omitted_columns:
        parts.append(
            f"; {report.omitted_columns} could not be listed at all, so this is NOT the "
            f"full column list"
        )
    parts.append(". ")
    if report.reduced_count:
        parts.append(
            f"{report.reduced_count} of the listed columns are shown with name and type "
            f"ONLY: their documentation (description, unit, allowed values, synonyms) "
            f"exists and was NOT shown to you here. Do NOT assume what such a column "
            f"means, contains or is denominated in from its name — if your analysis "
            f"depends on one of them, ask the user rather than assuming. "
        )
    if dropped_sections:
        parts.append(
            f"Table-level sections omitted to fit: {', '.join(dropped_sections)}. "
        )
    return "".join(parts).rstrip() + "]"


def _assemble(
    *,
    base: dict[str, Any],
    entries: list[Any],
    detail_flags: list[bool],
    dropped_sections: list[str],
    total_columns: int,
    compacted: bool,
    max_tokens: int,
) -> tuple[dict[str, Any], SchemaFitReport]:
    """Build one candidate payload AND its report, marker included, so every
    trial measures exactly what would be returned."""
    listed = len(entries)
    detailed = sum(detail_flags)
    report = SchemaFitReport(
        total_columns=total_columns,
        listed_columns=listed,
        detailed_count=detailed,
        reduced_count=listed - detailed,
        omitted_columns=total_columns - listed,
        compacted=compacted,
        base_kept=not dropped_sections,
        base_dropped_count=len(dropped_sections),
        marker_added=False,
        fits=False,
    )
    payload: dict[str, Any] = {**base, "columns": entries}
    lossy = bool(report.reduced_count or report.omitted_columns or dropped_sections)
    if lossy:
        payload[MARKER_KEY] = _marker_text(report, dropped_sections)
    size = _estimate_tokens(json.dumps(payload, default=str))
    return payload, dataclasses.replace(
        report, marker_added=lossy, fits=size <= max_tokens
    )


# ---------------------------------------------------------------------------
# 4. The entry point
# ---------------------------------------------------------------------------


def fit_schema_under_cap(
    schema: dict[str, Any], max_tokens: int, *, question: str | None = None
) -> tuple[dict[str, Any], SchemaFitReport]:
    """Fit *schema* (a `getTableSchema` result) under *max_tokens*, degrading in
    the order documented at the top of this module: compact, skeleton, detail,
    base. Returns the payload the model should see and a counts-only report.

    *question* is the user's raw turn text. It is used ONLY to order the detail
    upgrades and never appears in either return value (D25).
    """
    # `schema["columns"]` is a list by the caller's own branch condition
    # (`_cap_nontabular_result` dispatches here on `isinstance(..., list)`), so a
    # missing/other-shaped value is a programming error and should raise rather
    # than be silently dropped from the payload. Its ITEMS are not assumed to be
    # dicts — `_compact_column`/`_skeleton_of` pass anything else through.
    columns: list[Any] = list(schema["columns"])
    base_all = {key: value for key, value in schema.items() if key != "columns"}

    compacted_columns = [_compact_column(column) for column in columns]
    compacted = any(new != old for new, old in zip(compacted_columns, columns, strict=True))
    skeletons = [_skeleton_of(column) for column in compacted_columns]
    # A column the catalog never documented is already whole at skeleton size —
    # nothing is withheld by showing it as name/type, so it counts as detailed.
    bare = [
        skeleton == column
        for skeleton, column in zip(skeletons, compacted_columns, strict=True)
    ]
    total = len(columns)

    def build(
        base: dict[str, Any],
        entries: list[Any],
        flags: list[bool],
        dropped: list[str],
    ) -> tuple[dict[str, Any], SchemaFitReport]:
        return _assemble(
            base=base,
            entries=entries,
            detail_flags=flags,
            dropped_sections=dropped,
            total_columns=total,
            compacted=compacted,
            max_tokens=max_tokens,
        )

    # --- tier 2: every column named --------------------------------------
    base = dict(base_all)
    dropped_sections: list[str] = []
    entries: list[Any] = list(skeletons)
    flags: list[bool] = list(bare)
    payload, report = build(base, entries, flags, dropped_sections)

    # --- base degradation: only to make room for the column NAMES ---------
    if not report.fits:
        droppable = sorted(
            (key for key in base_all if key not in _IDENTITY_KEYS),
            key=lambda key: _estimate_tokens(json.dumps(base_all[key], default=str)),
            reverse=True,
        )
        for key in droppable:
            if report.fits:
                break
            del base[key]
            dropped_sections.append(key)
            payload, report = build(base, entries, flags, dropped_sections)
        # The marker NAMES the dropped sections in the schema's own key order, not
        # in the size order they happened to be dropped in — so the payload has to
        # be re-rendered once the list is final (same names, so the fit is
        # unchanged; asserted by the report `fits` that comes back with it).
        dropped_sections.sort(key=list(base_all).index)
        payload, report = build(base, entries, flags, dropped_sections)

    # --- last resort: even the skeleton does not fit — keep its HEAD ------
    if not report.fits and entries:
        kept: list[Any] = []
        kept_flags: list[bool] = []
        for entry, flag in zip(entries, flags, strict=True):
            _, trial_report = build(
                base, [*kept, entry], [*kept_flags, flag], dropped_sections
            )
            if not trial_report.fits:
                break
            kept.append(entry)
            kept_flags.append(flag)
        entries, flags = kept, kept_flags
        payload, report = build(base, entries, flags, dropped_sections)
        return payload, report

    # --- tier 3: restore detail while the fit holds -----------------------
    for index in _rank_columns(compacted_columns, question):
        if flags[index]:
            continue
        candidate = list(entries)
        candidate[index] = compacted_columns[index]
        candidate_flags = list(flags)
        candidate_flags[index] = True
        trial, trial_report = build(base, candidate, candidate_flags, dropped_sections)
        if not trial_report.fits:
            # A LATER column may still fit — a 40-token entry can follow a
            # 900-token one that did not — so this keeps going rather than
            # breaking, which is what makes the packing dense.
            continue
        entries, flags = candidate, candidate_flags
        payload, report = trial, trial_report

    return payload, report
