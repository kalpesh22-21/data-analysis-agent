from dataclasses import replace

import pytest

from data_agent.runtime.context.scope_filter import compute_scope_hash
from data_agent.runtime.composite.analysis_state import classify_block_evidence, validate_block_evidence
from data_agent.runtime.dispatch.tool_dispatcher import _build_preview
from data_agent.runtime.provenance.field_availability import field_availability
from data_agent.runtime.session.models import TrailEntry
from tests.runtime.test_harness_improvements import CREDS


def receipt():
    return TrailEntry(
        turn_index=0, tool_call_id="schema", tool_name="getTableSchema",
        args={"database": "hr", "table": "employee", "columns": ["salary", "ssn"]},
        status="ok", error_code=None, provenance=frozenset(),
        result_preview=_build_preview({
            "database": "hr", "table": "employee",
            "columns": [{"name": "salary", "type": "Decimal"}],
            "columns_not_found": ["ssn"],
            "column_check": {"coverage": "complete_authorized_table_schema", "matching": "exact_names"},
        }, 20, tool_name="getTableSchema"),
        result_full_ref=None, ts="now",
        model_response={"scope_hash": compute_scope_hash(CREDS.column_scope)},
    )


def test_targeted_receipt_closes_unavailable_part_without_denial_or_failed_query():
    entry = receipt()
    assert classify_block_evidence("schema", [entry], 0) == "REQUIRED_DATA_UNAVAILABLE"
    assert validate_block_evidence("schema", "NO_ACCESS", [entry], 0) is not None
    assert field_availability(entry)["unavailable_fields"] == ["ssn"]
    assert field_availability(entry)["table"] == "hr.employee"
    assert classify_block_evidence("schema", [entry], 1) is None


@pytest.mark.parametrize("change", ["absent_marker", "incomplete", "unrequested", "failed", "other_tool"])
def test_incomplete_or_untrusted_absence_does_not_close_request(change):
    entry = receipt()
    payload = entry.result_preview.preview_rows[0][0]
    if change == "absent_marker":
        del payload["column_check"]
    elif change == "incomplete":
        payload["column_check"]["coverage"] = "partial"
    elif change == "unrequested":
        payload["columns_not_found"] = ["other"]
    elif change == "failed":
        entry = replace(entry, status="error")
    else:
        entry = replace(entry, tool_name="runQuery")
    assert field_availability(entry) is None
    assert classify_block_evidence("schema", [entry], 0) is None


def test_column_documentation_trimming_does_not_erase_authoritative_receipt():
    entry = receipt()
    entry = replace(entry, result_preview=replace(entry.result_preview, truncated=True))
    assert field_availability(entry)["unavailable_fields"] == ["ssn"]


async def test_both_judges_receive_only_current_scope_field_limitations():
    from data_agent.runtime.loop.answer_judge import JudgeBrief, _system_prompt
    from data_agent.runtime.loop.judge_evidence import enrich_brief
    from data_agent.runtime.prompts import FIELD_AVAILABILITY_GUIDANCE
    from data_agent.runtime.session.memory_store import InMemorySessionStore

    entry = receipt()
    store = InMemorySessionStore()
    for item in (entry, replace(entry, turn_index=-1, tool_call_id="old"),
                 replace(entry, tool_call_id="other", model_response={"scope_hash": "other"})):
        await store.append_trail_entry(CREDS.session_id, item)
    for site in ("ask_user", "exit_prose"):
        brief = await enrich_brief(
            JudgeBrief(site, "Salary and SSN"), trail=[], turn_index=0,
            session_id=CREDS.session_id, store=store, catalog_provider=None,
            credentials=CREDS, analysis_state=None,
        )
        assert brief.evidence_package["field_limitations"] == [
            {"result_id": "schema", **field_availability(entry)}]
        assert FIELD_AVAILABILITY_GUIDANCE in _system_prompt(site)
