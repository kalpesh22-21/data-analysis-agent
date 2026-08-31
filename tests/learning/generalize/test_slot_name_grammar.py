"""A slot NAME becomes template TEXT — so it is an injection site, and this pins the guard.

`rewrite_sql_to_template` renders each slot as `exp.Placeholder(this=name)` and then string-
patches `{name: }` into `{name}`. Nothing on the chain checked what that name was: `_slot_plan`
takes any string, `_plan_params_ok` requires only `str`, and the rewrite itself only rejected a
non-string. So the name was the one field through which a plan author — the S3 extractor, a
human completing a form, or the §C reviser — could write SQL straight into the template.

⚠ THE FIVE STATIC CHECKS DO NOT CATCH IT, which is why the guard has to be at the splice:

    slot.name = "x} OR 1 = 1 --"
    →  ... WHERE department = {x} OR 1 = 1 --} AND region = {region}

`SLOT_TOKEN` reads `{x}` as an ordinary slot, so the colon form parses; `read_only_select` sees
one read-only SELECT; `date_literal_ok` finds no date; the injected `OR` adds no function shape
for `_check_rewritten` to compare against; and the trailing `--` comments out the remainder.
`decide_outcome` returns `("ok", None)` and the blueprint auto-lands — then answers every
department at replay, which is exactly the D97 confidently-wrong class the checks exist to stop.
"""

from __future__ import annotations

import pytest

from data_agent.learning.generalize.rewrite import (
    _BARE_SLOT_NAME,
    RewriteError,
    rewrite_sql_to_template,
)
from data_agent.learning.generalize.validate import (
    check_no_frozen_date_literal,
    check_read_only_select,
    decide_outcome,
)
from data_agent.runtime.blueprint.template import SLOT_TOKEN

SQL = (
    "SELECT sum(gross_pay) AS t FROM payroll.payroll_fact "
    "WHERE department = '0420' AND region = 'NA'"
)


def _plan(name: str) -> list[dict]:
    return [
        {
            "locator": {"table": "payroll.payroll_fact", "column": "department", "value": "0420"},
            "role": "slot",
            "slot": {
                "name": name,
                "type": "entity",
                "binds_to": "payroll.payroll_fact.department",
                "required": True,
            },
        },
        {
            "locator": {"table": "payroll.payroll_fact", "column": "region", "value": "NA"},
            "role": "slot",
            "slot": {
                "name": "region",
                "type": "entity",
                "binds_to": "payroll.payroll_fact.region",
                "required": True,
            },
        },
    ]


@pytest.mark.parametrize(
    "name",
    [
        "x} OR 1 = 1 --",           # the original: nullify the filter, comment out the rest
        "x} UNION ALL SELECT 1 --",  # a second query arm
        "dept; DROP TABLE employees",
        "a b",                       # whitespace ends the token
        "1abc",                      # must not start with a digit
        "dept-name",
        "",
    ],
)
def test_a_slot_name_that_is_not_a_bare_identifier_is_refused(name: str) -> None:
    """`RewriteError` is this module's contract for an un-rewritable plan, and it is the only
    exception its callers catch — so refusing here routes the candidate to fail-to-review
    rather than escaping as a TypeError into a queue worker."""
    with pytest.raises(RewriteError):
        rewrite_sql_to_template(SQL, _plan(name))


def test_an_ordinary_name_still_rewrites() -> None:
    out = rewrite_sql_to_template(SQL, _plan("department"))
    assert "{department}" in out and "{region}" in out
    assert set(SLOT_TOKEN.findall(out)) == {"department", "region"}


def test_the_injection_would_have_passed_every_static_check() -> None:
    """The guard's justification, asserted rather than argued.

    If the checks caught this, the right fix would be one of them. They do not: the crafted
    template is a valid, read-only, date-free SELECT whose slot tokens read back cleanly. That
    is why the grammar belongs at the splice, where the name becomes text.
    """
    crafted = (
        "SELECT sum(gross_pay) AS t FROM payroll.payroll_fact "
        "WHERE department = {x} OR 1 = 1 --} AND region = {region}"
    )
    assert check_read_only_select(crafted) is True
    assert check_no_frozen_date_literal(crafted) is True
    assert decide_outcome(
        explain_ok=True,
        binds_to_subset_uses=True,
        dag_ok=True,
        read_only_select=check_read_only_select(crafted),
        date_literal_ok=check_no_frozen_date_literal(crafted),
    ) == ("ok", None)
    # ...and the injected predicate really is live SQL, not inert text.
    assert "OR 1 = 1" in SLOT_TOKEN.sub(lambda m: f":{m.group(1)}", crafted)


@pytest.mark.parametrize(
    "name", ["a", "_a", "A1", "dept", "pay_period", "x2y", "_", "Z_9"]
)
def test_parity_every_accepted_name_round_trips_through_the_token_regex(name: str) -> None:
    """⚠ THE PARITY THE GUARD RESTS ON.

    `_BARE_SLOT_NAME` is spelled out rather than sliced off `SLOT_TOKEN.pattern` — string
    surgery on a regex breaks silently the first time the token pattern gains a group or an
    escape. This test is what holds the two in step instead: a name the guard accepts MUST be
    recoverable from `{name}` by `SLOT_TOKEN`, because the template is the only record of its
    own slots.
    """
    assert _BARE_SLOT_NAME.fullmatch(name)
    assert SLOT_TOKEN.findall("{" + name + "}") == [name]


@pytest.mark.parametrize("name", ["x}y", "a b", "1abc", "a-b", "", "dé"])
def test_parity_every_rejected_name_fails_to_round_trip(name: str) -> None:
    """The other direction: anything the guard rejects is a name `SLOT_TOKEN` would not give
    back intact, so emitting it would produce a template whose slots cannot be read off it."""
    assert not _BARE_SLOT_NAME.fullmatch(name)
    assert SLOT_TOKEN.findall("{" + name + "}") != [name]
