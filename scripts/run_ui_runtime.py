"""Dev launcher: the Phase-0 agent runtime (`create_app`), wired to Layer-1
fakes only, so the minimal UI (`ui/`) can be exercised end-to-end WITHOUT an
OpenAI API key, Couchbase, or the real ClickHouse MCP.

Wiring (mirrors `tests/runtime/test_app.py::_build_client`, but as a live
uvicorn process rather than a `TestClient`):

    - `model_client`  -> `DemoModelClient` (below) — a small, content-routed
      `ModelClient` double. NOT `ScriptedModelClient` verbatim: that class is
      a single-use, strictly-ordered cassette (`AssertionError` once its
      script is exhausted) — perfect for one pytest assertion, but this
      launcher needs to serve an UNBOUNDED number of demo turns, from any
      number of browser sessions, in either order. `DemoModelClient`
      reproduces the same `ModelClient` Protocol (`send_turn`/`begin_turn`)
      and the same `ModelTurnResult`/`ToolCallRequest` DTOs, but decides its
      canned response by inspecting the canonical message shape the loop
      hands it (see its docstring) instead of popping a fixed queue — so it
      can be replayed indefinitely.
    - `mcp_client`    -> `DemoMCPClient` (below), a thin subclass of
      `data_agent.runtime.mcp.fake_client.FakeMCPClient`, scripted with a
      repeated `getTableSchema` response (also for the same "must survive
      many demo turns" reason — a `FakeMCPClient` scripted with exactly one
      response would raise `AssertionError` on the second call). `runQuery`
      is NOT scripted through the normal FIFO queue: `FakeMCPClient` forks
      purely on tool name + call order, never on `args`, so it cannot itself
      express "the SAME tool, called with different SQL, denied for
      different reasons" — which the scope-denial (D57) and
      parser-fail-closed (D63) conformance scenarios both need from the one
      `runQuery` tool. `DemoMCPClient` overrides `call_tool` to content-route
      `runQuery` off the submitted `query` string instead (see its
      docstring); every other tool falls through to the base class unchanged.
    - `session_store` -> `data_agent.runtime.session.memory_store.InMemorySessionStore`.
    - `catalog`       -> a small hand-built `CatalogHandle` with one table
      (`analytics.employees`) so `getTableSchema`'s provenance capture has
      something real to resolve.

JWT verification is NOT bypassed/monkeypatched: `RuntimeSettings.jwks_url` /
`jwt_issuer` / `jwt_audience` are pointed at the already-running `l2-token`
container's real JWKS endpoint (`docker-compose.integration.yml`, unmodified,
untouched) — so a JWT minted by `ui/server.py` via the real token service is
ALSO really verified here, exactly like production. Only the model provider
and the ClickHouse MCP are faked (no OpenAI key, no live ClickHouse needed).

Trigger phrases (content-routed on the FIRST user message of the turn,
case-insensitive substring match — see `DemoModelClient.send_turn`):

    | Phrase contains...        | Scenario                                    |
    |----------------------------|---------------------------------------------|
    | "ask"                      | clarify: askUser -> chip options -> resume   |
    | "keep going" / "forever"   | budget-cap: never finishes -> paused_budget_cap |
    | "salaries" / "salary"      | scope-denial (D57): COLUMN_SCOPE_VIOLATION   |
    | "raw sql"                  | parser-fail-closed (D63): PARSE_FAILED_CLOSED |
    | "headcount by department"  | runBlueprint fast path (D89): verified answer |
    | "bad headcount"            | runBlueprint no-silent-verify (D56): VERIFY_FAILED -> raw loop |
    | "average tenure"           | runBlueprint slot ask->clarify->resume (D49) |
    | "approve headcount"        | runBlueprint approval pause/resume (D45/D59b) |
    | (anything else)            | normal: getTableSchema -> final answer       |

The four runBlueprint scenarios (D89) require the demo runtime to advertise a
blueprint fast path, which `create_app` wires ONLY when a `RetrievalPipeline`
is injected (`active_retrieval` non-None registers the read tools + runBlueprint
+ the BlueprintExecutor). This launcher injects a HERMETIC pipeline built from a
seeded `FakeVectorIndex` (keyed `get_blueprint` corpus — recall `entries` are
left EMPTY, so recall returns 0 cards for EVERY question and the pre-injection
step is inert for the 5 pre-existing scenarios) + a `FakeEmbeddingClient` (no
neo4j, no embedder network). The blueprint executor's per-node `runQuery`/domain/
grain probes are content-routed by `DemoMCPClient` off the SQL text (both the
`sql` and `query` arg keys), statelessly — see its docstring.

`RuntimeSettings.max_loop_iterations` is deliberately set LOW (3) below so
the budget-cap scenario is reachable in a handful of demo turns without a
real wall-clock wait; the normal/clarify/denial/parse-fail scenarios each
only ever consume a single loop iteration, so they complete well under that
cap regardless.

Run:
    uv run python scripts/run_ui_runtime.py
    # listens on :8000 (same port/shape the real runtime uses)
"""

from __future__ import annotations

from typing import Any

import uvicorn

from data_agent.runtime.app import create_app
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.mcp.client import MCPToolError, MCPToolSpec
from data_agent.runtime.mcp.fake_client import FakeMCPClient, RecordedCall
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex
from data_agent.runtime.session.memory_store import InMemorySessionStore

# The real l2-token container (docker-compose.integration.yml), already
# running — see docker-compose.integration.yml's `token` service. We only
# READ its JWKS/issue endpoints here; nothing about the stack is modified.
_TOKEN_ISSUER = "http://token:8000/"  # must match the token the JWT's "iss" claim carries
_TOKEN_AUDIENCE = "clickhouse-api"
_JWKS_URL = "http://localhost:19000/.well-known/jwks.json"

_DEMO_DATABASE = "analytics"
_DEMO_TABLE = "employees"
_DEMO_SCHEMA_RESPONSE: dict[str, Any] = {
    "database": _DEMO_DATABASE,
    "table": _DEMO_TABLE,
    "columns": {
        "id": "UInt64",
        "name": "String",
        "department": "String",
        "salary": "Decimal64(2)",
    },
}


# Sentinel substrings `DemoModelClient` embeds into the `runQuery` SQL it
# emits, and `DemoMCPClient.call_tool` (below) matches on, to content-route
# the SAME tool name to two different denial codes (see `DemoMCPClient`'s
# docstring for why a plain FIFO-scripted `FakeMCPClient` cannot do this on
# its own).
_SCOPE_DENIAL_QUERY = f"SELECT AnnualSalary FROM {_DEMO_DATABASE}.{_DEMO_TABLE}"
_PARSE_FAIL_QUERY = "RAW_SQL_DEMO -- ; DROP TABLE employees; --"


# --------------------------------------------------------------------------
# runBlueprint fixtures (D89 Slice 1) — the seeded corpus + the sentinel demo
# tables `DemoMCPClient` content-routes on.
#
# The four blueprint scenarios are driven by keyed `getBlueprint` fetches (by id)
# over a seeded `FakeVectorIndex`, NOT by recall — so the vector index's recall
# `entries` are left EMPTY (recall returns 0 cards for every question, keeping the
# retrieval pre-injection inert for the 5 pre-existing scenarios; §4 regression
# guard). Each blueprint's inner `runQuery` (per-node query, DISTINCT-domain slot
# probe, D56 grain probe) is content-routed off its SQL text below.
# --------------------------------------------------------------------------
_BP_GOOD_TABLE = "demo.headcount_by_dept"
_BP_BAD_TABLE = "demo.headcount_by_dept_bad"
_BP_TENURE_TABLE = "demo.avg_tenure_by_dept"
_BP_DEPT_DIM_TABLE = "demo.dept_dim"
_BP_APPROVAL_UPSTREAM_TABLE = "demo.approval_upstream"
_BP_APPROVAL_TERMINAL_TABLE = "demo.headcount_with_approval"

_BP_GOOD_ID = "headcount_by_dept"
_BP_BAD_ID = "headcount_by_dept_bad"
_BP_TENURE_ID = "avg_tenure_by_dept"
_BP_APPROVAL_ID = "headcount_with_approval"

# The no-silent-verification (D56) negative fixture: its node "result" carries a
# distinctive sentinel row + figure that the fan-out grain check catches and the
# executor WITHHOLDS (ExecFailed → raw loop). The Layer-3 scenario asserts NEITHER
# ever reaches the DOM — a far more robust leak probe than a bare digit (a uuid4
# session id can contain any 2-digit run by chance).
_FANOUT_LEAK_ROW = "FANOUT_LEAK_ROW_MUST_NOT_RENDER"
_FANOUT_LEAK_FIGURE = 987654

# The blueprint tables' schemas, folded into the demo `CatalogHandle` so the
# executor's inner runQuery provenance is DETERMINED (not `None`) — a runBlueprint
# result with undetermined provenance is dropped by the D44 replay filter even
# within its own turn (`context/scope_filter.filter_trail`: a successful `status
# == "ok"` current-turn entry is NOT exempt), which would strand the model in a
# re-emit loop. With the tables catalogued, `capture_provenance` resolves a real
# USES set, the verified result survives to the next round-trip, and the model
# narrates it. (The BFF mints an allow-all scope, so any determined provenance is
# in-scope.)
_BP_TABLE_SCHEMAS: dict[str, dict[str, str]] = {
    _BP_GOOD_TABLE: {"department": "String", "emp_id": "UInt64"},
    _BP_BAD_TABLE: {"department": "String", "emp_id": "UInt64"},
    _BP_TENURE_TABLE: {"department": "String", "tenure_days": "UInt32"},
    _BP_DEPT_DIM_TABLE: {"department": "String"},
    _BP_APPROVAL_UPSTREAM_TABLE: {"emp_id": "UInt64"},
    _BP_APPROVAL_TERMINAL_TABLE: {"department": "String", "emp_id": "UInt64"},
}


def _rows_result(columns: list[str], rows: list[list[Any]], row_count: int) -> dict[str, Any]:
    """A fresh `{columns, rows, row_count, truncated}` runQuery result (a NEW dict
    per call so no long-lived server state is ever mutated across demo turns)."""
    return {
        "columns": list(columns),
        "rows": [list(r) for r in rows],
        "row_count": row_count,
        "truncated": False,
    }


def _grain_probe_result(total: int, distinct: int) -> dict[str, Any]:
    """The D56 grain-integrity probe result: `COUNT(*)` (__bp_n) vs
    `COUNT(DISTINCT <grain>)` (__bp_d). `total == distinct` PASSES; a fan-out
    (`total != distinct`) FAILS the gate (verify.py)."""
    return _rows_result(["__bp_n", "__bp_d"], [[total, distinct]], row_count=1)


def _blueprint_run_query(sql: str) -> dict[str, Any] | None:
    """Content-route one blueprint inner `runQuery` off its SQL text (D-L3-2).

    Returns a canned `{columns, rows, row_count, truncated}` result, or `None`
    when the SQL is not a blueprint query (so the caller falls through to the
    base FakeMCPClient). Pure function of the SQL string — no per-call state.

    Ordering matters: the D56 grain probe wraps the node SQL in a subquery aliased
    with `__bp_*`, so it contains BOTH the `__bp_` marker AND the node's table
    name — check the grain marker FIRST. And `demo.headcount_by_dept_bad` is a
    superstring of `demo.headcount_by_dept`, so match the `_bad` table first.
    """
    # 1. D56 grain-integrity probe (COUNT(*), COUNT(DISTINCT <grain>)).
    if "__bp_" in sql:
        if _BP_BAD_TABLE in sql:
            return _grain_probe_result(12, 3)  # fan-out double-count → verify FAILS
        return _grain_probe_result(3, 3)  # total == distinct → verify PASSES

    # 2. DISTINCT-domain slot probe (ask→clarify slot resolution).
    if _BP_DEPT_DIM_TABLE in sql:
        return _rows_result(
            ["department"], [["Sales"], ["Engineering"], ["Support"]], row_count=3
        )

    # 3. Per-node queries (most specific table name first).
    if _BP_BAD_TABLE in sql:
        # The withheld fan-out result — a sentinel row + figure the D56 gate
        # blocks; the Layer-3 scenario asserts NEITHER reaches the DOM.
        return _rows_result(
            ["department", "headcount"],
            [[_FANOUT_LEAK_ROW, _FANOUT_LEAK_FIGURE]],
            row_count=12,
        )
    if _BP_GOOD_TABLE in sql:
        return _rows_result(
            ["department", "n"], [["Sales", 3], ["Engineering", 5], ["Support", 2]], row_count=3
        )
    if _BP_TENURE_TABLE in sql:
        return _rows_result(["department", "avg_tenure"], [["Sales", 512.0]], row_count=1)
    if _BP_APPROVAL_UPSTREAM_TABLE in sql:
        return _rows_result(["total"], [[42]], row_count=1)  # the upstream scalar
    if _BP_APPROVAL_TERMINAL_TABLE in sql:
        return _rows_result(["department", "n"], [["Sales", 3]], row_count=1)
    return None


def build_blueprint_details() -> dict[str, BlueprintDetail]:
    """The seeded `getBlueprint`-keyed corpus for the four runBlueprint scenarios.

    `uses` is a NON-None frozenset (a `None` uses fails the scope pre-filter
    closed even under an allow-all scope); the BFF mints an allow-all
    (`column_scope=[]`) JWT, so every blueprint is in scope. The additive DAG
    fields (`slots`/`sql_template`/`composes`/`result_grain`) are the JSON-decoded
    shapes `BlueprintDetail` carries — `Blueprint.parse` turns them into the typed
    executor objects.
    """
    return {
        # 1. Fast-path: a clean single-node blueprint. Bound department → node query
        #    → grain probe PASSES ([[3,3]]) → verified answer.
        _BP_GOOD_ID: BlueprintDetail(
            id=_BP_GOOD_ID,
            intent="Active headcount by department (demo fast path)",
            slots_summary="department",
            uses=frozenset(
                {f"{_BP_GOOD_TABLE}.department", f"{_BP_GOOD_TABLE}.emp_id"}
            ),
            status="validated",
            drift_status="clean",
            hit_count=0,
            catalog_sha="",
            slots=[{"name": "department", "type": "string", "required": True}],
            sql_template=(
                "SELECT department, COUNT(DISTINCT emp_id) AS n "
                f"FROM {_BP_GOOD_TABLE} WHERE department = {{department}} GROUP BY department"
            ),
            result_grain=["department"],
        ),
        # 2. No-silent-verification (D56): the grain probe FAILS ([[12,3]] — a
        #    fan-out double-count), so the result is WITHHELD → raw-loop fallback.
        _BP_BAD_ID: BlueprintDetail(
            id=_BP_BAD_ID,
            intent="Headcount by department, wrong grain (demo D56 negative)",
            slots_summary="department",
            uses=frozenset(
                {f"{_BP_BAD_TABLE}.department", f"{_BP_BAD_TABLE}.emp_id"}
            ),
            status="validated",
            drift_status="clean",
            hit_count=0,
            catalog_sha="",
            slots=[{"name": "department", "type": "string", "required": True}],
            sql_template=(
                "SELECT department, COUNT(DISTINCT emp_id) AS n "
                f"FROM {_BP_BAD_TABLE} WHERE department = {{department}} GROUP BY department"
            ),
            result_grain=["department"],
        ),
        # 3. Ask→clarify (D49): a required `binds_to` slot. An EMPTY slot_bindings
        #    pauses on the missing slot (no probe fires, n3); on resume the filled
        #    value fires the DISTINCT-domain probe over demo.dept_dim → binds →
        #    node query → grain probe PASSES → verified answer.
        _BP_TENURE_ID: BlueprintDetail(
            id=_BP_TENURE_ID,
            intent="Average tenure by department (demo slot clarify)",
            slots_summary="department",
            uses=frozenset(
                {
                    f"{_BP_TENURE_TABLE}.department",
                    f"{_BP_TENURE_TABLE}.tenure_days",
                    f"{_BP_DEPT_DIM_TABLE}.department",
                }
            ),
            status="validated",
            drift_status="clean",
            hit_count=0,
            catalog_sha="",
            slots=[
                {
                    "name": "department",
                    "type": "string",
                    "required": True,
                    "binds_to": f"{_BP_DEPT_DIM_TABLE}.department",
                }
            ],
            sql_template=(
                "SELECT department, AVG(tenure_days) AS avg_tenure "
                f"FROM {_BP_TENURE_TABLE} WHERE department = {{department}} GROUP BY department"
            ),
            result_grain=["department"],
        ),
        # 4. Approval pause/resume (D45/D59b): a scalar-passing DAG — node 0 computes
        #    an upstream scalar ([[42]]), node 1 is an APPROVAL gate (pause showing
        #    the upstream aggregate), node 2 is the terminal query. On approve the
        #    executor re-enters at the awaiting node → terminal query → grain probe
        #    PASSES → verified answer.
        _BP_APPROVAL_ID: BlueprintDetail(
            id=_BP_APPROVAL_ID,
            intent="Headcount by department with an approval gate (demo D45)",
            slots_summary="department",
            uses=frozenset(
                {
                    f"{_BP_APPROVAL_UPSTREAM_TABLE}.emp_id",
                    f"{_BP_APPROVAL_TERMINAL_TABLE}.department",
                    f"{_BP_APPROVAL_TERMINAL_TABLE}.emp_id",
                }
            ),
            status="validated",
            drift_status="clean",
            hit_count=0,
            catalog_sha="",
            slots=[{"name": "department", "type": "string", "required": True}],
            result_grain=["department"],
            composes=[
                {
                    "order": 0,
                    "node_kind": "query",
                    "output": {"total": "scalar"},
                    "sql_template": (
                        f"SELECT COUNT(DISTINCT emp_id) AS total FROM {_BP_APPROVAL_UPSTREAM_TABLE}"
                    ),
                },
                {
                    "order": 1,
                    "node_kind": "approval",
                    "feeds_from": [0],
                    "requires_approval": {
                        "prompt": "Approve running the headcount report before I continue?"
                    },
                },
                {
                    "order": 2,
                    "node_kind": "query",
                    "feeds_from": [1],
                    "sql_template": (
                        "SELECT department, COUNT(DISTINCT emp_id) AS n "
                        f"FROM {_BP_APPROVAL_TERMINAL_TABLE} "
                        "WHERE department = {department} GROUP BY department"
                    ),
                },
            ],
        ),
    }


class DemoModelClient:
    """A repeatable, content-routed `ModelClient` double for interactive demos.

    Reads the ALREADY-BUILT canonical message list the loop hands it (same
    shape `ScriptedModelClient` receives — see `model/client.py`) and decides
    its canned response purely from that shape, so it needs no external
    per-session state and never runs out of "script". Routed off the
    ORIGINAL (first) user message of the turn (case-insensitive substring
    match — see the module docstring's trigger-phrase table):

      - "ask" -> demo the askUser/chip flow.
          * 1st call this turn (only one user message seen so far) -> emit an
            `askUser` tool call with a couple of chip `options`.
          * 2nd call (a resume already appended the user's answer -> two user
            messages now) -> final answer that echoes the chosen option.
      - "keep going" / "forever" -> demo the budget-cap pause (D47).
          * Every call THIS window (still only one user message seen) -> emit
            another `getTableSchema` tool call, never a final answer, so the
            loop's `BudgetGuard` is the only thing that can end the window.
          * Once resumed with "continue"/"refine" (a second user message is
            now present, appended by `AgentLoop.resume`/D55's fresh window) ->
            a final answer immediately, so the turn ends cleanly on the very
            first call of the new window.
      - "salaries" / "salary" -> demo a column-scope denial (D57).
          * 1st call (no `tool` messages yet) -> emit a `runQuery` tool call
            whose SQL is `_SCOPE_DENIAL_QUERY` (matched by `DemoMCPClient`).
          * 2nd call (the denied tool result was replayed back) -> a graceful
            final answer that surfaces the denial in plain language — never
            raw data (there is none: the MCP denied the call before any rows
            existed).
      - "raw sql" -> demo a fail-closed parser rejection (D63).
          * 1st call (no `tool` messages yet) -> emit a `runQuery` tool call
            whose SQL is `_PARSE_FAIL_QUERY` (matched by `DemoMCPClient`).
          * 2nd call (the denied tool result was replayed back) -> a graceful
            "couldn't validate that query" final answer — rejected, not
            crashed.
      - Otherwise -> demo an ordinary tool-call turn.
          * 1st call (no `tool` messages yet) -> emit a `getTableSchema`
            tool call.
          * 2nd call (a `tool` role message is now present, i.e. the
            dispatched result was replayed back) -> final free-text answer.
    """

    async def send_turn(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        user_messages = [m for m in messages if m.get("role") == "user"]
        tool_messages = [m for m in messages if m.get("role") == "tool"]

        if not user_messages:
            return ModelTurnResult(assistant_text="I didn't receive a message to respond to.")

        first_user_text = str(user_messages[0].get("content") or "")
        first_lower = first_user_text.lower()

        # NB: every branch below is a BARE case-insensitive SUBSTRING match, so
        # ORDER IS SIGNIFICANT — a more-specific trigger that is a superstring (or
        # shares a token) with a broader one must be checked FIRST (e.g. "bad
        # headcount" before "headcount by department"; see the D89 block). Kept as
        # substring matching (not word-boundary) to mirror the existing demo
        # doubles; the ordering is the guard.
        if "ask" in first_lower:
            if len(user_messages) == 1:
                return ModelTurnResult(
                    tool_calls=[
                        ToolCallRequest(
                            id="call-ask-1",
                            name="askUser",
                            arguments={
                                "question": "Which department should I focus the analysis on?",
                                "options": ["Sales", "Engineering", "Support"],
                            },
                        )
                    ]
                )
            answer_text = str(user_messages[-1].get("content") or "").strip()
            return ModelTurnResult(
                assistant_text=(
                    f"Got it — focusing on {answer_text or 'that'}. "
                    f"Here is a placeholder summary for the {answer_text or 'chosen'} department."
                )
            )

        if "keep going" in first_lower or "forever" in first_lower:
            if len(user_messages) >= 2:
                # Resumed into a fresh budget window (D55) -> finish cleanly
                # on the first call of the new window rather than looping
                # forever again.
                return ModelTurnResult(
                    assistant_text=(
                        "Wrapping up after the extra budget window — here is a "
                        "placeholder summary of what was found so far. "
                        "(Placeholder answer — scripted demo runtime.)"
                    )
                )
            call_id = f"call-keepgoing-{len(tool_messages) // 2 + 1}"
            return ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id=call_id,
                        name="getTableSchema",
                        arguments={"database": _DEMO_DATABASE, "table": _DEMO_TABLE},
                    )
                ]
            )

        if "salaries" in first_lower or "salary" in first_lower:
            if not tool_messages:
                return ModelTurnResult(
                    tool_calls=[
                        ToolCallRequest(
                            id="call-scope-1",
                            name="runQuery",
                            arguments={"query": _SCOPE_DENIAL_QUERY},
                        )
                    ]
                )
            return ModelTurnResult(
                assistant_text=(
                    "I can't access those columns — that needs access outside your "
                    "current column scope. I can share a summary of the columns you "
                    "do have access to instead, if that helps."
                )
            )

        if "raw sql" in first_lower:
            if not tool_messages:
                return ModelTurnResult(
                    tool_calls=[
                        ToolCallRequest(
                            id="call-parsefail-1",
                            name="runQuery",
                            arguments={"query": _PARSE_FAIL_QUERY},
                        )
                    ]
                )
            return ModelTurnResult(
                assistant_text=(
                    "I couldn't validate that query safely, so I didn't run it. "
                    "Could you rephrase it as a normal question instead?"
                )
            )

        # --- runBlueprint scenarios (D89) --------------------------------------
        # The loop intercepts `runBlueprint` via the runtime-tool registry (wired
        # once `create_app` is given a retrieval pipeline), so the double emits it
        # regardless of the advertised MCP schema — content-routed like every other
        # branch here. ORDER MATTERS (bare-substring triggers): "bad headcount" is
        # checked BEFORE "headcount by department" so a phrase carrying both routes
        # to the no-silent-verification blueprint, not the fast path.
        if "bad headcount" in first_lower:
            # No-silent-verification: runBlueprint fails the D56 grain gate and
            # returns VERIFY_FAILED (an error tool result carrying NO rows). The
            # model does exactly what production does — falls back to the raw loop
            # and answers WITHOUT the withheld fan-out numbers.
            if not tool_messages:
                return ModelTurnResult(
                    tool_calls=[
                        ToolCallRequest(
                            id="call-bp-bad-1",
                            name="runBlueprint",
                            arguments={
                                "id": _BP_BAD_ID,
                                "slot_bindings": {"department": "Sales"},
                            },
                        )
                    ]
                )
            return ModelTurnResult(
                assistant_text=(
                    "The fast path couldn't produce a verified result for that, so I "
                    "did not return any figures. Try rephrasing the question and I can "
                    "answer it from the raw tools instead. (Scripted demo runtime.)"
                )
            )

        if "headcount by department" in first_lower:
            # Fast path: emit runBlueprint; on the verified tool result, a final
            # answer. The verified rows are surfaced by the runtime — the answer
            # copy is scripted demo prose (never a data contract).
            if not tool_messages:
                return ModelTurnResult(
                    tool_calls=[
                        ToolCallRequest(
                            id="call-bp-fast-1",
                            name="runBlueprint",
                            arguments={
                                "id": _BP_GOOD_ID,
                                "slot_bindings": {"department": "Sales"},
                            },
                        )
                    ]
                )
            return ModelTurnResult(
                assistant_text=(
                    "Here is the verified headcount by department from the blueprint "
                    "fast path. (Placeholder answer — scripted demo runtime.)"
                )
            )

        if "average tenure" in first_lower:
            # Ask→clarify→resume: an EMPTY slot_bindings pauses on the missing
            # required slot (blueprint_slot). On resume (the clarified department is
            # now the latest user message) re-emit runBlueprint with it filled; on
            # the verified tool result, a final answer.
            if not tool_messages:
                if len(user_messages) >= 2:
                    department = str(user_messages[-1].get("content") or "").strip()
                    return ModelTurnResult(
                        tool_calls=[
                            ToolCallRequest(
                                id="call-bp-tenure-2",
                                name="runBlueprint",
                                arguments={
                                    "id": _BP_TENURE_ID,
                                    "slot_bindings": {"department": department},
                                },
                            )
                        ]
                    )
                return ModelTurnResult(
                    tool_calls=[
                        ToolCallRequest(
                            id="call-bp-tenure-1",
                            name="runBlueprint",
                            arguments={"id": _BP_TENURE_ID, "slot_bindings": {}},
                        )
                    ]
                )
            return ModelTurnResult(
                assistant_text=(
                    "Here is the verified average tenure for the department you chose. "
                    "(Placeholder answer — scripted demo runtime.)"
                )
            )

        if "approve headcount" in first_lower:
            # Approval pause/resume: emit runBlueprint; the executor runs the
            # upstream scalar node, then PAUSES at the approval gate. On approve the
            # loop re-enters the executor at the awaiting node → verified result →
            # this final answer (a tool result is present by then).
            if not tool_messages:
                return ModelTurnResult(
                    tool_calls=[
                        ToolCallRequest(
                            id="call-bp-approval-1",
                            name="runBlueprint",
                            arguments={
                                "id": _BP_APPROVAL_ID,
                                "slot_bindings": {"department": "Sales"},
                            },
                        )
                    ]
                )
            return ModelTurnResult(
                assistant_text=(
                    "Approved — here is the verified headcount report. "
                    "(Placeholder answer — scripted demo runtime.)"
                )
            )

        if not tool_messages:
            return ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="call-schema-1",
                        name="getTableSchema",
                        arguments={"database": _DEMO_DATABASE, "table": _DEMO_TABLE},
                    )
                ]
            )

        return ModelTurnResult(
            assistant_text=(
                f"The `{_DEMO_DATABASE}.{_DEMO_TABLE}` table has columns "
                f"{', '.join(_DEMO_SCHEMA_RESPONSE['columns'])}. "
                "(Placeholder answer — scripted demo runtime, no real ClickHouse query ran.)"
            )
        )

    def begin_turn(self) -> DemoModelClient:
        # No per-turn state to isolate (mirrors ScriptedModelClient's own
        # degenerate case, model/client.py's `begin_turn` docstring).
        return self


class DemoMCPClient(FakeMCPClient):
    """`FakeMCPClient`, extended so `runQuery` denials are content-routed off
    the submitted SQL string rather than a fixed FIFO queue.

    `FakeMCPClient.call_tool` forks purely on `(tool_name, call order)` — it
    has no visibility into `args` at all — so it cannot express "the SAME
    tool, called with different SQL, must be denied for two different
    reasons depending on what the SQL says", which the scope-denial (D57)
    and parser-fail-closed (D63) conformance scenarios both need from one
    `runQuery` tool. This override intercepts `runQuery` specifically and
    matches its `query` argument against the two sentinel strings
    `DemoModelClient` emits (`_SCOPE_DENIAL_QUERY`/`_PARSE_FAIL_QUERY`);
    every other tool call (including any `runQuery` call that matches
    neither sentinel) falls straight through to the base class's normal
    scripted-queue behavior unchanged.

    This is demo-launcher plumbing ONLY — the real MCP derives these same
    denial codes from its actual scope-check/parser, never from string
    matching; nothing here is a substitute for `dispatch/denial_mapping.py`
    or the adopted MCP's own enforcement.

    The `runBlueprint` scenarios (D89) add a SECOND, purely-additive content
    route: the `BlueprintExecutor`'s inner per-node `runQuery`s, its DISTINCT-
    domain slot probes, and its D56 grain probes are all matched off the SQL
    text and answered with canned results (`_blueprint_run_query` below). Because
    routing is a pure function of the SQL string — matched off WHICHEVER of the
    `sql`/`query` arg keys is present (the executor dispatches with `sql`; the
    model-emitted `runQuery` uses `query`) — any number of sessions/turns replay
    identically, with NO per-instance FIFO to drift across the long-lived server.
    The two sentinel denials + the `getTableSchema` queue fall through untouched.
    """

    async def call_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        jwt: str,
        session_id: str,
    ) -> dict[str, Any] | list[Any]:
        if tool_name == "runQuery":
            # Read the SQL from whichever key is present: the executor dispatches
            # runQuery with the `sql` key, whereas a model-emitted runQuery uses
            # `query` — match on either (defensive per D-L3-2).
            sql = str(args.get("sql") or args.get("query") or "")
            if _SCOPE_DENIAL_QUERY in sql:
                self.calls.append(
                    RecordedCall(tool_name=tool_name, args=dict(args), jwt=jwt, session_id=session_id)
                )
                raise MCPToolError(
                    "COLUMN_SCOPE_VIOLATION",
                    "[COLUMN_SCOPE_VIOLATION] column 'AnnualSalary' is outside the "
                    "caller's column scope",
                )
            if _PARSE_FAIL_QUERY in sql:
                self.calls.append(
                    RecordedCall(tool_name=tool_name, args=dict(args), jwt=jwt, session_id=session_id)
                )
                raise MCPToolError(
                    "PARSE_FAILED_CLOSED",
                    "[PARSE_FAILED_CLOSED] could not parse/validate the submitted SQL",
                )
            blueprint_result = _blueprint_run_query(sql)
            if blueprint_result is not None:
                self.calls.append(
                    RecordedCall(tool_name=tool_name, args=dict(args), jwt=jwt, session_id=session_id)
                )
                return blueprint_result
        return await super().call_tool(tool_name, args, jwt=jwt, session_id=session_id)


def build_demo_app():
    settings = RuntimeSettings(
        jwks_url=_JWKS_URL,
        jwt_issuer=_TOKEN_ISSUER,
        jwt_audience=_TOKEN_AUDIENCE,
        # Deliberately LOW (module docstring): makes the budget-cap
        # conformance scenario ("keep going forever") reachable in exactly 3
        # demo tool-call iterations instead of a real 15-round wait, while
        # every other scenario here (normal/clarify/denial/parse-fail) only
        # ever consumes a single iteration and so still completes well under
        # this cap.
        max_loop_iterations=3,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
    )

    catalog = CatalogHandle(
        {
            f"{_DEMO_DATABASE}.{_DEMO_TABLE}": _DEMO_SCHEMA_RESPONSE["columns"],
            # The blueprint tables (D89) — so the executor's inner runQuery
            # provenance is DETERMINED and the verified result survives D44 replay.
            **_BP_TABLE_SCHEMAS,
        }
    )

    mcp_client = DemoMCPClient(
        tools=[
            MCPToolSpec(
                name="getTableSchema",
                description="Return the column schema of a database.table.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "database": {"type": "string"},
                        "table": {"type": "string"},
                    },
                    "required": ["database", "table"],
                },
            ),
            MCPToolSpec(
                name="runQuery",
                description="Run a read-only SQL query against the warehouse.",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ),
        ],
        # Repeated 200x so this launcher survives many demo turns/sessions
        # without exhausting FakeMCPClient's per-tool response queue (see
        # module docstring). `runQuery` is NOT scripted here — DemoMCPClient
        # intercepts it directly (see its docstring).
        scripted={"getTableSchema": [dict(_DEMO_SCHEMA_RESPONSE) for _ in range(200)]},
    )

    # runBlueprint fast path (D89): `create_app` wires the read tools + runBlueprint
    # + the BlueprintExecutor ONLY when an `active_retrieval` pipeline is present.
    # Inject a HERMETIC one — a seeded `FakeVectorIndex` (keyed getBlueprint corpus;
    # EMPTY recall `entries` so recall returns 0 cards for every question and the
    # pre-injection step stays inert for the 5 pre-existing scenarios, §4 regression
    # guard) + a `FakeEmbeddingClient` (deterministic hash vectors, no network). The
    # executor's inner runQuery/domain/grain probes flow through the SAME per-request
    # ToolDispatcher as production and are content-routed by DemoMCPClient.
    retrieval = build_retrieval_pipeline(settings)

    return create_app(
        settings=settings,
        session_store=InMemorySessionStore(),
        mcp_client=mcp_client,
        model_client=DemoModelClient(),
        catalog=catalog,
        retrieval=retrieval,
    )


def build_retrieval_pipeline(settings: RuntimeSettings) -> RetrievalPipeline:
    """Build the hermetic Slice-1 retrieval pipeline: a `FakeVectorIndex` seeded
    with the four blueprint fixtures (keyed `get_blueprint` fetch; NO recall
    entries) + a `FakeEmbeddingClient`. Deterministic, no neo4j, no embedder.

    Exposed (not inlined) so a non-e2e test can drive the exact seeded
    executor path this launcher wires — proving runBlueprint end-to-end without
    a browser or a live JWKS."""
    vector_index = FakeVectorIndex(entries=[], details=build_blueprint_details())
    return RetrievalPipeline(
        embedding_client=FakeEmbeddingClient(),
        reranker=None,
        vector_index=vector_index,
        user_memory=NullUserMemoryProvider(),
        recall_k=settings.retrieval_recall_k,
        top_k_blueprints=settings.retrieval_top_k_blueprints,
        top_k_knowledge=settings.retrieval_top_k_knowledge,
        knowledge_min_score=settings.retrieval_knowledge_min_score,
    )


app = build_demo_app()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
