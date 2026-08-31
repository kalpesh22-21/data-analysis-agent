"""Minting a blueprint from an expert's submission, end to end through the REAL validator.

The claim `learning/mint/engine.py` makes is that minting owns almost nothing: it builds an
envelope carrying the expert's SQL as the accepted SQL and hands it to the same
`ParameterizationCompleter` a human's completed form goes through. That claim is only worth
anything if the envelope it builds actually survives `to_candidate`, the D97 totality walk and
the write-router stages — so these tests wire the genuine article rather than a double, and the
central one asserts a minted blueprint comes out with a real generalized template.

The two provenance modes are tested separately because they are different guarantees, not two
paths to one:

  * `exact` — the expert's SQL is used VERBATIM and the model has no field to rewrite it in;
  * `pseudo`/`none` — the model writes the SQL, and what makes that safe is the walk plus the
    review, not the drafting.
"""

from __future__ import annotations

import pytest

from data_agent.catalog.loader import build_sqlglot_schema_from_catalog
from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.generalize import GeneralizeStage
from data_agent.learning.inbox import ParameterizationCompleter
from data_agent.learning.leakage import LeakageGateStage
from data_agent.learning.mint import (
    BlueprintMinter,
    MintInputError,
    MintRequest,
    MintResponseError,
    mint_content_hash,
)
from data_agent.learning.mint.engine import MINT_SESSION_PREFIX
from data_agent.learning.writer import WriterStage
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from tests._catalog_fixture import fixture_catalog

from ..extractor.helpers import PAYROLL_SQL, payroll_parameterization

_CATALOG = fixture_catalog()

# The tables the payroll fixture reads. An expert selects these on the form, and they are what
# `_columns_for` narrows the grounding to.
TABLES = ("payroll.payroll_fact",)

# Every queue a candidate could have landed in. Enumerated so "no second row anywhere" is a
# real sweep rather than a check of the one status the happy path happens to use.
_ALL_STATUSES = (
    CandidateStatus.EXTRACTED,
    CandidateStatus.CANDIDATE,
    CandidateStatus.IN_REVIEW,
    CandidateStatus.NEEDS_PARAMETERIZATION,
    CandidateStatus.VALIDATED,
    CandidateStatus.PROMOTED,
    CandidateStatus.REJECTED,
)


class ScriptedModelClient:
    """Replays one queued turn; records what it was asked."""

    def __init__(self, turns: list[ModelTurnResult]) -> None:
        self._turns = list(turns)
        self.calls: list[tuple[list[dict], list[dict]]] = []

    async def send_turn(self, messages: list[dict], tools: list[dict]) -> ModelTurnResult:
        self.calls.append((messages, tools))
        return self._turns.pop(0) if self._turns else ModelTurnResult(tool_calls=[])


def classify_turn(*, entries=None, intent="total earnings for a department in a given year"):
    """One well-formed `classify_blueprint` call covering every literal in `PAYROLL_SQL`."""
    return ModelTurnResult(
        tool_calls=[
            ToolCallRequest(
                id="m1",
                name="classify_blueprint",
                arguments={
                    "intent": intent,
                    "entries": payroll_parameterization() if entries is None else entries,
                    "rationale": "department and year vary; record_type defines the metric",
                },
            )
        ]
    )


def draft_turn(*, sql=PAYROLL_SQL, entries=None):
    """One well-formed `draft_blueprint` call — the model writes the SQL as well."""
    return ModelTurnResult(
        tool_calls=[
            ToolCallRequest(
                id="m1",
                name="draft_blueprint",
                arguments={
                    "intent": "total earnings for a department in a given year",
                    "sql": sql,
                    "entries": payroll_parameterization() if entries is None else entries,
                    "rationale": "drafted from the expert's steps",
                },
            )
        ]
    )


def make_minter(turns, *, store=None, stages=None):
    store = store if store is not None else InMemoryCandidateStore()
    stages = (
        stages
        if stages is not None
        else (
            GeneralizeStage(catalog_schema=build_sqlglot_schema_from_catalog(_CATALOG)),
            LeakageGateStage(candidate_store=store),
            WriterStage(sampler=lambda env: False),
        )
    )
    client = ScriptedModelClient(turns)
    minter = BlueprintMinter(
        model_client=client,
        completer=ParameterizationCompleter(
            store=store, known_rules=frozenset(), rule_index=None, stages=stages
        ),
        catalog_columns=("payroll.payroll_fact.gross_pay",),
    )
    return minter, store, client


def exact_request(**overrides) -> MintRequest:
    kwargs = {
        "question": "what were total earnings for a department in a year?",
        "tables": TABLES,
        "steps": ("filter to earnings rows", "sum gross_pay"),
        "assumptions": ("earnings only, not deductions",),
        "sql": PAYROLL_SQL,
        "sql_mode": "exact",
    }
    kwargs.update(overrides)
    return MintRequest(**kwargs)


# --- the central claim --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_exact_submission_becomes_a_real_candidate_with_a_generalized_template() -> None:
    """The whole design in one assertion: an expert's query goes in, a candidate carrying a
    DERIVED template comes out, having passed the same validator every mined candidate faces.

    The template is checked for slot tokens rather than for equality with the input, because
    that is exactly what makes the result a blueprint rather than a saved query."""
    minter, store, _ = make_minter([classify_turn()])

    result = await minter.mint(exact_request())

    assert result.outcome == "completed", result.decline_detail
    stored = await store.get(result.candidate_id)
    assert stored is not None
    template = stored.payload["generalization"]["sql_template"]
    assert "{department}" in template and "{year}" in template
    # record_type was classified INLINE, so it must survive as a frozen literal.
    assert "'EARNING'" in template
    assert stored.status != CandidateStatus.EXTRACTED  # the router placed it somewhere


@pytest.mark.asyncio
async def test_the_expert_sql_is_used_verbatim_as_the_accepted_sql() -> None:
    """`exact` mode's guarantee. The accepted SQL is what the expert typed, byte for byte —
    it is the provenance root the template is derived from, and a minter that normalized or
    re-emitted it would quietly move that root."""
    minter, store, _ = make_minter([classify_turn()])

    result = await minter.mint(exact_request())

    assert result.accepted_sql == PAYROLL_SQL
    stored = await store.get(result.candidate_id)
    assert stored.revalidation is not None
    # Keyed PER NODE — `mint0` for a single blueprint, which is a one-node DAG's worth
    # of the same uniform shape a composite uses.
    assert stored.revalidation.sql_by_ref["mint0"] == (PAYROLL_SQL,)


@pytest.mark.asyncio
async def test_the_snapshot_is_marked_authored_and_not_reconstructed() -> None:
    """The provenance marker the whole `authored` field exists for.

    `reconstructed` would be the WRONG flag and not merely an imprecise one: it tells a reader
    the first totality walk was circular and proved nothing. Here the SQL came from outside the
    entries entirely, so that walk is the strongest check this candidate ever faces."""
    minter, store, _ = make_minter([classify_turn()])

    result = await minter.mint(exact_request())

    snapshot = (await store.get(result.candidate_id)).revalidation
    assert snapshot.authored is True
    assert snapshot.reconstructed is False
    assert snapshot.session_id.startswith(MINT_SESSION_PREFIX)


# --- the mode split -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_mode_offers_the_model_no_field_in_which_to_write_sql() -> None:
    """The safety argument for the two-tool split, asserted on the REQUEST rather than the
    response: a guard that only rejects SQL after the fact still let the model spend its turn
    writing some."""
    minter, _, client = make_minter([classify_turn()])

    await minter.mint(exact_request())

    (_messages, tools) = client.calls[0]
    assert tools[0]["name"] == "classify_blueprint"
    assert "sql" not in tools[0]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_a_sketch_mode_submission_lets_the_model_write_the_sql() -> None:
    """The other half: with no query to vouch for, the drafting tool IS offered, and what the
    model writes becomes the accepted SQL."""
    minter, store, client = make_minter([draft_turn()])

    result = await minter.mint(
        exact_request(sql="sum gross pay for a dept, earnings only", sql_mode="pseudo")
    )

    (_messages, tools) = client.calls[0]
    assert tools[0]["name"] == "draft_blueprint"
    assert "sql" in tools[0]["parameters"]["properties"]
    assert result.accepted_sql == PAYROLL_SQL
    assert result.outcome == "completed", result.decline_detail
    assert (await store.get(result.candidate_id)).revalidation.authored is True


@pytest.mark.asyncio
async def test_sql_returned_in_exact_mode_is_refused_rather_than_ignored() -> None:
    """A response written against a contract this system does not have. Refused, because
    ignoring it would let the accompanying rationale describe an edit that never happened."""
    minter, _, _ = make_minter(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="m1",
                        name="classify_blueprint",
                        arguments={
                            "intent": "x",
                            "sql": "SELECT 1",
                            "entries": payroll_parameterization(),
                            "rationale": "r",
                        },
                    )
                ]
            )
        ]
    )

    with pytest.raises(MintResponseError, match="cannot be rewritten"):
        await minter.mint(exact_request())


# --- what the walk catches ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unclassified_literal_declines_instead_of_landing() -> None:
    """The gate that makes minting safe. `region = 'NA'` is dropped from the entries, so the
    totality walk must refuse — and the candidate must still be FILED, carrying the complaint,
    because an expert who submitted a near-miss needs the row the assistant can act on."""
    entries = [e for e in payroll_parameterization() if "region" not in str(e)]
    minter, store, _ = make_minter([classify_turn(entries=entries)])

    result = await minter.mint(exact_request())

    assert result.outcome == "declined"
    assert "region" in (result.decline_detail or "") or result.decline_reason
    stored = await store.get(result.candidate_id)
    assert stored is not None
    assert stored.status == CandidateStatus.NEEDS_PARAMETERIZATION
    assert stored.decline is not None


# --- idempotency --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_submission_of_the_same_form_never_forks_a_second_row() -> None:
    """A double-clicked Draft button must not produce two review rows for one blueprint.

    The first submission landed, so the second is REFUSED and told where the row is — which is
    the useful answer, because the expert's next action is to open it. What must not happen,
    and is what this asserts, is a second candidate existing at all."""
    minter, store, _ = make_minter([classify_turn()])
    request = exact_request()

    first = await minter.mint(request)
    minter.model_client = ScriptedModelClient([classify_turn()])
    with pytest.raises(MintInputError, match=first.candidate_id):
        await minter.mint(request)

    everywhere = [
        env
        for status in _ALL_STATUSES
        for env in await store.list_by_status(status, limit=50)
    ]
    assert [env.candidate_id for env in everywhere] == [first.candidate_id]


@pytest.mark.asyncio
async def test_a_declined_draft_is_not_re_minted_over_the_reviewers_work() -> None:
    """The guard keys on EXISTENCE, not status, and the reason is what `put` actually does.

    "Re-minting resumes the row" was wrong: `put` REPLACES the envelope, so a reviewer who had
    been fixing a declined draft through the completion form — whose merged entries and
    correction history `_still_declined` persists — would silently lose all of it. And a
    re-mint can add nothing, because an identical submission asks the model the same question.
    The row is where the work is, so the row is where the expert is sent."""
    dropped = [e for e in payroll_parameterization() if "region" not in str(e)]
    minter, store, _ = make_minter([classify_turn(entries=dropped)])
    request = exact_request()

    declined = await minter.mint(request)
    assert declined.outcome == "declined"

    minter.model_client = ScriptedModelClient([classify_turn()])
    with pytest.raises(MintInputError, match="keeps the work already done"):
        await minter.mint(request)


def test_the_hash_is_over_content_and_excludes_the_submitter() -> None:
    """Two experts who independently write the same blueprint should meet at one row; dedup
    is supposed to be arguing about content, not about who typed it."""
    assert mint_content_hash(exact_request()) == mint_content_hash(
        exact_request()
    )
    assert mint_content_hash(exact_request()) != mint_content_hash(
        exact_request(question="a different question entirely")
    )


@pytest.mark.asyncio
async def test_a_submission_whose_candidate_already_moved_on_is_refused() -> None:
    """Re-running the completer on an approved or rejected row would drag it back to
    `extracted` and re-enter work a human deliberately finished (D29)."""
    from dataclasses import replace

    minter, store, _ = make_minter([classify_turn()])
    first = await minter.mint(exact_request())
    await store.put(
        replace(await store.get(first.candidate_id), status=CandidateStatus.PROMOTED)
    )
    minter.model_client = ScriptedModelClient([classify_turn()])

    with pytest.raises(MintInputError, match="already minted"):
        await minter.mint(exact_request())


# --- grounding ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_brief_is_narrowed_to_the_tables_the_expert_selected() -> None:
    """A model shown the whole catalog reads tables the expert did not choose, and the
    column-scope check then rejects the draft blaming a table nobody picked."""
    minter, _, client = make_minter([classify_turn()])
    minter.catalog_columns = (
        "payroll.payroll_fact.gross_pay",
        "hr.employees.salary",  # a table this expert did NOT select
    )

    await minter.mint(exact_request())

    brief = client.calls[0][0][1]["content"]
    assert "payroll.payroll_fact.gross_pay" in brief
    assert "hr.employees.salary" not in brief


# --- composite: the expert declares the DAG, the model fills the queries -------------------

DEPT_SQL = (
    "SELECT sum(gross_pay) AS dept_total FROM payroll.payroll_fact "
    "WHERE department = '0420' AND record_type = 'EARNING'"
)
# ⚠ THE CONSUMED VALUE IS A BRACE TOKEN, not the `$0.dept_total` consume grammar. That grammar
# names the edge in the `consumes` MAP; it is not SQL and does not tokenize, so a node written
# that way is refused. These tests originally used it and did not notice, because none of them
# asserted `outcome` — they checked the DAG shape of a candidate that had in fact declined.
RATIO_SQL = (
    "SELECT sum(gross_pay) AS company_total FROM payroll.payroll_fact "
    "WHERE record_type = 'EARNING'"
)


def composite_request(**overrides) -> MintRequest:
    from data_agent.learning.mint.models import MintNode

    kwargs = {
        "question": "what share of company earnings does a department account for?",
        "tables": TABLES,
        "sql_mode": "none",
        # INDEPENDENT steps. A step that CONSUMES an earlier one is refused today — see
        # `test_a_consuming_step_is_refused_rather_than_minted_broken` and the note in
        # `MintRequest.__post_init__`.
        "nodes": (
            MintNode(step_intent="total earnings for the department", output_name="dept_total"),
            MintNode(step_intent="total earnings company-wide", output_name="company_total"),
        ),
    }
    kwargs.update(overrides)
    return MintRequest(**kwargs)


def composite_turn(*, nodes=None, entries=None):
    """One `draft_blueprint` call carrying per-step SQL plus ONE flat entry list."""
    return ModelTurnResult(
        tool_calls=[
            ToolCallRequest(
                id="m1",
                name="draft_blueprint",
                arguments={
                    "intent": "a department's share of company earnings",
                    "nodes": nodes
                    if nodes is not None
                    else [{"order": 0, "sql": DEPT_SQL}, {"order": 1, "sql": RATIO_SQL}],
                    "entries": payroll_parameterization() if entries is None else entries,
                    "rationale": "drafted both steps",
                },
            )
        ]
    )


@pytest.mark.asyncio
async def test_a_composite_submission_files_a_dag_the_expert_declared() -> None:
    """The structure comes from the EXPERT, not the model: same node count, same order, same
    edges. A model that decomposed the prose itself would invent the dependency edges, and
    `check_dag` proves a graph is well-formed — never that it is the one that was meant."""
    minter, store, _ = make_minter([composite_turn()])

    result = await minter.mint(composite_request())

    # ASSERTED FIRST, and it is the assertion that was missing: a DAG shape is worth nothing
    # if the candidate carrying it was refused.
    assert result.outcome == "completed", result.decline_detail
    stored = await store.get(result.candidate_id)
    assert stored.payload["kind"] == "composite"
    composes = stored.payload["composes"]
    assert [n["order"] for n in composes] == [0, 1]
    assert composes[0]["output"] == {"dept_total": "scalar"}
    assert composes[1]["feeds_from"] == []
    assert composes[1]["output"] == {"company_total": "scalar"}


@pytest.mark.asyncio
async def test_each_composite_node_gets_its_own_sql_and_citation() -> None:
    """`sql_by_ref` and the evidence pointers must agree with the DAG: one entry per node,
    keyed by the same ref the node names, or the totality walk and the graph describe
    different blueprints."""
    minter, store, _ = make_minter([composite_turn()])

    result = await minter.mint(composite_request())

    assert result.outcome == "completed", result.decline_detail
    snapshot = (await store.get(result.candidate_id)).revalidation
    assert snapshot.sql_by_ref == {"mint0": (DEPT_SQL,), "mint1": (RATIO_SQL,)}
    assert [p.tool_call_ref for p in snapshot.evidence] == ["mint0", "mint1"]
    composes = (await store.get(result.candidate_id)).payload["composes"]
    assert [n["source_tool_call_ref"] for n in composes] == ["mint0", "mint1"]


@pytest.mark.asyncio
async def test_a_step_left_without_sql_is_refused_rather_than_filed_with_a_hole() -> None:
    """A DAG missing one node's query would surface much later as an unrewritable template
    naming a node the expert cannot connect back to the step they wrote."""
    minter, _, _ = make_minter([composite_turn(nodes=[{"order": 0, "sql": DEPT_SQL}])])

    with pytest.raises(MintResponseError, match=r"step\(s\) \[2\]"):
        await minter.mint(composite_request())


@pytest.mark.asyncio
async def test_an_exact_composite_uses_the_experts_own_per_step_sql() -> None:
    """`exact` mode's guarantee holds per NODE. The model is handed the classify tool, which
    has no field for SQL at all, so there is no path by which a vouched-for query is rewritten."""
    from data_agent.learning.mint.models import MintNode

    request = composite_request(
        sql_mode="exact",
        nodes=(
            MintNode(step_intent="dept", output_name="dept_total", sql=DEPT_SQL),
            MintNode(step_intent="company", output_name="company_total", sql=RATIO_SQL),
        ),
    )
    minter, store, client = make_minter([classify_turn()])

    result = await minter.mint(request)

    assert result.outcome == "completed", result.decline_detail
    assert client.calls[0][1][0]["name"] == "classify_blueprint"
    snapshot = (await store.get(result.candidate_id)).revalidation
    assert snapshot.sql_by_ref == {"mint0": (DEPT_SQL,), "mint1": (RATIO_SQL,)}


def test_the_dag_is_part_of_the_submissions_identity() -> None:
    """The same question answered by a different set of steps is a different blueprint and
    must not collide with the first on the idempotency key."""
    from data_agent.learning.mint.models import MintNode

    other = composite_request(
        nodes=(
            MintNode(step_intent="total earnings for the department", output_name="dept_total"),
            MintNode(step_intent="total earnings for the DIVISION", output_name="company_total"),
        )
    )
    assert mint_content_hash(composite_request()) != mint_content_hash(other)


# --- prior art: a warning, never a block --------------------------------------------------


class StubPriorArt:
    """A `PriorArtIndex` returning fixed cards, or raising."""

    def __init__(self, cards=None, *, boom: bool = False) -> None:
        self._cards = cards or []
        self._boom = boom
        self.queries: list[str] = []

    async def search(self, text, *, kinds=("blueprint",), limit=5):
        self.queries.append(text)
        if self._boom:
            raise RuntimeError("index down")
        return self._cards[:limit]

    async def get_by_structural_key(self, key):
        return None


def _card(intent="an existing blueprint", tier="mcp", status="promoted"):
    from data_agent.learning.priorart.models import PriorArtCard

    return PriorArtCard(
        id="bp-existing",
        kind="blueprint",
        tier=tier,
        status=status,
        verified=True,
        drift_status="none",
        intent=intent,
        result_grain=(),
        uses_rules=(),
        structural_key="k",
        embedding_model="m",
        similarity=0.94,
        model_matched=True,
    )


@pytest.mark.asyncio
async def test_an_existing_blueprint_warns_but_does_not_block() -> None:
    """The user's call, and the reason for it: a near-duplicate INTENT with genuinely
    different SQL is a real case — the same question at another grain, or on a different date
    basis — and hard-refusing would leave the expert no route at all."""
    minter, store, _ = make_minter([classify_turn()])
    minter.prior_art = StubPriorArt([_card()])

    result = await minter.mint(exact_request())

    assert result.outcome == "completed"
    assert await store.get(result.candidate_id) is not None  # it was still filed
    assert result.prior_art[0]["id"] == "bp-existing"
    assert result.prior_art[0]["tier"] == "mcp"
    assert any("already answer this" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_the_check_runs_before_the_model_is_paid_for_a_draft() -> None:
    """The expert should learn a blueprint already exists without first buying a drafting
    turn. The warning is just as true either way, so the cheap order is the right one."""
    minter, _, client = make_minter([classify_turn()])
    index = StubPriorArt([_card()])
    minter.prior_art = index

    await minter.mint(exact_request())

    assert index.queries == [exact_request().question]
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_an_unavailable_index_does_not_stop_minting() -> None:
    """A warning may never break the page. A missed duplicate is caught by a reviewer; a hard
    failure means an expert cannot author at all."""
    minter, _, _ = make_minter([classify_turn()])
    minter.prior_art = StubPriorArt(boom=True)

    result = await minter.mint(exact_request())

    assert result.outcome == "completed"
    assert result.prior_art == ()


@pytest.mark.asyncio
async def test_no_index_wired_means_no_warning_and_no_error() -> None:
    minter, _, _ = make_minter([classify_turn()])

    assert await minter.find_prior_art("anything") == ()


# --- a hand-authored blueprint is never auto-landed ----------------------------------------


@pytest.mark.asyncio
async def test_a_clean_minted_blueprint_still_goes_to_review_not_the_auto_land_path() -> None:
    """⚠ FOUND LIVE, and it made the page's central promise false.

    A clean, unsampled MINED blueprint auto-lands at `candidate` — the flywheel's whole point,
    and it is earned: that candidate was OBSERVED answering a real user's question. A minted
    one has no session behind it, and in `pseudo`/`none` mode its SQL was written by a model
    from prose and has never run. Landing it the same way put it on the auto-promotion path
    where the scheduler could promote it without a human ever opening it, while the authoring
    page said "you review the draft there".

    The sampler here returns False — i.e. this candidate was NOT sampled for review — so the
    only thing that can route it to `in_review` is being authored."""
    minter, store, _ = make_minter([classify_turn()])

    result = await minter.mint(exact_request())

    assert result.outcome == "completed", result.decline_detail
    stored = await store.get(result.candidate_id)
    assert stored.status == CandidateStatus.IN_REVIEW
    assert stored.verified is False


def test_the_routing_rule_reads_the_snapshot_flag_not_the_session_id_shape() -> None:
    """Keyed on `authored`, which is stamped where the accepted SQL is recorded. Keying on the
    `mint::` id prefix would be a display convention deciding an access-control-adjacent
    routing rule, and it would break silently in the PERMISSIVE direction the first time
    somebody renamed it."""
    from dataclasses import replace

    from data_agent.learning.writer.routing import derive_inbox_reason, route_candidate

    minted = _minted_envelope()
    assert route_candidate(minted, sampled_for_inbox=False).status == CandidateStatus.IN_REVIEW
    assert derive_inbox_reason(minted) == "hand_authored"

    # Same envelope, same `mint::` session id, but the flag cleared → ordinary routing.
    not_authored = replace(
        minted, revalidation=replace(minted.revalidation, authored=False)
    )
    assert route_candidate(not_authored, sampled_for_inbox=False).status == (
        CandidateStatus.CANDIDATE
    )


def _minted_envelope():
    """A minted envelope that has passed generalization, leakage and dedup cleanly."""
    from dataclasses import replace

    from data_agent.learning.mint.engine import BlueprintMinter

    minter = BlueprintMinter.__new__(BlueprintMinter)
    object.__setattr__(minter, "catalog_columns", ())
    env = BlueprintMinter._envelope(
        minter,
        exact_request(),
        intent="i",
        sql_per_node=[PAYROLL_SQL],
        rationale="r",
    )
    return replace(
        env,
        payload={
            **env.payload,
            "generalization": {
                "sql_template": PAYROLL_SQL,
                "uses": ["payroll.payroll_fact.gross_pay"],
                "static_validation": {"outcome": "ok", "reason": None},
            },
        },
        entity_scan={"result": "pass", "hits": [], "scanned_fields": ["intent"],
                     "scanner": "regex+ner"},
    )


SCALAR_CONSUME_SQL = (
    "SELECT sum(gross_pay) / {dept_total} AS share FROM payroll.payroll_fact "
    "WHERE record_type = 'EARNING'"
)


@pytest.mark.asyncio
async def test_a_step_can_consume_an_earlier_steps_scalar() -> None:
    """The shape the whole DAG exists for, and the one that was broken.

    A node's accepted SQL may carry `{dept_total}` — the token the executor binds the upstream
    output into. It survives now because the rewrite parses through the RUNTIME's
    `parse_template`, which rewrites `{name}` -> `:name` first; a bare `sqlglot.parse_one` reads
    `{name}` as an empty ClickHouse map literal and silently emitted `/ map()`.
    """
    from data_agent.learning.mint.models import MintNode

    request = composite_request(
        nodes=(
            MintNode(step_intent="dept total", output_name="dept_total"),
            MintNode(step_intent="its share of the company", feeds_from=(0,)),
        )
    )
    minter, store, _ = make_minter(
        [composite_turn(nodes=[{"order": 0, "sql": DEPT_SQL},
                               {"order": 1, "sql": SCALAR_CONSUME_SQL}])]
    )

    result = await minter.mint(request)

    assert result.outcome == "completed", result.decline_detail
    stored = await store.get(result.candidate_id)
    assert stored.payload["composes"][1]["consumes"] == {"dept_total": "$0.dept_total"}
    # ⚠ THE ASSERTION THAT WOULD HAVE CAUGHT THE ORIGINAL BUG: the placeholder is still a
    # placeholder in the template, not `map()` and not sqlglot's `{dept_total: }` round-trip.
    templates = stored.payload["generalization"]["node_templates"]
    assert "{dept_total}" in templates[1]["sql_template"]
    assert "map()" not in templates[1]["sql_template"]


@pytest.mark.asyncio
async def test_a_step_can_consume_an_earlier_steps_whole_table() -> None:
    """The primitive for MANY values flowing between steps. A table output materializes as
    `scratch.<name>` and is read as a FROM source, so it uses the bare `$0` consume grammar
    rather than a bound token — both are `NODE_OUTPUT_KINDS` members the executor supports."""
    from data_agent.learning.mint.models import MintNode

    request = composite_request(
        nodes=(
            MintNode(step_intent="per-employee earnings", output_name="emp_earnings",
                     output_kind="table"),
            MintNode(step_intent="roll them up", feeds_from=(0,)),
        )
    )
    source = (
        "SELECT employee_code, sum(gross_pay) AS earnings FROM payroll.payroll_fact "
        "WHERE department = '0420' AND record_type = 'EARNING' GROUP BY employee_code"
    )
    rollup = "SELECT sum(x.earnings) AS total FROM scratch.emp_earnings AS x"
    minter, store, _ = make_minter(
        [composite_turn(nodes=[{"order": 0, "sql": source}, {"order": 1, "sql": rollup}])]
    )

    result = await minter.mint(request)

    assert result.outcome == "completed", result.decline_detail
    composes = (await store.get(result.candidate_id)).payload["composes"]
    assert composes[0]["output"] == {"emp_earnings": "table"}
    assert composes[1]["consumes"] == {"emp_earnings": "$0"}


@pytest.mark.asyncio
async def test_a_node_can_consume_two_upstream_steps() -> None:
    """A diamond. `consumes` is a dict, so several edges resolve independently — checked
    because every earlier composite test was a two-node chain."""
    from data_agent.learning.mint.models import MintNode

    request = composite_request(
        nodes=(
            MintNode(step_intent="a", output_name="a"),
            MintNode(step_intent="b", output_name="b"),
            MintNode(step_intent="both", feeds_from=(0, 1)),
        )
    )
    combined = (
        "SELECT ({a} + {b}) AS combined FROM payroll.payroll_fact "
        "WHERE record_type = 'EARNING'"
    )
    minter, store, _ = make_minter(
        [composite_turn(nodes=[{"order": 0, "sql": DEPT_SQL}, {"order": 1, "sql": DEPT_SQL},
                               {"order": 2, "sql": combined}])]
    )

    result = await minter.mint(request)

    assert result.outcome == "completed", result.decline_detail
    composes = (await store.get(result.candidate_id)).payload["composes"]
    assert composes[2]["consumes"] == {"a": "$0.a", "b": "$1.b"}


@pytest.mark.asyncio
async def test_a_table_consumer_that_never_reads_the_scratch_table_is_refused() -> None:
    """`check_dag` says explicitly it cannot check this — it sees the plan, not the templates —
    so this is the only place a fictional table edge can be caught before landing."""
    from data_agent.learning.mint.models import MintNode

    request = composite_request(
        nodes=(
            MintNode(step_intent="src", output_name="emp_earnings", output_kind="table"),
            MintNode(step_intent="rollup", feeds_from=(0,)),
        )
    )
    minter, _, _ = make_minter(
        [composite_turn(nodes=[
            {"order": 0, "sql": "SELECT employee_code FROM payroll.payroll_fact "
                                "WHERE department = '0420'"},
            {"order": 1, "sql": "SELECT 1 AS total FROM payroll.payroll_fact"},
        ])]
    )

    with pytest.raises(MintResponseError, match="never reads scratch.emp_earnings"):
        await minter.mint(request)
