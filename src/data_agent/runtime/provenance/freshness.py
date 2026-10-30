"""Validate API-owned source receipts and combine blueprint execution receipts."""

from copy import deepcopy

from .capture import capture_data_as_of


def timestamp(value):
    return capture_data_as_of("runQuery", {"data_as_of": value})


def capture_freshness(result):
    raw = result.get("freshness")
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("mode"), str)
        or raw.get("mode") not in {"live", "ingested", "mixed", "unknown"}
    ):
        return None
    sources = raw.get("sources")
    if not isinstance(sources, list):
        return None
    clean = []
    names = set()
    for source in sources:
        if not isinstance(source, dict):
            return None
        name, kind = source.get("table"), source.get("kind")
        if (
            not isinstance(name, str)
            or not name.strip()
            or name in names
            or not isinstance(kind, str)
            or kind not in {"live", "ingested", "unknown"}
        ):
            return None
        names.add(name)
        item = {"table": name, "kind": kind}
        if "data_as_of" in source:
            date = timestamp(source["data_as_of"])
            if kind != "ingested" or date is None:
                return None
            item["data_as_of"] = date
        clean.append(item)
    known = bool(clean) and all(s["kind"] == "live" or "data_as_of" in s for s in clean)
    kinds = {s["kind"] for s in clean}
    expected = ("mixed" if len(kinds) > 1 else next(iter(kinds))) if known else "unknown"
    # Unknown may include unresolved source expressions alongside known tables.
    if raw["mode"] != "unknown" and raw["mode"] != expected:
        return None
    return {"mode": raw["mode"], "sources": clean}


def ingestion_date(freshness):
    if not freshness or freshness["mode"] == "unknown":
        return None
    dates = [s["data_as_of"] for s in freshness["sources"] if s["kind"] == "ingested"]
    return min(dates) if dates else None


def merge_freshness(receipts):
    """Any missing execution receipt keeps the aggregate unknown; live reads do not age it."""
    sources = {}
    unknown = not receipts
    for receipt in receipts:
        if not receipt or receipt["mode"] == "unknown":
            unknown = True
        for item in (receipt or {}).get("sources", []):
            name = item["table"]
            previous = sources.get(name)
            if previous is None:
                sources[name] = deepcopy(item)
            elif previous["kind"] != item["kind"]:
                sources[name] = {"table": name, "kind": "unknown"}
                unknown = True
            elif item["kind"] == "ingested":
                if "data_as_of" in previous and "data_as_of" in item:
                    previous["data_as_of"] = min(previous["data_as_of"], item["data_as_of"])
                else:
                    previous.pop("data_as_of", None)
                    unknown = True
    kinds = {s["kind"] for s in sources.values()}
    mode = "unknown" if unknown or not kinds else "mixed" if len(kinds) > 1 else next(iter(kinds))
    return {"mode": mode, "sources": [sources[k] for k in sorted(sources)]}
