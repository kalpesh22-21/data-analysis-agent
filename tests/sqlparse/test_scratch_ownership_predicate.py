"""The D64 scratch-ownership rule as a PUBLIC predicate — `is_own_session_scratch_table`.

`_validate_scratch_name` is the one place the rule lives, and the extractor calls it
directly (it wants the raise and its specific message). A SECOND caller — the blueprint
executor, re-validating a materialized `scratch.…` table name that came back from a
persisted, therefore untrusted, pause checkpoint before binding it into a JOIN — wants a
branch, not an exception. `is_own_session_scratch_table` is that form, exported from the
`sqlparse` package so the two callers cannot drift apart.

These tests pin the predicate's own contract at the package boundary (the import path the
executor uses), including the properties the rule was TIGHTENED to guarantee: exact session
extraction rather than a prefix match, and fail-closed on a falsy session_id.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.sqlparse import is_own_session_scratch_table
from data_agent.sqlparse.provenance import ScratchSessionError, _validate_scratch_name

_SID = "s" + "a" * 32  # the runtime sid contract: identifier-safe, underscore-free


def test_the_owning_session_is_accepted() -> None:
    assert is_own_session_scratch_table(f"s_{_SID}_bp_{'0' * 32}", _SID)


def test_a_different_session_is_rejected() -> None:
    """The cross-session read D64 exists to stop."""
    assert not is_own_session_scratch_table(f"s_{'b' * 32}_bp_1", _SID)
    assert not is_own_session_scratch_table(f"s_{_SID}_bp_1", "s" + "b" * 32)


@pytest.mark.parametrize(
    "session_id",
    [pytest.param(None, id="none"), pytest.param("", id="empty-string")],
)
def test_a_falsy_session_id_fails_closed(session_id: str | None) -> None:
    """FAIL-CLOSED on a falsy sid (D64, auth-hardening Slice 1). Reaching this
    predicate means a `scratch.*` name is about to be trusted, and one whose owning
    session is unknown can never be proven to belong to the caller. The empty-string
    half matters on its own: an empty sid would otherwise match `s__<suffix>`
    (extracted session == "" == session_id) — an ownership hole."""
    assert not is_own_session_scratch_table(f"s_{_SID}_bp_1", session_id)
    assert not is_own_session_scratch_table("s__bp_1", session_id)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("", id="empty"),
        pytest.param("emp_earnings", id="a-declared-placeholder-not-a-session-table"),
        pytest.param(f"{_SID}_bp_1", id="no-leading-s_"),
        pytest.param(f"s_{_SID}", id="no-suffix"),
        pytest.param(f"s_{_SID}_", id="empty-suffix"),
        pytest.param(f"S_{_SID}_bp_1", id="wrong-case-prefix"),
    ],
)
def test_structurally_malformed_names_are_rejected(name: str) -> None:
    assert not is_own_session_scratch_table(name, _SID)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(None, id="none"),
        pytest.param(12345, id="int"),
        pytest.param(["s", _SID], id="list"),
        pytest.param({"table": f"s_{_SID}_bp_1"}, id="dict"),
        pytest.param(True, id="bool"),
    ],
)
def test_a_non_string_name_is_rejected_without_raising(name: Any) -> None:
    """The predicate's callers hand it values decoded from untrusted JSON, so a
    non-`str` must be a plain `False`, never a `TypeError` that escapes as a crash."""
    assert not is_own_session_scratch_table(name, _SID)


def test_exact_extraction_not_a_prefix_match() -> None:
    """The TIGHTENING this rule exists for. The old test was
    `startswith(f"s_{sid}_")`, which had a `_`-boundary ambiguity: `s_a_b_1` starts
    with BOTH `s_a_` and `s_a_b_`, so it matched a session bound to `a` AND one bound
    to `a_b`. Exact extraction — the run after `s_` up to the NEXT `_` — gives a
    validly-produced table exactly ONE claimable owner: `a`. The `a_b` session, which
    the loose prefix let in, is now rejected (the fail-closed direction; the paired
    discipline is that real sids are minted underscore-free, so no legitimate session
    lands here)."""
    assert is_own_session_scratch_table("s_a_b_1", "a")  # the ONE extractable owner
    assert not is_own_session_scratch_table("s_a_b_1", "a_b")  # the old prefix hole
    assert not is_own_session_scratch_table("s_a_b_1", "a_b_1")


def test_the_predicate_is_the_raising_validator_and_not_a_second_copy() -> None:
    """The point of exporting a wrapper: ONE rule, two shapes. Over a spread of names
    the predicate agrees with `_validate_scratch_name` case for case, so a future
    change to the rule cannot leave the executor's copy behind — there is no copy."""
    names = [
        f"s_{_SID}_bp_1",
        f"s_{'b' * 32}_bp_1",
        "s_a_b_1",
        f"s_{_SID}",
        "emp_earnings",
        "",
    ]
    for name in names:
        try:
            _validate_scratch_name(name, _SID)
        except ScratchSessionError:
            raises = True
        else:
            raises = False
        assert is_own_session_scratch_table(name, _SID) is (not raises), name
