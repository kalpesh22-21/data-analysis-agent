"""schema_preview — shape a `getTableSchema` result for the model: hide the
tenancy columns the platform pre-applies, keep every table-level section whole,
and fit only the COLUMNS section under its own token budget (ISSUES C5 / C5b).

WHAT WAS WRONG (C5). `_cap_nontabular_result`'s columns branch kept the HEAD of
the column list: on `dbpcm_warehouse.employee` (130 emitted columns, ~12k
estimated tokens) a 4,000-token cap kept the first ~35 columns WHOLE and dropped
the other ~95 ENTIRELY — names included. `annual_salary` sits at index ~93, so
the model was never told the column exists. C5 replaced that with a two-tier fit.

WHAT C5b CHANGES (user spec, 2026-08-18), in the order the pipeline applies it:

  0. TENANCY COLUMNS ARE NOT MODEL-FACING AT ALL. `client_code` and the
     proc-center family are removed from `columns` on EVERY path — over budget
     and under it. They are pre-applied by the platform (the ClickHouse row
     policy binds them to the caller's token claims: see the canon's
     `docs/row-policy.md` and `mcp_projection.hidden_columns`), so the model
     never writes a predicate on them; showing them can only invite one. The MCP
     already hides them for the four tables that declare `hidden_columns`
     (employee, payroll, department, labor_allocation) — this strip is what makes
     the other seven catalogued tables, which expose `client_code` as an ordinary
     column, behave the same. The strip is SILENT by design (no marker clause):
     naming a column the model must not use re-introduces exactly the thing the
     removal exists to prevent. The operator hears the count via the fit report.

     BASE SECTIONS ARE NOT SCRUBBED. A `rule` or `join_keys` entry that mentions
     a tenancy column describes filtering the PLATFORM applies, and section
     completeness (below) outranks name-hiding; a sweep of the catalog found no
     rule whose PREDICATE names one (see the C5b report).

  1. TABLE-LEVEL SECTIONS RIDE COMPLETE, ALWAYS. `description, grain, temporal,
     primary_key, join_keys, measures, rules, ambiguities` are exempt from the
     budget — the C5 largest-first base-degradation ladder is DELETED. Those
     sections are the semantics the system prompt tells the model to read; a
     budget that can silently delete `rules` buys column detail with correctness.

  2. THE BUDGET APPLIES TO THE COLUMNS SECTION ONLY (`schema_columns_token_budget`,
     6,000 by default — NOT the generic `max_tool_result_tokens`, which still
     bounds every other tool result). Total preview = full base + ≤ budget of
     columns + the marker.

  3. THE COLUMNS SECTION IS FITTED IN A FIXED LADDER:
     a. COMPACT (LOSSLESS), ALWAYS — UNDER BUDGET TOO. The MCP emits a fixed
        10-key entry per column
        (`clickhouse-api semantic_catalog/overlay.py::_merge_columns`) and
        defaults the overlay half to `None`/`False` for any column the catalog
        does not document; on employee ~38% of the column payload is that
        boilerplate. A key is dropped ONLY when its value is null/False/""/[]/{}
        — a truthy field is never lost and a numeric `0` is NOT empty
        (`_is_empty`). Lossless, so no marker.

        USER SPEC 2026-08-18 point 5 — UNCONDITIONAL; overrides the earlier
        byte-identity deviation. The first C5b draft made this a BUDGET device:
        a schema whose raw entries already fit was returned byte-identical, null
        keys and all, and only an over-budget one was compacted. The user's spec
        is explicit that null keys are stripped from the response ALWAYS, to save
        tokens — `"unit": null` teaches the model nothing at any size, and the
        request budget is shared with every other tool result in the turn. So the
        under-budget contract is now COMPACTED-STABLE, not byte-identical: the
        keys that survive keep their original order and their values verbatim,
        `columns` keeps its position among the table-level sections, and a
        compacted under-budget schema carries NO marker (nothing the model needed
        was withheld). Everything BELOW this rung — skeletons, grouping,
        reordering, the marker — remains over-budget-only.
     b. SKELETON-FOR-ALL, THEN GROUPED DETAIL UPGRADES. Every column is present
        as `{name, type}` (~2.0k tokens for all of employee's), then entries are
        upgraded back to their full compacted form while the fit holds:
        STRUCTURAL columns first (primary-key + join-key columns, read from the
        schema's own `primary_key`/`join_keys` — the model cannot join or
        de-duplicate without them, whatever the question was), then the rest in
        question-relevance order (`_rank_columns`).
     c. Only if not even the skeletons fit does the tail of the emission order
        come off — and the marker then says plainly that this is not the full
        column list.

  4. THE EMITTED ORDER IS GROUPED, NOT PHYSICAL: [detailed group first, in
     relevance order] + [the remaining skeletons in the table's ORIGINAL order].
     The marker states this, so the model never reads the list as physical
     column order (it has no other way to find out) — and it states it in the
     form that is TRUE OF THIS PAYLOAD: only a turn whose question actually
     ranked the columns is told the order is relevance order; with no usable
     question the ranking IS list order, so the marker claims only that the key
     and leading columns come first.

  5. THE MARKER IS INSIDE THE FIT — part of every trial render, so what is
     measured is what is returned. Its text makes no promise the runtime cannot
     keep. It used to end at "ask the user rather than assuming", because
     `getTableSchema` had no narrowing argument; the MCP now takes an optional
     `columns: ["<name>", …]` argument, so the marker carries the ACTIONABLE
     contract instead — a name/type-only column's documentation can be fetched by
     calling `getTableSchema` again for the same table with `columns` naming just
     the ones needed. That second call is NOT declined by the repeated-read guard:
     `loop/read_guard.py::idempotent_read_signature` keys on the tool name plus
     the canonicalized ARGUMENTS, so a call carrying `columns` is a different
     signature from the bare fetch that produced this payload.

D25 (load-bearing): *question* is RAW USER TEXT. It may ONLY influence the ORDER
in which columns are upgraded and emitted. It is never written into the returned
schema, the marker, the report, or anything derived from them — a test scans the
whole output for it.
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

# Tenancy columns: PRE-APPLIED by the platform, never model-facing (see §0).
# Matched on a normalized form (case-folded, underscores removed) so a column
# spelled `client_code`, `ClientCode` or `CLIENT_CODE` is the same column — the
# warehouse was snake-migrated and both spellings exist in the repo's history.
# The set is the REAL column names swept out of the catalog canon
# (`clickhouse-api/app/semantic_catalog/data/*.yaml`, all 11 catalogued tables)
# and the warehouse DDL (`docker/clickhouse-init/*.sql`): `client_code` on all
# eleven, `proc_center` on the four RLS-scoped ones. No `processing_center` /
# `proc_center_code` variant exists anywhere in either source; the extra spellings
# below are cheap insurance against a rename, not evidence of one.
_TENANCY_COLUMNS = frozenset(
    {
        "clientcode",
        "proccenter",
        "processingcenter",
        "proccentercode",
        "processingcentercode",
    }
)

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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

    # Columns the MODEL could be shown, i.e. after the tenancy strip. The hidden
    # ones are counted separately below and are not part of any other count here.
    total_columns: int
    # Present in the returned schema at all (skeleton or detail).
    listed_columns: int
    # Listed WITH everything the schema had for them (a column the catalog never
    # documented is `detailed` the moment its name/type are shown — nothing about
    # it was withheld).
    detailed_count: int
    # Listed as `{name, type}` only, with documentation withheld to fit.
    reduced_count: int
    # Not listed at all (only when even the skeletons do not fit).
    omitted_columns: int
    # Structural columns (primary key + join keys) pinned into the detailed group
    # regardless of the question.
    pinned_count: int
    # Tenancy columns removed before the fit even started (§0).
    tenancy_hidden_count: int
    # At least one column entry lost a null/False/empty key (lossless). True on
    # under-budget results too since the compaction became unconditional (§3a) —
    # it reports what the payload IS, not that the budget forced anything.
    compacted: bool
    marker_added: bool
    # The COLUMNS SECTION (plus the marker) measures at or under the columns
    # budget. False only for a budget so small that even zero columns plus the
    # marker exceeds it. The base sections are exempt and never counted here.
    fits: bool


# ---------------------------------------------------------------------------
# 0. Tenancy strip (always, under budget too)
# ---------------------------------------------------------------------------


def _normalized(name: Any) -> str:
    return name.replace("_", "").lower() if isinstance(name, str) else ""


def _is_tenancy_column(column: Any) -> bool:
    """FAIL CLOSED on the entry shape. A column entry is normally the MCP's dict,
    but this module never assumes that (`_compact_column`/`_skeleton_of` pass any
    other shape through), so a BARE STRING entry — `"client_code"` — has to be
    recognized as the same column the dict form would be. No `_merge_columns` path
    emits one today; the point is that the shape check must not be what decides a
    tenancy column reaches the model. Anything that is neither a string nor a dict
    has no name to match and is kept."""
    if isinstance(column, str):
        name: Any = column
    elif isinstance(column, dict):
        name = column.get("name", "")
    else:
        return False
    return _normalized(name) in _TENANCY_COLUMNS


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
# 2. Structural pins — primary key + join keys
# ---------------------------------------------------------------------------


def _structural_names(schema: dict[str, Any]) -> set[str]:
    """Column names the schema's OWN `primary_key` / `join_keys` sections name.

    These are pinned into the detailed group whatever the question is: a model
    that cannot see a key column's documentation cannot join, de-duplicate or
    check grain, and no question phrasing makes that less true.

    Over-collection is harmless — the result is intersected with the table's real
    column names by the caller — so `join_on` clause fragments are mined for bare
    identifiers ("primary_supervisor_ee_code = employee_code" pins both sides of a
    self-join) rather than parsed. Reading the RESPONSE's own sections (not a
    catalog lookup) keeps this module a pure function of its input.
    """
    names: set[str] = set()

    def _add(value: Any) -> None:
        if isinstance(value, str):
            names.add(value)
        elif isinstance(value, list):
            names.update(item for item in value if isinstance(item, str))

    _add(schema.get("primary_key"))
    join_keys = schema.get("join_keys")
    if isinstance(join_keys, list):
        for entry in join_keys:
            if isinstance(entry, str):
                names.add(entry)
                continue
            if not isinstance(entry, dict):
                continue
            _add(entry.get("column"))
            join_on = entry.get("join_on")
            if isinstance(join_on, list):
                for clause in join_on:
                    if isinstance(clause, str):
                        names.update(_IDENTIFIER_RE.findall(clause))
    return names


# ---------------------------------------------------------------------------
# 3. Question-relevance ranking (ordering ONLY — D25)
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
# 4. Assembly + the marker
# ---------------------------------------------------------------------------


def _marker_text(report: SchemaFitReport, *, ranked: bool) -> str:
    """The model-facing note. Every clause is a fact about THIS payload; there is
    no instruction the runtime cannot honour.

    TWO ORDERING VARIANTS, because there are two orderings. *ranked* is True only
    when the turn's question produced usable tokens and therefore actually ranked
    the columns (`_rank_columns`'s own gate). Without one the emission order is the
    structural pins plus the head of the table's own list — a real and useful
    ordering, but NOT a relevance ordering, and a marker that called it one would
    be telling the model the leading columns were chosen for its request when
    nothing about the request was consulted. The unranked variant claims only what
    is true: the key and leading columns come first.

    THE NARROWING CLAUSE IS AN INSTRUCTION THE RUNTIME CAN HONOUR (2026-08-18). It
    names the MCP's optional `columns` argument to `getTableSchema`, which returns
    the full documentation for just the named columns; the repeated-read guard
    keys on the canonicalized arguments, so that call is a different signature from
    the bare fetch and is dispatched, not declined. It replaces the old "ask the
    user rather than assuming" dead end, which made a recoverable gap look like a
    conversation the model had to interrupt.

    It says nothing about the tenancy columns removed upstream of the fit: those
    are pre-applied by the platform and are not the model's to reason about, and
    naming them would invite exactly the predicate their removal prevents (§0)."""
    parts = [
        f"…[the COLUMN LIST of this schema was reduced to fit its token budget "
        f"(every other section — description, grain, temporal, primary_key, "
        f"join_keys, measures, rules, ambiguities — is complete and was not "
        f"touched). {report.listed_columns} of {report.total_columns} columns "
        f"are listed"
    ]
    if report.omitted_columns:
        parts.append(
            f"; {report.omitted_columns} could not be listed at all, so this is NOT the "
            f"full column list"
        )
    if ranked:
        parts.append(
            ". The columns are listed in RELEVANCE order, not the table's physical "
            "column order: the ones most likely to matter for the current request — "
            "and the key columns — come first, with the remainder after them in the "
            "table's own order. "
        )
    else:
        parts.append(
            ". The columns are NOT listed in the table's physical column order: the "
            "key columns and the columns the table lists first come first, with the "
            "remainder after them in the table's own order. "
        )
    if report.reduced_count:
        parts.append(
            f"{report.reduced_count} of the listed columns are shown with name and type "
            f"ONLY: their documentation (description, unit, allowed values, synonyms) "
            f"exists and was NOT shown to you here. Do NOT assume what such a column "
            f"means, contains or is denominated in from its name — if your analysis "
            f"depends on one of them, call getTableSchema again for this table with "
            f'columns: ["<name>", ...] naming just those columns, and their full '
            f"documentation is returned. "
        )
    return "".join(parts).rstrip() + "]"


def _assemble(
    *,
    base: dict[str, Any],
    key_order: list[str],
    entries: list[Any],
    detail_flags: list[bool],
    total_columns: int,
    pinned_count: int,
    tenancy_hidden_count: int,
    compacted: bool,
    lossy: bool,
    ranked: bool,
    columns_budget: int,
) -> tuple[dict[str, Any], SchemaFitReport]:
    """Build one candidate payload AND its report, marker included, so every
    trial measures exactly what would be returned.

    ONLY the columns section and the marker are measured against
    *columns_budget*: the base sections ride complete by contract (§1) and are
    not the fit's to trade away, so counting them would let a big `rules` buy
    itself column detail it must not be able to buy.
    """
    listed = len(entries)
    detailed = sum(detail_flags)
    report = SchemaFitReport(
        total_columns=total_columns,
        listed_columns=listed,
        detailed_count=detailed,
        reduced_count=listed - detailed,
        omitted_columns=total_columns - listed,
        pinned_count=pinned_count,
        tenancy_hidden_count=tenancy_hidden_count,
        compacted=compacted,
        marker_added=False,
        fits=False,
    )
    # The MCP's OWN key order is preserved, `columns` in its original position
    # (design §1.2 puts it between `join_keys` and `measures`): under budget this
    # function returns the caller's response minus the strips (§0/§3a), and a
    # reshuffled — even if equal — dict is a gratuitous difference for anything
    # diffing renderings.
    payload: dict[str, Any] = {
        key: (entries if key == "columns" else base[key]) for key in key_order
    }
    marker = ""
    if lossy:
        marker = _marker_text(report, ranked=ranked)
        payload[MARKER_KEY] = marker
    size = _estimate_tokens(json.dumps(entries, default=str) + marker)
    return payload, dataclasses.replace(
        report, marker_added=lossy, fits=size <= columns_budget
    )


# ---------------------------------------------------------------------------
# 5. The entry point
# ---------------------------------------------------------------------------


def fit_schema_under_cap(
    schema: dict[str, Any], columns_token_budget: int, *, question: str | None = None
) -> tuple[dict[str, Any], SchemaFitReport]:
    """Shape *schema* (a `getTableSchema` result) for the model: strip the tenancy
    columns and the information-free keys unconditionally, keep every table-level
    section complete, and fit the COLUMNS SECTION under *columns_token_budget* by
    the ladder documented at the top of this module. Returns the payload the model
    should see and a counts-only report.

    A schema that fits comes back COMPACTED-STABLE, not byte-identical (§3a, user
    spec 2026-08-18): tenancy columns and null/False/empty keys are gone on every
    path, the surviving keys keep their order and values, and no marker is added.

    *question* is the user's raw turn text. It is used ONLY to order the detail
    upgrades and the emitted list, and never appears in either return value (D25).
    """
    # `schema["columns"]` is a list by the caller's own branch condition
    # (`_cap_nontabular_result` dispatches here on `isinstance(..., list)`), so a
    # missing/other-shaped value is a programming error and should raise rather
    # than be silently dropped from the payload. Its ITEMS are not assumed to be
    # dicts — `_compact_column`/`_skeleton_of` pass anything else through.
    raw_columns: list[Any] = list(schema["columns"])
    # §0: NOT a budget decision — this happens on every path, including the
    # under-budget return below.
    columns = [column for column in raw_columns if not _is_tenancy_column(column)]
    tenancy_hidden = len(raw_columns) - len(columns)
    # §3a: NOT a budget decision either (USER SPEC 2026-08-18 point 5). The null
    # keys come off before the first trial render, so the under-budget return and
    # every over-budget rung below measure and emit the SAME compacted entries.
    compacted_columns = [_compact_column(column) for column in columns]
    compacted = any(new != old for new, old in zip(compacted_columns, columns, strict=True))
    # §1: every non-`columns` key rides COMPLETE. There is no ladder step that
    # touches this dict.
    base = {key: value for key, value in schema.items() if key != "columns"}
    key_order = list(schema)
    total = len(columns)

    structural = _structural_names(schema)
    pinned = [
        index
        for index, column in enumerate(columns)
        if isinstance(column, dict) and column.get("name") in structural
    ]
    # Did the question actually RANK anything? `_rank_columns`'s own gate, resolved
    # once here so the marker cannot claim an ordering the fit did not perform.
    ranked = bool(_tokens(question)) if question else False

    def build(
        entries: list[Any], flags: list[bool], *, lossy: bool
    ) -> tuple[dict[str, Any], SchemaFitReport]:
        return _assemble(
            base=base,
            key_order=key_order,
            entries=entries,
            detail_flags=flags,
            total_columns=total,
            pinned_count=len(pinned),
            tenancy_hidden_count=tenancy_hidden,
            compacted=compacted,
            lossy=lossy,
            ranked=ranked,
            columns_budget=columns_token_budget,
        )

    # --- ladder step (a): the compacted columns (lossless) ----------------
    all_detailed = [True] * total
    payload, report = build(list(compacted_columns), all_detailed, lossy=False)
    if report.fits:
        return payload, report

    # --- ladder step (b): skeleton for all, then grouped detail upgrades ---
    skeletons = [_skeleton_of(column) for column in compacted_columns]
    # A column the catalog never documented is already whole at skeleton size —
    # nothing is withheld by showing it as name/type, so it counts as detailed and
    # there is nothing to upgrade it TO.
    bare = [
        skeleton == column
        for skeleton, column in zip(skeletons, compacted_columns, strict=True)
    ]
    rank = _rank_columns(compacted_columns, question)
    forms: list[Any] = list(skeletons)
    detailed_flags: list[bool] = list(bare)
    # The DETAILED GROUP: the structural pins from the start (§3b/§4 — they lead
    # the list whether or not the question mentions them), plus every column the
    # budget lets us upgrade below.
    front: set[int] = set(pinned)

    def emission_order() -> list[int]:
        # [detailed group, relevance-ordered] + [the rest, ORIGINAL order].
        ordered_front = [index for index in rank if index in front]
        tail = [index for index in range(total) if index not in front]
        return ordered_front + tail

    def render() -> tuple[dict[str, Any], SchemaFitReport]:
        order = emission_order()
        return build(
            [forms[index] for index in order],
            [detailed_flags[index] for index in order],
            lossy=True,
        )

    # Structural columns first (question-independent), then question relevance.
    # `bare` columns are skipped: their skeleton IS their full entry, so an
    # "upgrade" would be a no-op that only costs a trial render.
    upgrade_order = [index for index in rank if index in front and not bare[index]]
    upgrade_order += [index for index in rank if index not in front and not bare[index]]
    for index in upgrade_order:
        if forms[index] == compacted_columns[index]:
            continue
        previous_form, previous_flag, was_front = forms[index], detailed_flags[index], (
            index in front
        )
        forms[index] = compacted_columns[index]
        detailed_flags[index] = True
        front.add(index)
        _, trial_report = render()
        if trial_report.fits:
            continue
        # A LATER column may still fit — a 40-token entry can follow a 900-token
        # one that did not — so this restores and keeps going rather than
        # breaking, which is what makes the packing dense.
        forms[index], detailed_flags[index] = previous_form, previous_flag
        if not was_front:
            front.discard(index)

    payload, report = render()
    if report.fits:
        return payload, report

    # --- ladder step (c): not even the skeletons fit ----------------------
    # Columns are ADMITTED in structural-then-relevance order, so what survives is
    # the keys plus what the question is about — the least relevant names are what
    # goes, rather than whatever happened to sit late in the physical order. With
    # no question `rank` IS list order, so this degrades to the head-cut the C5
    # floor already had. The SURVIVORS are still emitted in the grouped order.
    admit_order = [index for index in rank if index in front]
    admit_order += [index for index in rank if index not in front]
    admitted: set[int] = set()
    for index in admit_order:
        trial_indexes = [*sorted(admitted), index]
        _, trial_report = build(
            [forms[i] for i in trial_indexes],
            [detailed_flags[i] for i in trial_indexes],
            lossy=True,
        )
        if not trial_report.fits:
            break
        admitted.add(index)
    kept = [index for index in emission_order() if index in admitted]
    return build(
        [forms[i] for i in kept],
        [detailed_flags[i] for i in kept],
        lossy=True,
    )
