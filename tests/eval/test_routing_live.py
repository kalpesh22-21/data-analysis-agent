"""A2 — routing decisions, LIVE model, real prompt (07 §A/§B.3/§D).

**This is the suite that can go red on a bad prompt, and it is the release gate.**

A1 cannot. `ScriptedModelClient.send_turn` never reads `messages`, so every tool
name and argument there comes from the fixture and the suite passes identically
against today's prompt, a badly-rewritten one, or one that says "never use
blueprints". Deliverable 01 changes nothing but a string, so a suite that
discards the string tests none of it. Here the model reads the prompt and the
pre-injected cards and DECIDES, and the assertions are over what it decided.

WHAT IS REAL: the model, the base system prompt, `create_app`, the whole
`AgentLoop`, the retrieval pipeline, the evidence validators, finalization
enforcement. WHAT IS DOUBLED: the MCP transport (answers deterministically from
the committed catalog export) and the session store. §B.3: warehouse and corpus
may stay committed fixtures — what must be real is the model and the prompt.

REPORTED AS A PASS-RATE OVER N RUNS, NOT A BOOLEAN. The model is
non-deterministic and a single red run is noise. `LIVE_EVAL_RUNS` (default 3) and
`LIVE_EVAL_MIN_PASS_RATE` (default two-thirds, i.e. "one red run of three is
tolerated" — see `MIN_PASS_RATE`, it is NOT 0.67) tune it.

    RUN_LIVE_EVAL=1 uv run pytest tests/eval/test_routing_live.py -q -s

Without `RUN_LIVE_EVAL` the whole module SKIPS — following `tests/e2e/`'s
`RUN_E2E=1` idiom — so `uv run pytest` costs no live model calls.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_agent.runtime import app as app_module
from data_agent.runtime.app import create_app
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.mcp.client import MCPToolError, MCPToolSpec
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import AnalysisState, TrailEntry
from tests._catalog_fixture import fixture_catalog, fixture_catalog_handle
from tests._tool_specs_fixture import fixture_tool_specs

from . import metrics
from .conftest import (
    RecordingObserver,
    blueprint_candidate,
    blueprint_detail,
    parse_sse,
    seed_corpus,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_LIVE_EVAL"),
    reason=(
        "A2 spends live model calls; set RUN_LIVE_EVAL=1 to run it. A1 "
        "(test_runtime_mechanics.py) covers the runtime mechanics per commit."
    ),
)

RUNS = int(os.environ.get("LIVE_EVAL_RUNS", "3"))

# THE FLOOR IS TWO-THIRDS, NOT `0.67`. The docstring's tolerance — "a single red
# run is noise" — is what the number has to encode, and at the default RUNS=3 the
# only rate a case with one red run can reach is 2/3 = 0.6666…, which is BELOW
# 0.67. The literal `0.67` therefore made the gate "3/3 or fail" while claiming to
# tolerate one red run, and L6 failed on exactly that arithmetic (2/3 = 67% < 67%).
# The epsilon absorbs the float division; the env override is unchanged, and
# anything a deployment sets there wins verbatim.
MIN_PASS_RATE = float(os.environ.get("LIVE_EVAL_MIN_PASS_RATE", str(2 / 3 - 1e-9)))

_BASE_DATABASE = "dbpcm_warehouse"
_SESSION_PREFIX = "sess-live-eval"


# ---------------------------------------------------------------------------
# The MCP double — deterministic, but NOT scripted
# ---------------------------------------------------------------------------


class LiveEvalMCPClient:
    """An `MCPClient` that answers ANY call deterministically.

    `FakeMCPClient` cannot be used here: it consumes an ORDERED queue per tool
    name and raises `AssertionError` on an unscripted call, which is exactly right
    for A1 (where the fixture chose the calls) and exactly wrong for A2 (where the
    MODEL chooses them, and a routing miss would surface as a harness crash rather
    than as a failed case).

    Schemas and table lists come from the committed catalog export, so the model
    sees the real HR warehouse shape. Query results are synthetic and fixed —
    A2 grades the ROUTE, not the numbers. Grading the numbers is A3, deferred.

    THE TOOL CATALOGUE IS THE REAL ONE (`tests/_tool_specs_fixture.py`). It used to
    be six names with `description=""` and an EMPTY `input_schema`, and
    `mcp/tool_schema.py::translate_tool_spec` ships `description`/`input_schema`
    VERBATIM to the model — so the model was told `runQuery()` takes no `sql` and
    `getTableSchema()` takes no table. Every case needing ad-hoc SQL or a real
    schema read was unwinnable, and A2 was silently a blueprint-only arena. A
    harness may double the transport; it may not lie about the contract.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._catalog = fixture_catalog()

    async def call_tool(
        self, tool_name: str, args: dict[str, Any], *, jwt: str, session_id: str
    ) -> dict[str, Any] | list[Any]:
        self.calls.append((tool_name, dict(args)))
        handler = getattr(self, f"_{tool_name}", None)
        if handler is None:
            return {"columns": [], "rows": [], "row_count": 0, "truncated": False}
        return handler(args)

    async def list_tools(self, *, jwt: str, session_id: str) -> list[MCPToolSpec]:
        """The MCP's REAL `tools/list`, from the committed fixture.

        Frozen, never fetched: A2 must not need a live MCP. See
        `tests/fixtures/mcp_tool_specs.json` for the regen recipe and the drift
        guard."""
        return fixture_tool_specs()

    # -- handlers ---------------------------------------------------------

    def _tables(self) -> list[str]:
        return sorted(
            key.split(".", 1)[1]
            for key in self._catalog
            if key.startswith(f"{_BASE_DATABASE}.")
        )

    # `listDatabases`/`listTables` return BARE LISTS of dicts, matching
    # `clickhouse-api/app/service.py`. The emulated-discovery sweep parses exactly
    # that shape (`discovery_emulation._database_names`), and a `{"databases": […]}`
    # wrapper would make it inject `listDatabases` alone and silently skip the
    # `listTables` half — the model would then spend a real round-trip on
    # discovery on every A2 run, which is not the composition being graded.
    def _listDatabases(self, _args: dict[str, Any]) -> list[dict[str, str]]:  # noqa: N802
        return [{"name": _BASE_DATABASE}]

    def _listTables(self, args: dict[str, Any]) -> list[dict[str, str]]:  # noqa: N802
        database = args.get("database", _BASE_DATABASE)
        return [
            {"database": database, "name": table, "engine": "MergeTree"}
            for table in self._tables()
        ]

    def _entry(self, database: str, table: str) -> dict[str, Any]:
        """The catalog entry for `database.table`, or the MCP's own TABLE_NOT_FOUND.

        The real server raises `TableNotFoundError` for a table `system.columns`
        does not know (`clickhouse-api/app/service.py`), and the runtime's denial
        mapping already understands that code. Returning an empty envelope instead
        would tell the model the table exists and has no columns — a lie it cannot
        recover from."""
        entry = self._catalog.get(f"{database}.{table}")
        if entry is None:
            raise MCPToolError(
                "TABLE_NOT_FOUND",
                f"[TABLE_NOT_FOUND] Table '{database}.{table}' not found or has no "
                "columns. Check the database and table names with listTables.",
            )
        return entry

    def _visible_columns(self, entry: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """`[(name, spec)]` minus `mcp_projection.hidden_columns`.

        The overlay drops those UNCONDITIONALLY on every transport (the RLS
        physical columns `client_code`/`proc_center`), so a double that emitted
        them would invite SQL the real server rejects."""
        hidden = set((entry.get("mcp_projection") or {}).get("hidden_columns") or ())
        columns = entry.get("columns") or {}
        return [
            (name, spec if isinstance(spec, dict) else {})
            for name, spec in columns.items()
            if name not in hidden
        ]

    def _getTableSchema(self, args: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        """The merged catalog+introspection shape `svc_get_table_schema` returns.

        NOT a column-name list. The previous version rendered `type` as `str(spec)`
        — the whole catalog dict stringified into the type field — and truncated at
        40 columns, which is how L5's metadata half was "answered" by a schema read
        that carried no usable schema. Every documented column, its real ClickHouse
        type, and the catalog description as the `comment`."""
        database = str(args.get("database") or _BASE_DATABASE)
        table = str(args.get("table") or "")
        entry = self._entry(database, table)
        return {
            "database": database,
            "table": table,
            "catalogued": True,
            "description": entry.get("description"),
            "grain": entry.get("grain"),
            "primary_key": entry.get("primary_key"),
            "join_keys": entry.get("join_keys"),
            "rules": entry.get("rules"),
            "columns": [
                {
                    "name": name,
                    "type": str(spec.get("type") or ""),
                    "comment": str(spec.get("description") or ""),
                }
                for name, spec in self._visible_columns(entry)
            ],
        }

    def _runQuery(self, args: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        sql = str(args.get("sql") or "")
        if "__bp_n" in sql:
            # The D56 grain probe. `n == d` so the row-count teeth pass and a
            # correctly-routed blueprint can earn `authoritative` — otherwise every
            # blueprint would fail verification and L1/L2/L6 would be unwinnable
            # for a reason that has nothing to do with routing.
            return {
                "columns": ["__bp_n", "__bp_d"],
                "rows": [[2, 2]],
                "row_count": 1,
                "truncated": False,
            }
        return {
            "columns": ["group_key", "value"],
            "rows": [["Sales", 4], ["Support", 3]],
            "row_count": 2,
            "truncated": False,
        }

    def _sampleRows(self, args: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        """Two synthetic rows over the REQUESTED table's real visible columns.

        Now that the schema advertises `database`/`table`, the model names them —
        and a sample that answered `payroll` with `employee_code, department_name`
        would be the same class of lie the tool specs were. The VALUES stay
        synthetic (A2 grades the route, not the numbers); only the SHAPE is real.

        EVERY visible column, uncapped: `svc_sample_rows` projects the introspected
        set minus `mcp_projection.hidden_columns` and returns all of it, so a cap
        here would be a shape the real server never produces — the model would plan
        against a narrower table than it will actually get."""
        database = str(args.get("database") or _BASE_DATABASE)
        table = str(args.get("table") or "")
        names = [name for name, _spec in self._visible_columns(self._entry(database, table))]
        return {
            "columns": names,
            "rows": [[f"{name}-1" for name in names], [f"{name}-2" for name in names]],
            "row_count": 2,
            "truncated": False,
        }

    def _explainQuery(self, _args: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        return {"columns": ["explain"], "rows": [["Aggregating"]], "row_count": 1, "truncated": False}


# ---------------------------------------------------------------------------
# The shipped composition, with a live model
# ---------------------------------------------------------------------------


def _full_corpus_retrieval() -> RetrievalPipeline:
    """EVERY committed blueprint, pre-injected.

    All cards share one vector and `top_k_blueprints` is the corpus size, so
    ranking is a no-op and the model is handed the whole corpus. That is
    deliberate: it removes the embedder as a confound, and it makes the test
    HARDER — the model must pick the right blueprint out of all of them rather
    than out of a three-card shortlist an embedder already narrowed.
    """
    seeds = seed_corpus()
    index = FakeVectorIndex()
    for seed in seeds.values():
        index.add(blueprint_candidate(seed), [1.0, 0.0])
        index.add_detail(blueprint_detail(seed))
    return RetrievalPipeline(
        embedding_client=FakeEmbeddingClient(dim=2),
        reranker=None,
        vector_index=index,
        user_memory=NullUserMemoryProvider(),
        recall_k=max(30, len(seeds)),
        top_k_blueprints=len(seeds),
        top_k_knowledge=3,
    )


def _full_scope() -> frozenset[str]:
    """The union of every seeded blueprint's footprint, so recall's `uses ⊆ scope`
    pre-filter drops nothing. An empty scope would drop EVERY card."""
    return frozenset(
        column for seed in seed_corpus().values() for column in seed.uses
    )


@dataclass
class LiveRun:
    outcome: dict[str, Any]
    trail: list[TrailEntry]
    state: AnalysisState | None
    events: list[tuple[str, dict[str, Any]]]
    mcp_calls: list[tuple[str, dict[str, Any]]]

    @property
    def serves_intent(self) -> dict[str, set[str]]:
        """Derived from the persisted trail (07 §C.2). In A2 there is no fixture
        to declare it, and `loop_intent_completed` carries `evidence_tool_name` —
        a tool NAME cannot tell two `runQuery` calls apart. The
        `updateAnalysisState` trail entries carry the model's own
        `{intent_id, evidence_tool_call_id}` bindings, which is the binding
        itself."""
        return metrics.serves_intent_from_trail(self.trail, 0)

    def succeeded(self, tool_name: str) -> list[TrailEntry]:
        return [e for e in self.trail if e.tool_name == tool_name and e.status == "ok"]

    def blueprint_ids(self) -> set[str]:
        return {
            str(e.args.get("id"))
            for e in self.succeeded("runBlueprint")
            if isinstance(e.args, dict) and e.args.get("id")
        }

    def authoritative_blueprint_ids(self) -> set[str]:
        """The blueprints whose result the D56 gate VERIFIED.

        `blueprint_ids` is "the call did not error", which is a weaker claim: a
        successful-but-unverified run is not an authoritative result, and
        `metrics.re_derivation` only ever considers authoritative ones."""
        return {
            str(e.args.get("id"))
            for e in self.succeeded("runBlueprint")
            if isinstance(e.args, dict) and e.args.get("id") and e.authoritative is True
        }

    def blueprint_attempts(self) -> list[str]:
        """`["<id>:<status>[:unverified]"]` for EVERY `runBlueprint` call.

        So "no blueprint result" can say WHICH of the three worlds it was: the
        model never called one, it called one that was denied, or it ran one the
        D56 gate would not certify."""
        return [
            f"{e.args.get('id') if isinstance(e.args, dict) else '?'}:{e.status}"
            + ("" if e.authoritative is True or e.status != "ok" else ":unverified")
            for e in self.trail
            if e.tool_name == "runBlueprint"
        ]

    def evidence_tool_names(self) -> set[str]:
        """Every tool NAME bound to a tracked intent, from BOTH binding sources.

        `AnalysisState.intent.evidence_tool_call_id` alone under-reports: an intent
        closed by a CALL-TIME `serves_intent` tag carries no
        `evidence_tool_call_id` at all (`metrics.serves_intent_from_trail`'s source
        1), so a correctly-tagged turn looked like a turn with no evidence — which
        is a harness misreport, not a routing miss. The union is what "which tool
        answered which intent" actually means today."""
        by_id = {e.tool_call_id: e for e in self.trail}
        names = set()
        for intent in (self.state.intents if self.state else ()):
            entry = by_id.get(intent.evidence_tool_call_id or "")
            if entry is not None:
                names.add(entry.tool_name)
        tracked = {intent.intent_id for intent in (self.state.intents if self.state else ())}
        for call_id, intents in self.serves_intent.items():
            entry = by_id.get(call_id)
            if entry is not None and entry.status == "ok" and (intents & tracked):
                names.add(entry.tool_name)
        return names

    def designation_counts(self) -> list[tuple[int, int, int]]:
        """`[(table_count, blueprint_table_count, verified_table_count)]`, one per
        `loop_answer_tables_designated` (06 §L).

        Reported, never gated. With faithful tool specs the model can designate a
        table by RAW SQL instead of by `blueprint_id`, which keeps `table_count` up
        while collapsing `verified_table_count` to 0 — a regression invisible to
        every predicate here, so it is printed beside every case's rate."""
        return [
            (
                int(payload.get("table_count") or 0),
                int(payload.get("blueprint_table_count") or 0),
                int(payload.get("verified_table_count") or 0),
            )
            for name, payload in self.events
            if name == "loop_answer_tables_designated"
        ]


def _run_live(question: str, run_index: int) -> LiveRun:
    import anyio

    settings = RuntimeSettings()
    if not settings.openai_api_key:
        pytest.skip("RUN_LIVE_EVAL is set but no OpenAI API key is configured.")

    session_id = f"{_SESSION_PREFIX}-{run_index}-{abs(hash(question)) % 10**8}"
    scope = _full_scope()
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(app_module, "verify_jwt", lambda *a, **k: scope)
        store = InMemorySessionStore()
        mcp = LiveEvalMCPClient()
        observer = RecordingObserver()
        app = create_app(
            settings=settings,
            session_store=store,
            mcp_client=mcp,
            # `model_client=None` on purpose: `create_app` builds the REAL
            # OpenAI client from settings, so A2 exercises the shipped
            # construction rather than a hand-made one.
            catalog=fixture_catalog_handle(),
            retrieval=_full_corpus_retrieval(),
            extra_observers=(observer,),
        )
        client = TestClient(app)
        response = client.post(
            "/turn",
            json={"message": question},
            headers={"Authorization": "Bearer live-eval", "X-Session-Id": session_id},
        )
        assert response.status_code == 200, response.text
        final = parse_sse(response.text)[-1]
        assert final["event"] == "result", final
        doc = anyio.run(store.get_or_create_session, session_id)
        return LiveRun(
            outcome=final["data"],
            trail=list(doc.tool_trail),
            state=doc.analysis_state,
            events=list(observer.events),
            mcp_calls=list(mcp.calls),
        )
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# The six routing cases (07 §D)
# ---------------------------------------------------------------------------


def _not_terminal_detail(run: LiveRun) -> str:
    """"" if every tracked intent reached a terminal disposition, else WHY NOT.

    THREE DISTINCT WORLDS, three distinct details. `_all_terminal` used to return a
    bare `False` for all of them, so "not every intent reached a terminal
    disposition" was printed for a turn that never initialized an `analysisState`
    at all — the one that is a real, separate runtime defect (the model never
    declares intents) reported as if intents had been left hanging. `_l4` already
    split the first two; this is that split, made shareable."""
    if run.state is None:
        return "no analysisState was initialized"
    if not run.state.intents:
        return "analysisState was initialized with NO intents"
    unfinished = [
        f"{intent.intent_id}:{intent.status}"
        for intent in run.state.intents
        if intent.status not in metrics.TERMINAL_INTENT_STATUSES
    ]
    if unfinished:
        return f"intents still pending: {unfinished}"
    return ""


def _re_derivation_detail(run: LiveRun, *, single_intent: bool) -> str:
    """"" if the turn did not re-derive an authoritative result, else WHY.

    An UNJUDGEABLE turn is not a pass. `metrics.re_derivation` returns `False` when
    nothing bound a blueprint run to an intent, so an untracked turn used to clear
    this check by vacuity — the predicate reported a measurement it never made.

    On a turn whose ground truth is a SINGLE intent the runtime is RIGHT not to
    track anything (03 §E: initializing state for a single-intent request is the
    false positive the detection metric counts), so there the turn-scoped form is
    not an approximation — with one intent, any `runQuery` after the authoritative
    blueprint IS a re-derivation of it. On a multi-intent turn it is the rejected
    predicate (§C.2: it flags the legal blueprint+residual shape), so an
    unjudgeable multi-intent turn is reported as unjudgeable and fails."""
    judgement = metrics.re_derivation_judgement(run.trail, run.serves_intent, turn_index=0)
    if judgement.re_derived is True:
        return judgement.detail
    if judgement.re_derived is None:
        if not single_intent:
            return judgement.detail
        if metrics.re_derivation_turn_scoped(run.trail, turn_index=0):
            return (
                "a runQuery followed the authoritative blueprint on a single-intent "
                f"turn ({judgement.detail}; judged turn-scoped, which is exact for one intent)"
            )
    return ""


def _l1_projection_routed_to_its_blueprint(run: LiveRun) -> tuple[bool, str]:
    if "bp-hires-projection" not in run.blueprint_ids():
        return (
            False,
            f"no successful bp-hires-projection run (runBlueprint={run.blueprint_attempts()})",
        )
    detail = _re_derivation_detail(run, single_intent=True)
    if detail:
        return False, detail
    return True, ""


def _l2_two_blueprints(run: LiveRun) -> tuple[bool, str]:
    ids = run.blueprint_ids()
    if len(ids) < 2:
        return False, f"expected two distinct blueprints, got {ids}"
    return True, ""


def _l3_blueprint_plus_residual(run: LiveRun) -> tuple[bool, str]:
    if not run.blueprint_ids():
        return (
            False,
            f"the blueprint half was answered with ad-hoc SQL (runBlueprint={run.blueprint_attempts()})",
        )
    if not run.succeeded("runQuery"):
        return False, "the residual half never ran a query"
    # Two intents by ground truth, so the turn-scoped fallback is NOT available:
    # this is the exact shape (blueprint + distinct residual) it wrongly flags.
    detail = _re_derivation_detail(run, single_intent=False)
    if detail:
        return False, detail
    return True, ""


def _l4_three_intents_tracked_and_terminal(run: LiveRun) -> tuple[bool, str]:
    if run.state is None:
        return False, "no analysisState was initialized for a three-intent request"
    if len(run.state.intents) < 3:
        return False, f"tracked {len(run.state.intents)} intents, expected 3"
    detail = _not_terminal_detail(run)
    if detail:
        return False, f"at a terminal outcome: {detail}"
    return True, ""


def _l5_metadata_and_analytical(run: LiveRun) -> tuple[bool, str]:
    detail = _not_terminal_detail(run)
    if detail:
        return False, detail
    names = run.evidence_tool_names()
    if "getTableSchema" not in names:
        return False, f"the metadata intent was not answered from schema ({names})"
    if not names & {"runQuery", "runBlueprint"}:
        return False, f"the analytical intent was not answered from data ({names})"
    return True, ""


def _l7_three_deliverables_three_tables(run: LiveRun) -> tuple[bool, str]:
    """08's headline measurement, and the ONLY thing that can prove the fix.

    The number being re-measured is **1/9** — `answerWithTable` succeeded on 6/6
    single-deliverable turns and 1/9 multi-intent ones, because the payload could
    name only ONE of the three result sets a three-part turn produces. A1 cannot
    read this: `answer_tables` populating there is definitionally true, since the
    fixture writes the `tables` array. Every claim in 08 §G is a claim about a LIVE
    model reading a prompt, so only this can fail on a bad one.

    The predicate is deliberately "more than one table", not "exactly three": the
    scalar clause survives (a part answered by a single number belongs in the
    prose), and one query legitimately covering two parts is one table. The failure
    being measured is the turn that produces three result sets and ships ONE grid.
    """
    tables = run.outcome.get("answer_tables") or []
    if not tables:
        return False, "no answerWithTable designation at all — the turn answered in prose"
    if len(tables) < 2:
        return (
            False,
            f"{len(tables)} table for a three-part question — the other parts went to prose",
        )
    return True, ""


def _l6_no_re_derivation(run: LiveRun) -> tuple[bool, str]:
    # AUTHORITATIVE, not merely successful — which is what this failure message
    # always claimed, and what `metrics.re_derivation` actually quantifies over. A
    # run the D56 gate did not certify is not a result anything can re-derive, and
    # letting it satisfy the premise made the rest of the predicate vacuous.
    if not run.authoritative_blueprint_ids():
        return (
            False,
            "no authoritative blueprint result to re-derive in the first place "
            f"(runBlueprint={run.blueprint_attempts()})",
        )
    detail = _re_derivation_detail(run, single_intent=True)
    if detail:
        return False, detail
    return True, ""


@dataclass(frozen=True)
class LiveCase:
    id: str
    question: str
    predicate: Callable[[LiveRun], tuple[bool, str]]
    note: str


# L1-L4 are spec §3's stated test of whether per-intent `searchBlueprints` holds.
# If the blueprint miss-rate is high here, the Phase-2 fix is firing retrieval per
# DECLARED INTENT rather than once per turn on the raw question.
LIVE_CASES: tuple[LiveCase, ...] = (
    LiveCase(
        id="L1",
        question=(
            "At the pace we have been hiring recently, roughly how many people "
            "should we expect to join over the next six months?"
        ),
        predicate=_l1_projection_routed_to_its_blueprint,
        note="complex wording, one blueprint; no fresh SQL for that intent",
    ),
    LiveCase(
        id="L2",
        question=(
            "Give me the active headcount by department, and separately the "
            "average annual salary by department."
        ),
        predicate=_l2_two_blueprints,
        note="two independent blueprint intents, each routed to its own blueprint",
    ),
    LiveCase(
        id="L3",
        # THE RESIDUAL MUST HAVE NO CORPUS COVERAGE, or the case flaps for being
        # right. The old residual — "which single department has the most people in
        # it right now" — is exactly `bp-active-headcount-by-department` (its result
        # ordered by headcount), so a blueprint-first model answered BOTH halves
        # from blueprints, ran no `runQuery`, and failed a case measuring ad-hoc
        # routing while behaving correctly. Grouping headcount BY EMPLOYMENT STATUS
        # is covered by nothing in `tests/fixtures/corpus/blueprints.yaml`: every
        # seeded blueprint groups by department, month, or payroll line item, and
        # `employee_status` appears only as a FILTER inside them.
        #
        # It stays inside `_full_scope()` (employee.employee_code +
        # employee.employee_status are both in the corpus footprint) on purpose: an
        # out-of-scope residual would have its answer table dropped by
        # `is_answer_table_in_scope` at designation time, which would make this case
        # measure the scope filter instead of the route.
        question=(
            "How many new hires did we have per month over the last six months, "
            "and separately, how many employees are there in each employment status?"
        ),
        predicate=_l3_blueprint_plus_residual,
        note="blueprint for the hires half, ad-hoc runQuery for the uncovered residual",
    ),
    LiveCase(
        id="L4",
        question=(
            "I need three things: active headcount by department, average annual "
            "salary by department, and new hires per month over the last six months."
        ),
        predicate=_l4_three_intents_tracked_and_terminal,
        note="analysisState initialized; all three intents terminal",
    ),
    LiveCase(
        id="L5",
        question=(
            "What columns does the employee table have, and how many employees "
            "are currently active?"
        ),
        predicate=_l5_metadata_and_analytical,
        note="mixed metadata + analytical; both complete",
    ),
    LiveCase(
        id="L6",
        question=(
            "Using the standard analysis, what is the average annual salary by "
            "department? Please double-check the figure."
        ),
        predicate=_l6_no_re_derivation,
        note="an authoritative result is not re-derived with fresh SQL",
    ),
    LiveCase(
        id="L7",
        # Deliberately L4's question verbatim. L4 asks whether the three intents
        # were TRACKED and reached a terminal disposition; L7 asks whether the
        # three RESULTS reached the user as grids. The same turn failed the second
        # while passing the first, 8 times out of 9, which is the whole of 08.
        question=(
            "I need three things: active headcount by department, average annual "
            "salary by department, and new hires per month over the last six months."
        ),
        predicate=_l7_three_deliverables_three_tables,
        note="three deliverables => more than one table on the answer (re-measures 1/9)",
    ),
)


@pytest.mark.parametrize("case", LIVE_CASES, ids=[c.id for c in LIVE_CASES])
def test_routing_case_pass_rate(case: LiveCase, capsys) -> None:
    """One routing case, N runs, reported as a RATE.

    A boolean here would make the gate flap: the model is non-deterministic, so a
    single red run is noise, not a regression. The failure detail from every red
    run is printed, because "L3 failed" is not actionable and "the blueprint half
    was answered with ad-hoc SQL, 2 of 3 runs" is.
    """
    rate = metrics.PassRate(case.id)
    designations: list[tuple[int, int, int]] = []
    for index in range(RUNS):
        run = _run_live(case.question, index)
        ok, detail = case.predicate(run)
        rate.record(ok, detail)
        designations.extend(run.designation_counts())

    with capsys.disabled():
        print(f"\n[A2] {rate.line()}  — {case.note}")
        # REPORTED, NOT GATED. `answerWithTable` designating by raw `sql` instead of
        # `blueprint_id` keeps `tables` up while `verified` goes to 0, and no
        # predicate here can see it. Printing it is how the next slice finds out.
        print(
            "      answer_tables (table/blueprint/verified per designation): "
            f"{designations or 'none'}"
        )
        for failure in rate.failures:
            print(f"      red: {failure}")

    assert rate.rate >= MIN_PASS_RATE, f"{rate.line()} < {MIN_PASS_RATE:.0%}: {rate.failures}"


def test_multi_intent_detection_rate_is_measured_here_not_in_a1(capsys) -> None:
    """E.3's FIRST REAL READING.

    In A1 this is definitionally 1.0 — the numerator fires only because the
    fixture scripts `updateAnalysisState`. Here the MODEL decides whether to
    initialize, so the number carries information for the first time.

    The denominator is GROUND TRUTH, declared below, never self-reported: the
    failure being measured IS the model's own misjudgement, so asking the model
    whether the request was multi-intent measures nothing. It also cannot be
    replaced by `loop_analysis_state_late_init_rejected`, which catches only the
    model that missed the decomposition, LATER realised, and was refused — the
    failure this exists for is the model that never realises at all.

    Reported, not gated: it is a quality reading, and the routing gates above are
    what block a release.
    """
    ground_truth: Sequence[tuple[str, bool]] = (
        (LIVE_CASES[1].question, True),  # L2 — two intents
        (LIVE_CASES[3].question, True),  # L4 — three intents
        (LIVE_CASES[4].question, True),  # L5 — metadata + analytical
        (LIVE_CASES[0].question, False),  # L1 — single intent: false-positive check
    )
    observations = []
    for index, (question, multi_intent) in enumerate(ground_truth):
        run = _run_live(question, 100 + index)
        observations.append(
            metrics.DetectionObservation(
                case_id=f"detect-{index}",
                multi_intent=multi_intent,
                analysis_state_initialized=run.state is not None,
            )
        )
    result = metrics.multi_intent_detection(observations)
    with capsys.disabled():
        print(
            f"\n[A2] multi-intent detection: {result.numerator}/{result.denominator}"
            f" = {result.rate:.0%}  false positives:"
            f" {result.false_positives}/{result.single_intent_seen}"
        )
    assert result.denominator == 3


def test_live_suite_is_collectable_without_a_live_model() -> None:
    """Nothing above may need a network call, an API key or an env var AT IMPORT
    TIME — the module has to import and collect cleanly on every ordinary
    `uv run pytest`, and only then skip."""
    assert len(LIVE_CASES) == 7
    assert {c.id for c in LIVE_CASES} == {"L1", "L2", "L3", "L4", "L5", "L6", "L7"}
