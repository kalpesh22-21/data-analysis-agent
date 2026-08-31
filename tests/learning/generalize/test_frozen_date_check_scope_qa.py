"""THE SCOPE of the fifth static check (`check_no_frozen_date_literal`) — two gaps CLOSED,
one boundary held.

Written by QA while trying to break the check; the first two shapes below DID break it and
have since been fixed in `generalize/validate.py`, so these tests now pin the gaps SHUT —
each asserts the closed behavior, and a regression re-opens the exact hole it was found in.
The motivating incident lives in `test_frozen_date_literal.py`.

## Gap 1 — CLOSED: the exemption was HALF of the enumerator's rule

The check exempted a date literal under a comparison whose own side is column-free, derived
from `extractor/sql_predicates.py::_constant_text`. But `_constant_text` is only one of the
two things `_binary_predicate`/`_in_predicate`/`_between_predicate` demand: the OTHER side
must also resolve to EXACTLY ONE column (`_single_column`). So when the opposite side
carried ZERO columns or TWO (`coalesce(term_date, seniority_date) < '2026-08-28'`,
`greatest(a, b)`, a tuple compare), S3 enumerated NO predicate — and S4 still handed the
literal to S3 as if it had been adjudicated. Nobody adjudicated it. `coalesce(a, b) < '<run
date>'` is an ordinary HR shape, so this was not a theoretical corner.

FIXED by asking the enumerator instead of re-deriving its rule: `sql_predicates` promoted
`_predicate_of` to the public `literal_predicate_of`, and the check exempts a literal only
when that returns a `LiteralPredicate` for the enclosing comparison AND the literal sits on
that predicate's column-free side. Both halves are kept — see
`test_frozen_date_literal.py::test_a_frozen_date_on_a_column_bearing_side_fails` for why the
second is still load-bearing. There is now ONE definition of "adjudicated", in S3.

## Gap 2 — CLOSED: the shape regex was narrower than "ISO"

`_DATE_SHAPED` matched only `YYYY-MM-DD[ T]HH:MM:SS(.fff)`, so a `Z`/`+00:00` zone suffix or
minute precision (`2026-08-28 00:00`) slipped through in the incident's own position. The
time part now covers the ISO-8601 spellings a model emits. Still deliberately OUT of scope,
and asserted as such below: non-ISO (`28/08/2026`), compact (`20260828`) and unpadded
(`2026-8-28`) spellings, and bare years.

## Gap 3 — OPEN BY DESIGN: a frozen date UNDER a real predicate is still frozen

`WHERE hire_date >= '2026-02-28'` — "hired in the last 6 months", resolved against the run
date and pasted — passes, because S3 DOES enumerate it and the check deliberately does not
consult `parameterization`. If the plan classified that literal `role: inline`, the template
freezes the window and the blueprint ages exactly like the incident did. This is the check's
stated boundary (a literal the enumerator adjudicated is S3's to classify), and it is the
LARGEST remaining surface for the incident class — S4 covers the shapes S3 structurally
cannot see, and S3 + the role plan own the rest. Pinned here so the division of labour is
written down where the next incident will be triaged.
"""

from __future__ import annotations

from data_agent.learning.extractor.sql_predicates import literal_predicates
from data_agent.learning.generalize.validate import check_no_frozen_date_literal

_RUN_DATE = "2026-08-28"


# --- Gap 1: an opposite side S3 cannot read as a single column ----------------


def test_a_frozen_date_against_a_two_column_expression_is_caught_because_s3_never_saw_it():
    """BOTH halves are asserted, because the fix IS the agreement: S4 refuses the literal
    for exactly the reason S3 enumerated nothing — no single-column side."""
    sql = (
        "SELECT count() AS c FROM dbpcm_warehouse.employee "
        f"WHERE coalesce(termination_date, seniority_date) < '{_RUN_DATE}'"
    )
    assert literal_predicates(sql) == []  # nobody adjudicated this date...
    assert check_no_frozen_date_literal(sql) is False  # ...so S4 sends it to a human


def test_the_same_hole_through_greatest_and_a_tuple_compare_is_shut_too():
    """The other three spellings of "a comparison S3 cannot read as (one column, one
    constant)". They are one code path, but they are three shapes a model actually writes,
    and the fix has to hold for the shape — not for `coalesce` by name."""
    for sql in (
        "SELECT count() AS c FROM dbpcm_warehouse.employee "
        f"WHERE greatest(hire_date, rehire_date) > '{_RUN_DATE}'",
        "SELECT count() AS c FROM dbpcm_warehouse.employee "
        f"WHERE (employee_status, seniority_date) = ('Active', '{_RUN_DATE}')",
        "SELECT count() AS c FROM dbpcm_warehouse.employee "
        f"WHERE concat(first_name, last_name) BETWEEN '2020-01-01' AND '{_RUN_DATE}'",
    ):
        assert literal_predicates(sql) == [], sql
        assert check_no_frozen_date_literal(sql) is False, sql


def test_the_single_column_shape_is_the_one_s3_really_adjudicates():
    """The control for the two above, and the reason the fix is not just "refuse dates in
    comparisons": move the SAME comparison onto one column and S3 enumerates it, so the
    exemption applies and this template still promotes."""
    sql = (
        "SELECT count() AS c FROM dbpcm_warehouse.employee "
        f"WHERE seniority_date < '{_RUN_DATE}'"
    )
    assert check_no_frozen_date_literal(sql) is True
    assert [(p.column, p.operator, p.value) for p in literal_predicates(sql) or []] == [
        ("seniority_date", "<", _RUN_DATE)
    ]


# --- Gap 2: ISO spellings the shape regex does not match ----------------------


def test_every_iso_spelling_in_the_incident_position_is_now_date_shaped():
    """All of these are the INCIDENT's position — a bare, un-compared argument to date
    arithmetic. Every ISO-8601 spelling is now caught; the three still uncaught are the
    documented non-ISO scope line, pinned so a widening is a deliberate act."""
    for literal, caught in (
        (f"{_RUN_DATE}T00:00:00Z", True),
        (f"{_RUN_DATE} 00:00:00+00:00", True),
        (f"{_RUN_DATE} 00:00", True),
        (f"{_RUN_DATE} 00:00:00.123456", True),
        (f"{_RUN_DATE} 00:00:00", True),  # what the incident actually emitted
        ("2026-8-28", False),  # unpadded month/day — out of scope
        ("20260828", False),  # compact — out of scope
        ("28/08/2026", False),  # non-ISO — out of scope
    ):
        sql = (
            f"SELECT DATE_DIFF(DAY, seniority_date, parseDateTimeBestEffort('{literal}')) "
            "AS tenure_days FROM dbpcm_warehouse.employee"
        )
        assert check_no_frozen_date_literal(sql) is (not caught), literal


# --- Gap 3: the stated boundary ------------------------------------------------


def test_a_relative_window_resolved_to_the_run_date_passes_by_design():
    """"hired in the last 6 months" as the extractor saw it: a comparison against a date
    the model computed from the anchor. S4 leaves it to S3 + the role plan; if that plan
    says `inline`, the template freezes the window."""
    sql = (
        "SELECT count() AS c FROM dbpcm_warehouse.employee WHERE hire_date >= '2026-02-28'"
    )
    assert check_no_frozen_date_literal(sql) is True
    assert [p.value for p in literal_predicates(sql) or []] == ["2026-02-28"]
