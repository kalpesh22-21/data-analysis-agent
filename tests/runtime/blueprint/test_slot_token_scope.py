"""A `{name}` is a bind site ONLY outside a string literal and a comment.

⚠ THE REGRESSION THIS PINS IS A SILENT WRONG-ANSWER ONE, and it reached the mined path.

`_SLOT_TOKEN` is a raw-text regex. Substituting with it directly rewrote braces that were DATA:
an accepted, human-confirmed query doing `WHERE note LIKE '%{cfg}%'` became `'%:cfg%'`, and the
template kept that different constant. Nothing downstream could catch it — the D97 walk and the
parameterization entries are derived from the SAME corrupted parse, so they agree with each
other and the candidate stamps `completed`.

The tests are written against `parse_template` and `referenced_slots` because those are what the
corpus loader, the binder, the S4 rewrite and the minting node-check all go through.
"""

from __future__ import annotations

import pytest

from data_agent.runtime.blueprint.template import (
    TemplateBindError,
    bind_template,
    parse_template,
    referenced_slots,
)


def _rendered(sql: str) -> str:
    return parse_template(sql).sql(dialect="clickhouse")


def test_a_brace_inside_a_string_literal_is_data_not_a_bind_site() -> None:
    """The exact corruption: a LIKE pattern over text that happens to contain `{word}`."""
    sql = "SELECT name FROM db.t WHERE note LIKE '%{cfg}%' AND x > 3"

    assert "'%{cfg}%'" in _rendered(sql)
    assert ":cfg" not in _rendered(sql)
    assert referenced_slots(sql) == set()


def test_a_brace_inside_a_comment_is_not_a_bind_site() -> None:
    sql = "SELECT a FROM t -- needs {ghost}"

    assert referenced_slots(sql) == set()


def test_a_real_bind_site_beside_a_quoted_one_still_binds() -> None:
    """Both in one query, which is what makes this a scoping rule rather than an on/off switch."""
    sql = "SELECT a FROM t WHERE b = '{ghost}' AND c = {real}"

    assert referenced_slots(sql) == {"real"}
    bound = bind_template(sql, {"real": 7})
    assert "'{ghost}'" in bound
    assert "7" in bound


def test_a_genuine_clickhouse_map_literal_is_untouched() -> None:
    """`map('a',1)` and `{'k':'v'}` are values a query may legitimately compute. The token regex
    requires a bare identifier between the braces, so neither is mistaken for a slot — pinned
    because the whole reason `parse_template` exists is that `{name}` collides with map syntax."""
    sql = "SELECT map('a', 1) AS m FROM t WHERE d = {real}"

    assert referenced_slots(sql) == {"real"}
    assert "map('a', 1)" in _rendered(sql)


def test_an_unbound_quoted_token_is_not_reported_missing() -> None:
    """The binder must not demand a value for something that was never a bind site — otherwise
    a blueprint whose text mentions `{cfg}` becomes unbindable."""
    sql = "SELECT a FROM t WHERE note = '{cfg}'"

    assert bind_template(sql, {}) == sql


def test_a_binding_for_a_quoted_token_is_still_an_extra() -> None:
    """The other direction: passing a value for a name that only appears inside a string must
    stay an error, because the caller believes they are filling a hole that does not exist."""
    with pytest.raises(TemplateBindError):
        bind_template("SELECT a FROM t WHERE note = '{cfg}'", {"cfg": "x"})
