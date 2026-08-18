"""Shape readers for untrusted structured-output fields: checks that carry their own message.

A reader per OPERATION rather than a message per FIELD — the message is a BY-PRODUCT of the
check (every reader takes `requirement` as a mandatory keyword), so there is no registry to
forget to update and no silent fallback to an interpreter message that names no field.
Readers are named for what happens downstream: `as_object` is "we are about to `.get()`
this", `as_array` is "we are about to iterate this and the element type matters".

A `ShapeError` message becomes prompt text on the corrective turn and is recorded on the
final `Decline`, so it must be ENTITY-FREE (the path, the required shape and the JSON TYPE
that arrived — never the value) and SELF-CONTAINED (a full dotted path from the candidate
root, actionable to a reader who cannot see this file).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

__all__ = [
    "Reader",
    "ShapeError",
    "as_array",
    "as_flag",
    "as_int",
    "as_number",
    "as_object",
    "as_text",
    "json_type",
    "one_of",
    "optional",
    "require",
]


def json_type(value: Any) -> str:
    """The JSON type of *value* as a noun phrase, for a message. NEVER the value.

    `bool` is checked before `int` because it IS one in Python, and "a boolean arrived where a
    number was required" is the more useful sentence of the two.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int | float):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, Mapping):
        return "an object"
    if isinstance(value, list | tuple):
        return "an array"
    # Not a JSON type at all (bytes from a hand-fed payload, a datetime from a replay).
    # The PYTHON type name is safe to name — it describes the container, not the value.
    return f"a {type(value).__name__}"


class ShapeError(Exception):
    """One untrusted field whose shape the read beneath it cannot use.

    The message is assembled HERE, from the arguments of the check that raised, which makes
    "every rejection explains itself" a property of the type rather than a convention. `absent`
    and "wrong type" are different sentences on purpose.
    """

    def __init__(
        self, path: str, requirement: str, arrived: Any = None, *, absent: bool = False
    ) -> None:
        self.path = path
        self.requirement = requirement
        self.arrived = "nothing" if absent else json_type(arrived)
        if absent:
            message = f"{path} is required and was absent or null: it must be {requirement}"
        else:
            message = f"{path} must be {requirement}, but {self.arrived} arrived"
        super().__init__(message)


# A reader: `(value, *, at, requirement) -> value`, raising `ShapeError` on a shape the
# downstream operation cannot use. `require`/`optional` compose one with a key lookup.
Reader = Callable[..., Any]


def as_object(value: Any, *, at: str, requirement: str) -> dict[str, Any]:
    """We are about to read KEYS off this."""
    if not isinstance(value, dict):
        raise ShapeError(at, requirement, value)
    return value


def as_array(value: Any, *, at: str, requirement: str) -> list[Any]:
    """We are about to ITERATE this and the element type matters.

    A `str` is the reason this cannot be a duck-typed `Iterable` check: iterating one yields
    characters, so `enum_values: "NA,EU"` would become seven single-character values with no
    error anywhere. A tuple cannot arrive from JSON, and accepting one would let a hand-fed
    payload take a path model output never can.
    """
    if not isinstance(value, list):
        raise ShapeError(at, requirement, value)
    return value


def as_text(value: Any, *, at: str, requirement: str) -> str:
    """We are about to store this as a string.

    A NUMBER is coerced, because `str(2025)` is exactly as usable as `"2025"`. A CONTAINER is
    not: its `str()` is a Python repr, which is not a rejection but a corruption — it lands in
    the corpus as an intent or a `binds_to` nobody can parse. Nor is a BOOL: `str(True)` is
    never a value a column carries.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ShapeError(at, requirement, value)
    return str(value)


def as_flag(value: Any, *, at: str, requirement: str) -> bool:
    """This decides a branch, so only a real boolean will do.

    `bool(...)` would accept the string `"false"` and read it as TRUE — and the fields that go
    through here (`required`, `verifiable`, `contains_entities`) are exactly the ones where
    inverting the answer is silent.
    """
    if not isinstance(value, bool):
        raise ShapeError(at, requirement, value)
    return value


def as_number(value: Any, *, at: str, requirement: str) -> float:
    """We are about to compare/rank on this as a float (`CandidateHeader.confidence`).

    A numeric STRING is accepted — `float("0.9")` is the same number — but a bool is not:
    `float(True)` is 1.0, a maximum-confidence claim conjured out of a type confusion.
    """
    if isinstance(value, bool):
        raise ShapeError(at, requirement, value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            raise ShapeError(at, requirement, value) from None
    raise ShapeError(at, requirement, value)


def as_int(value: Any, *, at: str, requirement: str) -> int:
    """We are about to use this as an index into the session (`evidence.turn_ref`).

    Mirrors `validation.py::_node_index`: a numeric string is accepted (real models emit `"0"`),
    a bool is not (`True` would silently become turn 1), and a fractional float is a typo rather
    than something to truncate.
    """
    if isinstance(value, bool):
        raise ShapeError(at, requirement, value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ShapeError(at, requirement, value)
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            raise ShapeError(at, requirement, value) from None
    raise ShapeError(at, requirement, value)


def one_of(allowed: tuple[str, ...]) -> Reader:
    """A reader for a CLOSED set; returns a reader so it composes with `require`/`optional`.

    The allowed values are appended by the caller's `requirement` rather than injected here: a
    bare list of tokens is rarely the whole story, and a message assembled from two halves in
    two places is the kind nobody rereads.
    """

    def _read(value: Any, *, at: str, requirement: str) -> str:
        if not isinstance(value, str) or value not in allowed:
            raise ShapeError(at, requirement, value)
        return value

    return _read


def require(
    container: Mapping[str, Any], key: str, reader: Reader, *, at: str, requirement: str
) -> Any:
    """Read a MANDATORY key through *reader*. Absent or null ⇒ `ShapeError`.

    Null is treated as absent because every mandatory field here is one the caller goes on to
    USE, and `None` is not usable for any of them.
    """
    value = container.get(key)
    if value is None:
        raise ShapeError(f"{at}.{key}", requirement, None, absent=True)
    return reader(value, at=f"{at}.{key}", requirement=requirement)


def optional(
    container: Mapping[str, Any],
    key: str,
    reader: Reader,
    *,
    at: str,
    requirement: str,
    default: Any,
) -> Any:
    """Read an OPTIONAL key through *reader*, or return *default* when it is absent.

    ABSENT and NULL mean "not stated" and take the default; every other wrong type declines.
    `container.get(key) or default` would normalize `0`, `""` and `{}` into "not stated",
    throwing away the single most useful fact in a decline message — that the model sent a
    string where a list belongs.
    """
    value = container.get(key)
    if value is None:
        return default
    return reader(value, at=f"{at}.{key}", requirement=requirement)
