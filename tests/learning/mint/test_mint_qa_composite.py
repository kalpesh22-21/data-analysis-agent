"""QA sweep #2 over blueprint minting — the COMPOSITE DAG, and the `parse_template` swap.

The headline change under review is that three call sites stopped using a bare
`sqlglot.parse_one` and now go through the runtime's `parse_template`, which rewrites
`{name}` -> `:name` before parsing. Two of those sites (`generalize/rewrite.py` and
`extractor/sql_predicates.py`) are on the MINED path, and the claim made for them is
"identical behaviour for brace-free SQL". This file attacks that claim from the side the
claim does not cover — SQL that contains a brace for a reason that is NOT a slot — and then
walks the composite shapes end to end, through the real validator AND through the corpus
loader that will see the DAG at landing.

Tests whose name begins `test_defect_` FAIL against the implementation as it stands and
assert the behaviour that would be correct. Everything else passes and closes a gap.

⚠ THE TABLES ARE REAL CATALOG TABLES (`dbpcm_warehouse.*`), not the `payroll.payroll_fact`
of `test_mint_engine.py`. That table is absent from the frozen catalog fixture, so every
composite minted with it comes out with `uses == []` and `explain_ok == False` — which the
shipped tests never notice, because they assert the COMPLETER's outcome (`"completed"`) and
not the static validation stamped inside it. Grounding on a table the catalog actually has
is what makes "does this DAG survive landing?" a question with a meaningful answer.
"""

from __future__ import annotations

import asyncio

import pytest

from data_agent.catalog.loader import build_sqlglot_schema_from_catalog
from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.extractor.sql_predicates import literal_predicates
from data_agent.learning.generalize import GeneralizeStage
from data_agent.learning.generalize.mapping import blueprint_seed_from_candidate
from data_agent.learning.generalize.rewrite import rewrite_sql_to_template
from data_agent.learning.inbox import ParameterizationCompleter
from data_agent.learning.leakage import LeakageGateStage
from data_agent.learning.mint import (
    BlueprintMinter,
    MintConflictError,
    MintInputError,
    MintRequest,
    MintResponseError,
)
from data_agent.learning.mint.models import MAX_NODES, MintNode
from data_agent.learning.writer import WriterStage
from data_agent.learning.writer.routing import derive_inbox_reason, route_candidate
from data_agent.runtime.blueprint.compiler import validate_blueprint_dag
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from tests._catalog_fixture import fixture_catalog

from ..extractor.helpers import param_slot

# --- the harness --------------------------------------------------------------------------

_CATALOG = fixture_catalog()
_SCHEMA = build_sqlglot_schema_from_catalog(_CATALOG)
_COLUMNS = tuple(
    sorted(f"{table}.{column}" for table, cols in _SCHEMA.items() for column in cols)
)

PAYROLL = "dbpcm_warehouse.payroll"
TABLES = (PAYROLL,)

# One entry per literal predicate of every query below — the D97 totality walk is the gate a
# minted candidate faces, so every fixture query filters on exactly this one literal.
ENTRIES = [param_slot("department_code", value="0420", table=PAYROLL)]

# The producer every chain starts from: one scalar, one classified literal.
TOTAL_SQL = f"SELECT sum(amount) AS total FROM {PAYROLL} WHERE department_code = '0420'"


def consuming_sql(token: str, alias: str) -> str:
    """A node that genuinely reads `{token}` in an expression position."""
    return (
        f"SELECT sum(amount) / {{{token}}} AS {alias} FROM {PAYROLL} "
        "WHERE department_code = '0420'"
    )


class ScriptedModelClient:
    """Replays queued turns; records what it was asked. Sleeps if asked to."""

    def __init__(self, turns: list[ModelTurnResult], *, delay: float = 0.0) -> None:
        self._turns = list(turns)
        self.calls: list[tuple[list[dict], list[dict]]] = []
        self._delay = delay

    async def send_turn(self, messages: list[dict], tools: list[dict]) -> ModelTurnResult:
        self.calls.append((messages, tools))
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._turns.pop(0) if self._turns else ModelTurnResult(tool_calls=[])


def make_minter(turns, *, store=None, delay: float = 0.0, **kwargs):
    store = store if store is not None else InMemoryCandidateStore()
    stages = (
        GeneralizeStage(catalog_schema=_SCHEMA),
        LeakageGateStage(candidate_store=store),
        WriterStage(sampler=lambda env: False),
    )
    client = ScriptedModelClient(turns, delay=delay)
    minter = BlueprintMinter(
        model_client=client,
        completer=ParameterizationCompleter(
            store=store, known_rules=frozenset(), rule_index=None, stages=stages
        ),
        catalog_columns=_COLUMNS,
        **kwargs,
    )
    return minter, store, client


def composite_turn(sqls: list[str], *, entries=None, intent="a share of departmental pay"):
    return ModelTurnResult(
        tool_calls=[
            ToolCallRequest(
                id="m1",
                name="draft_blueprint",
                arguments={
                    "intent": intent,
                    "nodes": [{"order": i, "sql": s} for i, s in enumerate(sqls)],
                    "entries": ENTRIES if entries is None else entries,
                    "rationale": "drafted",
                },
            )
        ]
    )


def classify_turn(*, entries=None, intent="a share of departmental pay"):
    return ModelTurnResult(
        tool_calls=[
            ToolCallRequest(
                id="m1",
                name="classify_blueprint",
                arguments={
                    "intent": intent,
                    "entries": ENTRIES if entries is None else entries,
                    "rationale": "classified",
                },
            )
        ]
    )


def request(nodes=(), **overrides) -> MintRequest:
    kwargs = {
        "question": "what share of the department's pay is one slice?",
        "tables": TABLES,
        "sql_mode": "none",
        "nodes": nodes,
    }
    kwargs.update(overrides)
    return MintRequest(**kwargs)


async def mint(nodes, sqls, *, entries=None, **overrides):
    """Mint one composite and hand back `(result, envelope)`."""
    minter, store, _ = make_minter([composite_turn(sqls, entries=entries)])
    result = await minter.mint(request(nodes, **overrides))
    return result, await store.get(result.candidate_id)


def static_validation(env: CandidateEnvelope) -> dict:
    return (env.payload.get("generalization") or {}).get("static_validation") or {}


def node_templates(env: CandidateEnvelope) -> list[str]:
    gen = env.payload.get("generalization") or {}
    return [n["sql_template"] for n in sorted(gen["node_templates"], key=lambda n: n["order"])]


def land(env: CandidateEnvelope) -> None:
    """Project the candidate onto a landing seed and run the CORPUS LOADER's own DAG gate.

    This is the authority `check_dag` documents itself as unable to be — it sees the node
    TEMPLATES, so it is the only thing that can check a table-consume placeholder really
    appears as a `scratch.<name>` source, and the only thing that rejects a template token
    nobody declared. Raises `CorpusLoadError` exactly as the landing write would.
    """
    validate_blueprint_dag(blueprint_seed_from_candidate(env, id="bp::minted"))


# ==========================================================================================
# 1. THE `parse_template` SWAP — braces that are not slots
# ==========================================================================================


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT map('a', 1) AS m, amount FROM {PAYROLL} WHERE department_code = '0420'",
        f"SELECT {{}} AS m, amount FROM {PAYROLL} WHERE department_code = '0420'",
        f"SELECT {{'k': 'v'}} AS m, amount FROM {PAYROLL} WHERE department_code = '0420'",
    ],
)
def test_a_real_clickhouse_map_literal_is_untouched_by_the_new_parse(sql: str) -> None:
    """The claim "identical behaviour for brace-free SQL" is really a claim about braces
    that are NOT slot tokens, because ClickHouse spells a map with them. `SLOT_TOKEN` is
    strict — `{` must be followed by an identifier and then `}` — so `{}`, `{'k':'v'}` and
    `map(...)` never match, and the rewrite is a no-op on them. Pinned so a looser regex
    cannot be introduced without something failing."""
    import sqlglot

    from data_agent.runtime.blueprint.template import parse_template

    old = sqlglot.parse_one(sql, dialect="clickhouse").sql(dialect="clickhouse")
    new = parse_template(sql).sql(dialect="clickhouse")
    assert new == old


def test_a_map_literal_still_generalizes_to_the_same_template() -> None:
    """The same fact one level up, at the site that actually rewrites a mined candidate."""
    sql = (
        f"SELECT map('a', 1) AS m, sum(amount) AS total FROM {PAYROLL} "
        "WHERE department_code = '0420' GROUP BY m"
    )
    template = rewrite_sql_to_template(sql, ENTRIES, strict=True)
    assert "map('a', 1)" in template
    assert "{department_code}" in template


def test_a_brace_token_inside_a_string_literal_is_not_a_slot() -> None:
    """REGRESSION GUARD — found during this review, fixed mid-review by `_SKIP_OR_SLOT`.

    `_SLOT_TOKEN.sub` used to run over the raw SQL text with no idea where the string
    literals were, so routing `parse_accepted_sql` through `parse_template` turned
    `check_number = '{dept_total}'` into `check_number = ':dept_total'` BEFORE sqlglot ever
    saw it, and the template that landed carried the mutated constant. Nothing complained:
    it re-parsed, it was still a single read-only SELECT, and `_check_rewritten` compares
    function SHAPES, which were unchanged. On the MINED path that is the D97
    silent-wrong-answer class — the blueprint runs, matches nothing for ever, and every
    static check says `ok`.
    """
    sql = (
        f"SELECT sum(amount) AS total FROM {PAYROLL} "
        "WHERE department_code = '0420' AND check_number = '{dept_total}'"
    )
    template = rewrite_sql_to_template(sql, ENTRIES, strict=False)

    assert "':dept_total'" not in template, template
    assert "'{dept_total}'" in template, template


def test_the_totality_walk_names_the_predicate_the_sql_actually_contains() -> None:
    """REGRESSION GUARD — the same mutation, seen by D97's enumerator.

    `literal_predicates` parses through `parse_template` now, so before the fix it reported
    `check_number = ':dept_total'`: a literal appearing nowhere in the SQL the reviewer is
    looking at, which no honest entry could ever satisfy — an unresolvable decline rather
    than a fixable one.
    """
    sql = (
        f"SELECT sum(amount) AS total FROM {PAYROLL} "
        "WHERE check_number = '{dept_total}'"
    )
    found = literal_predicates(sql) or []
    assert [p.value for p in found] == ["{dept_total}"], found


def test_the_restore_loop_does_not_invent_tokens_in_brace_free_sql() -> None:
    """The render-time restore now iterates `slot_names | referenced_slots(accepted_sql)`.
    For ordinary mined SQL the second set is empty, so the loop is exactly what it was."""
    template = rewrite_sql_to_template(TOTAL_SQL, ENTRIES, strict=True)
    assert template.count("{") == 1
    assert "{department_code}" in template


# ==========================================================================================
# 2. `_check_nodes` — the edge that is a lie
# ==========================================================================================


@pytest.mark.asyncio
async def test_a_scalar_edge_whose_token_lives_only_in_a_comment_is_refused() -> None:
    """REGRESSION GUARD — `_check_nodes` claims a property `referenced_slots` did not have.

    Its docstring says: "`referenced_slots` is the runtime's own reader of `{token}`
    occurrences … Asking it beats a substring search, which would count a token inside a
    string literal or a comment." That was FALSE until `_SKIP_OR_SLOT` landed:
    `referenced_slots` was `_SLOT_TOKEN.findall` over raw text, so a comment counted, the
    fictional edge was admitted, and the composite minted `completed` with static validation
    `ok` and landed cleanly through the corpus loader — carrying
    `consumes: {"total": "$0.total"}` on a node whose template never mentions `total`
    (sqlglot drops the trailing comment on render). Exactly the failure `_check_nodes`
    documents: "That value would be bound and then ignored, so the step would silently
    answer a different question."
    """
    nodes = (
        MintNode(step_intent="the total", output_name="total"),
        MintNode(step_intent="the share", output_name="share", feeds_from=(0,)),
    )
    liar = (
        f"SELECT sum(amount) AS share FROM {PAYROLL} "
        "WHERE department_code = '0420' -- needs {total}"
    )
    with pytest.raises(MintResponseError, match="never uses"):
        await mint(nodes, [TOTAL_SQL, liar])


@pytest.mark.asyncio
async def test_a_scalar_edge_whose_token_lives_only_in_a_string_is_refused() -> None:
    """REGRESSION GUARD — the same hole through the other door named in the same docstring.

    Before the fix a `{total}` inside a string literal satisfied `_check_nodes`, and what
    stopped the row landing was an accident: the string had already been mutated to
    `':total'`, so the totality walk declined it for a predicate that does not exist — the
    candidate died with a message about the wrong thing.
    """
    nodes = (
        MintNode(step_intent="the total", output_name="total"),
        MintNode(step_intent="the share", output_name="share", feeds_from=(0,)),
    )
    liar = (
        f"SELECT sum(amount) AS share FROM {PAYROLL} "
        "WHERE department_code = '0420' AND check_number != '{total}'"
    )
    with pytest.raises(MintResponseError, match="never uses"):
        await mint(nodes, [TOTAL_SQL, liar])


@pytest.mark.asyncio
async def test_a_node_token_nobody_declared_is_refused_before_it_is_filed() -> None:
    """DEFECT — `_check_nodes` checks declared-edge ⇒ referenced, never the converse.

    A node whose SQL carries `{total}` while declaring `feeds_from=()` mints `completed`
    with `static_validation.outcome == "ok"`. It is the corpus loader that refuses it, at
    the landing write, with "references undeclared slot(s) ['total']" — past the review
    valve, after a human has approved it.

    This is precisely the class `MintRequest` already fixed for the node CAP ("a 17-step
    submission was accepted, paid for a drafting turn, was persisted, and only then declined
    … a guaranteed-dead review row and a wasted model call, for a limit that was knowable
    from the form alone"). Same shape, still open, and knowable from the SQL alone.

    Now refused at mint, so no row is filed at all.
    """
    nodes = (
        MintNode(step_intent="the total", output_name="total"),
        MintNode(step_intent="the share", output_name="share"),  # no edge declared
    )

    with pytest.raises(
        (MintInputError, MintResponseError), match="declares no step it comes from"
    ):
        await mint(nodes, [TOTAL_SQL, consuming_sql("total", "share")])


# ==========================================================================================
# 3. COMPOSITE SHAPES — end to end, and through the loader
# ==========================================================================================


@pytest.mark.asyncio
async def test_a_four_node_chain_mints_validates_and_survives_the_loader() -> None:
    """Everything shipped is two or three nodes. A chain deep enough that node 4's value
    depends on node 1 only transitively exercises the `consumes` derivation per hop."""
    nodes = (
        MintNode(step_intent="s1", output_name="a"),
        MintNode(step_intent="s2", output_name="b", feeds_from=(0,)),
        MintNode(step_intent="s3", output_name="c", feeds_from=(1,)),
        MintNode(step_intent="s4", output_name="d", feeds_from=(2,)),
    )
    result, env = await mint(
        nodes,
        [
            TOTAL_SQL,
            consuming_sql("a", "b"),
            consuming_sql("b", "c"),
            consuming_sql("c", "d"),
        ],
    )

    assert result.outcome == "completed", result.decline_detail
    assert static_validation(env)["outcome"] == "ok", static_validation(env)
    composes = env.payload["composes"]
    assert [n["feeds_from"] for n in composes] == [[], [0], [1], [2]]
    assert [n["consumes"] for n in composes] == [
        {},
        {"a": "$0.a"},
        {"b": "$1.b"},
        {"c": "$2.c"},
    ]
    # Every node template keeps its brace token — not `map()` and not sqlglot's round-trip
    # `{name: }` parameter form.
    templates = node_templates(env)
    assert "{a}" in templates[1] and "{b}" in templates[2] and "{c}" in templates[3]
    assert not any("map()" in t or ": }" in t for t in templates)
    land(env)


@pytest.mark.asyncio
async def test_the_four_refs_of_a_composite_all_agree() -> None:
    """`sql_by_ref`, the evidence pointers, `payload.source_tool_call_refs` and each
    `composes[].source_tool_call_ref` are four independent statements about the same set of
    queries. A composite is the only shape where they can disagree."""
    nodes = tuple(
        MintNode(step_intent=f"s{i}", output_name=f"n{i}", feeds_from=((i - 1,) if i else ()))
        for i in range(4)
    )
    sqls = [TOTAL_SQL] + [consuming_sql(f"n{i - 1}", f"n{i}") for i in range(1, 4)]
    _result, env = await mint(nodes, sqls)

    refs = ["mint0", "mint1", "mint2", "mint3"]
    snapshot = env.revalidation
    assert list(snapshot.sql_by_ref) == refs
    assert [e.tool_call_ref for e in snapshot.evidence] == refs
    assert {e.turn_ref for e in snapshot.evidence} == {0}
    assert env.payload["source_tool_call_refs"] == refs
    assert [n["source_tool_call_ref"] for n in env.payload["composes"]] == refs
    # And the SQL filed under each ref is the SQL for that node, in order.
    assert [snapshot.sql_by_ref[r][0] for r in refs] == sqls


@pytest.mark.asyncio
async def test_two_nodes_can_consume_the_same_upstream() -> None:
    """A fan-OUT. Every shipped composite fans in or chains; nothing covered one output
    feeding two consumers, which is where a `consumes` derivation keyed on the wrong node
    would show up."""
    nodes = (
        MintNode(step_intent="s0", output_name="a"),
        MintNode(step_intent="s1", output_name="b", feeds_from=(0,)),
        MintNode(step_intent="s2", output_name="c", feeds_from=(0,)),
    )
    result, env = await mint(
        nodes, [TOTAL_SQL, consuming_sql("a", "b"), consuming_sql("a", "c")]
    )

    assert result.outcome == "completed", result.decline_detail
    assert static_validation(env)["outcome"] == "ok"
    composes = env.payload["composes"]
    assert composes[1]["consumes"] == {"a": "$0.a"}
    assert composes[2]["consumes"] == {"a": "$0.a"}
    land(env)


@pytest.mark.asyncio
async def test_a_duplicated_edge_is_carried_into_the_corpus_verbatim() -> None:
    """`feeds_from=[0, 0]` — the form's checkboxes cannot produce it, a crafted POST can.

    It is accepted everywhere: `MintRequest` checks each edge is backward, `check_dag`
    iterates without de-duplicating, `consumes` collapses to one key because it is a dict,
    and the loader lands it. So the landed DAG carries an edge twice while its `consumes`
    mentions the upstream once. Pinned as OBSERVED behaviour, not asserted as correct — the
    honest fix is to normalize the edge list where every other edge rule already lives.
    """
    nodes = (
        MintNode(step_intent="s0", output_name="a"),
        MintNode(step_intent="s1", output_name="b", feeds_from=(0, 0)),
    )
    result, env = await mint(nodes, [TOTAL_SQL, consuming_sql("a", "b")])

    assert result.outcome == "completed", result.decline_detail
    assert env.payload["composes"][1]["feeds_from"] == [0, 0]
    assert env.payload["composes"][1]["consumes"] == {"a": "$0.a"}
    land(env)


@pytest.mark.asyncio
async def test_a_table_intermediate_now_passes_static_validation() -> None:
    """DEFECT — the `table` output kind is offered and cannot produce a promotable blueprint.

    `_provenance_uses` runs the D69 column-provenance extractor over EVERY node template
    against the catalog schema. A consumer node reads `scratch.<name>`, which is not a
    catalog table, so the extractor raises and the whole composite comes back
    `explain_ok=False, uses=[], read_only_select=False` ⇒ `fail_to_review/explain_failed`.
    With `uses == []` the blueprint can also never land: the loader refuses the PRODUCER for
    reading a table outside an empty footprint.

    The runtime executes this shape (the canon ships
    `bp-earnings-by-department-via-scratch-join`) and `check_dag` was taught to accept it —
    that parity work stopped at the DAG gate and never reached the provenance walk. The
    minting page nevertheless offers "passes on a table" in a dropdown.

    The shipped test `test_a_step_can_consume_an_earlier_steps_whole_table` is green because
    it asserts the COMPLETER's outcome and the `composes` shape, never the static validation
    — and its table is absent from the catalog, so its `uses` is empty for a second reason.

    Observed: outcome `fail_to_review`, reason `explain_failed`, `uses == []`.
    Expected: `ok`, with `uses` naming the producer's warehouse columns and the scratch
              source excluded (which is exactly what the loader does for the same shape).
    """
    nodes = (
        MintNode(step_intent="per-employee pay", output_name="emp_pay", output_kind="table"),
        MintNode(step_intent="roll it up", output_name="rollup", feeds_from=(0,)),
    )
    producer = (
        f"SELECT employee_code, sum(amount) AS earn FROM {PAYROLL} "
        "WHERE department_code = '0420' GROUP BY employee_code"
    )
    consumer = "SELECT sum(x.earn) AS rollup FROM scratch.emp_pay AS x"
    result, env = await mint(nodes, [producer, consumer])

    # The DAG itself is right — the defect is entirely in the provenance walk.
    assert env.payload["composes"][0]["output"] == {"emp_pay": "table"}
    assert env.payload["composes"][1]["consumes"] == {"emp_pay": "$0"}
    assert result.outcome == "completed", result.decline_detail

    stamp = static_validation(env)
    assert stamp["outcome"] == "ok", stamp
    assert "dbpcm_warehouse.payroll.amount" in (env.payload["generalization"]["uses"])
    land(env)


@pytest.mark.asyncio
async def test_a_node_consuming_a_scalar_and_a_table_validates() -> None:
    """DEFECT — the mixed shape, blocked by the same provenance walk.

    Worth its own case because the DAG half is genuinely correct and previously untested:
    one node with two edges of DIFFERENT kinds derives `{"a": "$0.a", "tbl": "$1"}`, the two
    consume grammars side by side. Only the `scratch.` source in the template kills it.
    """
    nodes = (
        MintNode(step_intent="the total", output_name="a"),
        MintNode(step_intent="per-employee pay", output_name="tbl", output_kind="table"),
        MintNode(step_intent="both", output_name="both", feeds_from=(0, 1)),
    )
    producer = (
        f"SELECT employee_code, sum(amount) AS earn FROM {PAYROLL} "
        "WHERE department_code = '0420' GROUP BY employee_code"
    )
    consumer = "SELECT sum(x.earn) / {a} AS both FROM scratch.tbl AS x"
    _result, env = await mint(nodes, [TOTAL_SQL, producer, consumer])

    assert env.payload["composes"][2]["consumes"] == {"a": "$0.a", "tbl": "$1"}
    assert static_validation(env)["outcome"] == "ok", static_validation(env)


@pytest.mark.asyncio
async def test_the_node_cap_boundary_mints_and_one_more_is_refused_at_the_form() -> None:
    """`MAX_NODES` is imported from `check_dag`'s own `_MAX_NODES`. Both ends of the
    boundary, because an off-by-one either way is a dead review row or a lost capability."""
    assert MAX_NODES == 16
    nodes = tuple(
        MintNode(
            step_intent=f"step {i}",
            output_name=f"n{i}",
            feeds_from=((i - 1,) if i else ()),
        )
        for i in range(MAX_NODES)
    )
    sqls = [TOTAL_SQL] + [
        consuming_sql(f"n{i - 1}", f"n{i}") for i in range(1, MAX_NODES)
    ]
    result, env = await mint(nodes, sqls)
    assert result.outcome == "completed", result.decline_detail
    assert len(env.payload["composes"]) == MAX_NODES
    assert static_validation(env)["outcome"] == "ok"
    land(env)

    with pytest.raises(MintInputError, match="at most 16 steps"):
        request(nodes + (MintNode(step_intent="one too many", output_name="extra"),))


def test_an_output_name_cannot_be_reused_at_a_different_order() -> None:
    """Including the DEFAULTED name: an unnamed step 0 is `step_0`, so a later step that
    explicitly names itself `step_0` must collide with it rather than shadow it."""
    with pytest.raises(MintInputError, match="share an output name"):
        request(
            (
                MintNode(step_intent="unnamed"),
                MintNode(step_intent="named after the first", output_name="step_0"),
            )
        )
    with pytest.raises(MintInputError, match="share an output name"):
        request(
            (
                MintNode(step_intent="a", output_name="dup"),
                MintNode(step_intent="b", output_name="dup"),
            )
        )


@pytest.mark.asyncio
async def test_output_kinds_may_be_mixed_across_one_dag() -> None:
    """A DAG whose nodes declare different kinds. The `output` map is per node, so a `table`
    producer and a `scalar` producer coexist — asserted on the DAG the mint FILES, which is
    the part that is correct today (see the two defect cases above for what happens next)."""
    nodes = (
        MintNode(step_intent="scalar producer", output_name="a"),
        MintNode(step_intent="table producer", output_name="tbl", output_kind="table"),
        MintNode(step_intent="scalar consumer", output_name="b", feeds_from=(0,)),
    )
    producer = (
        f"SELECT employee_code, sum(amount) AS earn FROM {PAYROLL} "
        "WHERE department_code = '0420' GROUP BY employee_code"
    )
    _result, env = await mint(nodes, [TOTAL_SQL, producer, consuming_sql("a", "b")])
    assert [n["output"] for n in env.payload["composes"]] == [
        {"a": "scalar"},
        {"tbl": "table"},
        {"b": "scalar"},
    ]


# ==========================================================================================
# 4. 400 vs 409 vs 502 — which party is being blamed
# ==========================================================================================


@pytest.mark.asyncio
async def test_an_unknown_table_is_an_input_error_and_never_a_conflict() -> None:
    """`MintConflictError` subclasses `MintInputError`, so the ORDER of the two `except`
    arms in the HTTP route is the only thing keeping a typo from answering 409. Asserted
    from the type side: an unknown table must not be a conflict."""
    minter, _store, client = make_minter([classify_turn()])
    req = request(tables=("dbpcm_warehouse.not_a_table",))

    with pytest.raises(MintInputError) as caught:
        await minter.mint(req)
    assert not isinstance(caught.value, MintConflictError)
    # And the model was never called — the check is knowable from the submission alone.
    assert client.calls == []


@pytest.mark.asyncio
async def test_re_minting_the_same_submission_is_a_conflict_before_any_model_call() -> None:
    nodes = (
        MintNode(step_intent="the total", output_name="a"),
        MintNode(step_intent="the share", output_name="b", feeds_from=(0,)),
    )
    sqls = [TOTAL_SQL, consuming_sql("a", "b")]
    minter, store, client = make_minter([composite_turn(sqls), composite_turn(sqls)])
    await minter.mint(request(nodes))
    assert len(client.calls) == 1

    with pytest.raises(MintConflictError, match="already minted"):
        await minter.mint(request(nodes))
    assert len(client.calls) == 1, "a 409 must not bill a second drafting turn"
    assert isinstance(MintConflictError("x"), MintInputError)


@pytest.mark.asyncio
async def test_defect_the_experts_own_broken_sql_is_reported_as_a_model_fault() -> None:
    """DEFECT — a 502 for something the expert typed.

    In `sql_mode='exact'` the per-step SQL comes from the EXPERT and the model is given the
    classify tool, which has no field to write SQL in. `_check_nodes` nevertheless raises
    `MintResponseError` for it, and the HTTP route maps that to 502 with the comment "The
    expert did nothing wrong and the deployment is not broken — a model answered against a
    contract this system does not have". Here the expert did do something wrong, can fix it,
    and is told the service is broken.

    The message compounds it: "To use an earlier step's result, write its name in braces …
    not the $0.name form, which is the DAG's own wiring notation" is advice for whoever
    wrote the query — delivered as a bad-gateway error the page renders as a failure.

    Observed: `MintResponseError` (⇒ 502). Expected: `MintInputError` (⇒ 400).
    """
    nodes = (
        MintNode(
            step_intent="the total",
            output_name="a",
            sql=TOTAL_SQL,
        ),
        MintNode(
            step_intent="the share",
            output_name="b",
            feeds_from=(0,),
            # The exact mistake `_check_nodes` documents, typed by a human this time.
            sql=f"SELECT sum(amount) / $0.a AS b FROM {PAYROLL}",
        ),
    )
    minter, _store, _client = make_minter([classify_turn()])

    with pytest.raises(MintInputError):
        await minter.mint(request(nodes, sql_mode="exact"))


# ==========================================================================================
# 5. THE TIMEOUT
# ==========================================================================================


@pytest.mark.asyncio
async def test_a_slow_model_is_cut_off_and_leaves_nothing_behind() -> None:
    """`timeout_seconds` is applied (`asyncio.wait_for`), and NOTHING is persisted when it
    fires — the envelope is written only after the model answers, so the promise in the
    error message ("re-submitting the same form is safe") is true rather than hopeful.

    Checked across every queue, because a half-written row at `needs_parameterization` would
    turn the retry into a 409 on a candidate that carries no work."""
    from data_agent.learning.mint import MintUnavailableError

    minter, store, _ = make_minter([classify_turn()], delay=5.0, timeout_seconds=0.05)

    with pytest.raises(MintUnavailableError, match="did not answer within"):
        await minter.mint(request())

    for status in (
        CandidateStatus.EXTRACTED,
        CandidateStatus.NEEDS_PARAMETERIZATION,
        CandidateStatus.IN_REVIEW,
        CandidateStatus.CANDIDATE,
        CandidateStatus.VALIDATED,
        CandidateStatus.REJECTED,
    ):
        assert await store.list_by_status(status) == []


@pytest.mark.asyncio
async def test_a_timed_out_mint_is_a_mapped_error_and_not_an_unhandled_500() -> None:
    """REGRESSION GUARD — found during this review, fixed mid-review.

    `learning/inbox/service.py::mint` maps five exception types. A bare `TimeoutError` out of
    `asyncio.wait_for` was none of them and there is no exception handler on the app, so the
    expert who typed a whole blueprint and waited out the ceiling got `500 Internal Server
    Error` with no detail and no advice to retry. The sibling surface built for the same
    reason does the opposite (`revise/engine.py` answers "the assistant timed out; try
    again"), and minting is the more expensive of the two to lose.

    Pinned as a TYPE assertion rather than a message one: what makes the error usable is
    that the route already has an arm for it.
    """
    from data_agent.learning.mint import MintUnavailableError

    minter, _store, _ = make_minter([classify_turn()], delay=5.0, timeout_seconds=0.05)

    with pytest.raises(Exception) as caught:  # noqa: PT011 - the point is WHICH type
        await minter.mint(request())
    assert isinstance(
        caught.value, (MintInputError, MintResponseError, MintUnavailableError)
    ), type(caught.value)
    assert type(caught.value) is not TimeoutError


def test_the_mint_timeout_stays_below_the_bff_hop_budget() -> None:
    """REGRESSION GUARD — the shipped default briefly violated the invariant its own
    docstring states.

    `learning_mint_timeout_seconds` says: "Must stay BELOW the BFF's
    `_INBOX_MODEL_HOP_TIMEOUT_SECONDS` so the upstream's own graceful message reaches the
    browser instead of a bare 502." Both were 180.0. The BFF's read clock starts when the
    request is sent; the upstream's `wait_for` starts later, after body parsing, request
    validation and the prior-art embedding search — so on a genuinely slow model the BFF
    fires FIRST and the browser gets the bare gateway error the value exists to prevent.
    An EQUALITY is not headroom.
    """
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root))
    from ui.server import _INBOX_MODEL_HOP_TIMEOUT_SECONDS

    from data_agent.learning.config import LearningSettings

    configured = LearningSettings.model_fields["learning_mint_timeout_seconds"].default
    assert configured < _INBOX_MODEL_HOP_TIMEOUT_SECONDS, (
        f"mint ceiling {configured}s vs BFF hop {_INBOX_MODEL_HOP_TIMEOUT_SECONDS}s"
    )


# ==========================================================================================
# 6. ROUTING — `authored` never auto-lands, and never outranks a defect
# ==========================================================================================


def _clean_blueprint(**overrides) -> CandidateEnvelope:
    """A blueprint clean on every routing axis: it auto-lands unless something says not to."""
    env = CandidateEnvelope(
        candidate_id="candidate::r",
        type="blueprint",
        status=CandidateStatus.EXTRACTED,
        payload={
            "intent": "total pay by department",
            "generalization": {"static_validation": {"outcome": "ok"}},
        },
        source_session="s",
        source_trace="t",
        evidence_refs=(),
        extractor_rationale="",
        entity_scan={"result": "pass", "hits": []},
        confidence=0.9,
        proposed_action="new",
        depends_on=(),
        content_hash="h",
    )
    from dataclasses import replace

    return replace(env, **overrides) if overrides else env


def _authored_snapshot(**overrides):
    from data_agent.learning.candidate.decline import EvidencePointer, ValidationSnapshot

    kwargs = {
        "session_id": "mint::abc",
        "user_id": "",
        "trace_id": "",
        "content_hash": "sha256:h",
        "accepted_signal": "explicit_confirm",
        "sql_by_ref": {"mint0": (TOTAL_SQL,)},
        # REQUIRED for `from_doc` to rehydrate at all — the guard is derived from what the
        # completion path reads (identity, SQL, at least one citation).
        "evidence": (EvidencePointer(turn_ref=0, tool_call_ref="mint0"),),
        "authored": True,
    }
    kwargs.update(overrides)
    return ValidationSnapshot(**kwargs)


def test_a_mined_blueprint_still_auto_lands() -> None:
    """The flywheel is untouched: no snapshot, or a snapshot that is not `authored`."""
    decision = route_candidate(_clean_blueprint(), sampled_for_inbox=False)
    assert (decision.status, decision.control, decision.reason) == (
        CandidateStatus.CANDIDATE,
        "continue",
        None,
    )
    mined = _clean_blueprint(revalidation=_authored_snapshot(authored=False))
    assert route_candidate(mined, sampled_for_inbox=False).status == CandidateStatus.CANDIDATE


def test_an_authored_blueprint_is_always_reviewed() -> None:
    env = _clean_blueprint(revalidation=_authored_snapshot())
    decision = route_candidate(env, sampled_for_inbox=False)
    assert (decision.status, decision.control, decision.reason) == (
        CandidateStatus.IN_REVIEW,
        "route_inbox",
        "hand_authored",
    )
    assert derive_inbox_reason(env) == "hand_authored"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {
                "payload": {
                    "intent": "i",
                    "generalization": {"static_validation": {"outcome": "fail_to_review"}},
                }
            },
            "fail_to_review",
        ),
        ({"entity_scan": {"result": "quarantine", "hits": [], "scanned_fields": ["intent"],
           "scanner": "regex+ner"}}, "leakage_near_miss"),
        ({"entity_scan": {"result": "pending", "hits": []}}, "fail_to_review"),
    ],
)
def test_a_defect_reason_outranks_hand_authored(overrides, expected) -> None:
    """`hand_authored` is not a defect — it says why the row exists. Anything WRONG with the
    row must still be the reason a reviewer sees, and `route_candidate` and
    `derive_inbox_reason` must agree about it."""
    env = _clean_blueprint(revalidation=_authored_snapshot(), **overrides)
    decision = route_candidate(env, sampled_for_inbox=False)
    assert decision.reason == expected
    assert derive_inbox_reason(env) == expected


def test_a_dedup_conflict_outranks_hand_authored() -> None:
    from data_agent.learning.candidate.verdicts import DedupVerdict

    env = _clean_blueprint(
        revalidation=_authored_snapshot(),
        dedup=DedupVerdict(
            canonical_key="sha256:k",
            matched_id="bp-x",
            similarity=1.0,
            action="conflict",
            layer="soft",
        ),
    )
    assert route_candidate(env, sampled_for_inbox=False).reason == "dedup_conflict"
    assert derive_inbox_reason(env) == "dedup_conflict"


def test_the_authored_flag_round_trips_through_the_store() -> None:
    """A routing rule keyed on a persisted field is only as good as its rehydration."""
    from data_agent.learning.candidate.decline import ValidationSnapshot

    snapshot = _authored_snapshot()
    assert ValidationSnapshot.from_doc(snapshot.to_doc()).authored is True
    without = snapshot.to_doc()
    without.pop("authored")
    assert ValidationSnapshot.from_doc(without).authored is False


@pytest.mark.asyncio
async def test_a_real_minted_candidate_lands_in_review_as_hand_authored() -> None:
    """The routing rule, reached the way production reaches it, rather than from a fixture."""
    minter, store, _ = make_minter([classify_turn()])
    result = await minter.mint(
        request(sql_mode="exact", sql=TOTAL_SQL)
    )
    env = await store.get(result.candidate_id)
    assert result.outcome == "completed", result.decline_detail
    assert env.status == CandidateStatus.IN_REVIEW
    assert env.revalidation.authored is True
    assert derive_inbox_reason(env) == "hand_authored"


# ==========================================================================================
# 7. THE TRIAL-TOKEN SCRUB
# ==========================================================================================

_JWT = "eyJhbGciOiJIUzI1NiJ9." + "P" * 40 + ".sIgNaTuRe9876543210abcd"


def test_an_exact_token_and_a_truncated_one_are_both_removed() -> None:
    from data_agent.learning.inbox.inbox import _scrub

    assert _JWT not in _scrub(f"POST /runQuery Bearer {_JWT} -> 401", _JWT)
    assert _JWT[:30] not in _scrub(f"Bearer {_JWT[:30]}…", _JWT)


def test_the_minimum_run_is_a_real_boundary_not_an_approximation() -> None:
    from data_agent.learning.inbox.inbox import _MIN_SECRET_RUN, _scrub

    token = "Q" * 60
    at_boundary = token[:_MIN_SECRET_RUN]
    below = token[: _MIN_SECRET_RUN - 1]
    assert at_boundary not in _scrub(f"x {at_boundary} y", token)
    # One character short is left alone BY DESIGN (shared JWT header boilerplate).
    assert below in _scrub(f"x {below} y", token)


def test_a_token_shorter_than_the_minimum_run_is_still_removed_when_whole() -> None:
    from data_agent.learning.inbox.inbox import _scrub

    short = "tiny-token-123"
    assert short not in _scrub(f"Bearer {short} failed", short)


def test_defect_a_line_wrapped_token_leaves_its_signature_behind() -> None:
    """DEFECT — the case the docstring names is the case it does not handle.

    `_scrub` says it removes "any long run of it: some clients truncate or LINE-WRAP a
    header when they quote it back". Wrapping splits a token into a head and a TAIL. The
    loop only ever tries PREFIXES, so the head is redacted and the tail — for a JWT, the
    signature — is written verbatim into both the JSON the browser renders and this
    process's log.

    Observed: the 45-character tail survives.  Expected: neither half survives.
    """
    from data_agent.learning.inbox.inbox import _scrub

    wrapped = f"headers: authorization: Bearer {_JWT[:40]}\n{_JWT[40:]}\n(request failed)"
    scrubbed = _scrub(wrapped, _JWT)
    assert _JWT[40:] not in scrubbed, scrubbed


def test_defect_only_the_longest_truncation_is_scrubbed() -> None:
    """DEFECT — the loop `break`s after the first prefix it finds.

    A client that quotes the credential twice at different truncations (a URL and a
    retry-after log line, say) gets the longer one redacted and the shorter one left whole.

    Observed: the 30-character prefix survives.  Expected: every run of >= 24 characters is
    removed, which means not stopping at the first hit.
    """
    from data_agent.learning.inbox.inbox import _scrub

    text = f"url={_JWT[:45]} … retried with {_JWT[:30]}"
    scrubbed = _scrub(text, _JWT)
    assert _JWT[:30] not in scrubbed, scrubbed


def test_a_percent_encoded_token_survives_the_scrub_intact() -> None:
    """OBSERVED, not asserted as correct — and the docstring says so ("WHAT THIS CANNOT DO
    is reach a re-encoded token … and no string scrub can").

    Pinned so the limit is a measured fact rather than a claim. A token whose every
    24-character window contains a character percent-encoding rewrites (`+`, `/`) survives
    URL-encoding COMPLETELY: no window of the raw token appears in the encoded text.

    Not filed as a defect because an HTTP client's exception text quotes headers verbatim
    and this surface never puts the credential in a query string. It is the next hole if
    that ever changes — and the honest repair is to stop relaying provider exception text
    at all, not to enumerate encodings.
    """
    import urllib.parse

    from data_agent.learning.inbox.inbox import _scrub

    token = "+".join("abcdefgh" for _ in range(9))  # a `+` every 9 chars
    encoded = urllib.parse.quote(token, safe="")
    assert len(token) > 24
    assert encoded in _scrub(f"GET /q?token={encoded} -> 401", token)
    assert urllib.parse.unquote(encoded) == token


# ==========================================================================================
# 8. THE `_SKIP_OR_SLOT` FIX IS PARTIAL — the other readers of the same grammar
# ==========================================================================================
#
# `referenced_slots` and `parse_template` now skip braces inside strings and comments. Five
# other call sites still use the RAW `SLOT_TOKEN` regex over template text, so they read a
# different grammar from the one the executor binds. These are the two that a reviewer can
# actually reach.


def _template_with_a_brace_in_a_string() -> str:
    return (
        f"SELECT sum(amount) AS total FROM {PAYROLL} "
        "WHERE department_code = {department_code} AND check_number = '{dept_total}'"
    )


def test_the_two_readers_of_a_template_now_disagree_about_its_slots() -> None:
    """The divergence in one assertion, before the two consequences below.

    `referenced_slots` is the fixed reader; `SLOT_TOKEN.findall` is what five other sites
    still do. They now answer differently for the same template, which is worse than both
    being wrong together — the previous state was at least self-consistent.
    """
    from data_agent.runtime.blueprint.template import SLOT_TOKEN, referenced_slots

    template = _template_with_a_brace_in_a_string()
    assert referenced_slots(template) == {"department_code"}
    assert set(SLOT_TOKEN.findall(template)) == {"department_code", "dept_total"}


def test_defect_the_review_card_offers_a_slot_the_trial_run_will_ignore() -> None:
    """DEFECT — the card and the trial disagree about what the reviewer must supply.

    `inbox/models.py::_template_parts` splits the template with the raw `SLOT_TOKEN`, so the
    card renders an input chip for `dept_total`. `ReviewInbox.trial_run` derives its
    `required` set from `referenced_slots`, which does not include it, and then binds only
    `{k: bindings[k] for k in required}` — so whatever the reviewer types into that chip is
    dropped without a word, and the query runs with the string constant it always had.

    `_template_parts`' own docstring is the argument for fixing it: "Split rather than a
    regex in the browser because the token grammar is `runtime/blueprint/template.SLOT_TOKEN`
    and a second spelling of it in JS would fork a definition two layers already depend on."
    The fork happened anyway — inside Python.

    Observed: the card offers `dept_total`; the trial requires only `department_code`.
    Expected: both read the same grammar.
    """
    from data_agent.learning.inbox.models import _template_parts
    from data_agent.runtime.blueprint.template import referenced_slots

    template = _template_with_a_brace_in_a_string()
    chips = {p["slot"] for p in _template_parts({"generalization": {"sql_template": template}})
             if "slot" in p}
    assert chips == referenced_slots(template), chips


def test_defect_a_brace_in_a_string_makes_a_candidate_unrebuildable() -> None:
    """DEFECT — the snapshot backfill refuses a template it should simply copy through.

    `generalize/reconstruct.py` (used by `scripts/backfill_revalidation_snapshots.py`) still
    reads tokens with the raw `SLOT_TOKEN`, twice: it demands a parameterization entry for
    every `findall` hit, and it substitutes values into every `sub` hit. A `{dept_total}`
    living inside a string constant therefore either (a) has no entry, and the whole
    candidate is refused as "template references slot(s) … with no parameterization entry",
    or (b) has one, and the value is substituted INTO the string producing `''XYZ''`, which
    does not parse — so the reconstruction returns `None` either way.

    The consequence is not cosmetic: a candidate with no re-validation snapshot cannot be
    revised, and the reason it will be given names a slot that is not a slot.

    Observed: `None`.  Expected: the template rebuilt with `'{dept_total}'` intact.
    """
    from data_agent.learning.generalize.reconstruct import reconstruct_accepted_sql

    template = _template_with_a_brace_in_a_string()
    payload = {
        "generalization": {"sql_template": template},
        "parameterization": [param_slot("department_code", value="0420", table=PAYROLL)],
    }
    rebuilt = reconstruct_accepted_sql(payload)
    assert rebuilt is not None, "the brace in the string constant blocked the rebuild"
    assert "'{dept_total}'" in rebuilt


def test_the_string_aware_scan_survives_the_escapes_clickhouse_actually_uses() -> None:
    """The new `_SKIP_OR_SLOT` alternation carries the whole guarantee, so its string arm is
    worth attacking directly: doubled quotes, backslash escapes, a `--` inside a string, and
    an apostrophe inside a comment (which would otherwise open a phantom string and swallow
    every token after it)."""
    from data_agent.runtime.blueprint.template import referenced_slots

    cases = {
        "SELECT 1 WHERE a = 'it''s {x}' AND b = {real}": {"real"},
        r"SELECT 1 WHERE a = 'it\'s {x}' AND b = {real}": {"real"},
        "SELECT 1 WHERE a = 'x--y {x}' AND b = {real}": {"real"},
        "SELECT 1 -- don't use {x}\nWHERE b = {real}": {"real"},
        "SELECT 1 /* don't use {x} */ WHERE b = {real}": {"real"},
        "SELECT 1 WHERE b = {real}": {"real"},
    }
    for sql, expected in cases.items():
        assert referenced_slots(sql) == expected, sql


# ==========================================================================================
# 9. WHAT THE PAGE CAN SEND THAT THE SERVER REFUSES
# ==========================================================================================


def test_a_composite_submission_carrying_the_top_level_sql_box_is_refused() -> None:
    """The form keeps the whole-query SQL section VISIBLE in composite mode.

    `mint.html::onShapeChange` toggles `#nodes-panel` and nothing else, and `onSubmit` always
    reads `#sql` into `payload.sql`. So an expert who typed a query, then switched to
    "several steps that feed each other", submits both — and `MintRequest.__post_init__`
    refuses the whole thing, correctly ("a top-level query is never read"), after they have
    filled in every step.

    The refusal is right; the page offering the combination is not. Pinned server-side
    because that is where the rule lives — the fix belongs in `onShapeChange`, which should
    hide (or clear) the SQL section when the shape is composite.
    """
    with pytest.raises(MintInputError, match="whole-query box must be empty"):
        request(
            (
                MintNode(step_intent="a", output_name="a", sql=TOTAL_SQL),
                MintNode(step_intent="b", output_name="b", sql=TOTAL_SQL),
            ),
            sql=TOTAL_SQL,
            sql_mode="exact",
        )


def test_an_exact_composite_needs_sql_on_every_step_despite_what_the_placeholder_says() -> None:
    """The node textarea reads "SQL for this step (leave blank to have it written for you)".

    In `sql_mode="exact"` that is false: a blank step is a 400. The two controls are
    independent in the DOM and their combination is only adjudicated on the server.
    """
    with pytest.raises(MintInputError, match="every step needs its own SQL"):
        request(
            (
                MintNode(step_intent="a", output_name="a", sql=TOTAL_SQL),
                MintNode(step_intent="b", output_name="b"),  # blank, as the placeholder invites
            ),
            sql_mode="exact",
        )


def test_emptying_the_step_editor_silently_mints_a_single_blueprint() -> None:
    """OBSERVED. `readNodes()` returns `[]` when every step row has been removed, and an empty
    `nodes` array is indistinguishable from "this is not a composite" — so the shape selector
    still says "several steps" while a one-query blueprint is minted from the prose."""
    assert request(()).is_composite is False
    assert request((MintNode(step_intent="a", output_name="a"),)).is_composite is True
