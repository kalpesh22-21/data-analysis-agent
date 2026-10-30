"""Display metadata validation and formatting. Raw values are never modified."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

KINDS = {"text", "identifier", "integer", "number", "currency", "percent", "date", "datetime", "boolean"}


def validate_formats(raw):
    if not isinstance(raw, dict):
        raise ValueError("column_formats must be an object")
    for name, fmt in raw.items():
        if not isinstance(name, str) or not name or not isinstance(fmt, dict):
            raise ValueError("format entries require a column name and object")
        if not isinstance(fmt.get("kind"), str) or fmt["kind"] not in KINDS or set(fmt) - {"kind", "currency", "scale", "unit", "precision"}:
            raise ValueError(f"Invalid format for {name}")
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


def display_value(value, fmt):
    if value is None:
        return "Not available"
    kind = fmt.get("kind")
    if kind in {"text", "identifier", "date", "datetime"}:
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    if kind == "boolean":
        return "Yes" if value is True or value == 1 else "No" if value is False or value == 0 else str(value)
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return "Not available"
        if kind == "percent" and fmt.get("scale") == "ratio":
            number *= 100
        if kind == "number" and "precision" not in fmt:
            rendered = format(number, ",f")
            if "." in rendered:
                rendered = rendered.rstrip("0").rstrip(".")
            return rendered + (" " + fmt["unit"] if fmt.get("unit") else "")
        precision = fmt.get("precision", 0 if kind == "integer" else 2)
        # Integer display never silently rounds a fractional result.
        if kind == "integer" and number != number.to_integral_value():
            return format(number, ",f").rstrip("0").rstrip(".")
        number = number.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)
        rendered = format(number, f",.{precision}f")
        if kind == "number" and "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        if kind == "currency":
            rendered = ("$" if fmt["currency"] == "USD" else fmt["currency"] + " ") + rendered
        elif kind == "percent":
            rendered += "%"
        if fmt.get("unit"):
            rendered += " " + fmt["unit"]
        return rendered
    except (InvalidOperation, ValueError, TypeError):
        return str(value)
