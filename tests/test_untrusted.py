"""The untrusted-JSON coercers, tested as the BUG CLASS they exist to close.

`data_agent/untrusted.py` replaced four divergent private copies (`prior_art
._str_list`, `judge/prompt._str_list`, `priorart/neo4j_index._str/_float
/_bool_or_none`, `audit/judgement._float`). The copies did not differ because the
situations differed — they differed because each was written from scratch against the
one failure its author had just seen. So these cases are named after the failure, not
after the type: a reader adding a fifth call site should be able to see which
fabrications are already ruled out.

Every function is TOTAL: it runs inside a queue worker or a prompt renderer, where an
exception is a lost job or a dead turn, so the neutral value is always the answer.
"""

from __future__ import annotations

import math

import pytest

from data_agent.untrusted import as_bool_or_none, as_float, as_str, as_str_list

# --- as_str ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("text", "text"),
        ("", ""),
        (None, ""),
        (0, ""),
        (["a"], ""),
        ({"a": 1}, ""),
        (True, ""),
    ],
    ids=["str", "empty_str", "none", "int", "list", "dict", "bool"],
)
def test_as_str_type_gates_and_never_stringifies(raw: object, expected: str) -> None:
    """`str(None)` is `"None"` and `str(["a"])` is `"['a']"` — both read as real content
    in a prompt or a join. The type gate IS the operation."""
    assert as_str(raw) == expected


# --- as_bool_or_none ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, True), (False, False), (None, None), ("true", None), (1, None), (0, None)],
    ids=["true", "false", "none", "string_true", "one", "zero"],
)
def test_as_bool_or_none_is_tri_state_not_truthiness(raw: object, expected: bool | None) -> None:
    """ "The record does not say" is a different fact from `False` and must stay
    distinguishable from it — including for the `"true"` a foreign writer leaves."""
    assert as_bool_or_none(raw) is expected


# --- as_float ----------------------------------------------------------------


def test_as_float_rejects_bool_because_true_would_rank_as_one() -> None:
    """`isinstance(True, int)` is True: unguarded, a stored `true` is a perfect false
    positive on every threshold compare."""
    assert as_float(True) == 0.0
    assert as_float(False) == 0.0


@pytest.mark.parametrize(
    "raw", ["0.9", None, [0.9], {"v": 0.9}], ids=["str", "none", "list", "dict"]
)
def test_as_float_rejects_non_numeric(raw: object) -> None:
    assert as_float(raw) == 0.0


def test_as_float_survives_an_int_too_large_to_be_a_float() -> None:
    """`float(10**400)` raises `OverflowError` from inside what reads as a total
    function — it escaped a renderer and killed a turn before the model was called."""
    assert as_float(10**400) == 0.0
    assert as_float(10**400, lo=0.0, hi=1.0) == 0.0


def test_as_float_passes_a_usable_number_through() -> None:
    assert as_float(0.5) == 0.5
    assert as_float(3) == 3.0
    assert as_float(0.5, lo=0.0, hi=1.0) == 0.5


@pytest.mark.parametrize(
    "raw", [1.5, -0.1, float("inf"), float("-inf")], ids=["above", "below", "inf", "-inf"]
)
def test_bounded_as_float_rejects_out_of_range(raw: float) -> None:
    """A cosine outside `[0, 1]` is a BROKEN signal, not a weak one: `inf` renders as
    `match=inf` and sorts above every genuine hit."""
    assert as_float(raw, lo=0.0, hi=1.0) == 0.0


def test_bounded_as_float_rejects_nan_without_an_isnan_test() -> None:
    """NaN fails both comparisons, which is exactly why the range check is written as
    two `not (…)` tests rather than a chained clamp."""
    assert as_float(float("nan"), lo=0.0, hi=1.0) == 0.0


def test_unbounded_as_float_reports_what_was_stored() -> None:
    """The forensic read (`audit/judgement.py`): an audit row records what happened, so
    an out-of-range stored value must come back out of range rather than be replaced by
    a plausible in-range 0.0."""
    assert as_float(1.5) == 1.5
    assert as_float(float("inf")) == float("inf")
    assert math.isnan(as_float(float("nan")))


# --- as_str_list -------------------------------------------------------------


def test_as_str_list_rejects_a_bare_string_instead_of_exploding_it() -> None:
    """THE case. `list("abc")` is `['a','b','c']` and `", ".join("abc")` is `"a, b, c"`:
    a bare string where a list was expected FABRICATES three rule ids no registry has
    heard of, and raises nothing."""
    assert as_str_list("abc") == []


@pytest.mark.parametrize("raw", [None, 5, {"a": 1}, {"a", "b"}], ids=["none", "int", "dict", "set"])
def test_as_str_list_rejects_every_non_sequence_container(raw: object) -> None:
    assert as_str_list(raw) == []


def test_as_str_list_skips_unusable_members_rather_than_dropping_the_list() -> None:
    """Container vs member, on purpose: skipping a member cannot fabricate anything, and
    one bad member must not hide every good one."""
    assert as_str_list(["a", None, 3, ["nested"], "b"]) == ["a", "b"]


def test_as_str_list_accepts_a_tuple() -> None:
    assert as_str_list(("a", "b")) == ["a", "b"]


def test_max_items_caps_the_container_before_filtering() -> None:
    """The bound is on how much untrusted input is EXAMINED, not on how much survives —
    so a list padded with 10k nulls cannot make a renderer walk them all."""
    assert as_str_list([None, None, "a", "b"], max_items=3) == ["a"]


def test_max_chars_truncates_each_surviving_member() -> None:
    assert as_str_list(["abcdef", "gh"], max_chars=3) == ["abc", "gh"]


def test_unbounded_is_the_default() -> None:
    assert as_str_list([str(i) for i in range(50)])[-1] == "49"
