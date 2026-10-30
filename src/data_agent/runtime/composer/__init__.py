"""Build the one user-facing answer document from explicit, scope-filtered receipts.

Adapted from the answer-block design: values are host-built, previews must be
complete for visuals; unsupported presentations become explained tables for judge review.
Units never authorize arithmetic. There is no implicit execution selection.
"""

from __future__ import annotations

import math
import re
from copy import deepcopy
from hashlib import sha256
from datetime import date, datetime
from decimal import Decimal

from data_agent.runtime.answer_scrub import scrub_answer_prose


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
        "category_column": {**ID, "description": "Bars accept scalar labels, including numbers, booleans and dates. Lines accept ISO dates/timestamps or consistent YYYY, YYYY-Qn, YYYY-MM periods; the backend orders distinct periods chronologically. Not epoch numbers."},
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
        "supporting_data": {"type": "boolean"},
        "fallback": {"type": "boolean"},
        "caveats": {
            "type": "array",
            "items": TEXT,
            "maxItems": 8,
            "description": "Required; use [] when there are no caveats.",
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
        elif p.get("result_id") or section.get("supporting_data") or section.get("fallback"):
            raise PresentationError(
                "Text sections cannot select data tables; use a data presentation."
            )
        capabilities.extend(a["capability_ref"] for a in section["actions"])
    visible = [s for s in sections if s["presentation"]["kind"] == "table"]
    if len(visible) > 1 and any(not s.get("table_reason", "").strip() for s in visible):
        raise PresentationError(
            "Prefer zero or one visible table. Justify each visible table with table_reason when more are necessary; otherwise use supporting data."
        )
    return {
        **raw,
        "answer": raw["takeaway"],
        "tables": tables,
        "evidence": list(dict.fromkeys(refs)),
        "capability_refs": list(dict.fromkeys(capabilities)),
    }


def _text(value, business_values=frozenset()):
    if not isinstance(value, str):
        return ""
    clean, redactions = scrub_answer_prose(value, provenance=None, business_values=business_values)
    if redactions:
        raise PresentationError(
            "Presentation text contains internal identifiers. Use business wording."
        )
    return (clean or "").strip()


def _chart_number(value):
    """Normalize warehouse numeric cells to finite, precision-safe JSON numbers."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise PresentationError("Boolean measures are not numeric counts; select a numeric measure.")
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value):
            raise PresentationError("Chart measures must be numeric; formatted currency or locale-dependent numbers require a numeric column.")
        value = Decimal(value)
    if not isinstance(value, (int, float, Decimal)):
        raise PresentationError("Chart measures must be numeric scalars.")
    # Apply the same check to native numbers and serialized Decimal/Int64 cells.
    # UI geometry is a JavaScript number; display strings retain original precision.
    try:
        number = float(value)
        if math.isfinite(number) and abs(number) <= 2**53 - 1 and Decimal(str(number)) == Decimal(str(value)):
            return number
    except (OverflowError, ValueError):
        pass
    raise PresentationError("The chart value is non-finite or loses precision; use its table.")


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


def _format(value):
    if value is None:
        return "Not available"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PresentationError("A non-finite value cannot be displayed.")
        return f"{int(value):,}" if value.is_integer() else f"{value:,}"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise PresentationError("A non-finite value cannot be displayed.")
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, str):
        return value
    raise PresentationError("A structured cell requires a table presentation.")


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
        complete = not preview.truncated and preview.row_count == len(rows)
        source = {
            "retrieved_at": entry.retrieved_at or entry.ts,
            "row_count": preview.row_count,
            "completeness": "complete" if complete else "partial",
        }
        # Opaque handle resolves only through a persisted approved answer + scoped trail.
        if rid in sql_by_id or entry.tool_name == "runQuery" and entry.args.get("sql"):
            source["table_ref"] = table_reference(turn_index, rid)
        if entry.freshness is not None:
            source["freshness"] = deepcopy(entry.freshness)
        data_as_of = getattr(entry, "data_as_of", None)
        if data_as_of:
            source["data_as_of"] = data_as_of
        sources[rid] = source
        for flag, field in (
            ("supporting_data", "supporting_table"),
            ("fallback", "fallback_table"),
        ):
            if proposed.get(flag):
                if kind == "table" or "table_ref" not in source:
                    raise PresentationError(
                        "Supporting/fallback data needs a pageable source and a non-table primary presentation."
                    )
                section[field] = {"result_id": rid}
        if kind == "table":
            if "table_ref" not in source:
                raise PresentationError("The selected result has no pageable table.")
            rendered = {"kind": "table", "result_id": rid}
        else:
            try:
                if not complete or len(rows) > 25:
                    raise PresentationError(
                        "A visual requires the complete result, at most 25 rows; select a table without rerunning the query."
                    )
                rendered = _visual(p, columns, rows, business_values=business_values)
            except PresentationError as exc:
                if "table_ref" not in source:
                    raise
                section["caveats"].append(
                    f"The requested {kind} presentation is shown as a table: {exc}"
                )
                rendered = {"kind": "table", "result_id": rid}
                section.pop("fallback_table", None)
                section.pop("supporting_table", None)
        section["presentation"] = rendered
        sections.append(section)
    # Assumptions remain user-visible and are reviewed with the candidate.
    if assumptions:
        if sections:
            sections[0]["caveats"] = list(
                dict.fromkeys([*sections[0]["caveats"], *[text(a) for a in assumptions]])
            )
        else:
            sections.append(
                {
                    "id": "scope",
                    "evidence_refs": [],
                    "presentation": {
                        "kind": "text",
                        "markdown": "\n\n".join(text(a) for a in assumptions),
                    },
                    "caveats": [],
                    "actions": [],
                }
            )
    return {
        "takeaway": text(args.get("answer", args.get("takeaway", ""))),
        "sections": sections,
        "sources": sources,
    }


def _visual(p, columns, rows, *, business_values=frozenset()):
    kind, rid = p["kind"], p["result_id"]
    label = _text(p.get("label", ""), business_values)
    if any(len(r) != len(columns) for r in rows):
        raise PresentationError("The result has malformed row widths; use its table.")
    if kind in {"figure", "facts"}:
        if len(rows) != 1:
            raise PresentationError("Figures and facts require exactly one returned row.")
        if kind == "figure":
            index = _index(columns, p.get("column"))
            if not label:
                raise PresentationError(
                    "A figure needs a meaningful label including its unit where relevant."
                )
            return {
                "kind": kind,
                "result_id": rid,
                "label": label,
                "value": _format(rows[0][index]),
            }
        names = p.get("columns", [])
        if not names:
            raise PresentationError("Facts must name the returned columns to display.")
        return {
            "kind": kind,
            "result_id": rid,
            "items": [
                {"label": _text(c.replace("_", " "), business_values), "value": _format(rows[0][_index(columns, c)])}
                for c in names
            ],
        }
    category_names = p.get("category_columns") or [p.get("category_column")]
    xs = [_index(columns, name) for name in category_names]
    y = _index(columns, p.get("measure_column"))
    if y in xs or not rows:
        raise PresentationError(
            "The visual needs distinct category and measure columns with rectangular rows."
        )
    if not label:
        raise PresentationError(
            "A chart needs a meaningful measure label including its unit where relevant."
        )
    labels = [" · ".join(_chart_label(r[x]) for x in xs) for r in rows]
    original_values = [r[y] for r in rows]
    values = [_chart_number(v) for v in original_values]
    if len({label.strip() for label in labels}) != len(labels):
        raise PresentationError(
            "Chart categories must be distinct non-empty strings; use a table for ambiguous grouping."
        )
    if kind == "bars":
        if any(v is None for v in values):
            raise PresentationError("Bars require finite numeric values; missing is not zero.")
        direction = p.get("direction")
        if bool(direction) != bool(p.get("ties")):
            raise PresentationError(
                "For comparison bars, omit both direction and ties and preserve result order. A requested ranking requires both direction and tie policy."
            )
        if direction:
            if values != sorted(values, reverse=direction == "descending"):
                raise PresentationError(
                    "Ranking direction contradicts result order. For comparison bars, remove direction and ties to preserve existing rows; do not rerun the query for presentation."
                )
        rendered = {
            "kind": kind,
            "result_id": rid,
            "measure_label": label,
            "mode": "ranking" if direction else "comparison",
            "items": [],
        }
        rank, dense = 0, 0
        for i, (category, value) in enumerate(zip(labels, values, strict=True)):
            if i == 0 or value != values[i - 1]:
                dense += 1
                rank = dense if p.get("ties") == "dense" else i + 1
            item = {"label": category, "raw": value, "value": _format(original_values[i])}
            if direction:
                item["rank"] = rank
            rendered["items"].append(item)
        if direction:
            rendered["ranking"] = {"direction": direction, "ties": p["ties"]}
        return rendered
    if len(rows) < 2:
        raise PresentationError(
            "A line chart needs at least two periods and finite values or explicit null gaps."
        )
    try:
        periods = [_chart_period(r[xs[0]]) for r in rows]
        dates, times, grains = zip(*periods, strict=True)
        if len(set(grains)) != 1:
            raise ValueError
        if len(set(dates)) != len(dates):
            raise ValueError
        order = sorted(range(len(dates)), key=dates.__getitem__)
    except (ValueError, TypeError):
        raise PresentationError(
            "Line chart periods must be distinct chronological ISO dates/timestamps or YYYY, YYYY-Qn, YYYY-MM periods of consistent granularity; epoch and locale dates require an explicit date column."
        ) from None
    return {
        "kind": "line",
        "result_id": rid,
        "measure_label": label,
        "points": [
            {
                "time": time,
                "label": t,
                "value": v,
                "formatted_value": _format(raw) if v is not None else None,
            }
            for time, t, v, raw in ((times[i], labels[i], values[i], original_values[i]) for i in order)
        ],
    }


def table_reference(turn_index, result_id):
    """Stable opaque identity; authorization remains session- and scope-bound."""
    return "tbl_" + sha256(f"{turn_index}:{result_id}".encode()).hexdigest()


def seal_document(candidate, *, answer_id, generated_at, revision=1):
    return {
        "schema_version": 1,
        "answer_id": answer_id,
        "revision": revision,
        "generated_at": generated_at,
        **deepcopy(candidate),
    }


def text_document(text):
    """Runtime limitation/conversation with no automatically selected data."""
    return {"takeaway": text if text and text.strip() else "I could not produce a verified answer from the available evidence.", "sections": [], "sources": {}}
