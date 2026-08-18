"""The deterministic catalog-rule matchers behind the two hinted declines.

`nearest_known_rule` matches on the IDENTIFIER — "which rule was this trying to name?" —
for a plan that cited an id that does not exist. `rules_for_predicate` matches on the
PREDICATE — "does the catalog already declare exactly this filter?" — for a literal
predicate no `ParamPlan` covers, by EXACT structural equality only. Neither is a similarity
engine: each refuses wherever it is not certain, because a refusal costs a decline a human
reads while a wrong hint costs a model talked into a candidate that silently means
something else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .grounding import CatalogRule, RuleIndex
from .models import ParamPlan
from .sql_predicates import LiteralPredicate, sole_literal_predicate

# Identifier → tokens. Split on non-alphanumerics AND on camelCase boundaries, because
# the catalog carries both conventions (`gross_earnings`, `DepartmentLabel`).
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEPARATORS = re.compile(r"[^a-z0-9]+")

# Tokens that name no concept — they say something about the SHAPE of the rule ("only
# these rows", "the record filter") and appear in ids on both sides of a comparison
# without evidencing anything. Dropped from the score so `earnings_only` and
# `gross_earnings` are compared on `earnings` versus `gross earnings`, which is the
# comparison a human makes. Listed in the SINGULAR form because the check runs after
# `_singular`.
_STRUCTURAL_TOKENS = frozenset(
    {
        "a", "all", "an", "and", "any", "by", "default", "flag", "filter", "for",
        "id", "in", "is", "just", "of", "on", "only", "record", "row", "rule",
        "the", "to", "value",
    }
)

# Tokens that INVERT a rule rather than describe it. Never dropped, and never merely
# scored: a match is refused outright unless both ids carry the same ones (see
# `_polarity_matches`). `graduated_only` must not be "corrected" into `not_graduated` —
# that hint would silently invert the predicate the plan implements, which is the D56
# wrong-answer class this whole decline family exists to keep out of the corpus.
_POLARITY_TOKENS = frozenset({"exclude", "excluded", "never", "no", "non", "not", "without"})

# A shared token has to be a WORD, not a fragment: `pay`, `emp` and `dt` are shared by
# half the catalog. At least one shared token must be this long for a match to stand.
_MIN_SHARED_TOKEN_LEN = 4

# Jaccard over concept tokens. Two thresholds, because the two tiers rest on different
# evidence:
#
#   TABLE-SCOPED (0.5) — the plan told us WHICH TABLE the predicate is on and the
#     candidates are the rules declared there, typically under ten. `earnings_only` vs
#     `gross_earnings` scores exactly 0.5 ({earnings} ∩ {gross, earnings}), which is
#     what this number is calibrated on, and every other payroll rule scores 0.
#   UNSCOPED (0.75) — no usable table, so the candidates are every rule in the catalog
#     and a coincidental word is far likelier. 0.75 means "the same words, at most one
#     of them different"; below that, terminal.
_TABLE_SCOPED_MIN_SCORE = 0.5
_UNSCOPED_MIN_SCORE = 0.75


def nearest_known_rule(
    unknown_id: str,
    plan: ParamPlan,
    known_rules: frozenset[str],
    rules: RuleIndex | None = None,
) -> str | None:
    """The single catalog rule *unknown_id* can only have meant, or `None`.

    *known_rules* is authoritative for WHAT EXISTS — nothing outside it is ever returned, so a
    `RuleIndex` built from a different catalog snapshot can narrow the search but never widen
    it. *rules* is optional; without it the table-scoped tier cannot run. THREE TIERS, strongest
    first, and the first tier with evidence DECIDES: (1) EXACT after normalisation, a change of
    spelling and nothing more; (2) TABLE-SCOPED on the plan's own locator, where no winner
    means `None`, because a better-scoring rule elsewhere in the catalog is a different rule
    rather than a spelling fix; (3) UNSCOPED at a higher threshold, only when there is no usable
    table evidence at all. A tie is always `None`.
    """
    unknown_id = unknown_id.strip()
    if not unknown_id or not known_rules or unknown_id in known_rules:
        return None

    exact = _unique(
        [known for known in known_rules if _normalised(known) == _normalised(unknown_id)]
    )
    if exact is not None:
        return exact

    # A table the CATALOG KNOWS decides, even when it declares no rules at all: there is
    # then nothing on the plan's own table to have meant, and ranging over the rest of
    # the catalog would offer a rule from somewhere else. Only a qualifier that resolves
    # to nothing (an alias, an empty one, a typo) leaves tier 3 to answer. Same rule as
    # `rules_for_predicate`, deliberately.
    if rules is not None and rules.knows_table(plan.locator.table):
        scoped = [
            rule for rule in rules.on_table(plan.locator.table) if rule.id in known_rules
        ]
        on_column = [
            rule for rule in scoped if _mentions_column(rule.predicate, plan.locator.column)
        ]
        return _best_match(
            unknown_id,
            [rule.id for rule in (on_column or scoped)],
            minimum=_TABLE_SCOPED_MIN_SCORE,
        )

    return _best_match(unknown_id, sorted(known_rules), minimum=_UNSCOPED_MIN_SCORE)


def _best_match(unknown_id: str, candidates: list[str], *, minimum: float) -> str | None:
    """The single highest-scoring candidate at or above *minimum*, or `None`.

    `None` covers three situations on purpose — nothing scored high enough, two candidates tied
    at the top, or the winner shares only short fragments — because the caller declines
    terminally on all three.
    """
    unknown_tokens = _concept_tokens(unknown_id)
    if not unknown_tokens:
        return None
    scored = [
        (candidate, _score(unknown_tokens, _concept_tokens(candidate)))
        for candidate in candidates
        if _polarity_matches(unknown_id, candidate)
        and _shares_a_whole_word(unknown_tokens, _concept_tokens(candidate))
    ]
    qualified = [(candidate, score) for candidate, score in scored if score >= minimum]
    if not qualified:
        return None
    top = max(score for _candidate, score in qualified)
    return _unique([candidate for candidate, score in qualified if score == top])


def _unique(matches: list[str]) -> str | None:
    return matches[0] if len(matches) == 1 else None


def _score(left: frozenset[str], right: frozenset[str]) -> float:
    """Jaccard over concept tokens: |shared| / |union|.

    Jaccard rather than "how much of the unknown id is covered", because coverage is asymmetric
    in the dangerous direction: a one-token id like `earnings` would score 1.0 against every
    rule containing that word, however much else those rules say.
    """
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _shares_a_whole_word(left: frozenset[str], right: frozenset[str]) -> bool:
    return any(len(token) >= _MIN_SHARED_TOKEN_LEN for token in left & right)


def _polarity_matches(left_id: str, right_id: str) -> bool:
    """Both ids negate, or neither does. See `_POLARITY_TOKENS`."""
    return (_tokens(left_id) & _POLARITY_TOKENS) == (_tokens(right_id) & _POLARITY_TOKENS)


def _mentions_column(predicate: str, column: str) -> bool:
    """Does *predicate* name *column* as an identifier?

    Word-bounded so `type_code` does not match `code`, and case-insensitive because catalog
    predicates preserve authored casing (D70) while a model writes whatever it read off the
    SQL. A prose `predicate` simply never mentions a column, which costs nothing — the
    narrowing is an optional refinement of a set already scoped to one table.
    """
    column = column.strip()
    if not column or not predicate:
        return False
    return re.search(rf"(?<![0-9A-Za-z_]){re.escape(column)}(?![0-9A-Za-z_])", predicate, re.I) is not None


def _normalised(identifier: str) -> str:
    """`Gross-Earnings`, `grossEarnings` and `gross_earnings` → `gross_earnings`."""
    spaced = _CAMEL_BOUNDARY.sub("_", identifier)
    return _SEPARATORS.sub("_", spaced.lower()).strip("_")


def _tokens(identifier: str) -> frozenset[str]:
    return frozenset(
        _singular(token) for token in _normalised(identifier).split("_") if token
    )


def _concept_tokens(identifier: str) -> frozenset[str]:
    return _tokens(identifier) - _STRUCTURAL_TOKENS


# --- the predicate matcher ---------------------------------------------------------

# The operators a rule can be named FOR. A catalog rule declares which rows BELONG to a
# concept, so only a positive equality/membership test can be the same statement. `!=`,
# `<`, `LIKE` and `BETWEEN` are excluded not because no rule could ever express them but
# because the mapping is no longer an identity — `status != 'X'` is not the rule that
# selects `status = 'X'`, and offering it would invert the filter.
_NAMEABLE_OPERATORS = frozenset({"=", "IN"})

# Per literal value. More than this and the message stops being a list of options; the
# realistic count is 0 or 1 (a same-named column on another table is what makes it 2).
_MAX_RULES_PER_VALUE = 3


@dataclass(frozen=True)
class PredicateRuleMatch:
    """One catalog rule that declares exactly one literal of an uncovered predicate.

    `value` is the literal it accounts for — an `IN` list has one match per member.
    """

    value: str
    rule: CatalogRule


def rules_for_predicate(
    predicate: LiteralPredicate, rules: RuleIndex | None
) -> tuple[PredicateRuleMatch, ...]:
    """Every catalog rule whose predicate IS one of *predicate*'s literal comparisons.

    Exact structural equality in the strong sense: the rule's whole fragment parses to the same
    (column, literal, operator) and to nothing else. `()` — the catalog declares no rule for
    this filter — is a legitimate and common answer. TABLE SCOPING IS DECIDED BY WHETHER THE
    QUALIFIER RESOLVES, not by whether it yields rules: a qualifier the CATALOG KNOWS is
    authoritative even when that table declares none, while one that resolves to nothing (an
    alias, or an empty one) is NO EVIDENCE and the search covers every table. The cross-table
    risk that widening creates is closed downstream by `rule_contradicts_predicate` rather than
    by guessing here.
    """
    if rules is None or predicate.operator not in _NAMEABLE_OPERATORS:
        return ()
    if predicate.table and rules.knows_table(predicate.table):
        candidates: tuple[CatalogRule, ...] = rules.on_table(predicate.table)
    else:
        candidates = rules.rules
    matches: list[PredicateRuleMatch] = []
    for value in _literal_values(predicate):
        found = 0
        for rule in candidates:
            declared = _declared_predicate(rule.predicate)
            if declared is None or declared.operator != "=":
                continue
            if declared.column.lower() != predicate.column.lower() or declared.value != value:
                continue
            matches.append(PredicateRuleMatch(value=value, rule=rule))
            found += 1
            if found == _MAX_RULES_PER_VALUE:
                break
    return tuple(matches)


def rule_contradicts_predicate(
    rule_id: str, predicate: LiteralPredicate, rules: RuleIndex | None
) -> CatalogRule | None:
    """The cited rule, IF the catalog proves it declares a different filter from *predicate*.

    `None` in every other case, including every case of doubt. THE SEAM WITH TEETH: everything
    else in this module is advisory, but a landed `rule`-role entry puts a catalog rule's name
    on a blueprint's predicate FOREVER — later runs execute the RULE, not the literal the
    session used — so a plan citing `gross_earnings` for `register_type = 'DDUCT'` would ship a
    blueprint that computes deductions and calls them earnings, silently, in every future run.
    Nothing downstream re-derives that. CAN'T-VERIFY IS NOT REJECT, deliberately: many catalog
    rules are not a single literal comparison, and rejecting what cannot be parsed would make
    every complex rule un-citable — a far larger loss than the mis-citations it would catch.
    With no `RuleIndex` wired the whole check is inert, for the same reason.
    """
    if rules is None:
        return None
    declared_by = rules.by_id(rule_id)
    if not declared_by:
        return None  # not a catalog id at all — `_validate_roles` owns that decline
    mismatched: CatalogRule | None = None
    for rule in declared_by:
        declared = _declared_predicate(rule.predicate)
        if declared is None:
            return None  # not checkable ⇒ not disproved
        if _same_filter(declared, predicate):
            return None
        mismatched = mismatched or rule
    return mismatched


def _same_filter(declared: LiteralPredicate, predicate: LiteralPredicate) -> bool:
    """Do two literal predicates state the SAME filter?

    Same column, then the comparison itself. `=` and `IN` are compared as MEMBER SETS rather
    than as text, so `IN ('a','b')` and `IN ('b','a')` are one filter and so are `= 'x'` and
    `IN ('x')`. Anything else (`!=`, `>`, `LIKE`, `BETWEEN`) must match operator and value
    exactly — those forms carry no membership semantics to normalize, and loosening them is how
    an inverted filter would slip through. TABLE IS NOT COMPARED: the rule's is a
    `database.table` and the predicate's is usually an alias, so a table test would reject
    correct citations in aliased queries.
    """
    if declared.column.lower() != predicate.column.lower():
        return False
    if declared.operator in _NAMEABLE_OPERATORS and predicate.operator in _NAMEABLE_OPERATORS:
        return set(_literal_values(declared)) == set(_literal_values(predicate))
    return declared.operator == predicate.operator and declared.value == predicate.value


@lru_cache(maxsize=1024)
def _declared_predicate(fragment: str) -> LiteralPredicate | None:
    """`sole_literal_predicate`, memoised on the fragment text.

    Every catalog rule is re-read for every uncovered predicate, so a declining candidate would
    otherwise re-parse the whole catalog several times over. The cache is keyed on the fragment
    and the function is pure, so the only thing it can go stale against is a rule whose text
    changed under a running process — which arrives as a new `RuleIndex` built at startup.
    """
    return sole_literal_predicate(fragment)


def _literal_values(predicate: LiteralPredicate) -> tuple[str, ...]:
    """The individual literals a predicate constrains its column to.

    `LiteralPredicate.value` joins an `IN` list's members with "," — the enumerator's shape,
    which the totality COVERAGE check compares against, so it is not changed here. Splitting it
    back is exact for every literal that does not itself contain a comma; one that does yields
    fragments matching no rule, so the predicate is reported with no rule attached — a missed
    hint, never a wrong one.
    """
    if predicate.operator == "IN":
        return tuple(value for value in predicate.value.split(",") if value)
    return (predicate.value,)


def _singular(token: str) -> str:
    """A crude plural fold, applied to BOTH sides of every comparison.

    `active_employees` and `active_employee` are the same rule written twice; without this they
    score 1/3 and the correction never happens. Words ending in `ss`/`us`/`is` are left alone,
    and a token this mangles (`taxes` → `taxe`) still matches its own kind while simply failing
    to match `tax` — a missed hint (terminal, safe), never a wrong one.
    """
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token
