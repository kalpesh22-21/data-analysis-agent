"""The deterministic catalog-rule matchers behind the two hinted declines.

Two functions, two failures, one rule about what a hint may be:

  * `nearest_known_rule` — the plan cited a rule id that does not exist. Matches on the
    IDENTIFIER, and answers "which rule was this trying to name?"
  * `rules_for_predicate` — a literal predicate of the accepted SQL has no
    parameterization entry at all. Matches on the PREDICATE, and answers "does the
    catalog already declare exactly this filter?"

Neither is a similarity engine for the pipeline to form opinions with. Each exists so a
decline that would otherwise say only "no" can say what the catalog calls the thing the
candidate is talking about — and each refuses, everywhere it is not certain, because the
cost of a refusal is a decline that a human reads and the cost of a wrong hint is a
model talked into a candidate that silently means something else.

--------------------------------------------------------------------------------------
`nearest_known_rule` — the ONE thing that can make a `missing_rule` decline correctable
(`validation.py::_validate_roles`).

**The case this exists for.** A live extraction cited the rule `earnings_only` on
`dbpcm_warehouse.payroll`. No such rule exists; the catalog names that exact concept
`gross_earnings` (`predicate: register_type = 'EARN'`). The id came from a PRIOR ART
card — the blueprint corpus's `uses_rules` namespace had drifted from the catalog's —
so the model was quoting something it had been SHOWN. The proposal was correct in every
other respect and the decline threw it away, because a `missing_rule` decline is
terminal: the whole point of that decline is to tell a HUMAN that a rule is missing, and
re-asking a model to "cite an existing rule" is pressure to name any id that passes.

**What changes, and what emphatically does not.** This module answers one question and
answers it deterministically: *is there exactly one catalog rule the cited id can only
have meant?* When there is, the decline becomes correctable and the re-ask CARRIES that
id, so the model is never asked to go and choose one — the fix is handed to it and its
only decision is whether the fix is true of its plan. When there is not — no candidate,
a weak candidate, or two plausible ones — this returns `None` and the decline is
terminal, byte-identical to the behaviour before this module existed. Every threshold
below is set so that the ambiguous case falls on the terminal side.

**Every signal is the candidate's own content plus the catalog.** Nothing here reads the
model's prose, its rationale, or the conversation. The evidence is: the id the plan
cited, the `locator` the plan cited it FOR, and what the catalog declares. That is what
makes the hint a fix rather than a suggestion, and it is why a hint can be put in front
of the model without the pipeline having formed an opinion of its own.

**Why the score is over the ID and not over the rule's prose.** `applies_when` and
`description` are English sentences about the subject matter; scoring them would find
rules that are ABOUT the same area, and "about the same area" is a judgement — exactly
the thing a correctable decline may not make. Scoring identifier tokens can only find
rules the model was plausibly trying to NAME, which is a change of expression.

--------------------------------------------------------------------------------------
`rules_for_predicate` — what a `totality_violation` decline can say about the predicate
the plan left uncovered (`validation.py::_validate_totality`).

**The case this exists for.** A live session asked for the highest ratio of deductions
to earnings; the accepted SQL filtered `register_type IN ('DDUCT','EARN')`; the plan
covered neither value, and the decline said so and stopped. The catalog declares both
of them, as `employee_deductions` and `gross_earnings`. The model had two corrective
turns left over from unrelated shape fixes and was never told the one thing that would
have finished the candidate.

**No threshold, and no similarity — EXACT structural equality only.** A rule is named
for a predicate iff the rule's whole `predicate` fragment IS that comparison: same
column, same literal, same operator, and nothing else in the fragment (see
`sql_predicates.sole_literal_predicate`, which enforces the "nothing else" part at the
parse root). There is no near-match tier here and there must not be one: naming a rule
for a predicate is the pipeline telling a model what its query's filter MEANS, and the
only version of that which is not a judgement is a literal identity. A predicate no rule
matches is simply reported with no rule attached — which is the §7 signal that the
catalog may be missing one, arriving with a worked example attached.
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

    *known_rules* is authoritative for WHAT EXISTS: nothing outside it is ever returned,
    so a `RuleIndex` built from a different catalog snapshot can narrow the search but
    can never widen it. *rules* is optional — without it the table-scoped tier cannot
    run and only the exact-normalisation and unscoped tiers apply, which is the correct
    degrade for a deployment that grounds ids but not the index.

    THREE TIERS, strongest first, and the first tier that has evidence DECIDES:

      1. EXACT after normalisation — `Gross-Earnings`/`grossEarnings`/`gross_earnings`
         are the same id written three ways. A change of spelling, nothing more.
      2. TABLE-SCOPED — the plan's own locator names a table; the candidates are the
         catalog rules declared on it, narrowed further to those whose predicate
         mentions the plan's column when any do. Scored on shared identifier tokens.
         If the table is known and no candidate wins, the answer is `None`: the plan's
         binding evidence says the rule is on THIS table, so a better-scoring rule
         somewhere else in the catalog is not a spelling fix, it is a different rule.
      3. UNSCOPED — only when there is no usable table evidence at all. Same scoring
         against every known id, at the higher threshold.

    A tie is always `None`. Two plausible catalog counterparts mean the pipeline does
    not know which one the plan implements, and guessing there is precisely the coercion
    the terminal decline exists to prevent."""
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

    `None` covers three different situations on purpose — nothing scored high enough,
    two candidates tied at the top, or the winner shares only short fragments — because
    the caller does the same thing with all three: decline terminally."""
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

    Jaccard rather than "how much of the unknown id is covered", because coverage is
    asymmetric in the dangerous direction — a one-token id like `earnings` would score
    1.0 against every rule containing that word, however much else those rules say."""
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

    Word-bounded so `type_code` does not match `code`, and case-insensitive because
    catalog predicates preserve authored casing (D70) while a model writes whatever it
    read off the SQL. A few catalog `predicate`s are prose rather than SQL; those simply
    never mention a column, which costs nothing — the narrowing is an optional
    refinement of a candidate set that is already scoped to one table."""
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
    `value` is the literal it accounts for — an `IN` list has one match per member, and
    the live case (`register_type IN ('DDUCT','EARN')`) has two."""

    value: str
    rule: CatalogRule


def rules_for_predicate(
    predicate: LiteralPredicate, rules: RuleIndex | None
) -> tuple[PredicateRuleMatch, ...]:
    """Every catalog rule whose predicate IS one of *predicate*'s literal comparisons.

    Exact structural equality, in the strong sense — the rule's whole fragment parses to
    the same (column, literal, operator) and to nothing else. `()` means the catalog
    declares no rule for this filter, which is a legitimate and common answer: the
    caller reports the predicate on its own and the plan covers it with a slot or an
    inline classification instead.

    TABLE SCOPING IS DECIDED BY WHETHER THE QUALIFIER RESOLVES, not by whether it
    yields rules. `LiteralPredicate.table` is whatever qualified the column: a bare
    table name (`payroll`) for an unaliased FROM, an ALIAS (`p`) for the aliased form
    this module cannot resolve without a FROM clause, and "" for an unqualified column.
    So:

      * a qualifier the CATALOG KNOWS is authoritative, including when that table
        declares NO rules — the answer is then "no rule for this predicate", not "look
        somewhere else". This is the same posture `nearest_known_rule` takes, and the
        reason it must be the same is that the two hints appear side by side in one
        message: one of them ranging over the whole catalog while the other refuses to
        leave the plan's table would be an inconsistency a reader has to relearn.
      * a qualifier that resolves to nothing (an alias, or an empty one) is NO
        EVIDENCE, and the search covers every table. That is what makes the live case
        work at all (`FROM dbpcm_warehouse.payroll AS p` yields `p`), and it is honest
        because the caller prints each rule's own table beside its id — the message
        OFFERS named options and never asserts which one the plan means.

    The cross-table risk that widening creates is closed downstream rather than by
    guessing here: `rule_contradicts_predicate` re-checks whatever the model finally
    cites against what the catalog declares, so a rule offered from the wrong table
    still cannot land a candidate whose predicate it does not implement."""
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
    """The cited rule, IF the catalog proves it declares a different filter from
    *predicate*. `None` in every other case — including every case of doubt.

    THE SEAM THIS GUARDS. Everything else in this module is advisory: a hint is offered,
    the model decides, and a wrong hint costs a wasted turn. This is the one check with
    teeth, and it is what makes the advice safe to give. A `rule`-role entry that lands
    puts a catalog rule's name on a blueprint's predicate FOREVER — later runs execute
    the rule, not the literal the session used — so a plan that cites `gross_earnings`
    for `register_type = 'DDUCT'` ships a blueprint that computes deductions and calls
    them earnings, silently, in every future run. Nothing downstream re-derives that:
    S4's `binds_to ⊆ uses` says nothing about rule semantics.

    It closes three failure modes at once, which is why it is worth its weight:

      * a hint that was WRONG. The matchers are deterministic but not omniscient, and a
        cross-table offer from the widened search is exactly the case they cannot rule
        out by themselves.
      * a hint that was FORGED. A literal of the analyst's SQL cannot forge a hint line
        any more (`validation._quoted`), but the model can also simply be wrong, or be
        steered by any other text in a session it was told to read. The rendering rule
        makes authorship visible; this makes the outcome checkable.
      * a plan that was mistaken with no hint involved at all — the case that existed
        before any of this and had no gate.

    CAN'T-VERIFY IS NOT REJECT, and that is a deliberate asymmetry rather than a
    limitation to fix later. Many catalog rules are not a single literal comparison
    (`hours IS NOT NULL`, `employee_status != 'N' OR employee_status IS NULL`,
    `field_id IN ({field_codes})`, a prose note) — `sole_literal_predicate` returns
    `None` for all of them, and this returns `None` too, accepting the citation exactly
    as the pipeline did before this check existed. Rejecting what it cannot parse would
    turn every complex rule in the catalog into an un-citable one, which is a far larger
    and more certain loss than the mis-citations it would catch. With no `RuleIndex`
    wired the whole check is inert, for the same reason."""
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

    Same column, and then the comparison itself. `=` and `IN` are compared as MEMBER
    SETS rather than as text, which is the difference between a check and a nuisance:
    `IN ('Rejected','Knocked Out')` and `IN ('Knocked Out','Rejected')` are one filter,
    and so are `= 'EARN'` and `IN ('EARN')`. Anything else (`!=`, `>`, `LIKE`,
    `BETWEEN`) must match operator and value exactly — those forms carry no membership
    semantics to normalize, and loosening them is how an inverted filter would slip
    through the one check that exists to stop it.

    TABLE IS NOT COMPARED, deliberately. The rule's table is a `database.table` and the
    predicate's is whatever qualified the column — usually an alias — so a table test
    here would reject correct citations in aliased queries, which is most of them. The
    column-plus-filter identity is the part that can be established from what is
    actually in hand."""
    if declared.column.lower() != predicate.column.lower():
        return False
    if declared.operator in _NAMEABLE_OPERATORS and predicate.operator in _NAMEABLE_OPERATORS:
        return set(_literal_values(declared)) == set(_literal_values(predicate))
    return declared.operator == predicate.operator and declared.value == predicate.value


@lru_cache(maxsize=1024)
def _declared_predicate(fragment: str) -> LiteralPredicate | None:
    """`sole_literal_predicate`, memoised on the fragment text.

    Every catalog rule is re-read for every uncovered predicate, so a declining
    candidate would otherwise re-parse the whole catalog several times over. The cache
    is keyed on the fragment and the function is pure, so the only thing it can go stale
    against is a rule whose text changed under a running process — which would mean a
    new catalog, which arrives as a new `RuleIndex` built at startup."""
    return sole_literal_predicate(fragment)


def _literal_values(predicate: LiteralPredicate) -> tuple[str, ...]:
    """The individual literals a predicate constrains its column to.

    `LiteralPredicate.value` joins an `IN` list's members with "," (that is the
    enumerator's shape, and the totality COVERAGE check compares the joined form, so it
    is not changed here). Splitting it back is exact for every literal that does not
    itself contain a comma; one that does yields fragments that match no rule, so the
    predicate is reported with no rule attached — a missed hint, never a wrong one."""
    if predicate.operator == "IN":
        return tuple(value for value in predicate.value.split(",") if value)
    return (predicate.value,)


def _singular(token: str) -> str:
    """A crude plural fold, applied to BOTH sides of every comparison.

    `active_employees` and `active_employee` are the same rule written twice; without
    this they score 1/3 and the correction never happens. Words ending in `ss`/`us`/`is`
    are left alone (`gross`, `status`, `analysis`) — everything else is only ever
    compared against another token that went through the same fold, so a token this
    mangles (`taxes` → `taxe`) still matches its own kind and simply fails to match
    `tax`, which is a missed hint (terminal, safe), never a wrong one."""
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token
