"""Build the one user-facing answer document from explicit, scope-filtered receipts.

The backend selects scoped SQL and validates presentation selectors. The UI
fetches saved rows and owns value formatting, chart geometry and display fallback.
Units never authorize arithmetic. There is no implicit execution selection.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal

from .components import ALL_COMPONENTS, CURRENT_COMPONENTS, restrict_schema

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
        "kind": {"enum": list(ALL_COMPONENTS)},
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


PRESENTATION_SCHEMA['properties'].update({
    'series': {'type': 'array', 'minItems': 2, 'maxItems': 4,
               'items': _object({'measure_column': ID, 'label': {**TEXT, 'minLength': 1}}, ['measure_column', 'label'])},
    'series_column': ID,
    # `type` before `const`: a quoted "true" then fails as a type error, which names the
    # expected JSON type, rather than as an opaque const mismatch.
    'composition': {'type': 'boolean', 'const': True, 'description': 'The JSON boolean true, never the string "true". Exhaustive mutually exclusive additive parts of the stated whole; not top-N or rates.'},
    'additive': {'type': 'boolean', 'const': True, 'description': 'The JSON boolean true, never the string "true". Disjoint additive measures, not a total plus its components or rates.'},
})
for kind in ('multi_series_line', 'grouped_bars', 'stacked_bars', 'heatmap', 'donut'):
    fields = ['result_id', 'category_column', 'label']
    fields += ['series'] if kind in ('multi_series_line', 'grouped_bars', 'stacked_bars') else ['measure_column']
    fields += ['series_column'] if kind == 'heatmap' else ['composition'] if kind == 'donut' else ['additive'] if kind == 'stacked_bars' else []
    PRESENTATION_SCHEMA['allOf'].append({
        'if': {'properties': {'kind': {'const': kind}}},
        'then': {'required': fields, 'not': {'anyOf': [{'required': [key]} for key in
            (('measure_column', 'category_columns', 'direction', 'ties') if 'series' in fields else ('series', 'category_columns', 'direction', 'ties'))]}}})


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
            "description": "Default to []. Include only material business/data qualifications not already clear in the title or answer. No boilerplate, chart limits, paging, CSV, or UI instructions. Recorded assumptions are added automatically: do not repeat or paraphrase them here.",
        },
        "actions": {
            "description": "Required; use [] when no prepared actions are selected.",
            "type": "array",
            "maxItems": 8,
            "items": _object({"capability_ref": ID, "label": TEXT}, ["capability_ref", "label"]),
        },
        "table_reason": {**TEXT, "description": "When the answer has two or more table presentations, every table section, the first included, requires a non-blank reason explaining its distinct requested purpose."},
    },
    ["id", "evidence_refs", "presentation", "caveats", "actions"],
)
ASSUMPTIONS = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Optional. The plain-English interpretations behind this answer, one short sentence each, "
        "in the user's terms; no SQL, codes or column names. When present it replaces the turn's "
        "assumptions exactly as recordAssumptions does, and [] clears them; omit it to keep what "
        "recordAssumptions recorded. They are shown with the answer: do not repeat them in caveats."
    ),
}
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
        "assumptions": ASSUMPTIONS,
    },
    ["takeaway", "sections"],
)


# Count only explicit table presentations; charts and their data access do not
# create additional visible tables. Keep this on sections so recovery and judge
# correction schemas inherit it even when they add their own top-level allOf.
_TABLE_SECTION = {
    "type": "object", "required": ["presentation"],
    "properties": {"presentation": {
        "type": "object", "required": ["kind"],
        "properties": {"kind": {"const": "table"}},
    }},
}
PROPOSAL_SCHEMA["properties"]["sections"]["allOf"] = [{
    "if": {"contains": _TABLE_SECTION, "minContains": 2},
    "then": {"items": {
        "if": _TABLE_SECTION,
        "then": {"required": ["table_reason"],
                 "properties": {"table_reason": {"type": "string", "pattern": r"\S"}}},
    }},
}]


# Recovery uses the same selectors and materializer as ordinary finalization.
RECOVERY_SCHEMA = _object(
    {"takeaway": {"type": "string", "minLength": 1},
     "sections": PROPOSAL_SCHEMA["properties"]["sections"],
     "evidence": REFS,
     "unfinished": {"type": "array", "minItems": 1,
                    "items": {"type": "string", "minLength": 1}}},
    ["takeaway", "sections", "evidence", "unfinished"],
)


_EMPTY = (None, "", [], {})
# Presentation flags whose only valid value is true. A quoted "true" can mean nothing
# else, so it is read as true; any other value is left for the schema to reject.
_TRUE_ONLY_FLAGS = ("composition", "additive")
# Section lists where absence and [] mean the same thing.
_SECTION_LISTS = ("caveats", "actions")
# Undeclared answer-level fields models invent that never carry answer content
# (`sections_note`: "ok", "na", "omit", "single section"). Dropped whatever their value;
# any other undeclared field is still rejected by name, so real content such as
# top-level caveats is never lost silently. Add a name only when its values are junk.
_JUNK_ANSWER_FIELDS = frozenset({"sections_note"})


def _drop_empty(obj, declared, *, any_field=False, keep=(), junk=frozenset()):
    """Drop an empty (null, "", [] or {}) undeclared field, or any empty field with
    `any_field`, and any undeclared field named in `junk`. A declared null stays: an
    explicit null `evidence` is not an absent one. Returns `obj` itself when nothing
    is dropped."""
    drop = [
        key for key, value in obj.items()
        if key not in keep and (
            (any_field or key not in declared) and value in _EMPTY
            or key in junk and key not in declared
        )
    ]
    return {key: value for key, value in obj.items() if key not in drop} if drop else obj


def _normalize_section(section):
    if not isinstance(section, dict):
        return section
    out = _drop_empty(section, SECTION_SCHEMA["properties"])
    missing = [key for key in _SECTION_LISTS if key not in out]
    if missing:
        out = {**out, **{key: [] for key in missing}}
    presentation = out.get("presentation")
    if isinstance(presentation, dict):
        # Every declared presentation field except markdown is invalid when empty, so an
        # empty one is a field that does not apply (e.g. `column: ""` on bars).
        cleaned = _drop_empty(presentation, PRESENTATION_SCHEMA["properties"], any_field=True,
                              keep=("kind", "markdown"))
        quoted = [
            flag for flag in _TRUE_ONLY_FLAGS
            if isinstance(cleaned.get(flag), str) and cleaned[flag].strip().lower() == "true"
        ]
        if quoted:
            cleaned = {**cleaned, **dict.fromkeys(quoted, True)}
        if cleaned is not presentation:
            out = {**out, "presentation": cleaned}
    return out


def normalize_answer_arguments(raw, fields=None):
    """Decode only allowlisted, top-level JSON-encoded arrays, without mutation.

    Invalid JSON and non-arrays remain untouched so the schema owns path/type
    feedback. Prose and nested fields are never interpreted as JSON. Beyond that, only
    conversions that cannot change meaning: empty undeclared fields and known-junk ones
    (`_JUNK_ANSWER_FIELDS`) are dropped, empty
    presentation selectors are absent, a section without caveats or actions has [],
    and a quoted "true" in a true-only presentation flag is true. `fields` are the
    top-level fields the calling tool declares (default: finalizeAnswer's).
    """
    if not isinstance(raw, dict):
        return raw

    def invalid_constant(value):
        raise ValueError("Non-JSON constant")

    normalized = raw
    for field in ("sections", "evidence"):
        if not isinstance(raw.get(field), str):
            continue
        try:
            value = json.loads(raw[field], parse_constant=invalid_constant)
        except (ValueError, RecursionError):
            continue
        if isinstance(value, list):
            if normalized is raw:
                normalized = dict(raw)
            normalized[field] = value
    normalized = _drop_empty(normalized, PROPOSAL_SCHEMA["properties"] if fields is None else fields,
                             junk=_JUNK_ANSWER_FIELDS)
    sections = normalized.get("sections")
    if isinstance(sections, list):
        cleaned = [_normalize_section(section) for section in sections]
        if any(new is not old for new, old in zip(cleaned, sections, strict=True)):
            normalized = {**normalized, "sections": cleaned}
    return normalized


# Fields a rendered answer document carries that a proposal never sets: the renderer
# materializes `sources` from the selected results and `seal_document` adds the rest.
RUNTIME_OWNED_DOCUMENT_FIELDS = frozenset(
    {"sources", "schema_version", "answer_id", "revision", "generated_at"}
)


def _proposal_presentation(rendered):
    """Undo `_visual`/`charts.chart_selection` field names; never overwrite a proposal name."""
    if not isinstance(rendered, dict):
        return rendered
    p = dict(rendered)
    heatmap = p.get("kind") == "heatmap"
    renames = {"x_column": "category_column", "x_columns": "category_columns",
               "y_column": "series_column" if heatmap else "measure_column"}
    if heatmap:
        renames["value_column"] = "measure_column"
    for old, new in renames.items():
        if old in p and new not in p:
            p[new] = p.pop(old)
    ranking = p.get("ranking")
    if isinstance(ranking, dict) and "direction" not in p and "ties" not in p:
        p.pop("ranking")
        p.update({k: ranking[k] for k in ("direction", "ties") if k in ranking})
    if isinstance(p.get("series"), list):
        p["series"] = [
            {("measure_column" if k == "y_column" else k): v for k, v in item.items()}
            if isinstance(item, dict) and "measure_column" not in item else item
            for item in p["series"]
        ]
    return p


def proposal_from_rendered(document):
    """Return a document a model copied from a rendered answer to proposal form.

    A reviewer or recovery model is shown the rendered document but must answer in
    proposal form. Runtime-owned fields are dropped and rendered presentation names
    mapped back, by inverting the renderer; nothing else is repaired. Returns the
    document and how many fields changed, without mutating the input.
    """
    if not isinstance(document, dict):
        return document, 0
    kept = {k: v for k, v in document.items() if k not in RUNTIME_OWNED_DOCUMENT_FIELDS}
    changed = len(document) - len(kept)
    if isinstance(kept.get("sections"), list):
        sections = []
        for section in kept["sections"]:
            if isinstance(section, dict) and "presentation" in section:
                presentation = _proposal_presentation(section["presentation"])
                if presentation != section["presentation"]:
                    changed += 1
                    section = {**section, "presentation": presentation}
            sections.append(section)
        kept["sections"] = sections
    return (kept, changed) if changed else (document, 0)


_SECTION_FIELD = "is a section field: put it in the section it belongs to (with no sections, say it in the takeaway)"
_PRESENTATION_FIELD = "belongs inside presentation"
# Where an undeclared field the model sends actually belongs, by the level it was sent at.
_FIELD_HOMES = {
    "answer": {
        **dict.fromkeys(("caveats", "actions", "table_reason", "title", "explanation"), _SECTION_FIELD),
        **dict.fromkeys(("recordAssumptions", "record_assumptions"), "is not a field: send the list as assumptions"),
    },
    "section": {
        **dict.fromkeys(PRESENTATION_SCHEMA["properties"], _PRESENTATION_FIELD),
        "assumptions": "is an answer-level field: send it next to takeaway, not inside a section",
    },
    "presentation": {
        **dict.fromkeys(("title", "explanation", "caveats", "actions", "table_reason", "evidence_refs"),
                        "is a section field, not a presentation field"),
        "composite": "is not a field: use composition",
    },
}
# Only identifier-like names are echoed back, so a value sent as a key is never repeated.
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_JSON_TYPES = {str: "string", list: "array", dict: "object", bool: "boolean",
               int: "number", float: "number", type(None): "null"}


def _schema_problem(error):
    """Say what is wrong in terms the model can act on; never echo response values."""
    if error.validator == "required" and isinstance(error.instance, dict):
        missing = [key for key in error.validator_value if key not in error.instance]
        detail = "missing required field(s): " + ", ".join(missing)
        if "table_reason" in missing:
            detail += " (with two or more table sections, every table section needs a table_reason, the first included)"
        return detail
    if error.validator == "type":
        detail = "expected type: " + str(error.validator_value) + ", got: " + _JSON_TYPES.get(type(error.instance), "unknown")
        if isinstance(error.instance, str) and error.validator_value in ("array", "object"):
            try:
                decoded = json.loads(error.instance)
            except RecursionError:
                # Nesting too deep to decode at all; the normalizer gives up the same way.
                detail += f" (text nested too deeply to read). Send the {error.validator_value} itself, not a string"
            except ValueError as exc:
                detail += (
                    f" (text that is not valid JSON: {exc.msg} at character {exc.pos} of {len(error.instance)}). "
                    f"Send the {error.validator_value} itself, not a string, and close each {{ with }} and each [ with ]"
                )
            else:
                detail += f" (text that decodes to {_JSON_TYPES.get(type(decoded), 'unknown')})"
        return detail
    if error.validator == "const":
        return "must be exactly " + json.dumps(error.validator_value)
    if error.validator == "enum":
        return "must be one of: " + ", ".join(map(str, error.validator_value))
    if error.validator == "additionalProperties" and isinstance(error.instance, dict):
        declared = error.schema.get("properties", {})
        level = "answer" if "takeaway" in declared else "section" if "evidence_refs" in declared else (
            "presentation" if "kind" in declared else "")
        homes = _FIELD_HOMES.get(level, {})
        extras = [
            f"{key} ({homes.get(key, 'is not a declared field: remove it')})"
            if isinstance(key, str) and _FIELD_NAME.fullmatch(key) else "an unnamed field (remove it)"
            for key in error.instance if key not in declared
        ]
        return "undeclared field(s): " + "; ".join(extras) + ". Use only declared fields: " + ", ".join(declared)
    named = _required_names(error.validator_value)
    if error.validator == "oneOf" and named and isinstance(error.instance, dict):
        sent = [key for key in named if key in error.instance]
        return ("send only one of: " if sent else "send exactly one of: ") + ", ".join(named)
    if error.validator == "not" and named and isinstance(error.instance, dict):
        sent = [key for key in named if key in error.instance]
        return "remove field(s) this kind does not use: " + ", ".join(sent)
    if error.validator == "pattern":
        return "must contain text, not only spaces" if error.validator_value == r"\S" else "has the wrong format"
    if error.validator in _LIMIT_PROBLEMS:
        return _LIMIT_PROBLEMS[error.validator].format(n=error.validator_value)
    return "check the declared field schema"


_LIMIT_PROBLEMS = {
    "minLength": "must have at least {n} character(s)",
    "maxLength": "must have at most {n} characters",
    "minItems": "needs at least {n} item(s)",
    "maxItems": "allows at most {n} items",
    "uniqueItems": "must not repeat an item",
}


def _required_names(clause):
    """The field names a `required` clause, or alternatives of them, names; else ()."""
    alternatives = clause if isinstance(clause, list) else clause.get("anyOf") if isinstance(clause, dict) else None
    if isinstance(clause, dict) and "required" in clause:
        alternatives = [clause]
    if not isinstance(alternatives, list):
        return ()
    names = []
    for option in alternatives:
        if not isinstance(option, dict) or set(option) != {"required"}:
            return ()
        names.extend(key for key in option["required"] if key not in names)
    return tuple(names)


def proposal_to_internal(raw):
    """Project the new proposal into internal evidence/selection bookkeeping only.

    The agent and API accept only the new contract; the existing query verification
    and repair machinery can continue using its private table/evidence structures.
    """
    from jsonschema import Draft202012Validator

    raw = normalize_answer_arguments(raw)
    error = next(Draft202012Validator(restrict_schema(PROPOSAL_SCHEMA)).iter_errors(raw), None)
    if error:
        location = ".".join(map(str, error.absolute_path)) or "root"
        raise PresentationError(f"Invalid answer proposal at {location}: {_schema_problem(error)}.")
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
                    "Every visual must cite its exact result_id in section evidence_refs: section "
                    f"{json.dumps(section['id'])} shows result_id {json.dumps(rid)} but its evidence_refs "
                    f"are {json.dumps(section['evidence_refs'])}. Copy the one exact result ID of the "
                    "result this section shows into both."
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
            "if it is in an assumption, send the corrected full list as finalizeAnswer.assumptions "
            "(or recordAssumptions), or [] to clear it. Reuse existing results and preserve requested reporting dates."
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


_VISUAL_NAMES = {
    "figure": "figure", "facts": "summary", "bars": "bar chart", "line": "line chart",
    "multi_series_line": "line chart", "grouped_bars": "grouped bar chart",
    "stacked_bars": "stacked bar chart", "heatmap": "heatmap", "donut": "donut chart",
}


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
    fallbacks=None,
):
    """Materialize a candidate for judge review. trail MUST already be scope-filtered.

    `fallbacks`, when given, collects each visual shown as a table with the backend's
    reason. The reason is model-facing, so it is for review only and never shipped.
    """
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
        unusable = [ref for ref in proposed["evidence_refs"] if ref not in entries]
        if unusable:
            raise PresentationError(
                "Section evidence must reference successful accessible current-turn receipts: "
                f"section {json.dumps(proposed['id'])} cites {json.dumps(unusable)}, which are not "
                "successful data or document results from this turn (for example a refusal receipt "
                "or a failed call). Remove them; a section that states a refusal or limitation uses "
                "evidence_refs: []."
            )
        section = {
            "id": proposed["id"],
            "evidence_refs": list(proposed["evidence_refs"]),
            "caveats": [text(v) for v in proposed["caveats"]],
            "actions": [],
        }
        for key in ("title", "explanation", "table_reason"):
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
        if kind not in CURRENT_COMPONENTS.get():
            raise PresentationError("This presentation is disabled; select an enabled component.")
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
            # Says what to change: "needs its executed SQL" read as a request to show SQL.
            raise PresentationError(
                f"Result {json.dumps(rid)} cannot be displayed: the runtime has no saved query for it. "
                "This is not a request for SQL; never put SQL in the answer. Present a result from a "
                "query or verified blueprint run in this turn, or run the query again."
            )
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
                # The error text instructs the model ("or use a table"), so the user sees
                # a plain notice and review gets the reason separately.
                fallback = (f"The requested {_VISUAL_NAMES.get(kind, 'chart')} couldn't be drawn "
                            "from this result, so it is shown as a table.")
                section["explanation"] = " ".join(
                    value for value in (section.get("explanation"), fallback) if value
                )
                if fallbacks is not None:
                    fallbacks.append({"section_id": section["id"], "requested_kind": kind, "reason": str(exc)})
                rendered = {"kind": "table", "result_id": rid}
        if rendered["kind"] in {"line", "multi_series_line"}:
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
    from data_agent.runtime.composite.assumption_text import clean_caveats, clean_document_caveats, displayed_qualification_keys
    from data_agent.runtime.composite.record_assumptions import clean_assumptions
    assumptions = clean_assumptions([text(a) for a in (assumptions or ())])
    takeaway = text(args.get("answer", args.get("takeaway", "")))
    seen = displayed_qualification_keys({"takeaway": takeaway, "sections": sections})
    for section in sections:
        section["caveats"] = clean_caveats(section["caveats"], seen=seen)
    remaining = clean_caveats(assumptions, seen=seen)
    if remaining:
        if sections:
            sections[0]["caveats"].extend(remaining)
        else:
            sections.append({
                "id": "scope", "evidence_refs": [],
                "presentation": {"kind": "text", "markdown": "\n\n".join(remaining)},
                "caveats": [], "actions": [],
            })
    clean_document_caveats({"takeaway": takeaway, "sections": sections})
    return {
        "takeaway": takeaway,
        "sections": sections,
        "sources": sources,
    }


def _visual(p, columns, rows, *, business_values=frozenset(), column_formats=None):
    """Validate selectors and emit instructions; the UI fetches and renders values."""
    formats = column_formats or {}
    kind = p["kind"]
    result = {"kind": kind, "result_id": p["result_id"]}
    label = _text(p.get("label", ""), business_values)
    if kind not in CURRENT_COMPONENTS.get():
        raise PresentationError("This presentation is disabled.")
    if kind in {'multi_series_line', 'grouped_bars', 'stacked_bars', 'heatmap', 'donut'}:
        from .charts import chart_selection
        return chart_selection(p, columns, formats, label)
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
    from data_agent.runtime.composite.assumption_text import clean_document_caveats

    candidate = deepcopy(candidate)
    clean_document_caveats(candidate)
    return {
        "schema_version": 2,
        "answer_id": answer_id,
        "revision": revision,
        "generated_at": generated_at,
        **candidate,
    }


def text_document(text):
    """Runtime limitation/conversation with no automatically selected data."""
    return {"takeaway": text if text and text.strip() else "I could not produce a verified answer from the available evidence.", "sections": [], "sources": {}}
