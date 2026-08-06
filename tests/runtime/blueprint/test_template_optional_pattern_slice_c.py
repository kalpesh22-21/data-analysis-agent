"""Layer-1: Slice C — `optional_pattern` predicate replacement in `bind_template`.

When an OPTIONAL slot is OMITTED, its `optional_pattern` is a boolean SQL fragment
that REPLACES the WHOLE predicate containing the slot's `{token}` — never a value
substituted for the token (`WHERE region = {region}` + `TRUE` → `WHERE TRUE`, NOT
`region = TRUE`). One arm of an AND/OR is replaced in place; the rest renders
normally. A provided value still binds as a typed literal (unchanged). A malformed
pattern, or an omitted slot with NO pattern, fails closed → the raw loop.
"""

from __future__ import annotations

import pytest
import sqlglot

from data_agent.runtime.blueprint.template import (
    TemplateBindError,
    bind_template,
    validate_optional_pattern,
)

# -- the core replacement: sole predicate ------------------------------------


def test_sole_where_predicate_replaced_by_pattern() -> None:
    # `region = {region}` is the ONLY WHERE predicate — omitted → `WHERE TRUE`
    # (all regions), NOT `region = TRUE`.
    out = bind_template(
        "SELECT x FROM t WHERE region = {region}",
        {},
        optional_patterns={"region": "TRUE"},
    )
    assert out == "SELECT x FROM t WHERE TRUE"
    assert "{region}" not in out and "region = " not in out


def test_and_arm_replaced_in_place_rest_renders() -> None:
    # One arm of an AND — only that arm becomes the pattern; the sibling stays.
    out = bind_template(
        "SELECT x FROM t WHERE dept = 'Eng' AND region = {region}",
        {},
        optional_patterns={"region": "TRUE"},
    )
    assert out == "SELECT x FROM t WHERE dept = 'Eng' AND TRUE"


def test_or_arm_replaced_in_place_rest_renders() -> None:
    out = bind_template(
        "SELECT x FROM t WHERE dept = 'Eng' OR region = {region}",
        {},
        optional_patterns={"region": "TRUE"},
    )
    assert out == "SELECT x FROM t WHERE dept = 'Eng' OR TRUE"


def test_in_list_predicate_replaced_whole() -> None:
    # An `IN {slot}` list predicate — the entire `code IN (...)` is replaced.
    out = bind_template(
        "SELECT x FROM t WHERE code IN {codes}",
        {},
        optional_patterns={"codes": "TRUE"},
    )
    assert out == "SELECT x FROM t WHERE TRUE"


def test_multiple_occurrences_all_replaced() -> None:
    out = bind_template(
        "SELECT x FROM t WHERE a = {r} OR b = {r}",
        {},
        optional_patterns={"r": "TRUE"},
    )
    assert out == "SELECT x FROM t WHERE TRUE OR TRUE"
    assert "{r}" not in out


def test_pattern_can_be_a_real_condition_not_just_true() -> None:
    # The pattern is any boolean fragment — here it broadens to a set membership.
    out = bind_template(
        "SELECT x FROM t WHERE a = 1 AND region = {region}",
        {},
        optional_patterns={"region": "region IN (1, 2)"},
    )
    assert out == "SELECT x FROM t WHERE a = 1 AND region IN (1, 2)"


# -- coexistence with provided values ----------------------------------------


def test_provided_value_binds_normally_alongside_omitted_pattern() -> None:
    # `dept` is provided (typed literal); `region` is omitted (pattern) — both in
    # the same AND tree, each handled independently.
    out = bind_template(
        "SELECT x FROM t WHERE dept = {dept} AND region = {region}",
        {"dept": "Eng"},
        optional_patterns={"region": "TRUE"},
    )
    assert out == "SELECT x FROM t WHERE dept = 'Eng' AND TRUE"


def test_provided_optional_slot_binds_as_literal_when_present() -> None:
    # An optional slot that WAS provided is a normal value binding — no pattern
    # passed (the executor only supplies a pattern for an OMITTED slot).
    out = bind_template(
        "SELECT x FROM t WHERE region = {region}",
        {"region": "EMEA"},
    )
    assert out == "SELECT x FROM t WHERE region = 'EMEA'"


def test_omitted_pattern_value_never_substituted_for_token() -> None:
    # Guard against the WRONG behavior (token substitution): a hostile-looking
    # pattern name must never surface as `region = <pattern>`.
    out = bind_template(
        "SELECT x FROM t WHERE region = {region}",
        {},
        optional_patterns={"region": "TRUE"},
    )
    tree = sqlglot.parse_one(out, dialect="clickhouse")
    # No `EQ` comparison survives — the whole predicate was replaced, not the RHS.
    assert not any(isinstance(n, sqlglot.exp.EQ) for n in tree.walk())


# -- fail-closed --------------------------------------------------------------


def test_omitted_slot_with_no_pattern_still_fails_closed() -> None:
    # No pattern supplied for an omitted `{region}` → the token stays unbound →
    # TemplateBindError (the executor turns this into the raw-loop fallback).
    with pytest.raises(TemplateBindError):
        bind_template("SELECT x FROM t WHERE region = {region}", {})


def test_malformed_pattern_fails_closed() -> None:
    # A pattern that is not a boolean condition (a full SELECT) → fail-closed.
    with pytest.raises(TemplateBindError):
        bind_template(
            "SELECT x FROM t WHERE region = {region}",
            {},
            optional_patterns={"region": "SELECT 1"},
        )


def test_unparseable_pattern_fails_closed() -> None:
    with pytest.raises(TemplateBindError):
        bind_template(
            "SELECT x FROM t WHERE region = {region}",
            {},
            optional_patterns={"region": "))(( not sql"},
        )


def test_pattern_for_unreferenced_token_is_inert() -> None:
    # A pattern whose token does NOT appear in the template is a no-op — it is
    # never even parsed (so a bad pattern for an unused slot cannot fail the bind).
    out = bind_template(
        "SELECT x FROM t WHERE dept = {dept}",
        {"dept": "Eng"},
        optional_patterns={"region": "SELECT 1"},  # malformed, but unreferenced
    )
    assert out == "SELECT x FROM t WHERE dept = 'Eng'"


def test_missing_provided_binding_still_fails_even_with_other_pattern() -> None:
    # `dept` has neither a value nor a pattern → still missing (fail-closed); the
    # `region` pattern does not mask an unrelated unbound slot.
    with pytest.raises(TemplateBindError):
        bind_template(
            "SELECT x FROM t WHERE dept = {dept} AND region = {region}",
            {},
            optional_patterns={"region": "TRUE"},
        )


# -- REVIEW blocker 1: never silently drop a predicate SIBLING ---------------


def test_between_with_provided_sibling_fails_closed_not_dropped() -> None:
    # `x BETWEEN {lo} AND {hi}` — `hi` is a PROVIDED value, `lo` an omitted pattern.
    # Replacing the whole BETWEEN would DROP `hi=5` (a silent wrong answer that still
    # passes the grain gate). Fail closed instead → raw loop.
    with pytest.raises(TemplateBindError):
        bind_template(
            "SELECT x FROM t WHERE x BETWEEN {lo} AND {hi}",
            {"hi": 5},
            optional_patterns={"lo": "TRUE"},
        )


def test_two_omitted_tokens_in_one_predicate_fails_closed() -> None:
    # Two DIFFERENT omitted-pattern tokens sharing one predicate (a BETWEEN) — which
    # pattern wins is ambiguous; replacing drops a sibling. Fail closed.
    with pytest.raises(TemplateBindError):
        bind_template(
            "SELECT x FROM t WHERE x BETWEEN {lo} AND {hi}",
            {},
            optional_patterns={"lo": "TRUE", "hi": "FALSE"},
        )


def test_anonymous_placeholder_sibling_fails_closed() -> None:
    # A bare `?` (anonymous placeholder, this=None) sharing the predicate is a
    # non-target bind site too — replacing the whole predicate would drop it. The
    # guard must treat anonymous placeholders as foreign, not just NAMED ones.
    with pytest.raises(TemplateBindError):
        bind_template(
            "SELECT x FROM t WHERE x BETWEEN ? AND {lo}",
            {},
            optional_patterns={"lo": "TRUE"},
        )


# -- REVIEW blocker 2: a placeholder-bearing pattern must not loop/hang -------


def test_self_referential_pattern_raises_and_does_not_hang() -> None:
    # A pattern that re-inserts its OWN placeholder (`:r`) would spin the re-walk
    # loop forever — rejected at parse (self-contained patterns only).
    with pytest.raises(TemplateBindError):
        bind_template(
            "SELECT x FROM t WHERE a = {r}",
            {},
            optional_patterns={"r": "b = :r"},
        )


def test_foreign_colon_placeholder_pattern_raises() -> None:
    with pytest.raises(TemplateBindError):
        bind_template(
            "SELECT x FROM t WHERE a = {r}",
            {},
            optional_patterns={"r": "b = :z"},
        )


def test_curly_token_in_pattern_raises() -> None:
    # A curly `{allowed}` inside a pattern is NOT substituted (patterns parse raw) —
    # it would silently become a `map()` literal. Reject it as a non-self-contained
    # pattern rather than emit garbage SQL.
    with pytest.raises(TemplateBindError):
        bind_template(
            "SELECT x FROM t WHERE a = {r}",
            {},
            optional_patterns={"r": "region IN {allowed}"},
        )


# -- REVIEW should-fix 3: a NOT-wrapped predicate must fail closed -----------


def test_pattern_directly_under_not_fails_closed() -> None:
    # `WHERE NOT (region = {region})` + `TRUE` would become `WHERE NOT TRUE` — an
    # empty result that passes the grain gate vacuously. Fail closed.
    with pytest.raises(TemplateBindError):
        bind_template(
            "SELECT x FROM t WHERE NOT (region = {region})",
            {},
            optional_patterns={"region": "TRUE"},
        )


def test_pattern_nested_under_not_fails_closed() -> None:
    # The NOT guard is NOT one-level-deep: `NOT (a = 1 AND region = {region})` with
    # region omitted → `NOT (a = 1 AND TRUE)` ≡ `NOT (a = 1)` — a silent polarity
    # narrowing. Any NOT ancestor between the token and its clause boundary fails.
    with pytest.raises(TemplateBindError):
        bind_template(
            "SELECT x FROM t WHERE NOT (a = 1 AND region = {region})",
            {},
            optional_patterns={"region": "TRUE"},
        )


def test_pattern_deeply_nested_under_not_fails_closed() -> None:
    with pytest.raises(TemplateBindError):
        bind_template(
            "SELECT x FROM t WHERE NOT (a = 1 OR (b = 2 AND region = {region}))",
            {},
            optional_patterns={"region": "TRUE"},
        )


def test_not_on_a_different_column_still_replaces_the_slot_arm() -> None:
    # The NOT applies to `active`, NOT to the slot predicate — the slot arm is a
    # normal AND arm and is replaced cleanly.
    out = bind_template(
        "SELECT x FROM t WHERE NOT active AND region = {region}",
        {},
        optional_patterns={"region": "TRUE"},
    )
    assert out == "SELECT x FROM t WHERE NOT active AND TRUE"


# -- REVIEW nit 6: ClickHouse PREWHERE is handled ----------------------------


def test_prewhere_predicate_replaced() -> None:
    out = bind_template(
        "SELECT x FROM t PREWHERE region = {region}",
        {},
        optional_patterns={"region": "TRUE"},
    )
    assert out == "SELECT x FROM t PREWHERE TRUE"


# -- REVIEW nit 7: bare literal / arithmetic patterns are not conditions ------


@pytest.mark.parametrize("pattern", ["1", "1 + 1", "salary * 2", "'x'"])
def test_non_boolean_pattern_fragments_rejected(pattern: str) -> None:
    with pytest.raises(TemplateBindError):
        validate_optional_pattern("r", pattern)


@pytest.mark.parametrize("pattern", ["TRUE", "FALSE", "region IN (1, 2)", "a > 0 AND b < 5", "c IS NOT NULL"])
def test_boolean_pattern_fragments_accepted(pattern: str) -> None:
    validate_optional_pattern("r", pattern)  # no raise


def test_validate_optional_pattern_rejects_statement_and_placeholder() -> None:
    with pytest.raises(TemplateBindError):
        validate_optional_pattern("r", "SELECT 1")
    with pytest.raises(TemplateBindError):
        validate_optional_pattern("r", "b = :r")
