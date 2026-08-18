"""Coercers for values that arrived as UNTRUSTED JSON — rehydrated store documents, Neo4j
properties, model output, catalog exports.

Every function is TOTAL over any input type and returns the neutral value ("", 0.0, [],
None) rather than raising: these run inside queue workers and prompt renderers, where the
neutral value is the one that cannot fabricate a fact. Type GATES, never coercion —
`str(None)` is `"None"`, `list("abc")` is three entries, and `bool` is an `int`.
"""

from __future__ import annotations

from typing import Any

__all__ = ["as_bool_or_none", "as_float", "as_str", "as_str_list"]


def as_str(raw: Any) -> str:
    """*raw* when it is a `str`, else `""` — a type gate, never `str(raw)`, which turns a null into
    the literal `"None"` and a list into its repr.
    """
    return raw if isinstance(raw, str) else ""


def as_bool_or_none(raw: Any) -> bool | None:
    """*raw* when it is a real `bool`, else `None` — a TRI-STATE, not a truthiness test.

    `None` means "the record does not say", which is a different fact from `False`; the string
    `"true"` a foreign writer left behind is not a verdict.
    """
    return raw if isinstance(raw, bool) else None


def as_float(raw: Any, *, lo: float | None = None, hi: float | None = None) -> float:
    """*raw* as a usable `float`, else `0.0` — rejects `bool` (an `int` subclass), non-numeric types
    and ints too large to convert.

    *lo*/*hi* make the RANGE part of the type: out of range degrades to `0.0`, and because `NaN`
    fails both comparisons the same test rejects it. Leave them None for FORENSIC values (an
    audit row), where unbounded passes `NaN`/`inf` through rather than falsifying the record.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    try:
        value = float(raw)
    except (OverflowError, ValueError):  # an int too large to be a float
        return 0.0
    if lo is not None and not (value >= lo):  # NaN-rejecting on purpose
        return 0.0
    if hi is not None and not (value <= hi):
        return 0.0
    return value


def as_str_list(
    raw: Any, *, max_items: int | None = None, max_chars: int | None = None
) -> list[str]:
    """*raw* as a list of the `str` members it actually has, else `[]`.

    CONTAINER vs MEMBER: a bare `str` — or any non-`list`/`tuple` — is the WHOLE value rejected,
    never iterated (`list("abc")` manufactures three plausible entries and raises nothing); a
    non-`str` member is merely SKIPPED. *max_items* caps the container BEFORE filtering, so the
    bound is on how much untrusted input is examined, not on how much survives.
    """
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        return []
    items = raw if max_items is None else raw[:max_items]
    out = [item for item in items if isinstance(item, str)]
    if max_chars is not None:
        out = [item[:max_chars] for item in out]
    return out
