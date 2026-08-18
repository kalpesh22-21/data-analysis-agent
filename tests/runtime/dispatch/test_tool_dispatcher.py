"""Unit tests for dispatch/tool_dispatcher.py (Layer 1 — FakeMCPClient, no infra).

Includes the D5 injection-integrity invariant: the JWT/session_id reach the
FakeMCPClient transport boundary but never appear in the ToolResult.
"""

from __future__ import annotations

import dataclasses
import json

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle

_E = "dbpcm_warehouse.employee"

CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})

SECRET_JWT = "eyJhbGciOi.super-secret-jwt-body.sig"
SESSION_ID = "sess-integrity-test"


def _credentials(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=SECRET_JWT, column_scope=scope)


def _result_to_scannable_json(result: ToolResult) -> str:
    payload = dataclasses.asdict(result)
    # frozenset isn't JSON-serializable; render provenance as a sorted list first.
    if payload.get("provenance") is not None:
        payload["provenance"] = sorted(payload["provenance"])
    return json.dumps(payload, default=str)


async def test_ok_result_never_carries_credentials() -> None:
    mcp_client = FakeMCPClient(
        scripted={
            "getTableSchema": [
                {
                    "database": "dbpcm_warehouse",
                    "table": "employee",
                    "columns": [{"name": "EmployeeCode", "type": "String", "comment": ""}],
                }
            ]
        }
    )
    dispatcher = ToolDispatcher(mcp_client, CATALOG)

    result = await dispatcher.dispatch(
        "getTableSchema", {"database": "dbpcm_warehouse", "table": "employee"}, _credentials()
    )

    # Transport boundary DID receive the credentials.
    assert len(mcp_client.calls) == 1
    assert mcp_client.calls[0].jwt == SECRET_JWT
    assert mcp_client.calls[0].session_id == SESSION_ID

    # ToolResult NEVER carries them.
    blob = _result_to_scannable_json(result)
    assert SECRET_JWT not in blob
    assert SESSION_ID not in blob
    assert result.status == "ok"


async def test_denied_result_never_carries_credentials() -> None:
    mcp_client = FakeMCPClient(
        scripted={"runQuery": [MCPToolError("COLUMN_SCOPE_VIOLATION", "denied")]}
    )
    dispatcher = ToolDispatcher(mcp_client, CATALOG)

    result = await dispatcher.dispatch(
        "runQuery", {"sql": "SELECT Amount FROM payroll"}, _credentials()
    )

    assert result.status == "denied"
    assert result.error_code == "COLUMN_SCOPE_VIOLATION"
    assert result.retryable is False
    blob = _result_to_scannable_json(result)
    assert SECRET_JWT not in blob
    assert SESSION_ID not in blob


async def test_dispatch_captures_provenance_on_success() -> None:
    raw_result = {
        "columns": ["EmployeeCode", "Department"],
        "rows": [["E1", "Sales"]],
        "row_count": 1,
        "truncated": False,
    }
    mcp_client = FakeMCPClient(scripted={"runQuery": [dict(raw_result)]})
    dispatcher = ToolDispatcher(mcp_client, CATALOG)
    result = await dispatcher.dispatch(
        "runQuery",
        {"sql": "SELECT EmployeeCode, Department FROM employee"},
        _credentials(),
    )
    assert result.status == "ok"
    assert result.provenance == frozenset({(_E, "EmployeeCode"), (_E, "Department")})
    assert result.result_preview is not None
    assert result.result_preview.row_count == 1
    assert result.result_full == raw_result


async def test_preview_truncates_to_preview_row_count() -> None:
    rows = [[f"E{i}"] for i in range(50)]
    mcp_client = FakeMCPClient(
        scripted={
            "sampleRows": [
                {"columns": ["EmployeeCode"], "rows": rows, "row_count": 50, "truncated": False}
            ]
        }
    )
    dispatcher = ToolDispatcher(mcp_client, CATALOG, preview_row_count=5)
    result = await dispatcher.dispatch(
        "sampleRows", {"database": "dbpcm_warehouse", "table": "employee"}, _credentials()
    )
    assert result.result_preview.truncated is True
    assert len(result.result_preview.preview_rows) == 5


async def test_wide_get_table_schema_is_size_capped_with_a_marker() -> None:
    """A wide getTableSchema (100+ columns) must NOT be stored as one unbounded
    ~30k-token preview cell — its COLUMN LIST is fitted under
    `schema_columns_token_budget` and the stored preview stays a VALID, parseable
    dict.

    REWRITTEN TWICE. C5 replaced the head-cut that dropped every column past the
    cut INCLUDING ITS NAME (how `employee.annual_salary` became invisible). C5b
    moved the budget onto the COLUMNS SECTION ALONE, made the emitted order
    GROUPED (detailed group first, skeleton remainder in original order) and made
    the tenancy strip unconditional. The policy itself is pinned in
    `tests/runtime/dispatch/test_schema_preview.py`; this test pins the
    DISPATCHER's end of it: the fitted dict is what lands in the preview cell, the
    marker rides inside the fit, and `result_full` is untouched."""
    wide_schema = {
        "database": "dbpcm_warehouse",
        "table": "employee",
        "columns": [
            {"name": f"Column_{i}", "type": "String", "comment": "some descriptive comment"}
            for i in range(400)
        ],
    }
    mcp_client = FakeMCPClient(scripted={"getTableSchema": [wide_schema]})
    # 6,000 tokens holds all 400 NAMES (~4,000) with room for some detail. The
    # generic `max_tool_result_tokens` is deliberately LEFT AT ITS DEFAULT here:
    # it no longer governs this branch at all, and the assertions below would fail
    # if it did.
    dispatcher = ToolDispatcher(mcp_client, CATALOG, schema_columns_token_budget=6_000)

    result = await dispatcher.dispatch(
        "getTableSchema", {"database": "dbpcm_warehouse", "table": "employee"}, _credentials()
    )

    assert result.status == "ok"
    assert result.result_preview is not None
    assert result.result_preview.truncated is True
    # The stored cell is the fitted dict — still valid + parseable.
    capped = result.result_preview.preview_rows[0][0]
    assert isinstance(capped, dict)
    assert capped["database"] == "dbpcm_warehouse"
    assert capped["table"] == "employee"
    # EXISTENCE SURVIVES: all 400 columns are present.
    names = [c["name"] for c in capped["columns"]]
    assert sorted(names) == sorted(f"Column_{i}" for i in range(400))
    # DETAIL DEGRADES, AND THE DETAILED ONES LEAD: with no question the ranking is
    # list order, so the detailed group is the head of the list and the skeleton
    # remainder follows in the table's own order.
    detailed = [c["name"] for c in capped["columns"] if set(c) - {"name", "type"}]
    assert detailed
    assert len(detailed) < 400
    assert names[: len(detailed)] == detailed
    assert detailed[0] == "Column_0"
    assert capped["columns"][-1] == {"name": "Column_399", "type": "String"}
    # The marker says what was withheld and promises nothing unfollowable.
    assert "_truncated" in capped
    assert "400 of 400 columns are listed" in capped["_truncated"]
    assert "name and type ONLY" in capped["_truncated"]
    # NO question was passed, so the marker must not claim relevance ordering —
    # the leading columns here are the table's own, not the request's.
    assert "relevance" not in capped["_truncated"].lower()
    assert "NOT listed in the table's physical column order" in capped["_truncated"]
    assert "re-fetch" not in capped["_truncated"]
    # The withheld documentation has a RECOVERY, not a dead end (2026-08-18).
    assert 'columns: ["<name>", ...]' in capped["_truncated"]
    # Actually bounded — the COLUMNS SECTION plus the marker, which is what the
    # budget governs, and marker-INCLUSIVE (the marker is part of every trial
    # render, not appended after the fit was measured).
    budgeted = json.dumps(capped["columns"]) + capped["_truncated"]
    assert len(budgeted) // 4 <= 6_000

    # The FULL, un-capped result is still returned on result_full for the caller
    # (the preview budget bounds only the model-facing stored preview).
    assert len(result.result_full["columns"]) == 400
    assert result.result_full["columns"][399] == {
        "name": "Column_399",
        "type": "String",
        "comment": "some descriptive comment",
    }


async def test_the_tenancy_columns_never_reach_the_stored_preview() -> None:
    """C5b: `client_code` / `proc_center` are PRE-APPLIED by the platform (the row
    policy binds them to the caller's token claims), so the model never filters on
    them and must never be shown them. This schema is far UNDER any budget — the
    path that used to return the raw result byte-identically — and they are still
    gone, along with `hours`'s null `description` (user spec 2026-08-18 point 5:
    the null-key strip is unconditional too). No marker either way: neither strip
    withheld anything the model needed. `result_full` still carries the tenancy
    columns for the runtime's own use.

    The operator's channel for the strip is asserted in
    `test_the_tenancy_strip_is_reported_even_when_nothing_was_truncated` below."""
    schema = {
        "database": "dbpcm_warehouse",
        "table": "accrual_events",
        "rules": ["client_code is applied server-side by the row policy."],
        "columns": [
            {"name": "client_code", "type": "String", "description": "Tenant key."},
            {"name": "proc_center", "type": "String", "description": "Processing centre."},
            {"name": "employee_code", "type": "String", "description": "Employee key."},
            {"name": "hours", "type": "Float64", "description": None},
        ],
    }
    mcp_client = FakeMCPClient(scripted={"getTableSchema": [json.loads(json.dumps(schema))]})
    dispatcher = ToolDispatcher(mcp_client, CATALOG)

    result = await dispatcher.dispatch(
        "getTableSchema",
        {"database": "dbpcm_warehouse", "table": "accrual_events"},
        _credentials(),
    )

    stored = result.result_preview.preview_rows[0][0]
    assert [c["name"] for c in stored["columns"]] == ["employee_code", "hours"]
    # COMPACTED-STABLE: the documented column is verbatim, the null key is gone,
    # no reordering, no marker, and `truncated` stays False.
    assert stored["columns"] == [
        {"name": "employee_code", "type": "String", "description": "Employee key."},
        {"name": "hours", "type": "Float64"},
    ]
    assert "_truncated" not in stored
    assert result.result_preview.truncated is False
    # The base section that MENTIONS the tenant column rides complete: it explains
    # why the model does not write that predicate.
    assert stored["rules"] == schema["rules"]
    # The un-capped result is untouched for the runtime's own consumers.
    assert result.result_full["columns"] == schema["columns"]


async def test_the_tenancy_strip_is_reported_even_when_nothing_was_truncated() -> None:
    """SHOULD-FIX (2026-08-18): the tenancy count had ONE channel to the operator —
    the `tool_dispatch_schema_detail_dropped` payload — and that event fires only
    when the SAME result also blew its columns budget. So on a small schema, which
    is every one of the seven catalogued tables that expose `client_code` as an
    ordinary column, a column was removed from the model's view and nothing was
    recorded anywhere. The strip now has its own ungated event.

    Deliberately NOT a degrade signal: no marker is added, `truncated` stays False,
    and the log line is INFO — the columns are pre-applied by the platform, so
    removing them withholds nothing. It is an operator FACT, not a warning."""
    schema = {
        "database": "dbpcm_warehouse",
        "table": "accrual_events",
        "columns": [
            {"name": "client_code", "type": "String", "description": "Tenant key."},
            {"name": "employee_code", "type": "String", "description": "Employee key."},
        ],
    }
    events: list[tuple[str, dict]] = []
    mcp_client = FakeMCPClient(scripted={"getTableSchema": [json.loads(json.dumps(schema))]})
    dispatcher = ToolDispatcher(mcp_client, CATALOG, observer=lambda e, p: events.append((e, p)))

    result = await dispatcher.dispatch(
        "getTableSchema",
        {"database": "dbpcm_warehouse", "table": "accrual_events"},
        _credentials(),
    )

    assert ("tool_dispatch_tenancy_columns_hidden", {
        "tool_name": "getTableSchema",
        "tenancy_hidden_count": 1,
    }) in events
    # EXACTLY this event — the budget never bound, so no degrade was reported.
    assert [name for name, _ in events if name == "tool_dispatch_schema_detail_dropped"] == []
    stored = result.result_preview.preview_rows[0][0]
    assert "_truncated" not in stored
    assert result.result_preview.truncated is False
    assert [c["name"] for c in stored["columns"]] == ["employee_code"]


async def test_no_tenancy_event_when_there_was_nothing_to_hide() -> None:
    """The event is gated on `tenancy_hidden_count > 0` — a schema with no tenancy
    column must not emit a zero-count event on every schema fetch."""
    schema = {
        "database": "dbpcm_warehouse",
        "table": "employee",
        "columns": [{"name": "employee_code", "type": "String", "description": "Key."}],
    }
    events: list[tuple[str, dict]] = []
    mcp_client = FakeMCPClient(scripted={"getTableSchema": [json.loads(json.dumps(schema))]})
    dispatcher = ToolDispatcher(mcp_client, CATALOG, observer=lambda e, p: events.append((e, p)))

    await dispatcher.dispatch(
        "getTableSchema", {"database": "dbpcm_warehouse", "table": "employee"}, _credentials()
    )

    assert [name for name, _ in events if name == "tool_dispatch_tenancy_columns_hidden"] == []


async def test_wide_schema_detail_drop_is_reported_to_the_operator() -> None:
    """Degrade-not-fail, NEVER SILENTLY — the same posture as the card branch. The
    model is told by the in-fit marker; the operator gets a counts-only observer
    event (D25: no column name, no schema text, no question). `base_dropped_count`
    is GONE from the payload (C5b): base sections can no longer be dropped, so it
    could only ever have reported 0; `tenancy_hidden_count` takes its place, which
    is the one withholding the marker deliberately does not mention."""
    wide_schema = {
        "database": "dbpcm_warehouse",
        "table": "employee",
        "columns": [
            {"name": "client_code", "type": "String", "comment": "Tenant key."},
            *(
                {"name": f"Column_{i}", "type": "String", "comment": "some descriptive comment"}
                for i in range(400)
            ),
        ],
    }
    events: list[tuple[str, dict]] = []
    mcp_client = FakeMCPClient(scripted={"getTableSchema": [wide_schema]})
    dispatcher = ToolDispatcher(
        mcp_client,
        CATALOG,
        schema_columns_token_budget=6_000,
        observer=lambda e, p: events.append((e, p)),
    )

    result = await dispatcher.dispatch(
        "getTableSchema", {"database": "dbpcm_warehouse", "table": "employee"}, _credentials()
    )

    capped = result.result_preview.preview_rows[0][0]
    detailed = [c["name"] for c in capped["columns"] if set(c) - {"name", "type"}]
    assert ("tool_dispatch_schema_detail_dropped", {
        "tool_name": "getTableSchema",
        "detailed_count": len(detailed),
        # The tenancy column is NOT part of the model's column universe.
        "total_count": 400,
        "tenancy_hidden_count": 1,
    }) in events


async def test_a_big_base_is_no_longer_a_degrade_at_all() -> None:
    """THE C5b INVERSION of the old base-degradation test. This schema is far over
    the generic `max_tool_result_tokens` because of ONE enormous `rules` section
    beside four small documented columns. C5 dropped `rules` to fit and reported a
    `base_dropped_count`; the user decision is that table-level sections are NEVER
    truncated — so `rules` rides complete, every column keeps its detail, NOTHING
    was withheld, and therefore there is no marker, no `truncated` flag and no
    operator event. (The silent-degrade regression that test guarded against is
    now impossible by construction: the only degrade left is a column degrade, and
    the event is gated on the same `marker_added` the model sees.)"""
    schema = {
        "database": "dbpcm_warehouse",
        "table": "employee",
        "grain": "one row per employee",
        # ~9k tokens on its own — far over the 4,000-token generic cap.
        "rules": [
            f"Rule {i}: " + "consult the catalog before aggregating. " * 8
            for i in range(100)
        ],
        "ambiguities": ["'headcount' may mean active or all employees."],
        "columns": [
            {"name": "employee_id", "type": "String", "description": "Employee key."},
            {"name": "annual_salary", "type": "Decimal", "description": "Yearly pay, USD."},
            {"name": "department_code", "type": "String", "description": "Department key."},
            {"name": "employee_status", "type": "String", "description": "Active or not."},
        ],
    }
    events: list[tuple[str, dict]] = []
    mcp_client = FakeMCPClient(scripted={"getTableSchema": [json.loads(json.dumps(schema))]})
    dispatcher = ToolDispatcher(
        mcp_client,
        CATALOG,
        max_tool_result_tokens=4_000,
        observer=lambda e, p: events.append((e, p)),
    )

    result = await dispatcher.dispatch(
        "getTableSchema", {"database": "dbpcm_warehouse", "table": "employee"}, _credentials()
    )

    stored = result.result_preview.preview_rows[0][0]
    assert stored["rules"] == schema["rules"]
    assert stored["ambiguities"] == schema["ambiguities"]
    assert stored["columns"] == schema["columns"]
    assert "_truncated" not in stored
    assert result.result_preview.truncated is False
    assert [name for name, _ in events if name == "tool_dispatch_schema_detail_dropped"] == []


async def test_the_question_only_reorders_the_schema_fit() -> None:
    """The loop hands `dispatch` the turn's raw user text. It may ONLY choose which
    columns keep their documentation — D25 forbids it from reaching any stored,
    streamed or spanned surface, so the whole ToolResult is scanned for it (the
    same scan the credential invariant at the top of this module uses)."""
    nonce = "zqx-nonce-42"
    wide_schema = {
        "database": "dbpcm_warehouse",
        "table": "employee",
        "columns": [
            {"name": f"Column_{i}", "type": "String", "comment": "some descriptive comment"}
            for i in range(400)
        ]
        + [{"name": "annual_salary", "type": "Decimal", "comment": "Yearly pay, in USD."}],
    }
    mcp_client = FakeMCPClient(scripted={"getTableSchema": [dict(wide_schema)]})
    dispatcher = ToolDispatcher(mcp_client, CATALOG, schema_columns_token_budget=6_000)

    result = await dispatcher.dispatch(
        "getTableSchema",
        {"database": "dbpcm_warehouse", "table": "employee"},
        _credentials(),
        question=f"what is the average annual salary, {nonce}?",
    )

    capped = result.result_preview.preview_rows[0][0]
    detailed = {c["name"] for c in capped["columns"] if set(c) - {"name", "type"}}
    # The LAST column of 401 — unreachable by any head-cut — keeps its detail.
    assert "annual_salary" in detailed
    # ...and the question text itself is nowhere in the result.
    blob = _result_to_scannable_json(result)
    assert nonce not in blob
    assert "average annual salary" not in blob


def test_dispatch_estimator_matches_budget_estimator() -> None:
    """Parity guard (NIT 3): dispatch's per-result cap estimator and the trail
    budget walk's estimator must measure token cost IDENTICALLY. They cannot share
    an import (dispatch/__init__ eagerly imports tool_dispatcher and budget.py
    transitively imports dispatch → cycle), so this pins the two copies together —
    a future tokenizer swap must update both."""
    from data_agent.runtime.context.budget import _estimate_tokens as budget_estimate
    from data_agent.runtime.dispatch.tool_dispatcher import _estimate_tokens as dispatch_estimate

    for text in ["", "a", "SELECT * FROM t", '{"columns": [{"name": "x"}]}' * 100, "x" * 30_000]:
        assert dispatch_estimate(text) == budget_estimate(text)


async def test_small_get_table_schema_is_compacted_stable_no_marker() -> None:
    """A normal-sized getTableSchema is stored whole with `truncated=False` and no
    marker — the C5 contract — and, since the user spec of 2026-08-18 made the
    null/empty-key strip unconditional, with its information-free keys removed:
    `"comment": ""` costs the request budget and teaches the model nothing.
    Everything else is verbatim, key order included."""
    schema = {
        "database": "dbpcm_warehouse",
        "table": "employee",
        "columns": [
            {"name": "EmployeeCode", "type": "String", "comment": ""},
            {"name": "AnnualSalary", "type": "Decimal", "comment": "Yearly pay, USD."},
        ],
    }
    mcp_client = FakeMCPClient(scripted={"getTableSchema": [json.loads(json.dumps(schema))]})
    dispatcher = ToolDispatcher(mcp_client, CATALOG)
    result = await dispatcher.dispatch(
        "getTableSchema", {"database": "dbpcm_warehouse", "table": "employee"}, _credentials()
    )
    assert result.result_preview.truncated is False
    stored = result.result_preview.preview_rows[0][0]
    assert json.dumps(stored) == json.dumps(
        {
            "database": "dbpcm_warehouse",
            "table": "employee",
            "columns": [
                {"name": "EmployeeCode", "type": "String"},
                {"name": "AnnualSalary", "type": "Decimal", "comment": "Yearly pay, USD."},
            ],
        }
    )
    assert "_truncated" not in stored
    # `result_full` is never reshaped — the runtime's own consumers see the MCP's
    # response exactly as it arrived.
    assert result.result_full == schema


def test_over_cap_card_list_drops_whole_tail_cards_not_the_shape() -> None:
    """The cards-aware branch (release-1 §02 defect B): a `searchBlueprints`
    result carries no top-level `columns` key, so before this branch existed an
    over-cap card list fell through to STRINGIFY-and-truncate and the model got a
    mangled JSON string on the release's primary route. Now whole low-scoring
    cards are dropped from the tail and the shape is preserved."""
    from data_agent.runtime.dispatch.tool_dispatcher import _cap_nontabular_result

    result = {
        "count": 20,
        "degraded": False,
        "blueprints": [
            {"id": f"bp-{i:02d}", "intent": "x" * 300, "score": 1.0 - i / 100}
            for i in range(20)
        ],
    }
    events: list[tuple[str, dict]] = []
    capped, truncated = _cap_nontabular_result(
        result,
        500,
        observer=lambda e, p: events.append((e, p)),
        tool_name="searchBlueprints",
    )

    assert truncated is True
    assert isinstance(capped, dict)  # NOT a string
    kept = capped["blueprints"]
    assert 0 < len(kept) < 20
    assert kept == result["blueprints"][: len(kept)]  # head kept, whole, in order
    assert capped["degraded"] is False  # unrelated siblings ride along untouched
    # `count` is RECONCILED with what is actually present, and the pre-cap total
    # moves to `count_total`. Riding the pre-cap `count` along beside a shorter list
    # left the two signals disagreeing with no way for the model to tell which to
    # believe.
    assert capped["count"] == len(kept)
    assert capped["count_total"] == 20
    assert f"{20 - len(kept)} lowest-scoring of 20 blueprint cards" in capped["_truncated"]
    assert len(json.dumps(capped)) // 4 <= 500 * 2  # actually bounded
    assert events == [
        (
            "tool_dispatch_cards_dropped",
            {
                "tool_name": "searchBlueprints",
                "dropped_count": 20 - len(kept),
                "total_count": 20,
            },
        )
    ]


def test_card_list_under_cap_is_returned_unchanged() -> None:
    """No-regression: under the cap the SAME object comes back, no marker, no
    event — byte-identical to before the cards branch existed."""
    from data_agent.runtime.dispatch.tool_dispatcher import _cap_nontabular_result

    result = {"count": 1, "degraded": False, "blueprints": [{"id": "bp-1", "score": 0.9}]}
    events: list[tuple[str, dict]] = []
    capped, truncated = _cap_nontabular_result(
        result, 4_000, observer=lambda e, p: events.append((e, p)), tool_name="searchBlueprints"
    )
    assert truncated is False
    assert capped is result
    assert events == []


def test_over_cap_card_list_smaller_than_one_card_stays_a_dict() -> None:
    """The degenerate cap: not even the first card fits. The list comes back
    EMPTY rather than forcing an over-cap card back in (which would reintroduce
    the unbounded cell the cap exists to prevent) — but it is still a well-formed
    dict with a list under `blueprints`, and the marker says all N were dropped.

    THE TEXT MUST BE COHERENT AT ZERO. The kept-count phrasing read "the 0 best
    matches are shown in full", which describes a list the model can act on when
    there is none, so the zero case has its own sentence."""
    from data_agent.runtime.dispatch.tool_dispatcher import _cap_nontabular_result

    result = {"count": 3, "blueprints": [{"id": f"bp-{i}", "intent": "x" * 400} for i in range(3)]}
    capped, truncated = _cap_nontabular_result(result, 1, tool_name="searchBlueprints")
    assert truncated is True
    assert capped["blueprints"] == []
    assert "all 3 blueprint cards were omitted" in capped["_truncated"]
    assert "best matches are shown in full" not in capped["_truncated"]
    # ...and the ridden-along count agrees with the empty list it describes.
    assert capped["count"] == 0
    assert capped["count_total"] == 3


def test_over_cap_dict_with_neither_columns_nor_blueprints_still_stringifies() -> None:
    """The generic branch is unchanged for every other over-cap shape."""
    from data_agent.runtime.dispatch.tool_dispatcher import _cap_nontabular_result

    capped, truncated = _cap_nontabular_result({"blob": "x" * 10_000}, 500)
    assert truncated is True
    assert isinstance(capped, str)
    assert capped.endswith("chars omitted]")


async def test_observer_is_called_at_each_stage() -> None:
    events: list[tuple[str, dict]] = []

    def observer(event: str, payload: dict) -> None:
        events.append((event, payload))

    mcp_client = FakeMCPClient(scripted={"listDatabases": [[{"name": "dbpcm_warehouse"}]]})
    dispatcher = ToolDispatcher(mcp_client, CATALOG, observer=observer)
    await dispatcher.dispatch("listDatabases", {}, _credentials())

    event_names = [name for name, _ in events]
    assert event_names == ["tool_dispatch_start", "tool_dispatch_ok"]


async def test_default_observer_is_noop_by_default() -> None:
    """The default observer must not raise or require configuration (Pass-A seam)."""
    mcp_client = FakeMCPClient(scripted={"listDatabases": [[{"name": "dbpcm_warehouse"}]]})
    dispatcher = ToolDispatcher(mcp_client, CATALOG)
    result = await dispatcher.dispatch("listDatabases", {}, _credentials())
    assert result.status == "ok"


async def test_column_scope_denial_surfaces_the_named_column() -> None:
    """For COLUMN_SCOPE_VIOLATION SPECIFICALLY, the denied ToolResult.user_message
    carries the MCP's author-controlled, column-naming detail (exc.message) so the
    model sees WHICH column it lacks and can self-correct on the live turn.
    Column names are catalog metadata (not PII / cell values, D25)."""
    scope_message = (
        "This query needs access to columns outside your permitted scope: "
        "employee.EmployeeStatus. You do not have access to those columns — "
        "remove them from the query, or ask the user to grant access."
    )
    mcp_client = FakeMCPClient(
        scripted={"runQuery": [MCPToolError("COLUMN_SCOPE_VIOLATION", scope_message)]}
    )
    dispatcher = ToolDispatcher(mcp_client, CATALOG)

    result = await dispatcher.dispatch(
        "runQuery", {"sql": "SELECT EmployeeStatus FROM employee"}, _credentials()
    )

    assert result.status == "denied"
    assert result.error_code == "COLUMN_SCOPE_VIOLATION"
    assert result.retryable is False
    # The specific out-of-scope column NAME reaches the model.
    assert "employee.EmployeeStatus" in result.user_message
    assert result.user_message == scope_message
    # Still never leaks credentials.
    blob = _result_to_scannable_json(result)
    assert SECRET_JWT not in blob
    assert SESSION_ID not in blob


async def test_non_scope_denial_stays_generic_canned_message() -> None:
    """Regression guard for B4/D25: a NON-scope MCP error must NOT surface its raw
    exc.message — the model only ever sees the generic canned denial string. Here
    the raw message contains backend detail that must be suppressed."""
    raw_backend_detail = "Code: 47. DB::Exception: Unknown column secret_internal_col"
    mcp_client = FakeMCPClient(
        scripted={"runQuery": [MCPToolError("CLICKHOUSE_QUERY_ERROR", raw_backend_detail)]}
    )
    dispatcher = ToolDispatcher(mcp_client, CATALOG)

    result = await dispatcher.dispatch(
        "runQuery", {"sql": "SELECT bad FROM employee"}, _credentials()
    )

    assert result.status == "denied"
    assert result.error_code == "CLICKHOUSE_QUERY_ERROR"
    # Generic canned message only — the raw backend text must NOT leak.
    assert result.user_message == "That query didn't run correctly. Let me fix it and try again."
    assert "DB::Exception" not in result.user_message
    assert "secret_internal_col" not in result.user_message


async def test_raw_transport_exception_stays_generic_canned_message() -> None:
    """Regression guard for B4/D25: a RAW (non-MCPToolError) transport exception
    must never surface str(exc) — the model only sees the generic canned message."""
    raw_transport_detail = "ConnectionRefusedError: [Errno 61] to 10.0.0.5:8123"

    class _BoomClient(FakeMCPClient):
        async def call_tool(self, tool_name, args, *, jwt, session_id):
            raise RuntimeError(raw_transport_detail)

    dispatcher = ToolDispatcher(_BoomClient(), CATALOG)

    result = await dispatcher.dispatch("runQuery", {"sql": "SELECT 1"}, _credentials())

    assert result.status == "error"
    assert result.error_code == "INTERNAL_TRANSPORT_ERROR"
    assert result.user_message == (
        "Something went wrong reaching the data warehouse. Please try again."
    )
    assert "ConnectionRefusedError" not in result.user_message
    assert "10.0.0.5" not in result.user_message


# --- D75 Wave 1b: catalog PROVIDER seam -------------------------------------


async def test_catalog_provider_resolved_per_dispatch() -> None:
    """A ToolDispatcher given an async catalog PROVIDER (not a fixed handle)
    resolves it with THIS turn's credentials before capturing provenance, so a
    runQuery over the resolved catalog yields DETERMINED provenance."""
    calls: list[RuntimeCredentials] = []

    async def _provider(credentials: RuntimeCredentials) -> CatalogHandle:
        calls.append(credentials)
        return CATALOG

    mcp_client = FakeMCPClient(
        scripted={
            "runQuery": [
                {"columns": ["EmployeeCode"], "rows": [["E1"]], "row_count": 1, "truncated": False}
            ]
        }
    )
    dispatcher = ToolDispatcher(mcp_client, _provider)

    result = await dispatcher.dispatch(
        "runQuery", {"sql": f"SELECT EmployeeCode FROM {_E}"}, _credentials()
    )

    # The provider was awaited with the turn's credentials (D5: used only to
    # resolve the handle, never surfaced on the result).
    assert len(calls) == 1
    assert calls[0].jwt == SECRET_JWT
    # Provenance was captured against the resolved catalog (determined, non-None).
    assert result.status == "ok"
    assert result.provenance == frozenset({(_E, "EmployeeCode")})
    blob = _result_to_scannable_json(result)
    assert SECRET_JWT not in blob
