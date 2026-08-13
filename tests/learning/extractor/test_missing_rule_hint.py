"""The hinted `missing_rule` decline: a rule-role plan whose only defect is the LABEL.

**The failure.** A live extraction proposed a correct payroll blueprint and cited the
rule `earnings_only` — an id it had been SHOWN on a prior-art card, because the
blueprint corpus's `uses_rules` namespace had drifted from the catalog, which calls that
same concept `gross_earnings`. `missing_rule` is terminal, so the whole proposal was
thrown away over a name. The session had already demonstrated that a corrective turn
works (it spent one on an expression-level fix); the rule branch simply never used it.

**What these tests hold in place.** The correction fires ONLY when the catalog can name
the fix, the re-ask CARRIES that name, and everything else is unchanged: no hint means
the identical terminal decline, the hint is never an allowlist, the corrected candidate
is re-validated from scratch, and the corrective budget is the one that already existed.

Layer 1 — `to_candidate` directly for the validation half, a `ScriptedModelClient` for
the pipeline half. A scripted model that "takes the hint" proves the plumbing carries
it, not that a real model reads it; that evidence only comes from a live run.
"""

from __future__ import annotations

from data_agent.learning.consumer import _decline_details
from data_agent.learning.extractor.grounding import (
    known_rule_ids_from_catalog,
    rule_index_from_catalog,
)
from data_agent.learning.extractor.models import Decline, ExtractedCandidate
from data_agent.learning.extractor.validation import (
    REASON_MISSING_RULE,
    REASON_MISSING_RULE_HINTED,
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

# The live session's query, on the real warehouse coordinates: earnings rows for one
# department. Two literal predicates — the register-type filter the catalog has a rule
# for, and the department the blueprint parameterizes.
_EARNINGS_SQL = (
    "SELECT sum(p.amount) AS total_earnings "
    "FROM dbpcm_warehouse.payroll AS p "
    "WHERE p.register_type = 'EARN' AND p.department_code = '0420'"
)
_PAYROLL = "dbpcm_warehouse.payroll"

_CATALOG = fixture_catalog()
_KNOWN = known_rule_ids_from_catalog(_CATALOG)
_INDEX = rule_index_from_catalog(_CATALOG)


def _summary():
    return make_summary(tool_calls=(make_tool_call(ref="tc1", sql=_EARNINGS_SQL),))


def _candidate(rule_id: str, **kwargs) -> dict:
    """The live proposal, with the rule-role entry citing *rule_id*."""
    return blueprint_raw(
        intent="total earnings for a department",
        parameterization=[
            {
                "locator": {"table": _PAYROLL, "column": "register_type", "value": "EARN"},
                "role": "rule",
                "rule_id": rule_id,
            },
            {
                "locator": {"table": _PAYROLL, "column": "department_code", "value": "0420"},
                "role": "slot",
                "slot": {
                    "name": "department",
                    "type": "entity",
                    "binds_to": f"{_PAYROLL}.department_code",
                    "required": True,
                },
            },
        ],
        source_refs=("tc1",),
        **kwargs,
    )


def _validate(raw: dict, *, index=_INDEX, known=_KNOWN):
    return to_candidate(raw, _summary(), known_rules=known, rule_index=index)


# --- the two paths through the decline ---------------------------------------------


def test_the_live_case_declines_correctable_and_the_message_carries_the_catalog_id() -> None:
    """THE case. The decline still happens — nothing is accepted that was not before —
    but it now names the fix, so the corrective turn has something to say that is not
    "go and pick a valid id"."""
    out = _validate(_candidate("earnings_only"))

    assert isinstance(out, Decline)
    assert out.reason == REASON_MISSING_RULE_HINTED
    assert out.correctable is True
    assert "candidate.payload.parameterization[0].rule_id" in out.detail
    assert "'earnings_only'" in out.detail
    assert "'gross_earnings'" in out.detail
    # It states what the catalog calls the concept; it never asserts the plan implements
    # it, and it never invites a search for an id that passes.
    assert "if your plan implements that rule" in out.detail


def test_an_unknown_rule_the_catalog_cannot_name_is_terminal_and_unchanged() -> None:
    """`taxes_only` is as close to `employee_taxes` as to `employer_taxes`, so the
    matcher refuses to choose — and the decline is the one this module shipped with,
    field for field, including the §7 reason code a human acts on."""
    out = _validate(_candidate("taxes_only"))
    assert out == Decline("blueprint", REASON_MISSING_RULE, "unknown rule 'taxes_only'")


def test_with_no_rule_index_the_live_case_declines_exactly_as_it_did_before() -> None:
    """The guardrail stated as an equality: the feature is OFF unless an index is wired,
    and OFF means byte-identical, not merely similar."""
    out = _validate(_candidate("earnings_only"), index=None)
    assert out == Decline("blueprint", REASON_MISSING_RULE, "unknown rule 'earnings_only'")


def test_citing_the_hinted_id_passes_every_gate() -> None:
    """The hint has to be worth acting on: a candidate identical but for the id lands,
    so the corrective turn is a real route out of the decline rather than a politeness."""
    out = _validate(_candidate("gross_earnings"))
    assert isinstance(out, ExtractedCandidate)
    rule_plan = next(p for p in out.payload.parameterization if p.role == "rule")
    assert rule_plan.rule_id == "gross_earnings"


def test_the_hint_is_not_an_allowlist_and_a_different_unknown_id_still_declines() -> None:
    """A corrected candidate is validated against the CATALOG exactly as the first one
    was. Nothing is remembered about what was hinted, so a model that answers with some
    other invented id gains nothing at all."""
    assert isinstance(_validate(_candidate("zebra_quotient")), Decline)
    second = _validate(_candidate("deductions_only"))
    assert isinstance(second, Decline)  # hinted in its turn — accepted, never


def test_the_decline_detail_reaches_the_extract_span_under_its_own_reason_code() -> None:
    """Telemetry plumbing: the verbose span renders `reason: detail` for whatever the
    extractor declined, so the hinted variant is distinguishable from the §7 one in the
    place operators actually look — without either code changing meaning."""
    rendered = _decline_details(
        (
            _validate(_candidate("earnings_only")),  # type: ignore[arg-type]
            _validate(_candidate("taxes_only")),  # type: ignore[arg-type]
        )
    )
    assert rendered is not None
    assert "missing_rule_hinted: candidate.payload.parameterization[0].rule_id" in rendered
    assert "missing_rule: unknown rule 'taxes_only'" in rendered


# --- through the corrective turn ---------------------------------------------------


async def test_the_extractor_re_asks_with_the_hint_and_the_corrected_candidate_lands():
    """END TO END, the whole point of the slice: emit cites the drifted alias, the
    feedback carries the catalog id, the second emit cites it, and the candidate the
    pipeline would otherwise have thrown away is kept."""
    extractor = make_extractor(
        [scripted_turn([_candidate("earnings_only")]), scripted_turn([_candidate("gross_earnings")])],
        known_rules=_KNOWN,
        rule_index=_INDEX,
    )

    result = await extractor.extract(_summary(), KEEP_VERDICT)

    assert extractor._model_client.calls_made == 2
    assert result.corrections == 1
    assert result.declines == ()
    assert len(result.candidates) == 1
    payload = result.candidates[0].payload
    assert [p.rule_id for p in payload.parameterization if p.role == "rule"] == ["gross_earnings"]


async def test_the_correction_names_the_catalog_id_and_still_offers_the_exit() -> None:
    """What the model is actually told. It is not told its candidate "could not be
    READ" — that would be false of a candidate that parsed perfectly — and it is told,
    as every correction is, that omitting beats inventing."""
    extractor = make_extractor(
        [scripted_turn([_candidate("earnings_only")]), scripted_turn([])],
        known_rules=_KNOWN,
        rule_index=_INDEX,
    )
    await extractor.extract(_summary(), KEEP_VERDICT)

    correction = [
        m for m in extractor._model_client.calls[1].messages if m.get("role") == "tool"
    ][-1]["content"]
    assert "gross_earnings" in correction
    assert "earnings_only" in correction
    assert "could not be READ" not in correction
    assert "over the catalog rule cited for a predicate" in correction
    assert "does not declare" in correction  # from the decline's own line
    assert "omit" in correction
    assert "Do not change your analysis" in correction


async def test_the_corrected_candidate_is_re_validated_in_full() -> None:
    """The re-emit takes the ordinary path — `_validate_batch` → `to_candidate` — so
    every gate runs again on the new candidate, not just the one that complained. Here
    the model fixes the rule id and drops its evidence doing it; the D31 guard catches
    that, terminally."""
    extractor = make_extractor(
        [
            scripted_turn([_candidate("earnings_only")]),
            scripted_turn([_candidate("gross_earnings", evidence=[])]),
        ],
        known_rules=_KNOWN,
        rule_index=_INDEX,
    )

    result = await extractor.extract(_summary(), KEEP_VERDICT)

    assert result.candidates == ()
    assert [d.reason for d in result.declines] == ["no_evidence"]


async def test_a_model_that_ignores_the_hint_spends_the_existing_budget_and_no_more():
    """The bound. The hinted decline shares `max_shape_corrections` with the shape
    family rather than adding a budget of its own, so a model that keeps citing the same
    alias costs 1 emit + 2 corrections and then the decline stands — with the record
    showing it was asked, which is what separates a stubborn model from a disabled
    budget."""
    extractor = make_extractor(
        [scripted_turn([_candidate("earnings_only")]) for _ in range(3)],
        known_rules=_KNOWN,
        rule_index=_INDEX,
    )

    result = await extractor.extract(_summary(), KEEP_VERDICT)

    assert extractor._model_client.calls_made == 3  # 1 + max_shape_corrections
    assert result.candidates == ()
    assert len(result.declines) == 1
    assert result.declines[0].reason == REASON_MISSING_RULE_HINTED
    assert result.declines[0].corrections_attempted == 2


async def test_without_an_index_the_extractor_spends_one_turn_and_never_re_asks() -> None:
    """The unhinted path end to end. The script holds ONE turn, so a second provider
    round-trip would raise out of `ScriptedModelClient` — this asserts the terminal
    decline by construction rather than by counting."""
    extractor = make_extractor(
        [scripted_turn([_candidate("earnings_only")])], known_rules=_KNOWN
    )

    result = await extractor.extract(_summary(), KEEP_VERDICT)

    assert extractor._model_client.calls_made == 1
    assert result.corrections == 0
    assert [d.reason for d in result.declines] == [REASON_MISSING_RULE]
    assert result.declines[0].correctable is False
    assert result.declines[0].corrections_attempted == 0


async def test_a_shape_decline_and_a_rule_hint_share_one_corrective_turn() -> None:
    """One correction per BATCH, whatever mix of correctable families it holds — and a
    header that is true of both, since a batch containing a candidate that parsed fine
    cannot be told that everything in it was unreadable."""
    unreadable = blueprint_raw(
        result_signature={
            "shape": [{"column": "org_unit", "type": "String"}],
            "grain": "one row per organizational unit",
            "invariants": [],
        }
    )
    extractor = make_extractor(
        [
            scripted_turn([unreadable, _candidate("earnings_only")]),
            scripted_turn([_candidate("gross_earnings")]),
        ],
        known_rules=_KNOWN,
        rule_index=_INDEX,
    )

    result = await extractor.extract(_summary(), KEEP_VERDICT)

    correction = [
        m for m in extractor._model_client.calls[1].messages if m.get("role") == "tool"
    ][-1]["content"]
    assert "candidate 1" in correction and "candidate 2" in correction
    assert "result_signature.grain" in correction
    assert "gross_earnings" in correction
    assert "each for the one reason its line gives" in correction
    assert "could not be READ" not in correction
    assert result.corrections == 1
    assert len(result.candidates) == 1
