"""`rule_match.nearest_known_rule` — the deterministic matcher that decides whether a
`missing_rule` decline can name the id that was meant.

WHAT IS ON TRIAL HERE. Not "does it find a match" — a matcher that always found one
would pass half of these. The property under test is the ASYMMETRY: a match is returned
only when exactly one catalog rule can have been meant, and every ambiguity, every weak
similarity and every polarity flip returns `None`, because `None` is what keeps the
decline terminal and the §7 "a human must ADD a rule" signal intact. Roughly half the
cases below assert `None` for that reason, and they are the load-bearing half.

The real catalog fixture is used wherever the case is a real one (the live
`earnings_only` → `gross_earnings` failure, and its neighbours on the same table, which
are what make the ambiguity cases honest); synthetic two-rule catalogs are used where
the point is a single mechanism and a real table would drag in coincidences.
"""

from __future__ import annotations

import pytest

from data_agent.learning.extractor.grounding import (
    known_rule_ids_from_catalog,
    rule_index_from_catalog,
)
from data_agent.learning.extractor.models import Locator, ParamPlan
from data_agent.learning.extractor.rule_match import (
    nearest_known_rule,
    rule_contradicts_predicate,
    rules_for_predicate,
)
from data_agent.learning.extractor.sql_predicates import LiteralPredicate
from tests._catalog_fixture import fixture_catalog

# The live case's warehouse coordinates: the plan bound `register_type` on
# `dbpcm_warehouse.payroll` and labelled the rule `earnings_only`; the catalog declares
# that predicate (`register_type = 'EARN'`) as `gross_earnings`.
_PAYROLL = "dbpcm_warehouse.payroll"


def _plan(table: str = _PAYROLL, column: str = "register_type", value: str = "EARN") -> ParamPlan:
    return ParamPlan(locator=Locator(table=table, column=column, value=value), role="rule")


def _real() -> tuple[frozenset[str], object]:
    catalog = fixture_catalog()
    return known_rule_ids_from_catalog(catalog), rule_index_from_catalog(catalog)


def _synthetic(entries: dict[str, list[dict[str, str]]]) -> tuple[frozenset[str], object]:
    catalog = {table: {"rules": rules} for table, rules in entries.items()}
    return known_rule_ids_from_catalog(catalog), rule_index_from_catalog(catalog)


# --- the live case -----------------------------------------------------------------


def test_the_live_case_earnings_only_resolves_to_gross_earnings() -> None:
    """THE case this module exists for. `gpt-5.5` cited `earnings_only` — an id it had
    been SHOWN on a prior-art card, because the blueprint corpus's `uses_rules`
    namespace had drifted from the catalog — for a plan binding `register_type` on
    `dbpcm_warehouse.payroll`. The catalog calls that concept `gross_earnings`. The
    proposal was correct; only the label was wrong."""
    known, index = _real()
    assert "earnings_only" not in known
    assert nearest_known_rule("earnings_only", _plan(), known, index) == "gross_earnings"


def test_the_live_case_needs_the_index_and_says_nothing_without_it() -> None:
    """The honest degrade, asserted rather than assumed. Without the table evidence the
    index carries, `earnings_only` is judged against all 80-odd catalog ids at the
    higher unscoped threshold and does not clear it — so a deployment that grounds ids
    but builds no index keeps exactly today's terminal decline."""
    known, _index = _real()
    assert nearest_known_rule("earnings_only", _plan(), known, None) is None


# --- tier 1: the same id, written differently --------------------------------------


@pytest.mark.parametrize(
    "cited", ["Gross-Earnings", "GROSS_EARNINGS", "gross-earnings", "grossEarnings"]
)
def test_an_id_that_differs_only_in_case_or_separators_matches_exactly(cited: str) -> None:
    """Normalisation, not similarity: these are one id spelled four ways, and the tier
    that resolves them needs no table evidence at all."""
    known, index = _real()
    assert nearest_known_rule(cited, _plan(table="", column=""), known, index) == "gross_earnings"


def test_an_id_the_catalog_already_declares_is_never_hinted() -> None:
    """A guard on the caller's precondition. `_validate_roles` only asks about ids that
    failed the membership test, but a matcher that "corrected" an existing id to a
    different one would be inventing work."""
    known, index = _real()
    assert nearest_known_rule("gross_earnings", _plan(), known, index) is None


# --- tier 2: the plan's own binding evidence ---------------------------------------


def test_a_bare_table_name_still_finds_the_qualified_catalog_key() -> None:
    """Models write `payroll` about as often as `dbpcm_warehouse.payroll`. A bare name
    matches the LAST SEGMENT of a catalog key — never any segment, which would scope the
    search to half the warehouse."""
    known, index = _real()
    assert nearest_known_rule("earnings_only", _plan(table="payroll"), known, index) == (
        "gross_earnings"
    )


def test_a_rule_declared_on_another_table_is_never_offered() -> None:
    """Table evidence DECIDES; it does not merely rank. The same `earnings_only`, bound
    to a column on `employee`, gets nothing — the plan says the predicate is on that
    table, and a rule declared elsewhere is a different rule, not a spelling fix."""
    known, index = _real()
    plan = _plan(table="dbpcm_warehouse.employee", column="employee_status", value="A")
    assert nearest_known_rule("earnings_only", plan, known, index) is None


def test_the_plans_column_breaks_a_tie_between_two_rules_on_one_table() -> None:
    """The narrowing signal, isolated. Both rules score identically against `open_only`,
    so the id alone is ambiguous; the plan says the predicate is on `incident_state`,
    and only one of the two rules is about that column."""
    known, index = _synthetic(
        {
            "db.t": [
                {"id": "open_tickets", "predicate": "ticket_state = 'Open'"},
                {"id": "open_incidents", "predicate": "incident_state = 'Open'"},
            ]
        }
    )
    ambiguous = _plan(table="db.t", column="state", value="Open")
    assert nearest_known_rule("open_only", ambiguous, known, index) is None

    narrowed = _plan(table="db.t", column="incident_state", value="Open")
    assert nearest_known_rule("open_only", narrowed, known, index) == "open_incidents"


# --- tier 3: no usable table evidence ----------------------------------------------


def test_without_table_evidence_only_a_near_identical_id_matches() -> None:
    """The unscoped tier's higher bar, from both sides. A plural/singular slip clears
    it; a genuine-but-partial overlap does not, because with the whole catalog in scope
    a shared word is cheap."""
    known, index = _synthetic(
        {"db.t": [{"id": "gross_earnings", "predicate": "register_type = 'EARN'"}]}
    )
    unknown_table = _plan(table="db.nowhere", column="register_type")
    assert nearest_known_rule("gross_earning", unknown_table, known, index) == "gross_earnings"
    assert nearest_known_rule("earnings_only", unknown_table, known, index) is None


def test_a_plural_is_folded_to_its_singular() -> None:
    known, index = _real()
    plan = _plan(table="dbpcm_warehouse.employee", column="employee_status", value="A")
    assert nearest_known_rule("active_employees", plan, known, index) == "active_employee"


# --- the refusals, which are the point ---------------------------------------------


def test_two_plausible_counterparts_return_nothing() -> None:
    """`taxes_only` on the payroll register is exactly as close to `employee_taxes` as
    it is to `employer_taxes`, and those two are opposite sides of the same ledger.
    Picking either would be a guess, and a guess that lands is worse than a decline."""
    known, index = _real()
    assert nearest_known_rule("taxes_only", _plan(value="EETAX"), known, index) is None


def test_an_id_with_nothing_close_returns_nothing() -> None:
    known, index = _real()
    assert nearest_known_rule("zebra_quotient", _plan(), known, index) is None


def test_a_negated_rule_is_never_offered_as_the_fix_for_a_positive_one() -> None:
    """The dangerous near-match: `not_graduated` shares every concept token with
    `graduated_only` and inverts its meaning. A hint that flipped a predicate would put
    a silently-wrong filter in the corpus — the D56 class this decline family exists to
    keep out. Polarity tokens are compared, never scored."""
    known, index = _synthetic(
        {"db.t": [{"id": "not_graduated", "predicate": "graduated = 'No'"}]}
    )
    plan = _plan(table="db.t", column="graduated", value="Yes")
    assert nearest_known_rule("graduated_only", plan, known, index) is None
    assert nearest_known_rule("not_graduated_rows", plan, known, index) == "not_graduated"


def test_a_shared_fragment_shorter_than_a_word_is_not_a_match() -> None:
    """`tax` is a fragment half a payroll catalog shares. A match must turn on at least
    one whole word, or the matcher starts "correcting" ids that merely rhyme."""
    known, index = _synthetic({"db.t": [{"id": "tax", "predicate": "kind = 'T'"}]})
    assert nearest_known_rule("tax_only", _plan(table="db.t", column="kind"), known, index) is None


# --- the two invariants the caller relies on ---------------------------------------


def test_nothing_outside_known_rules_is_ever_returned() -> None:
    """`known_rules` is authoritative for what EXISTS. An index built from a newer
    catalog snapshot may narrow the search; it must never be able to widen it into an
    id validation would reject a moment later."""
    known, index = _synthetic(
        {"db.t": [{"id": "gross_earnings", "predicate": "register_type = 'EARN'"}]}
    )
    assert nearest_known_rule("gross_earning", _plan(table="db.t"), known, index) == (
        "gross_earnings"
    )
    assert nearest_known_rule("gross_earning", _plan(table="db.t"), frozenset(), index) is None


def test_an_empty_catalog_never_hints() -> None:
    """The `known_rules=frozenset()` deployment (the factory warns about it) keeps its
    behaviour exactly: every rule-role plan declines, terminally, with no correction."""
    empty_known, empty_index = _synthetic({})
    assert nearest_known_rule("earnings_only", _plan(), empty_known, empty_index) is None
    assert nearest_known_rule("earnings_only", _plan(), frozenset(), None) is None


@pytest.mark.parametrize(
    "cited",
    ["earnings_only", "taxes_only", "active_employees", "deductions_only", "zebra", "", "  "],
)
def test_every_answer_is_either_an_existing_rule_or_nothing(cited: str) -> None:
    """Swept, because this is the property `validation.py` puts in front of a model: the
    hint it prints is an id the catalog actually declares, so a model that takes the
    hint produces a candidate that passes the very check that declined it."""
    known, index = _real()
    match = nearest_known_rule(cited, _plan(), known, index)
    assert match is None or match in known


# --- what a table qualifier means, for BOTH matchers -------------------------------


def test_a_table_the_catalog_knows_is_authoritative_even_with_no_rules_on_it() -> None:
    """The narrowing rule, at its sharpest. `db.empty` is a real catalogued table that
    declares no rules; `db.other` declares one whose id is all but identical to the
    cited one. The plan says the predicate is on `db.empty`, so the answer is "there is
    no rule here" — not "here is one from somewhere else".

    Both matchers take this rule, and they have to take the SAME one: they appear side
    by side in one correction, and a reader should not have to learn that one of them
    leaves the plan's table and the other does not."""
    known, index = _synthetic(
        {
            "db.empty": [],
            "db.other": [{"id": "gross_earnings", "predicate": "register_type = 'EARN'"}],
        }
    )
    on_empty = _plan(table="db.empty")
    assert nearest_known_rule("gross_earning", on_empty, known, index) is None
    assert rules_for_predicate(
        LiteralPredicate(table="empty", column="register_type", value="EARN", operator="="),
        index,
    ) == ()


def test_a_qualifier_that_resolves_to_nothing_leaves_the_search_open() -> None:
    """The other half of the same rule: an ALIAS (or a typo, or an empty qualifier) is
    not evidence about anything, so the search covers the catalog rather than collapsing
    to nothing. Without this the live case — `FROM dbpcm_warehouse.payroll AS p`, which
    yields the qualifier `p` — would find no rule at all."""
    known, index = _synthetic(
        {"db.other": [{"id": "gross_earnings", "predicate": "register_type = 'EARN'"}]}
    )
    assert nearest_known_rule("gross_earning", _plan(table="p"), known, index) == "gross_earnings"
    assert [
        m.rule.id
        for m in rules_for_predicate(
            LiteralPredicate(table="p", column="register_type", value="EARN", operator="="),
            index,
        )
    ] == ["gross_earnings"]


# --- the landing gate ---------------------------------------------------------------


def _pred(column: str, value: str, operator: str = "=", table: str = "p") -> LiteralPredicate:
    return LiteralPredicate(table=table, column=column, value=value, operator=operator)


def test_a_rule_that_declares_a_different_literal_is_contradicted() -> None:
    known, index = _real()
    assert known  # the fixture grounds the ids this asserts against
    wrong = rule_contradicts_predicate("gross_earnings", _pred("register_type", "DDUCT"), index)
    assert wrong is not None
    assert wrong.id == "gross_earnings"
    assert wrong.predicate == "register_type = 'EARN'"


def test_a_rule_that_declares_exactly_this_predicate_is_not_contradicted() -> None:
    _known, index = _real()
    assert rule_contradicts_predicate("gross_earnings", _pred("register_type", "EARN"), index) is None


def test_a_different_column_contradicts_even_with_the_same_value() -> None:
    """The value alone means nothing: `register_type = 'EARN'` and `type_code = 'EARN'`
    are different filters."""
    _known, index = _real()
    assert rule_contradicts_predicate("gross_earnings", _pred("type_code", "EARN"), index) is not None


def test_an_unparseable_or_complex_rule_predicate_is_never_contradicted() -> None:
    """Can't-verify is not reject — the asymmetry that keeps the catalog's complex rules
    citable. A prose predicate, a null-check and an OR-fragment all decline to answer."""
    _known, index = _synthetic(
        {
            "db.t": [
                {"id": "prose_rule", "predicate": "Use department_name as the label."},
                {"id": "null_rule", "predicate": "hours IS NOT NULL"},
                {"id": "or_rule", "predicate": "status != 'N' OR status IS NULL"},
                {"id": "placeholder_rule", "predicate": "field_id IN ({codes})"},
            ]
        }
    )
    for rule_id in ("prose_rule", "null_rule", "or_rule", "placeholder_rule"):
        assert rule_contradicts_predicate(rule_id, _pred("status", "N"), index) is None


def test_an_unknown_id_and_a_missing_index_are_both_silent() -> None:
    _known, index = _real()
    assert rule_contradicts_predicate("no_such_rule", _pred("register_type", "EARN"), index) is None
    assert rule_contradicts_predicate("gross_earnings", _pred("register_type", "DDUCT"), None) is None


# --- the index the matcher reads ---------------------------------------------------


def test_the_index_and_the_id_set_are_two_views_of_one_parse() -> None:
    """They cannot be allowed to disagree about what the catalog contains — one is
    projected from the other, and this is what says so."""
    catalog = fixture_catalog()
    index = rule_index_from_catalog(catalog)
    assert index.ids() == known_rule_ids_from_catalog(catalog)
    assert index.ids()  # the fixture really does declare rules


def test_a_malformed_catalog_entry_is_skipped_rather_than_raised_on() -> None:
    """A malformed catalog must degrade the `rule` role, never stop the queue draining."""
    index = rule_index_from_catalog(
        {
            "db.t": {"rules": "not a list"},
            "db.u": {"rules": [{"no_id": 1}, "not a dict", {"id": "real_rule"}]},
            "db.v": {},
        }
    )
    assert index.ids() == frozenset({"real_rule"})
