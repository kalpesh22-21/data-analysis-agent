"""Rebuilding the accepted SQL from a candidate — the inverse of the S4 rewrite.

The rewrite is information-preserving in one direction: a `role="slot"` literal becomes
`{name}` with its value kept on the entry's locator, while `inline` and `rule` literals stay in
the template verbatim. So the candidate carries everything the rewrite consumed, which is what
lets a `ValidationSnapshot` be rebuilt for candidates extracted before one was stamped.

⚠ The result is marked `reconstructed=True` because re-validating against it is CIRCULAR — see
the module docstring. These tests pin the mechanics; the honesty flag is pinned in
`tests/learning/candidate/` and the migration itself in the backfill script.
"""

from __future__ import annotations

import sqlglot

from data_agent.learning.generalize.reconstruct import reconstruct_accepted_sql


def _payload(template: str, entries: list[dict]) -> dict:
    return {"generalization": {"sql_template": template}, "parameterization": entries}


def _slot(column: str, value: str, name: str, slot_type: str = "entity") -> dict:
    return {
        "locator": {"table": "db.t", "column": column, "value": value},
        "role": "slot",
        "slot": {"name": name, "type": slot_type, "binds_to": f"db.t.{column}"},
    }


def test_a_slot_gets_its_literal_back_and_an_inline_was_never_removed() -> None:
    """The two halves of the inversion: slots are re-substituted, and inline/rule literals
    never left the template in the first place."""
    sql = reconstruct_accepted_sql(
        _payload(
            "SELECT x FROM db.t WHERE dept = {dept} AND kind = 'EARN'",
            [_slot("dept", "0420", "dept")],
        )
    )
    assert sql is not None
    assert "'0420'" in sql
    assert "'EARN'" in sql
    assert "{" not in sql


def test_a_list_slot_becomes_a_tuple_so_an_in_site_parses() -> None:
    """⚠ A single quoting rule is wrong in both directions. Quoting the whole stored value
    turns `IN {depts}` into a comparison against one comma-joined string, which parses as
    something else entirely rather than failing loudly."""
    sql = reconstruct_accepted_sql(
        _payload(
            "SELECT x FROM db.t WHERE dept IN {depts}",
            [_slot("dept", "Sales,Ops", "depts", "list")],
        )
    )
    assert sql is not None
    assert "IN ('Sales', 'Ops')" in sql
    assert sqlglot.parse_one(sql, dialect="clickhouse") is not None


def test_a_numeric_bind_site_falls_back_to_an_unquoted_literal() -> None:
    """The stored `locator.value` has ALREADY lost its quoting, so `toYear(d) = 2025` and
    `code = '0420'` arrive indistinguishable. The quoted reading is tried first and the parse
    decides — guessing once would give up on every numeric predicate."""
    sql = reconstruct_accepted_sql(
        _payload(
            "SELECT x FROM db.t WHERE months = {n}",
            [_slot("months", "6", "n", "relative_window")],
        )
    )
    assert sql is not None
    assert "= 6" in sql


def test_a_value_containing_a_quote_is_escaped_not_broken() -> None:
    sql = reconstruct_accepted_sql(
        _payload("SELECT x FROM db.t WHERE name = {who}", [_slot("name", "O'Brien", "who")])
    )
    assert sql is not None
    assert sqlglot.parse_one(sql, dialect="clickhouse") is not None


def test_a_template_with_no_slots_comes_back_unchanged() -> None:
    template = "SELECT count() FROM db.t WHERE kind = 'EARN'"
    assert reconstruct_accepted_sql(_payload(template, [])) == template


def test_it_refuses_rather_than_guessing() -> None:
    """`None`, never a guess. A snapshot built on SQL that does not parse would replace
    "cannot re-validate" with a decline that blames the reviewer for a storage artefact."""
    # no template at all — a composite or a fail-to-review generalization
    assert reconstruct_accepted_sql({"generalization": {}, "parameterization": []}) is None
    assert reconstruct_accepted_sql({"generalization": {"sql_template": "  "}}) is None
    # a token with no entry: the plan and the template disagree, which is exactly what the
    # totality walk exists to catch — inventing a value would paper over it
    assert (
        reconstruct_accepted_sql(
            _payload("SELECT x FROM db.t WHERE a = {ghost}", [_slot("b", "1", "other")])
        )
        is None
    )
    # nothing that can be substituted parses
    assert reconstruct_accepted_sql(_payload("SELECT FROM WHERE {x}", [_slot("c", "v", "x")])) is None


def test_the_reconstruction_round_trips_through_the_real_rewrite() -> None:
    """THE PROPERTY THAT MAKES IT USABLE: feed the reconstruction back through the S4 rewrite
    with the same plan and the original template comes out. If that did not hold, a backfilled
    candidate would re-generalize into a different blueprint than the one under review."""
    from data_agent.learning.generalize.rewrite import rewrite_sql_to_template

    template = "SELECT sum(a) AS t FROM db.t WHERE dept = {dept} AND kind = 'EARN'"
    entries = [_slot("dept", "0420", "dept")]
    sql = reconstruct_accepted_sql(_payload(template, entries))
    assert sql is not None
    assert rewrite_sql_to_template(sql, entries) == template
