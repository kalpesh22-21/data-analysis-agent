"""Validated v2 chart selectors; no result aggregation or embedded preview values."""


def chart_selection(p, columns, formats, label):
    from . import PresentationError, _index

    kind = p["kind"]
    if not label:
        raise PresentationError("A chart needs a meaningful measure label.")
    x = p.get("category_column")
    _index(columns, x)
    out = {"kind": kind, "result_id": p["result_id"], "x_column": x, "label": label}

    def measure(name):
        _index(columns, name)
        fmt = formats.get(name, {})
        if name == x or fmt.get("kind") in {"text", "identifier", "date", "datetime", "boolean"}:
            raise PresentationError("Select distinct categories and quantitative measures.")
        return fmt

    if kind in {"multi_series_line", "grouped_bars", "stacked_bars"}:
        series = p.get("series", [])
        if not 2 <= len(series) <= 4:
            raise PresentationError("Select two to four measures from the same wide result.")
        names = [s["measure_column"] for s in series]
        labels = [s["label"] for s in series]
        if len(set(names)) != len(names) or len(set(labels)) != len(labels):
            raise PresentationError("Series columns and labels must be unique.")
        signatures = {
            tuple(measure(n).get(k) for k in ("kind", "currency", "scale", "unit")) for n in names
        }
        if len(signatures) != 1:
            raise PresentationError("Series require compatible formats on one axis.")
        if kind == "stacked_bars":
            if p.get("additive") is not True or any(
                formats.get(n, {}).get("kind") == "percent" for n in names
            ):
                raise PresentationError(
                    "Stacks require disjoint additive amounts, not rates or percentages."
                )
            out["additive"] = True
        out["series"] = [{"y_column": s["measure_column"], "label": s["label"]} for s in series]
    else:
        name = p.get("measure_column")
        fmt = measure(name)
        if kind == "heatmap":
            y = p.get("series_column")
            _index(columns, y)
            if len({x, y, name}) != 3:
                raise PresentationError("Heatmaps require two distinct dimensions and one measure.")
            out.update(y_column=y, value_column=name)
        else:
            if p.get("composition") is not True or fmt.get("kind") == "percent":
                raise PresentationError(
                    "Donuts require exhaustive disjoint additive amounts, not rates."
                )
            out.update(y_column=name, composition=True)
    return out
