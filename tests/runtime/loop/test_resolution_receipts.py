"""Value resolution reaches final review without copying candidate rows."""

import json
from dataclasses import replace

import pytest

from data_agent.runtime.dispatch.tool_dispatcher import _build_preview
from data_agent.runtime.loop.answer_judge import AnswerJudge
from data_agent.runtime.loop.resolution_receipts import resolution_receipts
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import load_catalog_handle_from_catalog
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import TrailEntry

TABLE = "hr.accrual_events"
PROVENANCE = frozenset({(TABLE, "EarnCode")})
VALUES = {
    "degraded": False,
    "ranking": "semantic+freq",
    "values": [
        {"value": "PTO", "description": "PRIVATE CANDIDATE DESCRIPTION", "freq": 999},
        {"value": "VAC", "description": "PRIVATE VACATION DESCRIPTION", "freq": 5},
    ],
}
SQL = "SELECT sum(Hours) FROM hr.accrual_events e WHERE e.EarnCode IN ('PTO', 'VAC')"


def entry(ref, tool, args, **kwargs):
    return TrailEntry(
        turn_index=0,
        tool_call_id=ref,
        tool_name=tool,
        args=args,
        status="ok",
        error_code=None,
        provenance=PROVENANCE,
        result_preview=None,
        result_full_ref=None,
        ts="now",
        **kwargs,
    )


async def packet(
    sql=SQL, values=VALUES, *, scope=frozenset(), receipt_change=None, query_first=False
):
    store = InMemorySessionStore()
    full_ref = await store.write_full_result("s", "resolve", values)
    receipt = replace(
        entry("resolve", "resolveValues", {"table": TABLE, "column": "EarnCode", "concept": "PTO"}),
        result_full_ref=full_ref,
    )
    if receipt_change:
        receipt = replace(receipt, **receipt_change)
    query = entry("query", "runQuery", {"sql": sql})
    trail = [query, receipt] if query_first else [receipt, query]
    return await resolution_receipts(
        trail=trail,
        results=[{"tool_call_id": "query", "execution": {"sql": sql}}],
        turn_index=0,
        session_id="s",
        store=store,
        column_scope=scope,
    )


async def test_resolved_pto_list_is_confirmed_without_candidate_rows():
    receipts = await packet()
    assert receipts[0]["concept"] == "PTO"
    assert receipts[0]["matched_count"] == 2
    assert receipts[0]["checks"][0]["in_list_subset_of_resolved_values"] is True
    encoded = json.dumps(receipts)
    assert "PRIVATE" not in encoded and "999" not in encoded and '"VAC"' not in encoded
    assert "values" not in receipts[0] and "rows" not in receipts[0]


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT sum(Hours) FROM hr.accrual_events WHERE EarnCode IN ('VAC')", True),
        ("SELECT sum(Hours) FROM hr.accrual_events WHERE EarnCode IN ('BONUS')", False),
        ("SELECT sum(Hours) FROM hr.other WHERE EarnCode IN ('PTO')", None),
        (
            "SELECT * FROM hr.accrual_events a JOIN hr.other b ON a.Id=b.Id WHERE EarnCode IN ('PTO')",
            None,
        ),
        (
            "SELECT * FROM hr.accrual_events a JOIN hr.other b ON a.Id=b.Id WHERE a.EarnCode IN ('PTO')",
            True,
        ),
        ("SELECT * FROM hr.accrual_events WHERE EarnCode IN (SELECT code FROM hr.other)", None),
        ("WITH x AS (SELECT 'PTO' AS EarnCode) SELECT * FROM x WHERE EarnCode IN ('PTO')", None),
        ("SELECT * FROM hr.accrual_events WHERE EarnCode IN (123)", None),
        ("SELECT (", None),
    ],
)
async def test_membership_is_bound_to_the_actual_table_and_literal_list(sql, expected):
    receipts = await packet(sql)
    assert receipts[0]["checks"][0]["in_list_subset_of_resolved_values"] is expected


@pytest.mark.parametrize(
    "change,scope",
    [
        ({"status": "error"}, frozenset()),
        ({"turn_index": 1}, frozenset()),
        ({"provenance": None}, frozenset()),
        ({}, frozenset({"hr.other.EarnCode"})),
    ],
)
async def test_failed_old_or_unauthorized_receipts_are_not_exposed(change, scope):
    assert await packet(receipt_change=change, scope=scope) == []


async def test_missing_receipt_is_unknown_and_later_resolution_cannot_backfill_execution():
    receipts = await packet(receipt_change={"result_full_ref": "missing"})
    assert receipts[0]["matched_count"] is None
    assert receipts[0]["checks"][0]["in_list_subset_of_resolved_values"] is None
    assert not receipts[0]["receipt_available"]
    assert (await packet(query_first=True))[0]["checks"] == []


async def test_empty_and_degraded_results_remain_distinguishable_from_missing():
    empty = (await packet(values={"values": [], "degraded": False}))[0]
    assert empty["matched_count"] == 0
    assert empty["checks"][0]["in_list_subset_of_resolved_values"] is False
    degraded = (await packet(values={**VALUES, "degraded": True, "ranking": "freq_only"}))[0]
    assert degraded["degraded"] is True and degraded["ranking"] == "freq_only"


async def test_only_complete_previews_can_substitute_for_absent_full_storage():
    preview = _build_preview(VALUES, 20)
    receipts = await packet(receipt_change={"result_full_ref": None, "result_preview": preview})
    assert receipts[0]["checks"][0]["in_list_subset_of_resolved_values"] is True
    receipts = await packet(
        receipt_change={"result_full_ref": None, "result_preview": replace(preview, truncated=True)}
    )
    assert receipts[0]["checks"][0]["in_list_subset_of_resolved_values"] is None


async def test_finalization_passes_resolution_receipt_to_real_judge_prompt(monkeypatch):
    from tests.runtime import test_harness_improvements as h

    catalog = load_catalog_handle_from_catalog(
        {
            TABLE: {
                "columns": {"EarnCode": {"type": "String"}, "Hours": {"type": "Float64"}},
                "rules": [
                    {
                        "id": "pto",
                        "predicate": "EarnCode IN (<resolved values>)",
                        "description": "Use resolveValues for PTO earn codes.",
                    }
                ],
            }
        }
    )
    monkeypatch.setattr(h, "CATALOG", catalog)

    class Resolve(h.Tool):
        async def run(self, *args, **kwargs):
            return replace(await super().run(*args, **kwargs), provenance=PROVENANCE)

    judge = h.Judge()
    loop, *_ = h.build(
        [
            h.discovery(),
            h.batch(
                h.call("resolveValues", "resolve", table=TABLE, column="EarnCode", concept="PTO")
            ),
            h.batch(h.call("runQuery", "q", sql=SQL)),
            h.batch(h.finish("There are 12 PTO hours, using the resolved PTO earn codes.")),
        ],
        judge,
        extra={"resolveValues": Resolve("resolveValues", VALUES)},
        rows=[{"columns": ["PTO Hours"], "rows": [[12]], "row_count": 1, "truncated": False}],
    )
    loop._judge_catalog = catalog
    outcome = await h.run(loop, "How many PTO hours?")
    assert outcome.review["status"] == "approved"
    brief = judge.briefs[0]
    assert all(r["tool_name"] != "resolveValues" for r in brief.results)
    receipt = brief.evidence_package["resolution_receipts"][0]
    assert receipt["checks"][0] == {
        "result_id": "q",
        "statement_index": 0,
        "predicate_index": 0,
        "in_list_subset_of_resolved_values": True,
    }
    messages = AnswerJudge(ScriptedModelClient([]), 32000).messages_for(brief)
    assert "PRIVATE CANDIDATE" not in messages[1]["content"]
    assert "missing/unavailable receipt" in messages[0]["content"]
    assert "UNKNOWN, never by itself" in messages[0]["content"]
