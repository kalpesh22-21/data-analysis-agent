"""Turn-scoped presentation availability; never mutate shared tool schemas."""

from contextvars import ContextVar
from copy import deepcopy
from functools import wraps

DEFAULT_COMPONENTS = ("text", "table", "figure", "facts", "bars", "line")
NEW_COMPONENTS = ("multi_series_line", "grouped_bars", "stacked_bars", "heatmap", "donut")
ALL_COMPONENTS = DEFAULT_COMPONENTS + NEW_COMPONENTS
CURRENT_COMPONENTS = ContextVar("answer_components", default=DEFAULT_COMPONENTS)


def validate_components(values):
    values = tuple(values)
    if len(set(values)) != len(values) or set(values) - set(ALL_COMPONENTS):
        raise ValueError("UI components must be unique supported component names.")
    if not {"text", "table"} <= set(values):
        raise ValueError(
            "UI components must include text and table for safe limitations and fallback."
        )
    return values


def presentation_boundary(method):
    @wraps(method)
    async def scoped(self, *args, **kwargs):
        token = CURRENT_COMPONENTS.set(self._allowed_ui_components)
        try:
            return await method(self, *args, **kwargs)
        finally:
            CURRENT_COMPONENTS.reset(token)

    return scoped


def restrict_schema(schema):
    """Project every nested presentation contract, including judge/recovery tools."""
    value = deepcopy(schema)

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            props = node.get("properties", {})
            if "kind" in props and "result_id" in props and "bars" in props["kind"].get("enum", []):
                allowed = CURRENT_COMPONENTS.get()
                props["kind"]["enum"] = [k for k in props["kind"]["enum"] if k in allowed]
                node["allOf"] = [
                    rule
                    for rule in node.get("allOf", [])
                    if set(
                        rule.get("if", {})
                        .get("properties", {})
                        .get("kind", {})
                        .get(
                            "enum",
                            [rule.get("if", {}).get("properties", {}).get("kind", {}).get("const")],
                        )
                    )
                    & set(allowed)
                ]
                if not set(allowed) & {"multi_series_line", "grouped_bars", "stacked_bars"}:
                    props.pop("series", None)
                if "heatmap" not in allowed:
                    props.pop("series_column", None)
                if "donut" not in allowed:
                    props.pop("composition", None)
                if "stacked_bars" not in allowed:
                    props.pop("additive", None)
            for item in node.values():
                walk(item)

    walk(value)
    return value


# The nearest enabled presentation when a blueprint suggests a disabled one. Text and
# table are always enabled (`validate_components`), so every chain ends somewhere usable.
_SUGGESTION_FALLBACK = {
    "figure": ("facts", "table"), "facts": ("table",), "line": ("bars", "table"), "bars": ("table",),
    # Donut is off by default; its parts compare just as well as bars on one axis.
    "donut": ("bars", "table"),
}


def _enabled_kind(kind, available):
    for candidate in (kind, *_SUGGESTION_FALLBACK.get(kind, ())):
        if candidate in available:
            return candidate
    return "table"


def suggested_presentation(hint):
    """A blueprint's reviewed presentation, restricted to this turn's enabled kinds.

    A disabled kind would steer the model into the composer's "presentation is disabled"
    refusal and cost a repair round, so it is replaced by the nearest enabled kind.
    Columns travel only with a chart they still describe; a table needs none.
    """
    if not isinstance(hint, dict) or not isinstance(hint.get("kind"), str):
        return None
    available = CURRENT_COMPONENTS.get()
    kind = _enabled_kind(hint["kind"], available)
    suggestion = {"kind": kind}
    if kind != "table":
        for key in ("category_columns", "measure_columns"):
            if hint.get(key):
                # Bars stay one category and one measure, including a line or donut
                # that fell back to bars; other kinds keep their alternatives.
                suggestion[key] = list(hint[key])[:1] if kind == "bars" else list(hint[key])
    overrides = {
        group: resolved
        for group, group_kind in (hint.get("by_group_by") or {}).items()
        if (resolved := _enabled_kind(group_kind, available)) != kind
    }
    if overrides:
        suggestion["by_group_by"] = overrides
    return suggestion


def component_guidance():
    available = CURRENT_COMPONENTS.get()
    notes = ["Enabled presentations: " + ", ".join(available) + ". Select only these kinds."]
    notes.append(
        "Honor an explicit presentation request when it is enabled and appropriate for the data. "
        "Otherwise choose an enabled alternative that preserves all requested information; "
        "use table when no enabled chart fits. Never drop measures, categories or requested detail, "
        "restrict the population, or change the metric just to fit a chart. "
        "Do not ask the user to choose a display format. Choose by analytical intent and result shape, "
        "not row count alone. Rendering limits are not SQL population limits."
    )
    notes.append(
        "A blueprint's suggested_presentation (from getBlueprint) is the reviewed view of its result: "
        "follow it, with any by_group_by entry for the group_by you bound, unless the user asks for "
        "another view or the actual result does not fit. Its measure_columns are alternatives; the "
        "first is the default."
    )
    selection_rules = {
        "text": "Use text for explanations, refusals or limitations without a verified data result.",
        "table": "Use table for detailed records, requested detail, requested measures in incompatible units, or results unsuitable for enabled charts.",
        "figure": "Use figure for one summary number.",
        "facts": "Use facts for several summary numbers from one row.",
        "bars": (
            "Use bars for one measure compared or ranked across categories, including a distribution on one "
            "category axis (categories, or ordered amount bands); prefer it when precise category comparison is "
            "the goal. Unrequested extra measures stay in the UI table/CSV and do not prevent bars. A distribution "
            "crossing two category dimensions or over about 25 categories reads better as a table."
        ),
        "line": "Use line for one measure trending over time.",
        "multi_series_line": "Use multi_series_line for two to four comparable measures trending over the same time periods.",
        "grouped_bars": "Use grouped_bars to compare two to four measures side by side within each category; additivity is not required.",
        "stacked_bars": "Use stacked_bars when the question concerns a total and its disjoint additive components within each category.",
        "heatmap": "Use heatmap to reveal patterns in one measure across two categorical dimensions, with one row per dimension pair.",
        "donut": "Use donut for a part-to-whole question with two to five complete, mutually exclusive categories; a small ranking alone is not composition.",
    }
    notes.extend(selection_rules[kind] for kind in available)
    if "bars" in available:
        notes.append(
            "Bars select category_column (or compound category_columns), measure_column and label. Ranking alone adds direction and ties; ordinary comparisons omit both."
        )
    if "line" in available:
        notes.append("Line selects a temporal category_column, measure_column and label.")
    if "figure" in available:
        notes.append("Figure selects one scalar column and label.")
    if "facts" in available:
        notes.append("Facts select columns from a one-row result.")
    if set(available) & {"multi_series_line", "grouped_bars", "stacked_bars"}:
        notes.append(
            "Multi-series charts select one wide result, category_column, label and series [{measure_column, label}], two to four measures. Never join or aggregate results in the UI. Use compatible units, currencies and percent scales."
        )
    if "stacked_bars" in available:
        notes.append(
            'Stacked bars require "additive": true (a JSON boolean, not a string): measures must be disjoint additive components of the same total, never rates, overlapping counts or a total plus its parts.'
        )
    if "donut" in available:
        notes.append(
            'Donut requires "composition": true (a JSON boolean, not a string), category_column and measure_column: exhaustive mutually exclusive parts of the stated whole, nonnegative additive values. Never use a truncated top-N or rates as shares; Other must be computed in SQL.'
        )
    if "heatmap" in available:
        notes.append(
            "Heatmap selects category_column, series_column and measure_column from one result; one row per category pair, no client aggregation."
        )
    return " ".join(notes)
