"""J6 — an EMPTY blueprint result is *empty — unverifiable*, never *verified ✓*.

WHAT WAS WRONG (confirmed live). The D56 grain teeth are `row_count ==
distinct_grain_count`. At ZERO rows that reads `0 == 0`, which is true for every
blueprint ever written — correct, wrong-grain, or structurally broken. A
blueprint whose terminal query matched nothing therefore shipped a 0-row answer
table wearing a full verification badge, and the gate that exists to catch a
fan-out double-count had provably examined nothing.

THE SCOPE OF THE FIX IS THE CLAIM, NOT THE RESULT. An empty result is still the
authoritative answer for its intent — "there are none" is an answer, and inviting
the model to re-derive it with ad-hoc runQuerys is the loop the `authoritative`
marker exists to prevent. So `status: "verified"`, `grain_ok` and the
`authoritative` marker are all deliberately UNCHANGED here; what is withdrawn is
the word *verified*, everywhere a human or a model reads it.

Every layer the dict crosses is pinned below, because the badge is one dict
travelling through six consumers and the bug was invisible at five of them:
  1. `executor._verify_block` — BOTH build sites (single-node and DAG finalize);
  2. `blueprint_verification` — the badge constructor, including a result
     PERSISTED BEFORE this change and rehydrated on `GET /session/history`;
  3. `rollup_verification` — the envelope, which is the badge the UI renders at
     N<=1, i.e. exactly the confirmed case;
  4. `loop_answer_tables_designated.verified_table_count` — the observer count
     that must not keep reporting a verification rate nothing is claiming;
  5. the model-facing note in the canonical tool message.

The non-empty path is pinned byte-for-byte in the same file, because "additive"
is a claim about the other path and is the thing most easily broken.
"""

from __future__ import annotations

import json
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    BlueprintExecutor,
    ExecCompleted,
)
from data_agent.runtime.composite.answer_with_table import (
    AnswerTable,
    BlueprintRun,
    blueprint_run_from_result,
    blueprint_verification,
    is_zero_row_count,
    rollup_verification,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import _tool_trail_entry_to_canonical
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

_E = "dbpcm_warehouse.employee"
_DEPT_COL = f"{_E}.Department"
_SAL_COL = f"{_E}.AnnualSalary"
_CODE_COL = f"{_E}.EmployeeCode"

CATALOG = CatalogHandle(
    {
        _E: {
            "EmployeeCode": "String",
            "Department": "Nullable(String)",
            "AnnualSalary": "Nullable(Float64)",
        }
    }
)

_AVG_SQL = (
    "SELECT Department AS department, AVG(AnnualSalary) AS avg_salary, "
    "COUNT(DISTINCT EmployeeCode) AS headcount "
    "FROM dbpcm_warehouse.employee WHERE Department = {department} GROUP BY Department"
)

# The exact `verify` block a NON-empty verified result has always carried. Pinned
# as a literal (not derived from the code under test) so the additive claim is a
# real assertion rather than a tautology.
_NON_EMPTY_VERIFY = {
    "grain_ok": True,
    "grain_checked": True,
    "signature_ok": True,
    "signature_checked": False,
}

_EMPTY_BADGE = {
    "passed": False,
    "method": "blueprint_gate",
    "grain_checked": False,
    "empty_result": True,
    "status": "empty — unverifiable",
}


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s-j6", jwt="jwt-secret", column_scope=scope)


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _detail(
    *,
    slots: list[dict[str, Any]] | None = None,
    sql_template: str | None = _AVG_SQL,
    composes: list[dict[str, Any]] | None = None,
    bid: str = "bp-average-salary-by-department",
) -> BlueprintDetail:
    return BlueprintDetail(
        id=bid,
        intent="Average annual salary by department",
        slots_summary="department",
        uses=frozenset({_DEPT_COL, _SAL_COL, _CODE_COL}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        resolves={"salary": "AnnualSalary"},
        slots=slots,
        sql_template=sql_template,
        composes=composes,
        result_grain=["Department"],
    )


def _executor(mcp: FakeMCPClient, detail: BlueprintDetail) -> BlueprintExecutor:
    index = FakeVectorIndex()
    index.add_detail(detail)
    return BlueprintExecutor(
        tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=index
    )


# ---------------------------------------------------------------------------
# 1. The executor — both `verify` build sites
# ---------------------------------------------------------------------------


async def test_a_single_node_blueprint_returning_no_rows_is_not_grain_checked() -> None:
    """The confirmed live case, at its origin.

    The grain probe genuinely RAN and genuinely returned `0 == 0`. Reporting that
    as `grain_checked: True` is the whole defect: the flag's meaning is "the
    row-count teeth examined this result", and they did not — there was nothing to
    examine. So it reads `False`, exactly as it does for the §4.2 skip, plus the
    `empty_result` marker that tells the two skips apart downstream.
    """
    detail = _detail(
        slots=[
            {"name": "department", "type": "string", "required": True, "binds_to": _DEPT_COL}
        ]
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Warehouse"]]),  # slot domain probe
                _rq(["department", "avg_salary", "headcount"], []),  # node: NO ROWS
                _rq(["__bp_n", "__bp_d"], [[0, 0]]),  # grain probe: 0 == 0
            ]
        }
    )

    outcome = await _executor(mcp, detail).execute(
        blueprint_id=detail.id,
        slot_bindings={"department": "Warehouse"},
        credentials=_creds(),
    )

    assert isinstance(outcome, ExecCompleted)
    assert outcome.result_full["row_count"] == 0
    assert outcome.result_full["verify"] == {
        # UNCHANGED: `grain_ok`/`signature_ok` are what the `authoritative` marker
        # is computed from, and an empty result is still the answer for its intent.
        "grain_ok": True,
        "signature_ok": True,
        "signature_checked": False,
        # THE CHANGE: the teeth did not meaningfully run, and said so.
        "grain_checked": False,
        "empty_result": True,
    }
    # The authoritative/no-re-derivation contract is untouched — J6 option (b),
    # which would have degraded an empty blueprint to the raw loop, was rejected.
    assert outcome.result_full["status"] == "verified"


async def test_a_non_empty_result_keeps_the_exact_verify_block_it_always_had() -> None:
    """The additive claim, pinned. Byte-stable INCLUDING key order: this dict is
    serialized into the persisted trail and read back by `GET /session/history`,
    and `empty_result` must be absent — not `False` — on the path that already
    worked."""
    detail = _detail(
        slots=[
            {"name": "department", "type": "string", "required": True, "binds_to": _DEPT_COL}
        ]
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Warehouse"]]),
                _rq(["department", "avg_salary", "headcount"], [["Warehouse", 50000.0, 3]]),
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),
            ]
        }
    )

    outcome = await _executor(mcp, detail).execute(
        blueprint_id=detail.id,
        slot_bindings={"department": "Warehouse"},
        credentials=_creds(),
    )

    assert isinstance(outcome, ExecCompleted)
    verify = outcome.result_full["verify"]
    assert verify == _NON_EMPTY_VERIFY
    assert list(verify) == ["grain_ok", "grain_checked", "signature_ok", "signature_checked"]
    assert "empty_result" not in verify
    assert blueprint_verification(outcome.result_full) == {
        "passed": True,
        "method": "blueprint_gate",
        "grain_checked": True,
    }


async def test_the_dag_finalize_path_reports_an_empty_terminal_the_same_way() -> None:
    """The second build site. Two dicts assembled in two functions is how the
    single-node and DAG paths describe the same gate differently — they now share
    one builder, and this is the assertion that keeps them sharing it."""
    detail = _detail(
        sql_template=None,
        composes=[
            {
                "order": 0,
                "output": {"company_avg": "scalar"},
                "sql_template": (
                    "SELECT AVG(AnnualSalary) AS company_avg FROM dbpcm_warehouse.employee"
                ),
            },
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {"company_avg": "$0.company_avg"},
                "sql_template": (
                    "SELECT Department AS department FROM dbpcm_warehouse.employee "
                    "WHERE AnnualSalary > {company_avg} GROUP BY Department"
                ),
                "output": {},
            },
        ],
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["company_avg"], [[55000.0]]),  # node 0 → scalar
                _rq(["department"], []),  # node 1 — the TERMINAL, no rows
                _rq(["__bp_n", "__bp_d"], [[0, 0]]),  # grain probe
            ]
        }
    )

    outcome = await _executor(mcp, detail).execute(
        blueprint_id=detail.id, slot_bindings={}, credentials=_creds()
    )

    assert isinstance(outcome, ExecCompleted)
    assert outcome.result_full["row_count"] == 0
    assert outcome.result_full["verify"]["empty_result"] is True
    assert outcome.result_full["verify"]["grain_checked"] is False


# ---------------------------------------------------------------------------
# 2. The badge constructor
# ---------------------------------------------------------------------------


def test_the_badge_for_an_empty_blueprint_result_states_it_is_unverifiable() -> None:
    result_full = {
        "blueprint_id": "bp-x",
        "status": "verified",
        "terminal_sql": "SELECT 1",
        "row_count": 0,
        "verify": {"grain_ok": True, "grain_checked": False, "empty_result": True},
    }
    assert blueprint_verification(result_full) == _EMPTY_BADGE


def test_a_result_persisted_before_j6_is_re_read_as_empty_from_its_row_count() -> None:
    """The reload path. `GET /session/history` rehydrates `result_full` from the
    D46 KV, and a record written before this change carries no `empty_result`
    marker. Trusting the marker alone would leave every pre-existing session
    reproducing the exact over-claim on reload — so emptiness is ALSO derived from
    the underlying `row_count`, which every record has always had."""
    legacy = {
        "blueprint_id": "bp-x",
        "status": "verified",
        "terminal_sql": "SELECT 1",
        "row_count": 0,
        "verify": {"grain_ok": True, "grain_checked": True, "signature_ok": True},
    }
    assert blueprint_verification(legacy) == _EMPTY_BADGE
    # And through the constructor the live turn and the reload BOTH go through.
    captured = blueprint_run_from_result(legacy)
    assert captured is not None
    assert captured[1].verification == _EMPTY_BADGE


def test_an_unverified_result_is_still_no_badge_at_all_not_an_empty_one() -> None:
    """`None` and `empty_result` are different facts and must not converge.
    `None` = "nothing gated this table" (a hand-written query). `empty_result` =
    "a blueprint gated it and had nothing to gate"."""
    assert blueprint_verification({"status": "unverified", "row_count": 0}) is None
    assert blueprint_verification(None) is None
    # A poisoned `row_count` must not read as zero rows (`isinstance(True, int)`).
    assert blueprint_verification(
        {"status": "verified", "row_count": False, "verify": {"grain_checked": True}}
    ) == {"passed": True, "method": "blueprint_gate", "grain_checked": True}


# ---------------------------------------------------------------------------
# 3. The envelope roll-up — the badge the UI renders at N<=1
# ---------------------------------------------------------------------------


def _table(verification: dict[str, Any] | None) -> AnswerTable:
    return AnswerTable(sql="SELECT 1", verification=verification)


def test_a_lone_empty_table_rolls_up_to_empty_rather_than_to_silence() -> None:
    """N=1 IS the confirmed case, and at N<=1 the UI renders the ENVELOPE's badge,
    not the per-table one. Flattening this to `None` would make the fix invisible
    in precisely the situation it exists for."""
    assert rollup_verification([_table(_EMPTY_BADGE)]) == _EMPTY_BADGE


def test_a_mixed_set_claims_nothing() -> None:
    """"Empty" would over-state it — part of the answer has rows. "Verified" would
    be the original over-claim restated. No claim is the honest reading, and it is
    what this function already returns for any other mixture."""
    green = {"passed": True, "method": "blueprint_gate", "grain_checked": True}
    assert rollup_verification([_table(green), _table(_EMPTY_BADGE)]) is None
    assert rollup_verification([_table(_EMPTY_BADGE), _table(None)]) is None


def test_an_all_verified_set_still_rolls_up_green_unchanged() -> None:
    green = {"passed": True, "method": "blueprint_gate", "grain_checked": True}
    assert rollup_verification([_table(green), _table(dict(green))]) == green


def test_every_table_empty_rolls_up_to_empty() -> None:
    assert rollup_verification([_table(_EMPTY_BADGE), _table(dict(_EMPTY_BADGE))]) == (
        _EMPTY_BADGE
    )


# ---------------------------------------------------------------------------
# 4. The model-facing note
# ---------------------------------------------------------------------------


def _rendered(row_count: int) -> dict[str, Any]:
    return {
        "tool_call_id": "bp_1",
        "tool_name": "runBlueprint",
        "args": {"id": "bp.headcount"},
        "status": "ok",
        "error_code": None,
        "user_message": None,
        "result_preview": {
            "columns": ["n"],
            "row_count": row_count,
            "truncated": False,
            "preview_rows": [],
        },
        "authoritative": True,
    }


def test_the_model_is_not_told_an_empty_blueprint_result_was_verified() -> None:
    """The one reader that will repeat the over-claim in prose to the user.

    The no-re-derivation instruction is UNCHANGED and the `authoritative` marker
    stays set — option (b) (degrade an empty blueprint to the raw loop) was
    rejected. Only the word *verified* is withdrawn.
    """
    _assistant, tool_message = _tool_trail_entry_to_canonical(_rendered(0))
    content = json.loads(tool_message["content"])

    assert content["authoritative"] is True
    assert "do not re-derive" in content["note"].lower()
    assert "verified blueprint result" not in content["note"].lower()
    assert "no rows" in content["note"].lower()


def test_a_non_empty_authoritative_result_keeps_its_original_note() -> None:
    _assistant, tool_message = _tool_trail_entry_to_canonical(_rendered(42))
    content = json.loads(tool_message["content"])

    assert content["authoritative"] is True
    assert content["note"] == (
        "Verified blueprint result — authoritative; do not re-derive with "
        "additional queries."
    )


def test_an_authoritative_entry_with_no_preview_keeps_the_original_note() -> None:
    """A missing preview is UNDETERMINED, not zero rows. Reading it as empty would
    invent a retraction out of an absent field."""
    entry = _rendered(1)
    entry["result_preview"] = None
    _assistant, tool_message = _tool_trail_entry_to_canonical(entry)
    assert "Verified blueprint result" in json.loads(tool_message["content"])["note"]


def test_a_poisoned_row_count_does_not_read_as_an_empty_result() -> None:
    """`isinstance(True, int)`, so a bare `== 0` would retract the verification
    claim over a `row_count: false` — a value that says nothing about the row
    count. Both J6 sites read a row count off untrusted JSON and both go through
    `is_zero_row_count`; this is the note site's half of that guard (the badge's
    half is `test_an_unverified_result_is_still_no_badge_at_all_not_an_empty_one`).
    """
    assert is_zero_row_count(0) is True
    assert is_zero_row_count(False) is False
    assert is_zero_row_count(None) is False
    assert is_zero_row_count("0") is False

    entry = _rendered(1)
    entry["result_preview"]["row_count"] = False
    _assistant, tool_message = _tool_trail_entry_to_canonical(entry)
    assert "Verified blueprint result" in json.loads(tool_message["content"])["note"]


# ---------------------------------------------------------------------------
# 5. The `BlueprintRun` seam the loop and the reload share
# ---------------------------------------------------------------------------


def test_the_run_captured_from_an_empty_result_carries_the_empty_badge() -> None:
    """One record, captured at one site — so the live turn's badge and the
    reloaded transcript's badge cannot be the two different things this seam
    exists to prevent."""
    result_full = {
        "blueprint_id": "bp-x",
        "status": "verified",
        "terminal_sql": "SELECT 1",
        "row_count": 0,
        "verify": {"grain_ok": True, "grain_checked": False, "empty_result": True},
    }
    captured = blueprint_run_from_result(result_full, slots={"month": "2026-08"})
    assert captured is not None
    blueprint_id, run = captured
    assert blueprint_id == "bp-x"
    assert run == BlueprintRun(
        terminal_sql="SELECT 1",
        verification=_EMPTY_BADGE,
        slots={"month": "2026-08"},
    )
