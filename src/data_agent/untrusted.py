"""Coercers for values that arrived as UNTRUSTED JSON.

Both planes read structures they did not construct: rehydrated store documents,
Neo4j node properties, model output, catalog exports. Every one of those readers had
grown its own `_str`/`_float`/`_str_list`, and the copies had diverged — which is the
whole problem, because the reason these exist is a bug CLASS, not a bug:

  * **char explosion.** `list("abc")` is `['a','b','c']` and `", ".join("abc")` is
    `"a, b, c"`. A bare string where a list was expected therefore FABRICATES entries —
    three rule ids no registry has heard of — and raises nothing. A container gate that
    accepts `str` (because `str` is iterable) is the same bug spelled optimistically.
  * **`str()` coercion.** `str(None)` is `"None"` and `str([1])` is `"[1]"`. A coercer
    that stringifies turns an ABSENT value into content that reads as real in a prompt,
    a join or a comparison. The type gate is the operation; `str()` is not.
  * **`bool` is an `int`.** `isinstance(True, int)` is `True`, so a stored `true`
    silently ranks as `1.0` — a perfect false positive on any threshold compare.
  * **numbers that are not usable numbers.** `json.loads` happily produces `NaN` and
    `Infinity`; `float(10**400)` raises `OverflowError` from inside what looks like a
    total function. `inf` outranks every genuine hit; `NaN` fails every comparison.

Each function is TOTAL over any input type and returns the neutral value rather than
raising: these run inside queue workers and prompt renderers where an exception is a
lost job or a dead turn, and the neutral value ("", 0.0, [], None) is in every case the
one that cannot fabricate a fact.

Stdlib only, and deliberately at the package root: the runtime plane, the learning
plane and the standalone scripts all read untrusted JSON, and a shared guard that lives
under one of them is a guard the others will re-type instead of importing.

WHAT DOES NOT BELONG HERE: prompt armoring. `extractor/prior_art.py::_sanitize` flattens
Unicode line/format/surrogate categories and collapses `=` runs so corpus text cannot
forge the block's delimiter. That is a property of THE RENDERED BLOCK, not of the value,
and it stays with the renderer that owns the delimiter. `as_str_list` gives that
renderer the container gate; the renderer supplies its own per-item armor.
"""

from __future__ import annotations

from typing import Any

__all__ = ["as_bool_or_none", "as_float", "as_str", "as_str_list"]


def as_str(raw: Any) -> str:
    """*raw* when it is a `str`, else `""`.

    NOT `str(raw)`: coercing turns a null into the literal `"None"` and a list into its
    repr, both of which then flow into joins, comparisons and (eventually) prompts
    looking like real content.
    """
    return raw if isinstance(raw, str) else ""


def as_bool_or_none(raw: Any) -> bool | None:
    """*raw* when it is a real `bool`, else `None` — a TRI-STATE, not a truthiness test.

    Only a stored boolean is a verdict. Anything else, including the string `"true"` a
    foreign writer might leave behind, means "the record does not say", which is a
    different fact from `False` and must stay distinguishable from it.
    """
    return raw if isinstance(raw, bool) else None


def as_float(raw: Any, *, lo: float | None = None, hi: float | None = None) -> float:
    """*raw* as a usable `float`, else `0.0`.

    Rejected: `bool` (an `int` subclass — a stored `true` reading as `1.0` is a perfect
    false positive), any non-numeric type (every consumer `>=`-compares this, which
    raises on a `str`/`None`), and an `int` too large to convert (`float(10**400)`
    raises `OverflowError` from inside what reads as a total function).

    *lo*/*hi* make the RANGE part of the type. Pass them when the value has a real
    domain — a cosine similarity is `[0.0, 1.0]`, so `inf` is not a strong signal but a
    broken one, and it would sort and READ as better than every genuine hit. Out of
    range degrades to `0.0` (the bottom), and because `NaN` fails both comparisons the
    same test rejects it with no separate `isnan`.

    Leave them None when the value is FORENSIC rather than operational: an audit row
    records what happened, and replacing a stored out-of-range number with a plausible
    in-range `0.0` would falsify the record instead of reporting it. Unbounded therefore
    also passes `NaN`/`inf` through — that is the point: the reader wanted the stored
    number, whatever it was.
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

    CONTAINER vs MEMBER, treated differently on purpose:

      * a bare `str` — or any non-`list`/`tuple` — is the WHOLE value rejected, never
        iterated. `list("abc")` manufactures three plausible-looking entries and raises
        nothing; that is the char-explosion class, and it is the reason this function
        exists rather than a comprehension at each call site.
      * a member that is not a `str` is SKIPPED, not fatal. Skipping cannot fabricate
        anything, and one bad member should not hide every good one.

    *max_items* caps the container BEFORE filtering, so the bound is on how much
    untrusted input is examined, not on how much survives. *max_chars* truncates each
    surviving member. Both default to unbounded; a caller rendering into a token budget
    or a fixed-width line passes them.
    """
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        return []
    items = raw if max_items is None else raw[:max_items]
    out = [item for item in items if isinstance(item, str)]
    if max_chars is not None:
        out = [item[:max_chars] for item in out]
    return out
