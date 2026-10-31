"""Build the one user-facing answer document from explicit, scope-filtered receipts.

The backend selects scoped SQL and validates presentation selectors. The UI
fetches saved rows and owns value formatting, chart geometry and display fallback.
Units never authorize arithmetic. There is no implicit execution selection.
"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal

from data_agent.runtime.answer_scrub import (
    scrub_answer_prose,
    strip_redundant_identifier_annotations,
)


class PresentationError(ValueError):
    """Safe, model-actionable presentation repair (never includes row contents)."""


def _object(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


TEXT = {"type": "string"}
ID = {"type": "string", "minLength": 1, "maxLength": 128}
REFS = {"type": "array", "items": ID, "uniqueItems": True, "maxItems": 32}
PRESENTATION_SCHEMA = _object(
    {
        "kind": {"enum": ["text", "figure", "facts", "table", "bars", "line"]},
        "result_id": ID,
        "markdown": TEXT,
        "label": {"type": "string", "minLength": 1, "description": "Required for figure, bars and line. State the measure and its unit, not just a section title."},
        "column": ID,
        "columns": {
            "type": "array",
            "items": ID,
            "minItems": 1,
            "maxItems": 16,
            "uniqueItems": True,
        },
        "category_column": {**ID, "description": "Bars accept scalar labels, including numbers, booleans and dates. Lines accept ISO dates/timestamps or consistent YYYY, YYYY-Qn, YYYY-MM periods; the UI orders distinct periods chronologically. Not epoch numbers."},
        "category_columns": {"type": "array", "items": ID, "minItems": 2, "maxItems": 6, "uniqueItems": True,
                             "description": "Bars only: exact columns identifying a compound category, e.g. Department and Pay Type. Use instead of category_column; all dimensions are preserved in the label without grouping rows."},
        "measure_column": {**ID, "description": "Numeric column: finite numbers or unformatted decimal/scientific strings. Never currency text, booleans, or inferred percentages. Nulls are gaps in lines; bars require non-null measures."},
        "direction": {
            "enum": ["ascending", "descending"],
            "description": "Only for an explicitly requested ranking in the returned row order. Omit direction and ties for ordinary comparison bars.",
        },
        "ties": {
            "enum": ["dense", "competition"],
            "description": "Required with direction for ranking; omit both for comparison bars.",
        },
    },
    ["kind"],
)
# Advertise the same per-kind requirements the materializer enforces, so the
# model can construct a valid first proposal rather than discover fields by retries.
PRESENTATION_SCHEMA["allOf"] = [
    {"if": {"properties": {"kind": {"enum": [kind]}}}, "then": {"required": fields}}
    for kind, fields in {
        "figure": ["result_id", "column", "label"],
        "facts": ["result_id", "columns"],
        "table": ["result_id"],
        "bars": ["result_id", "measure_column", "label"],
        "line": ["result_id", "category_column", "measure_column", "label"],
    }.items()
]
PRESENTATION_SCHEMA["allOf"].append({
    "if": {"properties": {"kind": {"const": "bars"}}},
    "then": {"oneOf": [{"required": ["category_column"]}, {"required": ["category_columns"]}]},
    "else": {"not": {"required": ["category_columns"]}},
})


SECTION_SCHEMA = _object(
    {
        "id": ID,
        "title": TEXT,
        "explanation": TEXT,
        "evidence_refs": REFS,
        "presentation": PRESENTATION_SCHEMA,
        "caveats": {
            "type": "array",
            "items": TEXT,
            "maxItems": 8,
            "description": "Only distinct limitations; use [] when none. Recorded assumptions are added automatically: do not repeat or paraphrase them here.",
        },
        "actions": {
            "description": "Required; use [] when no prepared actions are selected.",
            "type": "array",
            "maxItems": 8,
            "items": _object({"capability_ref": ID, "label": TEXT}, ["capability_ref", "label"]),
        },
        "table_reason": TEXT,
    },
    ["id", "evidence_refs", "presentation", "caveats", "actions"],
)
PROPOSAL_SCHEMA = _object(
    {
        "takeaway": TEXT,
        "sections": {"type": "array", "items": SECTION_SCHEMA, "maxItems": 16},
        "evidence": REFS,
        "deliverables": {
            "type": "array",
            "maxItems": 16,
            "items": _object(
                {
                    "intent_id": ID,
                    "answer": TEXT,
                    "result_ids": REFS,
                    "evidence_type": {"enum": ["warehouse", "product", "catalog", "capability"]},
                },
                ["intent_id", "answer", "result_ids"],
            ),
        },
    },
    ["takeaway", "sections"],
)


# Recovery uses the same selectors and materializer as ordinary finalization.
RECOVERY_SCHEMA = _object(
    {"takeaway": {"type": "string", "minLength": 1},
     "sections": PROPOSAL_SCHEMA["properties"]["sections"],
     "evidence": REFS,
     "unfinished": {"type": "array", "minItems": 1,
                    "items": {"type": "string", "minLength": 1}}},
    ["takeaway", "sections", "evidence", "unfinished"],
)


def proposal_to_internal(raw):
    """Project the new proposal into internal evidence/selection bookkeeping only.

    The agent and API accept only the new contract; the existing query verification
    and repair machinery can continue using its private table/evidence structures.
    """
    from jsonschema import Draft202012Validator

    error = next(Draft202012Validator(PROPOSAL_SCHEMA).iter_errors(raw), None)
    if error:
        location = ".".join(map(str, error.absolute_path)) or "root"
        detail = "check the declared field schema"
        if error.validator == "required" and isinstance(error.instance, dict):
            missing = [key for key in error.validator_value if key not in error.instance]
            detail = "missing required field(s): " + ", ".join(missing)
        elif error.validator == "type":
            detail = "expected type: " + str(error.validator_value)
        elif error.validator == "additionalProperties":
            detail = "use only declared fields: " + ", ".join(error.schema.get("properties", {}))
        raise PresentationError(f"Invalid answer proposal at {location}: {detail}.")
    sections = raw["sections"]
    if len({s["id"] for s in sections}) != len(sections):
        raise PresentationError("Section IDs must be unique.")
    tables, refs, capabilities = [], list(raw.get("evidence", [])), []
    seen = set()
    for section in sections:
        refs.extend(section["evidence_refs"])
        p = section["presentation"]
        if p["kind"] != "text":
            rid = p.get("result_id")
            if not rid or rid not in section["evidence_refs"]:
                raise PresentationError(
                    "Every visual must cite its exact result_id in section evidence_refs."
                )
            if rid not in seen:
                tables.append({"result_id": rid, "caption": section.get("title")})
                seen.add(rid)
        elif p.get("result_id"):
            raise PresentationError(
                "Text sections cannot select data tables; use a data presentation."
            )
        capabilities.extend(a["capability_ref"] for a in section["actions"])
    visible = [s for s in sections if s["presentation"]["kind"] == "table"]
    if len(visible) > 1 and any(not s.get("table_reason", "").strip() for s in visible):
        raise PresentationError(
            "Prefer zero or one visible table. Justify each visible table with table_reason when more are necessary; otherwise keep only the primary presentation."
        )
    return {
        **raw,
        "answer": raw["takeaway"],
        "tables": tables,
        "evidence": list(dict.fromkeys(refs)),
        "capability_refs": list(dict.fromkeys(capabilities)),
    }


_FRESHNESS_CLAIM = re.compile(
    r"\b(?:data|dataset|results?|information|figures|records)\s+"
    r"(?:(?:is|are|was|were)\s+(?:(?:currently|fully)\s+)?"
    r"(?:live|real[ -]time|up[ -]to[ -]date|fresh|stale|outdated|current)\b|as\s+of\b)",
    re.IGNORECASE,
)


def _text(value, business_values=frozenset()):
    if not isinstance(value, str):
        return ""
    if _FRESHNESS_CLAIM.search(re.sub(r"[*`_]", "", value)):
        raise PresentationError(
            "Freshness belongs only in code-provided metadata. Remove freshness prose; "
            "if recorded as an assumption, call recordAssumptions with the corrected full list "
            "or [] to clear it. Reuse existing results and preserve requested reporting dates."
        )
    value = strip_redundant_identifier_annotations(value, business_values=business_values)
    clean, redactions = scrub_answer_prose(value, provenance=None, business_values=business_values)
    if redactions:
        raise PresentationError(
            "Presentation text contains internal identifiers. Use business wording."
        )
    return (clean or "").strip()


def _chart_label(value):
    if value is None:
        return "Not available"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (int, float, Decimal)):
        if not Decimal(str(value)).is_finite():
            raise PresentationError("Chart categories must be finite scalars.")
        return str(value)
    if isinstance(value, str) and value.strip():
        return value
    raise PresentationError("Chart categories must be non-empty scalar labels; structured cells require a table.")


def _chart_period(value):
    """Interpret explicit calendar periods, never guess epoch units or local dates."""
    label = _chart_label(value)
    if value is None or isinstance(value, bool):
        raise ValueError
    if re.fullmatch(r"[0-9]{4}", label):
        anchor, grain = label + "-01-01", "year"
    elif re.fullmatch(r"[0-9]{4}-Q[1-4]", label):
        anchor, grain = f"{label[:4]}-{(int(label[-1]) - 1) * 3 + 1:02d}-01", "quarter"
    elif re.fullmatch(r"[0-9]{4}-[0-9]{2}", label):
        anchor, grain = label + "-01", "month"
    elif re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", label):
        anchor, grain = label, "day"
    elif re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ]", label):
        anchor, grain = label, "timestamp"
    else:
        raise ValueError
    parsed = datetime.fromisoformat(anchor.replace("Z", "+00:00"))
    return parsed, parsed.isoformat() if grain == "timestamp" else anchor, grain


def _index(columns, name):
    if not name or columns.count(name) != 1:
        raise PresentationError(
            "Select one exact, unambiguous returned column for the presentation."
        )
    return columns.index(name)


def build_document(
    args,
    trail,
    *,
    turn_index,
    cards=(),
    assumptions=(),
    sql_by_id=None,
    business_values=frozenset(),
    format_overrides=None,
):
    """Materialize a candidate for judge review. trail MUST already be scope-filtered."""
    from data_agent.runtime.loop.proposal import evidence_kind

    if "sections" not in args:
        raise PresentationError("Use finalizeAnswer with takeaway and sections.")

    def text(value):
        return _text(value, business_values)

    entries = {
        e.tool_call_id: e
        for e in trail
        if e.turn_index == turn_index and evidence_kind(e) and e.provenance is not None
    }
    selected_cards = {c["name"]: c for c in cards or ()}
    sql_by_id = sql_by_id or {}
    sources, sections = {}, []
    for proposed in args["sections"]:
        if any(ref not in entries for ref in proposed["evidence_refs"]):
            raise PresentationError(
                "Section evidence must reference successful accessible current-turn receipts."
            )
        section = {
            "id": proposed["id"],
            "evidence_refs": list(proposed["evidence_refs"]),
            "caveats": [text(v) for v in proposed["caveats"]],
            "actions": [],
        }
        for key in ("title", "explanation"):
            if proposed.get(key):
                section[key] = text(proposed[key])
        for action in proposed["actions"]:
            ref = action["capability_ref"]
            if ref not in selected_cards or not any(
                entries[r].capability_terminal and entries[r].tool_name == ref
                for r in proposed["evidence_refs"]
            ):
                raise PresentationError(
                    "An action must reference a selected prepared capability and its evidence receipt."
                )
            section["actions"].append({"capability_ref": ref, "label": text(action["label"])})
        p = proposed["presentation"]
        kind = p["kind"]
        if kind == "text":
            section["presentation"] = {"kind": "text", "markdown": text(p.get("markdown", ""))}
            if (
                not section["presentation"]["markdown"]
                and not section["actions"]
                and not section.get("explanation")
            ):
                raise PresentationError("An empty text section is not an answer.")
            sections.append(section)
            continue
        rid = p["result_id"]
        entry = entries.get(rid)
        if entry is None or evidence_kind(entry) != "warehouse" or entry.result_preview is None:
            raise PresentationError(
                "A data presentation needs a successful query or verified blueprint result."
            )
        preview = entry.result_preview
        columns, rows = list(preview.columns), preview.preview_rows
        formats = {**preview.column_formats, **(format_overrides or {}).get(rid, {})}
        complete = not preview.truncated and preview.row_count == len(rows)
        source = {
            "retrieved_at": entry.retrieved_at or entry.ts,
            "column_formats": deepcopy(formats),
            "row_count": preview.row_count,
            "completeness": "complete" if complete else "partial",
        }
        sql = sql_by_id.get(rid) or (entry.args.get("sql") if entry.tool_name == "runQuery" else None)
        if not isinstance(sql, str) or not sql.strip():
            raise PresentationError("A data presentation needs its exact executed SQL.")
        source["sql"] = sql
        if entry.freshness is not None:
            source["freshness"] = deepcopy(entry.freshness)
        data_as_of = getattr(entry, "data_as_of", None)
        if data_as_of:
            source["data_as_of"] = data_as_of
        sources[rid] = source
        if kind == "table":
            rendered = {"kind": "table", "result_id": rid}
        else:
            try:
                if kind in {"figure", "facts"} and preview.row_count != 1:
                    raise PresentationError("Figures and facts require exactly one returned row.")
                rendered = _visual(p, columns, rows, business_values=business_values, column_formats=formats)
            except PresentationError as exc:
                section["caveats"].append(
                    f"The requested {kind} presentation is shown as a table: {exc}"
                )
                rendered = {"kind": "table", "result_id": rid}
        if rendered["kind"] == "line":
            column = rendered["x_column"]
            index = columns.index(column)
            try:
                grains = {_chart_period(row[index])[2] for row in rows if row[index] is not None}
                declared = source["column_formats"].get(column, {})
                if len(grains) == 1 and declared.get("kind") in {None, "text", "date", "datetime"} and "grain" not in declared:
                    grain = grains.pop()
                    source["column_formats"][column] = {
                        "kind": "datetime" if grain == "timestamp" else "date", "grain": grain,
                    }
            except (ValueError, TypeError, PresentationError):
                pass  # Unknown dates stay as declared; the UI owns parsing/fallback.
        section["presentation"] = rendered
        sections.append(section)
    # Keep one visible occurrence of each recorded interpretation. Only normalize
    # presentation differences; semantic paraphrases are consolidated by the model.
    from data_agent.runtime.composite.record_assumptions import assumption_key, clean_assumptions
    assumptions = clean_assumptions([text(a) for a in (assumptions or ())])
    seen = set()
    for section in sections:
        caveats = []
        for caveat in section["caveats"]:
            key = assumption_key(caveat)
            if not key or key in seen:
                continue
            seen.add(key)
            caveats.append(caveat)
        section["caveats"] = caveats
    remaining = [a for a in assumptions if assumption_key(a) not in seen]
    if remaining:
        if sections:
            sections[0]["caveats"].extend(remaining)
        else:
            sections.append({
                "id": "scope", "evidence_refs": [],
                "presentation": {"kind": "text", "markdown": "\n\n".join(remaining)},
                "caveats": [], "actions": [],
            })
    return {
        "takeaway": text(args.get("answer", args.get("takeaway", ""))),
        "sections": sections,
        "sources": sources,
    }


def _visual(p, columns, rows, *, business_values=frozenset(), column_formats=None):
    """Validate selectors and emit instructions; the UI fetches and renders values."""
    formats = column_formats or {}
    kind = p["kind"]
    result = {"kind": kind, "result_id": p["result_id"]}
    label = _text(p.get("label", ""), business_values)
    if kind == "figure":
        _index(columns, p.get("column"))
        result["column"] = p["column"]
    elif kind == "facts":
        names = p.get("columns", [])
        if not names:
            raise PresentationError("Facts must name the returned columns to display.")
        for name in names:
            _index(columns, name)
        result["columns"] = list(names)
    else:
        xs = p.get("category_columns") or [p.get("category_column")]
        for name in xs:
            _index(columns, name)
        measure = p.get("measure_column")
        _index(columns, measure)
        if measure in xs or formats.get(measure, {}).get("kind") in {"identifier", "text", "date", "datetime", "boolean"}:
            raise PresentationError("Select distinct category columns and a quantitative measure, or use a table.")
        result.update({"x_columns": list(xs)} if len(xs) > 1 else {"x_column": xs[0]})
        result["y_column"] = measure
        if kind == "bars":
            if bool(p.get("direction")) != bool(p.get("ties")):
                raise PresentationError("Ranking bars require both direction and tie policy.")
            if p.get("direction"):
                result["ranking"] = {"direction": p["direction"], "ties": p["ties"]}
    if kind != "facts":
        if not label:
            raise PresentationError("A figure or chart needs a meaningful measure label.")
        result["label"] = label
    return result


def seal_document(candidate, *, answer_id, generated_at, revision=1):
    return {
        "schema_version": 2,
        "answer_id": answer_id,
        "revision": revision,
        "generated_at": generated_at,
        **deepcopy(candidate),
    }


def text_document(text):
    """Runtime limitation/conversation with no automatically selected data."""
    return {"takeaway": text if text and text.strip() else "I could not produce a verified answer from the available evidence.", "sections": [], "sources": {}}
