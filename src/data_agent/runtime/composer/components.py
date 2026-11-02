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


def component_guidance():
    available = CURRENT_COMPONENTS.get()
    notes = ["Enabled presentations: " + ", ".join(available) + ". Select only these kinds."]
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
            "Stacked bars require additive=true: measures must be disjoint additive components of the same total, never rates, overlapping counts or a total plus its parts."
        )
    if "donut" in available:
        notes.append(
            "Donut requires composition=true, category_column and measure_column: exhaustive mutually exclusive parts of the stated whole, nonnegative additive values. Never use a truncated top-N or rates as shares; Other must be computed in SQL."
        )
    if "heatmap" in available:
        notes.append(
            "Heatmap selects category_column, series_column and measure_column from one result; one row per category pair, no client aggregation."
        )
    return " ".join(notes)
