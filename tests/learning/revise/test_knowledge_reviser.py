"""The assistant that corrects a `global_knowledge` fact (knowledge-edit design §C.1, §F).

THE INVARIANT THAT IS DIFFERENT HERE. The blueprint reviser is REFUSED on a candidate whose
scan did not clear; this one is OFFERED precisely then, because removing the entity is the job.
So the withholding rule moves from the input to the OUTPUT, and that is what most of this file
is about: a draft the scanner still flags NEVER reaches the wire, and the reason that comes back
in its place names the field and the kind and never the span.

THE SECOND INVARIANT, shared with its sibling: it writes nothing. Every test here runs against
an unwritten envelope, because "proposes" and "applies" being the same call is the one refactor
that would move a model inside the validation boundary.

The worked case is the shape K2 actually produces: a fact a user taught the agent, carrying an
employee code, that a reviewer wants lifted into a rule everyone can use.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import EntityHit, LeakageVerdict
from data_agent.learning.inbox.knowledge_edit import stage_scanner
from data_agent.learning.leakage.gate import (
    _ENTITY_FREE_SURFACES,
    LeakageGateStage,
)
from data_agent.learning.revise import (
    ForbiddenKnowledgeEditError,
    KnowledgeReviser,
    build_knowledge_tool,
)
from data_agent.learning.revise.knowledge import WITHHELD, knowledge_diff
from data_agent.learning.revise.knowledge_schema import KNOWLEDGE_TOOL_NAME
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest

DIRTY_STATEMENT = "employee E10842 accrues 1.5 days of leave per month"
CLEAN_STATEMENT = "leave accrues at 1.5 days per month for full-time staff"


def knowledge_candidate(**over: Any) -> CandidateEnvelope:
    """An `in_review` `global_knowledge` candidate whose statement names an employee."""
    fields: dict[str, Any] = {
        "candidate_id": "candidate::kn-1",
        "type": "global_knowledge",
        "status": CandidateStatus.IN_REVIEW,
        "payload": {
            "statement": DIRTY_STATEMENT,
            "knowledge_type": "business_rule",
            "scope": "leave accrual",
        },
        "source_session": "sess-1",
        "source_trace": "trace-1",
        "evidence_refs": ("audit::1",),
        "extractor_rationale": "the user stated it and the answer used it",
        "entity_scan": LeakageVerdict(
            result="reject",
            hits=(EntityHit(field="statement", kind="employee_code", span="E10842"),),
            scanned_fields=("statement",),
            scanner="regex+ner",
        ).to_doc(),
        "confidence": 0.8,
        "proposed_action": "add",
        "depends_on": (),
        "content_hash": "hash-kn-1",
    }
    fields.update(over)
    return CandidateEnvelope(**fields)


def proposal_turn(
    *, arguments: Any = None, tool_name: str = KNOWLEDGE_TOOL_NAME
) -> ModelTurnResult:
    if arguments is None:
        arguments = {
            "statement": CLEAN_STATEMENT,
            "knowledge_type": "business_rule",
            "structured": {"rate": "1.5 days per month"},
            "related_terms": ["leave", "accrual"],
            "scope": "leave accrual",
            "rationale": "lifted the individual case into the rule it is an instance of",
        }
    return ModelTurnResult(
        tool_calls=[ToolCallRequest(id="k1", name=tool_name, arguments=arguments)]
    )


class ScriptedClient:
    def __init__(self, turns: list[ModelTurnResult]) -> None:
        self._turns = list(turns)
        self.calls: list[tuple[list[dict], list[dict]]] = []

    async def send_turn(self, messages: list[dict], tools: list[dict]) -> ModelTurnResult:
        self.calls.append((messages, tools))
        return self._turns.pop(0) if self._turns else ModelTurnResult(tool_calls=[])


def real_scanner():
    """A scanner over the REAL S5 gate — the same object the editor stamps with.

    A hand-written stub returning `{"result": "pass"}` would prove only that this file agrees
    with itself. The gate's regex battery is what actually decides whether a draft is clean, and
    it is what will decide again on apply.
    """
    return stage_scanner((LeakageGateStage(candidate_store=InMemoryCandidateStore()),))


def make_reviser(turns: list[ModelTurnResult], **kw: Any) -> KnowledgeReviser:
    options: dict[str, Any] = {"scanner": real_scanner(), "timeout_seconds": 5.0}
    options.update(kw)
    return KnowledgeReviser(model_client=ScriptedClient(turns), **options)


# --- the pin ----------------------------------------------------------------


def test_the_tool_offers_exactly_the_surfaces_the_leakage_gate_scans() -> None:
    """⚠ THE PIN THAT HOLDS THREE MODULES TOGETHER (§C.1).

    The gate scans `_ENTITY_FREE_SURFACES["global_knowledge"]`, intake closes the payload over
    the same five (`_GLOBAL_KNOWLEDGE_KEYS`, pinned by its own test), and the tool offers them.
    None of the three imports the others, so only a test can keep them equal — and the failure
    the equality prevents is the one that already happened once in production: a payload key
    nothing scanned, reported `pass` by a gate that had never read it.
    """
    props = build_knowledge_tool()["parameters"]["properties"]
    assert set(props) - {"rationale"} == set(_ENTITY_FREE_SURFACES["global_knowledge"])


def test_the_tool_offers_no_way_to_write_anything_else() -> None:
    """The schema is the primary defence; the parse guard is the backstop."""
    props = build_knowledge_tool()["parameters"]["properties"]
    assert "user_id" not in props
    assert "definition" not in props
    assert "intent" not in props
    assert build_knowledge_tool()["parameters"]["required"] == ["statement", "rationale"]


# --- the closed set ---------------------------------------------------------


@pytest.mark.parametrize("key", ["user_id", "definition", "intent", "sql_template"])
async def test_a_proposal_carrying_an_extra_key_is_rejected_not_ignored(key: str) -> None:
    """⚠ Rejected, not dropped. A sixth key is a text surface the entity scanner does not read,
    so ignoring it would let the model believe it had set something the system will never look
    at — and would put an unscanned field one human approval away from the global index."""
    reviser = make_reviser(
        [proposal_turn(arguments={"statement": CLEAN_STATEMENT, "rationale": "r", key: "x"})]
    )
    with pytest.raises(ForbiddenKnowledgeEditError) as exc:
        await reviser.propose(knowledge_candidate(), feedback="fix it")
    assert key in str(exc.value)


async def test_a_nested_object_inside_a_text_surface_is_rejected() -> None:
    """`related_terms` is an array of STRINGS. An object under it is a shape nothing reads, so
    it is the same mistake an extra top-level key is, one level down."""
    reviser = make_reviser(
        [
            proposal_turn(
                arguments={
                    "statement": CLEAN_STATEMENT,
                    "related_terms": [{"term": "leave"}],
                    "rationale": "r",
                }
            )
        ]
    )
    with pytest.raises(ForbiddenKnowledgeEditError) as exc:
        await reviser.propose(knowledge_candidate(), feedback="fix it")
    assert "term" in str(exc.value)


async def test_structured_may_use_any_key_because_its_keys_are_content() -> None:
    """⚠ THE ONE EXEMPTION, and it is not laxity. `structured` is declared as string→string
    supporting detail, so a fact about an overtime multiplier legitimately uses that word as a
    key. Sweeping its keys would refuse every proposal that used the feature."""
    reviser = make_reviser(
        [
            proposal_turn(
                arguments={
                    "statement": CLEAN_STATEMENT,
                    "structured": {"definition": "a rate", "user_id": "not a field name here"},
                    "rationale": "r",
                }
            )
        ]
    )
    proposal = await reviser.propose(knowledge_candidate(), feedback="fix it")
    assert proposal.payload["structured"]["definition"] == "a rate"


async def test_a_dict_nested_inside_structured_is_still_rejected() -> None:
    """Its VALUES are strings. A nested object there is a model writing a shape the mapper
    would flatten by index and nobody would ever read back."""
    reviser = make_reviser(
        [
            proposal_turn(
                arguments={
                    "statement": CLEAN_STATEMENT,
                    "structured": {"rate": {"amount": "1.5"}},
                    "rationale": "r",
                }
            )
        ]
    )
    with pytest.raises(ForbiddenKnowledgeEditError) as exc:
        await reviser.propose(knowledge_candidate(), feedback="fix it")
    assert "amount" in str(exc.value)


# --- the output gate --------------------------------------------------------


async def test_a_draft_that_still_scans_dirty_is_withheld_entirely() -> None:
    """⚠ THE RULE THIS WHOLE PATH TURNS ON.

    The reviewer clicked the assistant because the card is WITHHOLDING the flagged text. A draft
    that still carries an entity would put that text straight back on the page — through the
    surface added to remove it — under the label "here is your fix". So the payload is dropped
    and the reason names the field and the kind, which is what `propose_revision`'s refusal does
    and what `_leakage_view` does for the listing.
    """
    still_dirty = "employee E10842 accrues leave monthly, so the rule is per-employee"
    reviser = make_reviser(
        [proposal_turn(arguments={"statement": still_dirty, "rationale": "r"})]
    )

    proposal = await reviser.propose(knowledge_candidate(), feedback="remove the code")

    assert proposal.payload == {}
    assert proposal.has_proposal is False
    assert "statement (employee_code)" in proposal.reason
    # THE SPAN IS THE VALUE BEING WITHHELD. It may not appear anywhere on the wire — not in the
    # reason, not in the payload, not in a diff row.
    wire = proposal.to_wire()
    assert "E10842" not in str(wire)
    assert wire["payload"] == {}
    assert wire["diff"] == []


async def test_a_clean_draft_comes_back_with_its_diff() -> None:
    reviser = make_reviser([proposal_turn()])
    proposal = await reviser.propose(knowledge_candidate(), feedback="generalise it")

    assert proposal.payload["statement"] == CLEAN_STATEMENT
    assert proposal.rationale
    assert proposal.reason == ""
    wire = proposal.to_wire()
    # ALWAYS THE FIVE SURFACES on the wire, so the form on the other side has a shape to
    # prefill — see `KnowledgeProposal.to_wire`.
    assert set(wire["payload"]) == set(_ENTITY_FREE_SURFACES["global_knowledge"])


async def test_no_scanner_wired_means_no_draft_is_ever_shown() -> None:
    """⚠ FAIL-CLOSED, and deliberately so. The scan is the only thing between model prose about
    an unredacted payload and a response body, so "we could not check it" has to read as "we do
    not send it" — the same posture `settle_entity_scan` takes one layer down."""
    reviser = make_reviser([proposal_turn()], scanner=None)
    proposal = await reviser.propose(knowledge_candidate(), feedback="x")
    assert proposal.payload == {}
    assert "no entity scanner" in proposal.reason


async def test_a_scanner_that_raises_withholds_rather_than_leaking() -> None:
    """A scanner that blew up told us nothing, and "nothing" must not read as "clean"."""

    async def boom(_env):
        raise RuntimeError("neo4j is down")

    reviser = make_reviser([proposal_turn()], scanner=boom)
    proposal = await reviser.propose(knowledge_candidate(), feedback="x")
    assert proposal.payload == {}
    assert proposal.reason


# --- fail-soft --------------------------------------------------------------


async def test_a_timeout_is_a_reason_not_an_exception() -> None:
    class Hanging:
        async def send_turn(self, messages, tools):
            await asyncio.sleep(10)
            raise AssertionError("unreachable")

    reviser = KnowledgeReviser(
        model_client=Hanging(), scanner=real_scanner(), timeout_seconds=0.01
    )
    proposal = await reviser.propose(knowledge_candidate(), feedback="x")
    assert proposal.payload == {}
    assert "timed out" in proposal.reason


async def test_a_provider_raising_a_bare_base_exception_is_a_reason() -> None:
    """WIDER THAN `Exception`, for the reason `judge.py` records from QA: a provider SDK raising
    a bare `BaseException` escaped every narrower handler. A typing aid may not 500 the surface
    it is attached to."""

    class Exploding:
        async def send_turn(self, messages, tools):
            raise BaseException("provider went sideways")  # noqa: TRY002

    reviser = KnowledgeReviser(model_client=Exploding(), scanner=real_scanner())
    proposal = await reviser.propose(knowledge_candidate(), feedback="x")
    assert proposal.payload == {}
    assert "could not be reached" in proposal.reason


async def test_a_response_with_no_statement_is_no_suggestion() -> None:
    """A knowledge payload without a statement is not a partial proposal — it is nothing."""
    reviser = make_reviser([proposal_turn(arguments={"rationale": "I have no idea"})])
    proposal = await reviser.propose(knowledge_candidate(), feedback="x")
    assert proposal.payload == {}
    assert "usable statement" in proposal.reason


async def test_the_model_calling_the_wrong_tool_is_no_suggestion() -> None:
    reviser = make_reviser([proposal_turn(tool_name="something_else")])
    proposal = await reviser.propose(knowledge_candidate(), feedback="x")
    assert proposal.payload == {}
    assert proposal.reason


async def test_an_information_free_field_is_dropped_rather_than_stored_empty() -> None:
    """⚠ A LIVE MODEL EMITS EVERY DECLARED PROPERTY, so "leave the type alone" arrives as
    `knowledge_type: ""`. Carrying it would store an empty label as though the assistant had
    chosen one — the finding `schema.py::_is_information_free` records, in this dialect."""
    reviser = make_reviser(
        [
            proposal_turn(
                arguments={
                    "statement": CLEAN_STATEMENT,
                    "knowledge_type": "",
                    "structured": {},
                    "related_terms": [],
                    "scope": "",
                    "rationale": "r",
                }
            )
        ]
    )
    proposal = await reviser.propose(knowledge_candidate(), feedback="x")
    assert proposal.payload == {"statement": CLEAN_STATEMENT}


# --- what it does not do ----------------------------------------------------


async def test_it_writes_nothing_it_only_proposes() -> None:
    """The proposal is applied through `apply_knowledge`, which re-runs intake validation and
    the leakage scan. A reviser that wrote would be a second write path into a payload whose
    key set is a leakage rule."""
    env = knowledge_candidate()
    before = env.to_doc()
    reviser = make_reviser([proposal_turn()])
    await reviser.propose(env, feedback="generalise it")
    assert env.to_doc() == before


async def test_the_brief_shows_the_model_the_span_it_has_to_remove() -> None:
    """⚠ SERVER-SIDE ONLY, and necessary: a model asked to remove `E10842` while being shown
    `[redacted]` has nothing to work with. The boundary that holds is the OUTPUT scan, not the
    input — see `test_a_draft_that_still_scans_dirty_is_withheld_entirely`."""
    client = ScriptedClient([proposal_turn()])
    reviser = KnowledgeReviser(model_client=client, scanner=real_scanner())
    await reviser.propose(knowledge_candidate(), feedback="drop the employee code")

    brief = client.calls[0][0][1]["content"]
    assert "E10842" in brief
    assert "statement (employee_code)" in brief
    assert "drop the employee code" in brief


async def test_the_reviewers_feedback_is_capped_before_it_reaches_the_prompt() -> None:
    """An oversized paste must not push the scanner's findings out of the model's attention."""
    client = ScriptedClient([proposal_turn()])
    reviser = KnowledgeReviser(model_client=client, scanner=real_scanner())
    await reviser.propose(knowledge_candidate(), feedback="x" * 50_000)
    brief = client.calls[0][0][1]["content"]
    assert brief.count("x") < 5_000


# --- the diff ---------------------------------------------------------------


def test_a_flagged_fields_before_is_withheld_not_rendered() -> None:
    """⚠ THE DIFF IS A SECOND ROUTE TO THE SAME TEXT. A per-field before/after would ship the
    flagged statement under the label "before" — on the card that is withholding it."""
    rows = knowledge_diff(
        {"statement": DIRTY_STATEMENT, "scope": "leave accrual"},
        {"statement": CLEAN_STATEMENT, "scope": "leave accrual"},
        withhold=frozenset({"statement"}),
    )
    by_field = {row.field: row for row in rows}
    assert by_field["statement"].before == WITHHELD
    assert by_field["statement"].after == CLEAN_STATEMENT
    assert by_field["statement"].kind == "changed"
    # An UNFLAGGED field keeps its before: withholding everything would tell the reviewer
    # nothing about what is actually changing.
    assert by_field["scope"].before == "leave accrual"
    assert by_field["scope"].kind == "unchanged"


def test_every_surface_gets_a_row_including_the_untouched_ones() -> None:
    """A reviewer deciding whether to apply needs to see that the fields they already trusted
    are still there, which a deltas-only diff cannot say."""
    rows = knowledge_diff({"statement": "a"}, {"statement": "a", "scope": "s"})
    assert [row.field for row in rows] == list(_ENTITY_FREE_SURFACES["global_knowledge"])
    by_field = {row.field: row.kind for row in rows}
    assert by_field["scope"] == "added"
    assert by_field["statement"] == "unchanged"
    assert by_field["knowledge_type"] == "unchanged"


def test_a_field_the_proposal_drops_reads_as_removed() -> None:
    """The proposal REPLACES the payload, so an omitted field is a deletion — and a diff that
    called it "unchanged" would hide the destructive half of the operation, which is the half a
    reviewer most needs to see (`diff.py` makes the same argument about replace mode)."""
    rows = knowledge_diff({"statement": "a", "scope": "s"}, {"statement": "a"})
    by_field = {row.field: row.kind for row in rows}
    assert by_field["scope"] == "removed"


async def test_the_diff_is_built_against_the_real_current_payload() -> None:
    env = knowledge_candidate()
    reviser = make_reviser([proposal_turn()])
    proposal = await reviser.propose(env, feedback="x")
    statement_row = next(r for r in proposal.diff if r.field == "statement")
    # Flagged, so withheld — and the raw statement is nowhere on the wire.
    assert statement_row.before == WITHHELD
    assert DIRTY_STATEMENT not in str(proposal.to_wire())


async def test_an_unflagged_candidate_shows_its_before_normally() -> None:
    """Nothing is withheld when nothing was flagged: the withholding follows the SCAN, not the
    surface. A clean candidate being edited for wording reads as an ordinary diff."""
    env = replace(
        knowledge_candidate(),
        payload={"statement": CLEAN_STATEMENT, "scope": "leave accrual"},
        entity_scan=LeakageVerdict(result="pass", scanner="regex").to_doc(),
    )
    reviser = make_reviser([proposal_turn()])
    proposal = await reviser.propose(env, feedback="tighten the wording")
    statement_row = next(r for r in proposal.diff if r.field == "statement")
    assert statement_row.before == CLEAN_STATEMENT
