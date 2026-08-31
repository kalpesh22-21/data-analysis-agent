"""QA sweep over blueprint minting — the paths the shipped tests do not reach.

Tests marked `# DEFECT` fail against the implementation as it stands and say what the correct
behaviour would be. Tests marked `REGRESSION GUARD` pin a fault that was found during this
review and has since been fixed. The rest close coverage gaps and pass.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.mint import MintInputError, MintRequest, mint_content_hash
from data_agent.learning.mint.models import MintNode
from data_agent.learning.mint.schema import MintResponseError, coerce_node_sql
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest

from ..extractor.helpers import payroll_parameterization
from .test_mint_engine import (
    DEPT_SQL,
    TABLES,
    classify_turn,
    exact_request,
    make_minter,
)


def _node_turn(pairs, *, entries=None, intent="two payroll totals"):
    """One well-formed composite `draft_blueprint` call carrying per-step SQL."""
    return ModelTurnResult(
        tool_calls=[
            ToolCallRequest(
                id="m1",
                name="draft_blueprint",
                arguments={
                    "intent": intent,
                    "nodes": [{"order": o, "sql": s} for o, s in pairs],
                    "entries": payroll_parameterization() if entries is None else entries,
                    "rationale": "drafted",
                },
            )
        ]
    )


def independent_request(**overrides) -> MintRequest:
    """A composite whose steps do NOT feed each other — the only composite shape that can
    still be minted (see `test_a_step_that_depends_on_another_is_now_refused_outright`)."""
    kwargs = {
        "question": "two unrelated payroll totals",
        "tables": TABLES,
        "sql_mode": "none",
        "nodes": (
            MintNode(step_intent="total earnings for the department", output_name="dept_total"),
            MintNode(step_intent="total earnings company-wide", output_name="company_total"),
        ),
    }
    kwargs.update(overrides)
    return MintRequest(**kwargs)


# --- composite: what the DAG half of the feature can and cannot do -------------------------


async def test_a_step_that_depends_on_another_now_mints(minter_factory=None) -> None:
    """The gate is lifted: the rewrite parses through the runtime's `parse_template`, so a
    node's `{token}` survives instead of collapsing to `map()`. Kept as a QA-side check that
    the fix holds from this file's own fixtures too."""
    from data_agent.learning.mint.models import MintNode

    from .test_mint_engine import (
        DEPT_SQL,
        SCALAR_CONSUME_SQL,
        composite_request,
        composite_turn,
        make_minter,
    )

    request = composite_request(
        nodes=(
            MintNode(step_intent="dept total", output_name="dept_total"),
            MintNode(step_intent="share", feeds_from=(0,)),
        )
    )
    minter, store, _ = make_minter(
        [composite_turn(nodes=[{"order": 0, "sql": DEPT_SQL},
                               {"order": 1, "sql": SCALAR_CONSUME_SQL}])]
    )
    result = await minter.mint(request)
    assert result.outcome == "completed", result.decline_detail
    templates = (await store.get(result.candidate_id)).payload["generalization"][
        "node_templates"
    ]
    assert "map()" not in templates[1]["sql_template"]

def test_a_brace_placeholder_is_rewritten_into_an_empty_map_literal() -> None:
    """THE UNDERLYING FACT, pinned so the workaround above cannot be quietly undone.

    sqlglot's ClickHouse dialect reads `{name}` as an empty map constructor, so any accepted
    SQL carrying a brace token comes out of the rewrite with the token replaced by `map()` —
    silently, with no `RewriteError` for anything downstream to notice. (`DRAFT_SYSTEM_PROMPT`
    tells the model "a query containing braces will fail to parse", which is not what happens.)
    """
    import sqlglot

    out = sqlglot.parse_one(
        "SELECT sum(gross_pay) / {dept_total} AS share FROM payroll.payroll_fact",
        read="clickhouse",
    ).sql(dialect="clickhouse")

    assert "map()" in out
    assert "{dept_total}" not in out


def test_the_dollar_form_placeholder_does_not_tokenize_at_all() -> None:
    """The other half of the same fact."""
    import sqlglot
    from sqlglot.errors import TokenError

    with pytest.raises(TokenError):
        sqlglot.parse_one("SELECT sum(g) / $0.dept_total AS s FROM t", read="clickhouse")


@pytest.mark.asyncio
async def test_an_independent_two_step_composite_mints_and_validates() -> None:
    """What is LEFT of composite minting: steps with no edges. The shipped tests only ever
    build the (now refused) dependent shape, so nothing covered this."""
    minter, store, _ = make_minter([_node_turn([(0, DEPT_SQL), (1, DEPT_SQL)])])

    result = await minter.mint(independent_request())

    env = await store.get(result.candidate_id)
    assert env.payload["kind"] == "composite"
    assert [n["feeds_from"] for n in env.payload["composes"]] == [[], []]
    assert [n["consumes"] for n in env.payload["composes"]] == [{}, {}]
    assert [n["source_tool_call_ref"] for n in env.payload["composes"]] == ["mint0", "mint1"]
    assert result.outcome == "completed", result.decline_detail


def test_more_steps_than_check_dag_accepts_is_refused_before_the_model_is_paid() -> None:
    """REGRESSION GUARD. The form capped steps at 24 while `check_dag` caps a graph at 16, so a
    17-step submission paid for a drafting turn, was persisted, and was then declined
    `dag_invalid`. The cap is now DERIVED from `check_dag`'s and refused at construction."""
    from data_agent.learning.generalize.validate import _MAX_NODES

    nodes = tuple(
        MintNode(step_intent=f"step {i}", output_name=f"out_{i}")
        for i in range(_MAX_NODES + 1)
    )

    with pytest.raises(MintInputError, match=f"at most {_MAX_NODES} steps"):
        independent_request(nodes=nodes)


def test_an_output_name_may_be_a_sql_keyword_or_arbitrarily_long() -> None:
    """`_IDENTIFIER` is the only guard on an output name: no keyword check and no length cap.
    A 5,000-character name becomes the `consumes` key and the node-template placeholder that
    the corpus loader then validates. Coverage of the boundary, not a proven fault."""
    long_name = "a" * 5_000

    request = independent_request(
        nodes=(
            MintNode(step_intent="one", output_name="select"),
            MintNode(step_intent="two", output_name=long_name),
        )
    )

    assert request.nodes[0].output_for(0) == "select"
    assert request.nodes[1].output_for(1) == long_name


def test_two_steps_that_would_share_a_defaulted_output_name_are_refused() -> None:
    """An explicit `step_1` colliding with position 1's default. Gap closed."""
    with pytest.raises(MintInputError, match="output name"):
        independent_request(
            nodes=(
                MintNode(step_intent="one", output_name="step_1"),
                MintNode(step_intent="two"),
            )
        )


# --- exact mode: every path the expert's SQL could be dropped ------------------------------


def test_an_exact_composite_refuses_a_stray_whole_query() -> None:
    """REGRESSION GUARD. `sql_mode='exact'` with BOTH per-step SQL and a top-level query used
    to be accepted, shown to the model, and then dropped — a vouched-for query silently
    discarded. It is now refused."""
    whole = "SELECT sum(gross_pay) FROM payroll.payroll_fact WHERE record_type = 'EARNING'"

    with pytest.raises(MintInputError, match="whole-query"):
        independent_request(
            sql_mode="exact",
            sql=whole,
            nodes=(
                MintNode(step_intent="dept", output_name="dept_total", sql=DEPT_SQL),
                MintNode(step_intent="company", output_name="company_total", sql=DEPT_SQL),
            ),
        )


def test_an_exact_composite_with_only_some_step_sql_is_refused() -> None:
    """Gap closed: `exact` promises every step ran."""
    with pytest.raises(MintInputError, match="every step needs its own SQL"):
        independent_request(
            sql_mode="exact",
            nodes=(
                MintNode(step_intent="dept", output_name="dept_total", sql=DEPT_SQL),
                MintNode(step_intent="company", output_name="company_total"),
            ),
        )


@pytest.mark.asyncio
async def test_an_exact_composite_refuses_a_response_that_carries_sql() -> None:
    """`exact` composite is handed the classify tool, so the FULL forbidden-key sweep runs and
    a model that returns SQL anyway is refused rather than stripped. The guarantee has to hold
    for five queries as it does for one; only the one-query case was covered."""
    turn = classify_turn()
    turn.tool_calls[0].arguments["sql"] = "SELECT 1"
    request = independent_request(
        sql_mode="exact",
        nodes=(
            MintNode(step_intent="dept", output_name="dept_total", sql=DEPT_SQL),
            MintNode(step_intent="company", output_name="company_total", sql=DEPT_SQL),
        ),
    )
    minter, _, _ = make_minter([turn])

    with pytest.raises(MintResponseError, match="sql"):
        await minter.mint(request)


@pytest.mark.asyncio
async def test_a_composite_draft_carrying_a_top_level_sql_field_is_refused() -> None:
    """REGRESSION GUARD for the module's own rule applied unevenly.

    `schema.py` argues that "an ignored field is worse than an absent one", but in COMPOSITE
    draft mode the forbidden-key sweep was narrowed to `entries`, so a top-level `sql` was
    neither rejected nor used — the model believed it wrote the query and its rationale
    explained an edit that never happened."""
    turn = _node_turn([(0, DEPT_SQL), (1, DEPT_SQL)])
    turn.tool_calls[0].arguments["sql"] = "SELECT 'the model thought this was the query'"
    minter, _, _ = make_minter([turn])

    with pytest.raises(MintResponseError):
        await minter.mint(independent_request())


# --- live-model-style malformed responses --------------------------------------------------


@pytest.mark.asyncio
async def test_per_step_sql_comes_from_the_same_call_as_the_intent() -> None:
    """REGRESSION GUARD. `coerce_mint_response` picks the tool call BY NAME, but `_ask_model`
    re-derived it as "the first call with dict arguments" — unfiltered. A model emitting more
    than one dict-argument call (documented live behaviour) had its intent and entries read
    from one call and its per-step SQL from another."""
    stale = "SELECT 'stale draft' AS wrong"
    good = _node_turn([(0, DEPT_SQL), (1, DEPT_SQL)])
    turn = ModelTurnResult(
        tool_calls=[
            ToolCallRequest(
                id="stale",
                name="draft_blueprint_v1",
                arguments={"nodes": [{"order": 0, "sql": stale}, {"order": 1, "sql": stale}]},
            ),
            good.tool_calls[0],
        ]
    )
    minter, store, _ = make_minter([turn])

    minted = await minter.mint(independent_request())

    snapshot = (await store.get(minted.candidate_id)).revalidation
    assert stale not in str(snapshot.sql_by_ref)


def test_duplicate_orders_in_the_node_array_are_refused() -> None:
    """REGRESSION GUARD — this used to keep the LAST answer silently."""
    with pytest.raises(MintResponseError, match="two queries for step 1"):
        coerce_node_sql(
            [
                {"order": 0, "sql": "SELECT 1"},
                {"order": 0, "sql": "SELECT 2"},
                {"order": 1, "sql": "SELECT 3"},
            ],
            expected=2,
        )


def test_out_of_range_negative_and_non_list_node_arrays_are_refused() -> None:
    for raw in (
        [{"order": -1, "sql": "SELECT 1"}, {"order": 1, "sql": "SELECT 2"}],
        [{"order": 0, "sql": "SELECT 1"}, {"order": 7, "sql": "SELECT 2"}],
        {"order": 0, "sql": "SELECT 1"},
        None,
        "nodes",
    ):
        with pytest.raises(MintResponseError):
            coerce_node_sql(raw, expected=2)


@pytest.mark.asyncio
async def test_placeholder_shaped_entries_do_not_crash_the_minter() -> None:
    """The documented live-model failure: every declared property emitted with a placeholder
    (`{"name":"","type":"","binds_to":""}`, `"'N'"` with its quotes, `required` on an
    otherwise-empty slot). These must degrade to a decline, never to a 500."""
    junk = [
        {
            "locator": {"kind": "literal", "value": ""},
            "role": "",
            "slot": {"name": "", "type": "", "binds_to": ""},
        },
        {
            "locator": {"kind": "literal", "value": "'N'"},
            "role": "slot",
            "slot": {"name": "s", "type": "string", "binds_to": "", "required": False},
        },
    ]
    minter, _, _ = make_minter([classify_turn(entries=junk)])

    result = await minter.mint(exact_request())

    assert result.outcome == "declined"
    assert result.candidate_id


@pytest.mark.asyncio
async def test_entries_sent_as_an_object_degrade_to_a_decline() -> None:
    minter, _, _ = make_minter(
        [classify_turn(entries={"0": {"locator": {}, "role": "inline"}})]
    )

    result = await minter.mint(exact_request())

    assert result.outcome == "declined"


@pytest.mark.asyncio
async def test_a_control_character_intent_reaches_the_payload_unescaped() -> None:
    """`_one_line` collapses whitespace with `str.split()`, which does not treat NUL as
    whitespace. Coverage of what actually lands in `intent` — the field dedup embeds, retrieval
    matches on and the reviewer card renders."""
    minter, store, _ = make_minter([classify_turn(intent="total\x00earnings for a dept")])

    result = await minter.mint(exact_request())

    assert "\x00" in (await store.get(result.candidate_id)).payload["intent"]


# --- idempotency ---------------------------------------------------------------------------


def test_two_different_submissions_do_not_collide_on_one_candidate_id() -> None:
    """REGRESSION GUARD. The hash joined the list fields with a single space, so two steps
    `("a", "b")` hashed identically to one step `("a b",)` — and the hash IS the candidate id,
    so two genuinely different blueprints met at one review row."""
    one = exact_request(steps=("filter to earnings rows", "sum gross_pay"))
    two = exact_request(steps=("filter to earnings rows sum gross_pay",))
    three = exact_request(tables=("payroll.payroll_fact", "hr.employees"))
    four = exact_request(tables=("payroll.payroll_fact hr.employees",))

    assert mint_content_hash(one) != mint_content_hash(two)
    assert mint_content_hash(three) != mint_content_hash(four)


def test_reordering_the_steps_changes_the_hash() -> None:
    """Node identity is positional, so a reorder must not collide."""
    a = independent_request(
        nodes=(
            MintNode(step_intent="one", output_name="a"),
            MintNode(step_intent="two", output_name="b"),
        )
    )
    b = independent_request(
        nodes=(
            MintNode(step_intent="two", output_name="b"),
            MintNode(step_intent="one", output_name="a"),
        )
    )

    assert mint_content_hash(a) != mint_content_hash(b)


@pytest.mark.asyncio
async def test_a_resubmission_of_a_moved_row_is_refused_before_paying_for_a_draft() -> None:
    """REGRESSION GUARD. The 409 depends only on the content hash, which is known before the
    model is called — but the store check used to run AFTER `_ask_model`, so a double-clicked
    Draft button bought two drafting turns and one 409."""
    minter, store, client = make_minter([classify_turn(), classify_turn()])
    first = await minter.mint(exact_request())
    env = await store.get(first.candidate_id)
    await store.put(replace(env, status=CandidateStatus.PROMOTED))
    before = len(client.calls)

    with pytest.raises(MintInputError):
        await minter.mint(exact_request())

    assert len(client.calls) == before


@pytest.mark.asyncio
async def test_a_declined_draft_cannot_be_re_minted_and_an_edit_forks_a_new_row() -> None:
    """Coverage of the CONSEQUENCE of keying the guard on existence rather than status.

    Refusing a re-mint over a reviewer's merged entries is right. What it leaves behind is
    worth knowing: a declined draft can only be worked on through the completion form, and any
    edit on the minting page changes the content hash, so it FORKS a second row and the
    declined one stays on the queue with nothing pointing at it. Nothing here reaps it.
    """
    minter, store, _ = make_minter(
        [classify_turn(entries=[]), classify_turn(), classify_turn()]
    )
    request = exact_request()
    declined = await minter.mint(request)
    assert declined.outcome == "declined"

    from data_agent.learning.mint.models import MintConflictError

    with pytest.raises(MintConflictError):
        await minter.mint(request)

    edited = await minter.mint(exact_request(question="what were total earnings, exactly?"))

    assert edited.candidate_id != declined.candidate_id
    assert await store.get(declined.candidate_id) is not None, (
        "the declined row is still on the queue after the expert moved on"
    )


# --- the authored flag ---------------------------------------------------------------------


def test_the_authored_flag_round_trips_and_is_never_conflated_with_reconstructed() -> None:
    from data_agent.learning.candidate.decline import EvidencePointer, ValidationSnapshot

    snap = ValidationSnapshot(
        session_id="mint::abc",
        user_id="",
        trace_id="",
        content_hash="sha256:x",
        accepted_signal="explicit_confirm",
        sql_by_ref={"mint0": ("SELECT 1",)},
        evidence=(EvidencePointer(turn_ref=0, tool_call_ref="mint0"),),
        authored=True,
    )
    doc = snap.to_doc()
    assert doc["authored"] is True and doc["reconstructed"] is False

    back = ValidationSnapshot.from_doc(doc)
    assert back.authored is True and back.reconstructed is False

    # A document written BEFORE the field existed must read as not-authored.
    legacy = dict(doc)
    legacy.pop("authored")
    assert ValidationSnapshot.from_doc(legacy).authored is False
