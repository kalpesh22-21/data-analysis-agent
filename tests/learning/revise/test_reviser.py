"""The LLM typing aid for the fail-to-review form (design §C).

THE INVARIANT: the model edits ENTRIES, never the SQL. `sql_template` is DERIVED by AST rewrite
from the query that actually ran, and that derivation is what makes `explain_ok`,
`binds_to_subset_uses` and `read_only_select` mean anything — they check a provenance chain back
to a query the warehouse really answered. A model-authored template would leave all five checks
passing against a different subject.

THE SECOND INVARIANT: it writes nothing. Every test here asserts against a store that would
record a write, because "proposes" and "applies" being the same call is the one refactor that
would quietly move a model inside the validation boundary.

The worked case throughout is the REAL one from `learning-declined-candidate-review.md`: a
session processed three times, kept by triage and the coverage judge every time, whose SQL is
the exact query that answered the user's turn, declined all three times after six corrective
rounds on the same two predicates — one guarding a derived alias (no rule can declare it, no
slot can bind it) and one spanning two catalog rules (no single rule_id can ever validate).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from data_agent.learning.candidate.decline import DeclineBlock, ValidationSnapshot
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.revise import BlueprintReviser, ForbiddenTemplateEditError
from data_agent.learning.revise.schema import REVISE_TOOL_NAME
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"

RATIO_SQL = (
    "SELECT department, sum(deductions) / sum(total_earnings) AS ratio "
    "FROM payroll.payroll_fact WHERE total_earnings != '0' "
    "AND register_type IN ('DDUCT', 'EARN') GROUP BY department ORDER BY ratio DESC"
)

DECLINE_DETAIL = (
    "totality_violation: candidate.payload.parameterization has no entry for 2 literal "
    "predicate(s) of the accepted SQL: total_earnings != '0'; register_type IN ('DDUCT','EARN')"
)


def declined_blueprint(*, with_snapshot: bool = True) -> CandidateEnvelope:
    """A `needs_parameterization` blueprint carrying the decline block AND the snapshot."""
    base = json.loads((FIXTURES / "envelopes_each_reason.json").read_text())[
        "leakage_near_miss"
    ]
    env = CandidateEnvelope.from_doc(base)
    env = replace(
        env,
        status=CandidateStatus.NEEDS_PARAMETERIZATION,
        payload={
            **env.payload,
            "intent": "ratio of deductions to earnings by department",
            "parameterization": [
                {
                    "locator": {
                        "table": "payroll.payroll_fact",
                        "column": "department",
                        "value": "0420",
                    },
                    "role": "slot",
                    "slot": {
                        "name": "department",
                        "type": "entity",
                        "binds_to": "payroll.payroll_fact.department",
                        "required": True,
                    },
                    "why": None,
                }
            ],
        },
        decline=DeclineBlock(
            reason="totality_violation", detail=DECLINE_DETAIL, corrections_attempted=2
        ),
    )
    if not with_snapshot:
        return replace(env, revalidation=None)
    return replace(
        env,
        revalidation=ValidationSnapshot(
            session_id="sess-1",
            user_id="u1",
            trace_id="t1",
            content_hash="h1",
            accepted_signal="no_correction",
            sql_by_ref={"tc1": (RATIO_SQL,)},
        ),
    )


def proposal_turn(*, arguments: Any = None, tool_name: str = REVISE_TOOL_NAME) -> ModelTurnResult:
    if arguments is None:
        arguments = {
            "entries": [
                {
                    "locator": {
                        "table": "payroll.payroll_fact",
                        "column": "total_earnings",
                        "value": "0",
                    },
                    "role": "inline",
                    "why": "denominator guard for the ratio — excludes divide-by-zero rows",
                },
                {
                    "locator": {
                        "table": "payroll.payroll_fact",
                        "column": "register_type",
                        "value": "DDUCT,EARN",
                    },
                    "role": "inline",
                    "why": "spans two catalog rules, so no single rule_id corresponds",
                },
            ],
            "replace": False,
            "rationale": "both predicates can only be inlined; neither can cite a rule",
        }
    return ModelTurnResult(
        tool_calls=[ToolCallRequest(id="r1", name=tool_name, arguments=arguments)]
    )


class ScriptedClient:
    def __init__(self, turns: list[ModelTurnResult]) -> None:
        self._turns = list(turns)
        self.calls: list[tuple[list[dict], list[dict]]] = []

    async def send_turn(self, messages: list[dict], tools: list[dict]) -> ModelTurnResult:
        self.calls.append((messages, tools))
        return self._turns.pop(0) if self._turns else ModelTurnResult(tool_calls=[])


def make_reviser(turns: list[ModelTurnResult], **kw: Any) -> tuple[BlueprintReviser, Any]:
    client = kw.pop("client", None) or ScriptedClient(turns)
    options: dict[str, Any] = {
        "known_rules": frozenset({"gross_earnings", "employee_deductions"}),
        "catalog_schema": {
            "payroll.payroll_fact": {
                "department": "String",
                "total_earnings": "Float64",
                "register_type": "String",
            }
        },
        "timeout_seconds": 5.0,
    }
    options.update(kw)
    return BlueprintReviser(model_client=client, **options), client


# --- the invariant ----------------------------------------------------------


def test_the_tool_offers_no_way_to_write_sql() -> None:
    """The schema is the primary defence; the parse guard is the backstop. A model cannot
    propose a template it was never given a field for."""
    from data_agent.learning.revise import build_revise_tool

    props = build_revise_tool()["parameters"]["properties"]
    assert set(props) == {"entries", "replace", "rationale"}
    entry_props = props["entries"]["items"]["properties"]
    assert "sql" not in entry_props
    assert "sql_template" not in entry_props


@pytest.mark.parametrize(
    "key", ["sql_template", "template", "sql", "canonical_ast_norm"]
)
async def test_a_proposal_carrying_a_template_is_rejected_not_ignored(key: str) -> None:
    """⚠ Rejected, not ignored, and the difference is not pedantic: ignoring it would let the
    model believe it had changed something it had not, and would show the reviewer a rationale
    about a template edit that never happened."""
    reviser, _ = make_reviser(
        [
            proposal_turn(
                arguments={
                    "entries": [{"locator": {"table": "t", "column": "c", "value": "v"},
                                 "role": "inline", "why": "w"}],
                    "rationale": "r",
                    key: "SELECT 1",
                }
            )
        ]
    )
    with pytest.raises(ForbiddenTemplateEditError) as exc:
        await reviser.propose(declined_blueprint(), feedback="fix it")
    assert key in str(exc.value)


async def test_it_writes_nothing_it_only_proposes() -> None:
    """The proposal is applied through the EXISTING complete route, so a model's output faces
    the identical `to_candidate` re-validation a hand-typed array faces. A reviser that wrote
    would be a second write path into a candidate's payload."""
    reviser, _ = make_reviser([proposal_turn()])
    env = declined_blueprint()
    before = json.dumps(env.to_doc(), sort_keys=True)

    proposal = await reviser.propose(env, feedback="inline both")

    assert proposal.has_proposal
    assert json.dumps(env.to_doc(), sort_keys=True) == before
    # ...and the wire body carries no status, because nothing moved.
    assert "status" not in proposal.to_wire()


# --- what the model is shown ------------------------------------------------


async def test_the_brief_carries_the_validator_complaint_and_the_unredacted_sql() -> None:
    """The literals ARE the subject: a model asked to re-role `department = '0420'` while
    shown `[redacted]` has nothing to reason about. Same trust boundary the S3 extractor
    already sits inside, which is why this runs server-side and takes nothing from the browser
    but the feedback string."""
    reviser, client = make_reviser([proposal_turn()])
    await reviser.propose(declined_blueprint(), feedback="inline both predicates")

    brief = client.calls[0][0][-1]["content"]
    assert "totality_violation" in brief
    assert "total_earnings != '0'" in brief
    assert RATIO_SQL in brief
    assert "inline both predicates" in brief


async def test_the_brief_grounds_the_rule_ids_and_the_bindable_columns() -> None:
    """Without them a model invents a plausible `rule_id` or a `binds_to` for a column that
    does not exist, and the re-validation rejects the proposal for a reason the reviewer then
    has to decode. Shown as CLOSED lists so "I was not offered one" is reachable."""
    reviser, client = make_reviser([proposal_turn()])
    await reviser.propose(declined_blueprint(), feedback="")

    brief = client.calls[0][0][-1]["content"]
    assert "gross_earnings" in brief and "employee_deductions" in brief
    assert "payroll.payroll_fact.total_earnings" in brief


async def test_no_catalog_rules_says_so_rather_than_staying_silent() -> None:
    """A silent absence reads as "you just were not shown them"; the explicit sentence makes
    role='rule' visibly unavailable."""
    reviser, client = make_reviser([proposal_turn()], known_rules=frozenset())
    await reviser.propose(declined_blueprint(), feedback="")
    brief = client.calls[0][0][-1]["content"]
    assert "role='rule'" in brief and "not an option" in brief


async def test_an_oversized_feedback_paste_cannot_push_out_the_complaint() -> None:
    """The validator's complaint is the part the proposal must answer. An unbounded paste
    would be the easiest way to lose it."""
    reviser, client = make_reviser([proposal_turn()])
    await reviser.propose(declined_blueprint(), feedback="x" * 50_000)
    brief = client.calls[0][0][-1]["content"]
    assert "totality_violation" in brief
    assert len(brief) < 20_000


# --- the guards -------------------------------------------------------------


async def test_replace_defaults_to_append_on_anything_that_is_not_true() -> None:
    """`replace` selects the DESTRUCTIVE branch. A truthy string ("false"!) must not reach it,
    so anything that is not literally True reads as append — the branch that leaves
    already-valid classifications intact."""
    for raw in ["false", "true", 1, None, "yes"]:
        reviser, _ = make_reviser(
            [
                proposal_turn(
                    arguments={
                        "entries": [
                            {"locator": {"table": "t", "column": "c", "value": "v"},
                             "role": "inline", "why": "w"}
                        ],
                        "replace": raw,
                        "rationale": "r",
                    }
                )
            ]
        )
        proposal = await reviser.propose(declined_blueprint(), feedback="")
        assert proposal.replace is False, raw


async def test_replace_true_is_honoured() -> None:
    reviser, _ = make_reviser(
        [
            proposal_turn(
                arguments={
                    "entries": [
                        {"locator": {"table": "t", "column": "c", "value": "v"},
                         "role": "inline", "why": "w"}
                    ],
                    "replace": True,
                    "rationale": "r",
                }
            )
        ]
    )
    proposal = await reviser.propose(declined_blueprint(), feedback="")
    assert proposal.replace is True


@pytest.mark.parametrize(
    "turn",
    [
        ModelTurnResult(assistant_text="here you go", tool_calls=[]),
        ModelTurnResult(tool_calls=[ToolCallRequest(id="x", name="other", arguments={})]),
        proposal_turn(arguments="not an object"),
        proposal_turn(arguments={"entries": "not a list", "rationale": "r"}),
        proposal_turn(arguments={"entries": [], "rationale": "r"}),
        proposal_turn(arguments={"entries": ["not a dict"], "rationale": "r"}),
    ],
    ids=["free-text", "wrong-tool", "args-not-object", "entries-not-list",
         "entries-empty", "entries-not-objects"],
)
async def test_an_unusable_response_is_no_proposal_not_an_error(turn) -> None:
    """"The assistant had no suggestion" is an ORDINARY outcome — the form the model could not
    fill in is the same form the reviewer is still looking at. It answers 200 with a reason."""
    reviser, _ = make_reviser([turn])
    proposal = await reviser.propose(declined_blueprint(), feedback="")
    assert not proposal.has_proposal
    assert proposal.reason


async def test_a_candidate_with_no_snapshot_says_why_rather_than_raising() -> None:
    """The same precondition `ParameterizationCompleter.complete` opens with. An `in_review`
    candidate has no `ValidationSnapshot` at all — it is written at DECLINE time only — which
    is the structural reason §C.4 scopes the first cut to this one queue."""
    reviser, client = make_reviser([proposal_turn()])
    proposal = await reviser.propose(declined_blueprint(with_snapshot=False), feedback="x")
    assert not proposal.has_proposal
    assert "no re-validation snapshot" in proposal.reason
    assert client.calls == []


async def test_a_provider_explosion_is_no_proposal_not_a_500() -> None:
    """An optional convenience must not be able to take down the surface it is attached to."""

    class Boom:
        async def send_turn(self, messages, tools):
            raise RuntimeError("provider exploded")

    reviser, _ = make_reviser([], client=Boom())
    proposal = await reviser.propose(declined_blueprint(), feedback="")
    assert not proposal.has_proposal
    assert "could not be reached" in proposal.reason


async def test_a_timeout_is_no_proposal() -> None:
    class Slow:
        async def send_turn(self, messages, tools):
            await asyncio.sleep(10)

    reviser = BlueprintReviser(model_client=Slow(), timeout_seconds=0.01)
    proposal = await reviser.propose(declined_blueprint(), feedback="")
    assert not proposal.has_proposal
    assert "timed out" in proposal.reason


async def test_cancellation_still_propagates() -> None:
    class Cancelled:
        async def send_turn(self, messages, tools):
            raise asyncio.CancelledError()

    reviser, _ = make_reviser([], client=Cancelled())
    with pytest.raises(asyncio.CancelledError):
        await reviser.propose(declined_blueprint(), feedback="")


async def test_an_absurd_number_of_entries_is_capped() -> None:
    entry = {"locator": {"table": "t", "column": "c", "value": "v"}, "role": "inline", "why": "w"}
    reviser, _ = make_reviser(
        [proposal_turn(arguments={"entries": [entry] * 500, "rationale": "r"})]
    )
    proposal = await reviser.propose(declined_blueprint(), feedback="")
    assert 0 < len(proposal.entries) <= 64


# --- live-model transcription (found by running the real stack, 2026-08-28) -------


async def test_placeholder_objects_are_dropped_not_forwarded() -> None:
    """⚠ A MODEL EMITS EVERY DECLARED PROPERTY.

    Given a schema with `slot`, `rule_id` and `why`, gpt-5.5 filled all three on a
    `role="inline"` entry — `slot: {"name": "", "type": "", "binds_to": ""}`. Downstream that
    is WORSE than absent: `to_candidate` reads a present `slot` as a declaration and declines
    for a field the model never meant to set. Observed on the first real revise of the first
    real declined candidate, and invisible to every scripted test because the fixtures were
    hand-written the way a human would write them.
    """
    reviser, _ = make_reviser(
        [
            proposal_turn(
                arguments={
                    "entries": [
                        {
                            "locator": {"table": "t", "column": "employee_status", "value": "N"},
                            "role": "inline",
                            "slot": {"name": "", "type": "", "binds_to": ""},
                            "rule_id": "",
                            "why": "defines the measured population",
                        }
                    ],
                    "rationale": "r",
                }
            )
        ]
    )
    proposal = await reviser.propose(declined_blueprint(), feedback="")
    entry = proposal.entries[0]
    assert "slot" not in entry, "an empty slot object reads as a slot declaration downstream"
    assert "rule_id" not in entry
    assert entry["why"] == "defines the measured population"


async def test_a_locator_value_keeps_its_sql_quotes_off() -> None:
    """The predicate is `employee_status != 'N'`; the totality walk matches on the BARE literal,
    and the extractor's own entries carry `A`, never `'A'`. A model copying the value straight
    out of the SQL brings the quotes with it, and the form then comes back declined naming the
    predicate the reviewer just classified — the least actionable failure this path has."""
    reviser, _ = make_reviser(
        [
            proposal_turn(
                arguments={
                    "entries": [
                        {
                            "locator": {"table": "t", "column": "s", "value": "'N'"},
                            "role": "inline",
                            "why": "w",
                        }
                    ],
                    "rationale": "r",
                }
            )
        ]
    )
    proposal = await reviser.propose(declined_blueprint(), feedback="")
    assert proposal.entries[0]["locator"]["value"] == "N"


async def test_an_unquoted_or_oddly_quoted_value_is_left_alone() -> None:
    """Only a MATCHED surrounding pair is stripped: an apostrophe inside a name is content, and
    a value that merely starts with a quote is not a quoted literal."""
    for raw, expected in [("N", "N"), ("O'Brien", "O'Brien"), ("'N", "'N"), ("", "")]:
        reviser, _ = make_reviser(
            [
                proposal_turn(
                    arguments={
                        "entries": [
                            {"locator": {"table": "t", "column": "c", "value": raw},
                             "role": "inline", "why": "w"}
                        ],
                        "rationale": "r",
                    }
                )
            ]
        )
        proposal = await reviser.propose(declined_blueprint(), feedback="")
        assert proposal.entries[0]["locator"]["value"] == expected, raw


# --- the leakage refusal's MESSAGE (found by a reviewer hitting it live) ----------


async def _refusal_reason(status: str, hits: list[dict]) -> str:
    from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
    from data_agent.learning.candidate.verdicts import EntityHit, LeakageVerdict
    from data_agent.learning.inbox import ReviewInbox

    env = replace(
        declined_blueprint(),
        status=status,
        entity_scan=LeakageVerdict(
            result="quarantine",
            hits=tuple(EntityHit(**h) for h in hits),
            scanned_fields=("generalization.sql_template",),
            scanner="regex+ner",
        ).to_doc(),
    )
    store = InMemoryCandidateStore()
    await store.put(env)
    reviser, _ = make_reviser([proposal_turn()])
    inbox = ReviewInbox(store, reviser=reviser)
    proposal = await inbox.propose_revision(env.candidate_id, feedback="x")
    return proposal.reason


async def test_the_refusal_names_the_flagged_field_and_kind_never_the_span() -> None:
    """⚠ WHAT A REVIEWER NEEDS TO JUDGE IT.

    Observed live: an NER scanner flagged an 8-character leave-type enum inside
    `event_type = '<redacted> Request'` as a `person`, quarantining a time-off blueprint. The
    reviewer saw only "the scan has not settled a clean pass" — no way to tell a real leak from
    the false positive it was.

    Field and kind are entity-FREE by construction and are already on the card; the SPAN is the
    value being withheld and must never appear here. `sql_template (person)` on a query full of
    enum literals reads as what it usually is.
    """
    reason = await _refusal_reason(
        CandidateStatus.NEEDS_PARAMETERIZATION,
        [{"field": "generalization.sql_template", "kind": "person", "span": "Vacation"}],
    )
    assert "generalization.sql_template" in reason
    assert "person" in reason
    assert "quarantine" in reason
    assert "Vacation" not in reason, "the refusal leaked the very span it is withholding"


async def test_the_refusal_promises_no_action_that_does_not_exist() -> None:
    """The first version ended "or clear the scan first". THERE IS NO SUCH OPERATION — no
    re-scan, no override, on any surface — so it sent reviewers hunting for a button. A refusal
    that invents a remedy is worse than one that admits there is none."""
    reason = await _refusal_reason(
        CandidateStatus.IN_REVIEW,
        [{"field": "generalization.sql_template", "kind": "person", "span": "Vacation"}],
    )
    assert "clear the scan" not in reason
    assert "re-extracted" in reason, "it must say what actually re-evaluates the verdict"


async def test_the_refusal_only_offers_the_form_where_there_is_one() -> None:
    """"Supply the entries directly" is true on the fail-to-review FORM and false on the review
    queue, which has no entries box — the second is a control that is not on the page."""
    on_form = await _refusal_reason(
        CandidateStatus.NEEDS_PARAMETERIZATION,
        [{"field": "intent", "kind": "person", "span": "X"}],
    )
    on_queue = await _refusal_reason(
        CandidateStatus.IN_REVIEW, [{"field": "intent", "kind": "person", "span": "X"}]
    )
    assert "type the entries into the form" in on_form
    assert "type the entries into the form" not in on_queue
