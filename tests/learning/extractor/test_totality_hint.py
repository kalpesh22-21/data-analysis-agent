"""The hinted `totality_violation`: the plan left a predicate of the accepted SQL
unaccounted for, and the decline now says WHICH — and what the catalog calls it.

**The failure.** A live session asked for the highest ratio of deductions to earnings.
The judge ruled the blueprint novel and wanted it (0.76 against a 0.9 bar); extraction
declined `totality_violation: predicate register_type='DDUCT,EARN' has no
parameterization entry`, both corrective turns having already gone on unrelated shape
fixes; candidate_count 0. The catalog declares both of those values — `employee_deductions`
and `gross_earnings` — and nobody told the model.

**What these tests hold in place.** The decline enumerates EVERY uncovered predicate (a
correction that named one at a time would need a round per predicate and the budget is
two for the whole extraction); it names a catalog rule only when the rule IS that filter,
never when it merely mentions the column; it offers the three legal coverage roles and
nothing else; and when the budget is already spent the decline still carries all of that
for the human who reads it, which is the live case exactly.

`unrewritable_sql` is the control: it consults the same accepted SQL and stays terminal,
because a parser that failed has nothing to name.

**And the landing gate that makes the hints safe to give** (bottom section). Everything
above is advice; `rule_contradicts_predicate` is the check with teeth. A `rule`-role
entry that lands puts a catalog rule's name on a predicate forever — later runs execute
the RULE, not the literal — so a plan citing `gross_earnings` for `register_type =
'DDUCT'` would ship a blueprint that computes deductions and calls them earnings. That
now declines, correctably, with both halves of the disagreement printed. It catches a
hint that was wrong, a hint that was forged, and a plan that was simply mistaken with no
hint involved at all. What it never does is reject what it cannot verify.
"""

from __future__ import annotations

from data_agent.learning.extractor.grounding import (
    known_rule_ids_from_catalog,
    rule_index_from_catalog,
)
from data_agent.learning.extractor.models import Decline, ExtractedCandidate
from data_agent.learning.extractor.rule_match import rules_for_predicate
from data_agent.learning.extractor.sql_predicates import literal_predicates
from data_agent.learning.extractor.validation import (
    REASON_BAD_ROLE,
    REASON_TOTALITY,
    REASON_UNREWRITABLE,
    to_candidate,
)
from tests._catalog_fixture import fixture_catalog

from .helpers import (
    KEEP_VERDICT,
    blueprint_raw,
    make_extractor,
    make_summary,
    make_tool_call,
    scripted_turn,
)

# The live query: a per-employee deductions-to-earnings ratio. Three literal predicates
# — the IN list in the WHERE and one inside each `sumIf` — which is over-enumeration in
# the documented safe direction (`sql_predicates`), and makes this the case that proves
# the correction lists all of them rather than the first.
_RATIO_SQL = (
    "SELECT p.employee_code, "
    "sumIf(p.amount, p.register_type = 'DDUCT') / sumIf(p.amount, p.register_type = 'EARN') "
    "AS ratio "
    "FROM dbpcm_warehouse.payroll AS p "
    "WHERE p.register_type IN ('DDUCT','EARN') "
    "GROUP BY p.employee_code"
)
_PAYROLL = "dbpcm_warehouse.payroll"

_CATALOG = fixture_catalog()
_KNOWN = known_rule_ids_from_catalog(_CATALOG)
_INDEX = rule_index_from_catalog(_CATALOG)


def _summary(sql: str = _RATIO_SQL):
    return make_summary(tool_calls=(make_tool_call(ref="tc1", sql=sql),))


def _entry(column: str, value: str, **rest) -> dict:
    return {"locator": {"table": _PAYROLL, "column": column, "value": value}, **rest}


def _rule(column: str, value: str, rule_id: str) -> dict:
    return _entry(column, value, role="rule", rule_id=rule_id)


def _inline(column: str, value: str, why: str = "the ratio is defined over these two "
            "register types") -> dict:
    return _entry(column, value, role="inline", why=why)


def _candidate(parameterization: list[dict], **kwargs) -> dict:
    return blueprint_raw(
        intent="ratio of deductions to earnings per employee",
        parameterization=parameterization,
        source_refs=("tc1",),
        **kwargs,
    )


_UNCOVERED = _candidate([])  # the live emit: no entry for any of the three predicates
_COVERED = _candidate(
    [
        _inline("register_type", "DDUCT,EARN"),
        _rule("register_type", "DDUCT", "employee_deductions"),
        _rule("register_type", "EARN", "gross_earnings"),
    ]
)


def _validate(raw: dict, *, sql: str = _RATIO_SQL, index=_INDEX):
    return to_candidate(raw, _summary(sql), known_rules=_KNOWN, rule_index=index)


def _predicate(sql: str, index: int = 0):
    predicates = literal_predicates(sql)
    assert predicates is not None
    return predicates[index]


# --- the predicate → rule matcher --------------------------------------------------


def test_an_in_list_names_the_catalog_rule_for_each_member() -> None:
    """THE live predicate. One `IN` of two values, two catalog rules, one match each —
    and the match carries the VALUE it accounts for, because "the catalog declares
    employee_deductions" is only useful next to the literal it declares."""
    matches = rules_for_predicate(_predicate(_RATIO_SQL, 0), _INDEX)
    assert [(m.value, m.rule.id) for m in matches] == [
        ("DDUCT", "employee_deductions"),
        ("EARN", "gross_earnings"),
    ]
    assert all(m.rule.table == _PAYROLL for m in matches)


def test_a_single_equality_names_the_one_rule_that_is_that_filter() -> None:
    matches = rules_for_predicate(_predicate("SELECT x FROM t WHERE register_type = 'EARN'"), _INDEX)
    assert [(m.value, m.rule.id) for m in matches] == [("EARN", "gross_earnings")]


def test_a_predicate_no_rule_declares_returns_nothing() -> None:
    """The common case, and not a failure: the plan covers it with a slot or an inline
    instead, and the catalog may be owed a rule (§7)."""
    assert rules_for_predicate(
        _predicate("SELECT x FROM t WHERE department_code = '0420'"), _INDEX
    ) == ()


def test_one_query_of_mixed_predicates_is_matched_per_predicate() -> None:
    """Three predicates, three independent answers — the shape the decline message is
    built from."""
    sql = (
        "SELECT x FROM dbpcm_warehouse.payroll "
        "WHERE register_type = 'EARN' AND department_code = '0420' "
        "AND register_type IN ('DDUCT','EARN')"
    )
    predicates = literal_predicates(sql)
    assert predicates is not None
    named = {
        (pred.column, pred.value): [m.rule.id for m in rules_for_predicate(pred, _INDEX)]
        for pred in predicates
    }
    assert named == {
        ("register_type", "EARN"): ["gross_earnings"],
        ("department_code", "0420"): [],
        ("register_type", "DDUCT,EARN"): ["employee_deductions", "gross_earnings"],
    }


def test_an_inverted_predicate_is_never_given_a_rule() -> None:
    """`register_type != 'EARN'` selects everything the earnings rule excludes. Naming
    `gross_earnings` there would hand the model a rule that inverts its own filter — the
    D56 wrong-answer class. The operator is compared, never assumed."""
    for sql in (
        "SELECT x FROM t WHERE register_type != 'EARN'",
        "SELECT x FROM t WHERE register_type LIKE 'EARN'",
        "SELECT x FROM t WHERE amount > 'EARN'",
    ):
        assert rules_for_predicate(_predicate(sql), _INDEX) == ()


def test_a_rule_that_merely_contains_the_predicate_is_not_a_match() -> None:
    """`exclude_not_hired_default` is `employee_status != 'N' OR employee_status IS
    NULL`. A walk over that fragment finds an `employee_status`/`N` comparison, so a
    looser reader would offer it for `employee_status = 'N'` — a rule that is not that
    filter, and whose OR half nothing would tell the model about. The match is on the
    parse ROOT for exactly this reason."""
    matches = rules_for_predicate(
        _predicate("SELECT x FROM dbpcm_warehouse.employee WHERE employee_status = 'N'"), _INDEX
    )
    assert [m.rule.id for m in matches] == []


def test_a_qualifier_the_catalog_knows_scopes_the_search_and_an_alias_does_not() -> None:
    """The qualifier is whatever prefixed the column: a table NAME when the FROM is
    unaliased, an alias otherwise. A name the catalog knows narrows the candidates —
    here to `employee`, which declares no register-type rule, so nothing is offered. An
    alias (`p`, the live query's) resolves to no catalog table, and the search falls back
    to every table rather than silently to none."""
    scoped = "SELECT x FROM dbpcm_warehouse.employee WHERE employee.register_type = 'EARN'"
    assert rules_for_predicate(_predicate(scoped), _INDEX) == ()

    aliased = "SELECT x FROM dbpcm_warehouse.payroll AS p WHERE p.register_type = 'EARN'"
    assert [m.rule.id for m in rules_for_predicate(_predicate(aliased), _INDEX)] == [
        "gross_earnings"
    ]


def test_with_no_index_nothing_is_named() -> None:
    assert rules_for_predicate(_predicate(_RATIO_SQL, 0), None) == ()


# --- the decline ------------------------------------------------------------------


def test_the_live_case_declines_correctable_and_names_every_predicate_and_rule() -> None:
    """THE case. Same decline, same reason code, same human outcome if nothing else
    happens — but now the message is a checklist the model can act on."""
    out = _validate(_UNCOVERED)

    assert isinstance(out, Decline)
    assert out.reason == REASON_TOTALITY
    assert out.correctable is True
    assert "no entry for 3 literal predicate(s)" in out.detail
    assert "register_type IN ('DDUCT', 'EARN')" in out.detail
    assert "register_type = 'DDUCT'" in out.detail
    assert "register_type = 'EARN'" in out.detail
    assert "'DDUCT' as rule 'employee_deductions' on dbpcm_warehouse.payroll" in out.detail
    assert "'EARN' as rule 'gross_earnings' on dbpcm_warehouse.payroll" in out.detail
    # The three legal ways to cover a predicate, and no fourth.
    assert "role 'rule' with rule_id" in out.detail
    assert "role 'slot' with name/type/binds_to" in out.detail
    assert "role 'inline' with a 'why'" in out.detail
    assert "Change no predicate" in out.detail


def test_a_predicate_the_catalog_does_not_declare_is_named_with_no_rule() -> None:
    out = _validate(
        _candidate([]), sql="SELECT x FROM dbpcm_warehouse.payroll WHERE department_code = '0420'"
    )
    assert isinstance(out, Decline)
    assert "department_code = '0420'" in out.detail
    assert "no catalog rule declares this predicate" in out.detail
    assert "role 'slot' with name/type/binds_to" in out.detail  # still coverable


def test_the_message_survives_a_missing_index_and_simply_names_no_rules() -> None:
    """The index is optional here as it is for the id hint: without it the predicates
    are still named (the checker found them itself), only the catalog half is silent."""
    out = _validate(_UNCOVERED, index=None)
    assert isinstance(out, Decline)
    assert out.correctable is True
    assert "register_type IN ('DDUCT', 'EARN')" in out.detail
    assert "gross_earnings" not in out.detail


def test_a_plan_covering_every_predicate_still_passes() -> None:
    """The no-false-decline control: nothing about the hint changes what is ACCEPTED."""
    assert isinstance(_validate(_COVERED), ExtractedCandidate)


def test_more_uncovered_predicates_than_the_message_lists_are_counted() -> None:
    """A message listing forty predicates is not a checklist. The rest are counted and
    re-named next round if any survive."""
    sql = "SELECT x FROM t WHERE " + " AND ".join(f"c{i} = 'v{i}'" for i in range(9))
    out = _validate(_candidate([]), sql=sql)
    assert isinstance(out, Decline)
    assert "no entry for 9 literal predicate(s)" in out.detail
    assert "and 3 more, not listed here" in out.detail


def test_unparseable_accepted_sql_is_still_terminal() -> None:
    """The control that keeps the line honest. `unrewritable_sql` consults the same
    accepted SQL and stays terminal — not because of what it consults, but because a
    parse that failed can name nothing, so there is no fix to carry."""
    out = _validate(_candidate([]), sql="SELCT (( bad from")
    assert isinstance(out, Decline)
    assert out.reason == REASON_UNREWRITABLE
    assert out.correctable is False


def test_an_inline_entry_still_needs_a_why() -> None:
    """The correction offers `inline` as one of three roles, so the cheapest way to
    satisfy it is an inline entry — which is exactly why the `why` obligation has to
    keep biting. It does: this declines on the role, not on totality."""
    out = _validate(_candidate([_inline("register_type", "DDUCT,EARN", why="")]))
    assert isinstance(out, Decline)
    assert out.reason == REASON_BAD_ROLE
    assert "no 'why'" in out.detail


# --- through the corrective turn ---------------------------------------------------


async def test_the_uncovered_predicates_are_re_asked_and_the_corrected_plan_lands():
    """END TO END on the live session: the emit covers nothing, the feedback names the
    three predicates and the two rules, and the second emit — two rule entries and one
    inline for the IN list — is kept."""
    extractor = make_extractor(
        [scripted_turn([_UNCOVERED]), scripted_turn([_COVERED])],
        known_rules=_KNOWN,
        rule_index=_INDEX,
    )

    result = await extractor.extract(_summary(), KEEP_VERDICT)

    assert extractor._model_client.calls_made == 2
    assert result.corrections == 1
    assert result.declines == ()
    assert len(result.candidates) == 1
    roles = [(p.role, p.rule_id) for p in result.candidates[0].payload.parameterization]
    assert roles == [
        ("inline", None),
        ("rule", "employee_deductions"),
        ("rule", "gross_earnings"),
    ]


async def test_the_correction_frames_the_accepted_sql_as_fixed_and_still_says_omit():
    """What the model is told. Not that its analysis was wrong — that the SQL it was
    given is not up for negotiation and one entry per predicate is owed — and, as with
    every correction, that omitting beats inventing."""
    extractor = make_extractor(
        [scripted_turn([_UNCOVERED]), scripted_turn([])],
        known_rules=_KNOWN,
        rule_index=_INDEX,
    )
    await extractor.extract(_summary(), KEEP_VERDICT)

    correction = [
        m for m in extractor._model_client.calls[1].messages if m.get("role") == "tool"
    ][-1]["content"]
    assert "the accepted SQL is fixed" in correction
    assert "gross_earnings" in correction and "employee_deductions" in correction
    assert "could not be READ" not in correction
    assert "omit" in correction
    assert "Do not change your analysis" in correction


async def test_a_budget_already_spent_on_shape_leaves_the_hint_on_the_decline() -> None:
    """THE LIVE TRACE, reproduced: two corrective turns go on shape fixes, and the
    totality violation surfaces on the third emit with no budget left. The decline is
    final — bounded rounds beat completeness — and it carries the predicates and the
    rule ids, so the human who opens the inbox gets what the model did not."""
    prose_grain = blueprint_raw(
        result_signature={"shape": [], "grain": "one row per employee", "invariants": []}
    )
    extractor = make_extractor(
        [scripted_turn([prose_grain]), scripted_turn([prose_grain]), scripted_turn([_UNCOVERED])],
        known_rules=_KNOWN,
        rule_index=_INDEX,
    )

    result = await extractor.extract(_summary(), KEEP_VERDICT)

    assert extractor._model_client.calls_made == 3  # 1 + max_shape_corrections, not more
    assert result.candidates == ()
    assert [d.reason for d in result.declines] == [REASON_TOTALITY]
    decline = result.declines[0]
    assert decline.corrections_attempted == 2  # it was asked — about something else
    assert "'EARN' as rule 'gross_earnings' on dbpcm_warehouse.payroll" in decline.detail


# --- the landing gate: does the cited rule actually declare this predicate? ---------


def _cited(rule_id: str, value: str, *, column: str = "register_type") -> dict:
    """A one-entry plan whose `rule`-role entry claims the predicate `column = value`."""
    return _candidate([_rule(column, value, rule_id)])


def _sql(predicate: str) -> str:
    return f"SELECT sum(p.amount) AS total FROM dbpcm_warehouse.payroll AS p WHERE {predicate}"


def test_a_real_rule_cited_for_the_wrong_predicate_declines_with_both_halves_named():
    """THE STEERING CASE, and the reason the hints are safe to give at all. `EARN` and
    `DDUCT` are one column apart and opposite in meaning; a plan that cites
    `gross_earnings` for the deductions filter would land a blueprint that computes
    deductions and calls them earnings in every future run, because the rule — not the
    literal — is what later runs execute. Coverage alone said this plan was complete."""
    out = _validate(_cited("gross_earnings", "DDUCT"), sql=_sql("p.register_type = 'DDUCT'"))

    assert isinstance(out, Decline)
    assert out.reason == "rule_predicate_mismatch"
    assert out.correctable is True
    assert "'gross_earnings'" in out.detail
    assert "register_type = 'EARN'" in out.detail  # what the catalog declares
    assert "register_type = 'DDUCT'" in out.detail  # what the entry covers
    assert "Cite the rule that declares THIS predicate" in out.detail


def test_the_right_rule_for_the_right_predicate_passes() -> None:
    """The control. The check may not cost a correct citation anything."""
    assert isinstance(
        _validate(_cited("gross_earnings", "EARN"), sql=_sql("p.register_type = 'EARN'")),
        ExtractedCandidate,
    )


def test_an_inverted_predicate_does_not_correspond_to_the_positive_rule() -> None:
    """`register_type != 'EARN'` is covered by the entry (coverage is operator-blind,
    deliberately) and is NOT what `gross_earnings` declares. This is the direction that
    silently inverts a filter."""
    out = _validate(_cited("gross_earnings", "EARN"), sql=_sql("p.register_type != 'EARN'"))
    assert isinstance(out, Decline)
    assert out.reason == "rule_predicate_mismatch"


def test_a_rule_the_catalog_cannot_parse_is_accepted_unchecked() -> None:
    """CAN'T-VERIFY IS NOT REJECT. `exclude_not_hired_default` is `employee_status !=
    'N' OR employee_status IS NULL` — a fragment with no single literal comparison at
    its root, so nothing here can say whether it corresponds. Rejecting it would make
    every complex rule in the catalog un-citable, which is a bigger and more certain
    loss than the mis-citations it would catch."""
    out = _validate(
        _cited("exclude_not_hired_default", "A", column="employee_status"),
        sql="SELECT count(*) AS n FROM dbpcm_warehouse.employee WHERE employee_status = 'A'",
    )
    assert isinstance(out, ExtractedCandidate)


def test_an_in_list_corresponds_whatever_order_it_is_written_in() -> None:
    """Set comparison, not text comparison — otherwise the check would decline correct
    citations for the difference between `IN ('a','b')` and `IN ('b','a')`."""
    sql = (
        "SELECT count(*) AS n FROM dbpcm_warehouse.applicant_tracking_application "
        "WHERE application_status_enum IN ('Knocked Out', 'Rejected')"
    )
    out = _validate(
        _candidate(
            [
                _entry(
                    "application_status_enum",
                    "Knocked Out,Rejected",
                    role="rule",
                    rule_id="rejected_applications",
                )
            ]
        ),
        sql=sql,
    )
    assert isinstance(out, ExtractedCandidate)


def test_a_single_member_in_list_corresponds_to_an_equality_rule() -> None:
    """`= 'EARN'` and `IN ('EARN')` are one filter written two ways; a check that
    declined the pair would be a nuisance rather than a gate."""
    out = _validate(
        _candidate([_rule("register_type", "EARN", "gross_earnings")]),
        sql=_sql("p.register_type IN ('EARN')"),
    )
    assert isinstance(out, ExtractedCandidate)


def test_with_no_index_the_check_is_inert() -> None:
    """The degrade the wiring guarantees: no `RuleIndex`, no correspondence check, and
    the same acceptance the pipeline had before it existed."""
    assert isinstance(
        _validate(
            _cited("gross_earnings", "DDUCT"), sql=_sql("p.register_type = 'DDUCT'"), index=None
        ),
        ExtractedCandidate,
    )


def test_an_id_the_index_does_not_carry_is_not_disproved() -> None:
    """`known_rules` and the index can be built from different snapshots. An id the
    index has never heard of cannot be contradicted by it — `_validate_roles` owns the
    question of whether it exists at all."""
    out = to_candidate(
        _cited("some_newer_rule", "DDUCT"),
        _summary(_sql("p.register_type = 'DDUCT'")),
        known_rules=_KNOWN | {"some_newer_rule"},
        rule_index=_INDEX,
    )
    assert isinstance(out, ExtractedCandidate)


async def test_a_mis_cited_rule_is_re_asked_and_the_corrected_citation_lands() -> None:
    """Correctable, and the correction carries both halves — so the model is not asked
    to search for anything, only to say which of its two statements it meant."""
    sql = _sql("p.register_type = 'DDUCT'")
    extractor = make_extractor(
        [
            scripted_turn([_cited("gross_earnings", "DDUCT")]),
            scripted_turn([_cited("employee_deductions", "DDUCT")]),
        ],
        known_rules=_KNOWN,
        rule_index=_INDEX,
    )

    result = await extractor.extract(_summary(sql), KEEP_VERDICT)

    correction = [
        m for m in extractor._model_client.calls[1].messages if m.get("role") == "tool"
    ][-1]["content"]
    assert "different filters" in correction
    assert "omit" in correction and "Do not change your analysis" in correction
    assert result.corrections == 1
    assert result.declines == ()
    assert [p.rule_id for p in result.candidates[0].payload.parameterization] == [
        "employee_deductions"
    ]


async def test_the_corrected_plan_is_re_validated_in_full() -> None:
    """The corrected candidate takes the ordinary path, so a plan that covers the
    predicates and breaks something else declines on that instead of being waved
    through."""
    extractor = make_extractor(
        [scripted_turn([_UNCOVERED]), scripted_turn([_candidate(_COVERED["payload"]
                                                                ["parameterization"],
                                                                evidence=[])])],
        known_rules=_KNOWN,
        rule_index=_INDEX,
    )

    result = await extractor.extract(_summary(), KEEP_VERDICT)

    assert result.candidates == ()
    assert [d.reason for d in result.declines] == ["no_evidence"]
