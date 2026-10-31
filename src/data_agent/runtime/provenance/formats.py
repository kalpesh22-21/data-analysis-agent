"""Display metadata validation. Raw values are never modified."""
import re
from copy import deepcopy

KINDS = {"text", "identifier", "integer", "number", "currency", "percent", "date", "datetime", "boolean"}


def validate_formats(raw):
    if not isinstance(raw, dict):
        raise ValueError("column_formats must be an object")
    for name, fmt in raw.items():
        if not isinstance(name, str) or not name or not isinstance(fmt, dict):
            raise ValueError("format entries require a column name and object")
        if not isinstance(fmt.get("kind"), str) or fmt["kind"] not in KINDS or set(fmt) - {"kind", "currency", "scale", "unit", "precision", "grain"}:
            raise ValueError(f"Invalid format for {name}")
        if "grain" in fmt and (fmt["kind"] not in {"date", "datetime"} or fmt["grain"] not in ("year", "quarter", "month", "day", "timestamp")):
            raise ValueError(f"Invalid date grain for {name}")
        if "currency" in fmt and (fmt["kind"] != "currency" or not re.fullmatch(r"[A-Z]{3}", str(fmt["currency"]))):
            raise ValueError(f"Invalid currency for {name}")
        if fmt["kind"] == "currency" and "currency" not in fmt:
            raise ValueError(f"Currency required for {name}")
        if fmt["kind"] == "percent" and fmt.get("scale") not in ("ratio", "percent"):
            raise ValueError(f"Percent scale required for {name}")
        if "scale" in fmt and fmt["kind"] != "percent":
            raise ValueError(f"Unexpected scale for {name}")
        if "precision" in fmt and (type(fmt["precision"]) is not int or not 0 <= fmt["precision"] <= 8):
            raise ValueError(f"Invalid precision for {name}")
        if "unit" in fmt and (not isinstance(fmt["unit"], str) or not 0 < len(fmt["unit"]) <= 40):
            raise ValueError(f"Invalid unit for {name}")
    return deepcopy(raw)


def capture_formats(raw, columns):
    try:
        return {k: v for k, v in validate_formats(raw or {}).items() if k in columns}
    except (ValueError, TypeError):
        return {}
