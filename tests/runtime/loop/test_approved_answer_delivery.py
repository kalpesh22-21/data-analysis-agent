"""Delivery checks the approved payload and receipts, never asks a second judge."""

from dataclasses import replace

import pytest

from data_agent.runtime.loop.delivery import CURRENT_DELIVERY
from data_agent.runtime.loop.answer_judge import JudgeVerdict
from tests.runtime.test_harness_improvements import CREDS, Judge, build, discovery, query, run
from tests.runtime.loop.test_judge_prose_correction import final


@pytest.mark.parametrize(
    "change", ["none", "text", "caption", "preview", "failed", "removed", "scope", "unrelated"]
)
async def test_approval_binds_payload_scope_and_successful_receipts(change):
    class CountingJudge(Judge):
        salvage_calls = 0

        async def salvage(self, brief):
            self.salvage_calls += 1
            raise ValueError("No approved recovery")

    judge = CountingJudge([JudgeVerdict(True, reviewed=True)])
    loop, store, _, _, events = build(
        [discovery(), query(), final("There are 120 employees.")], judge
    )
    finish = loop._finish

    async def mutate(**kwargs):
        doc = await store.get_or_create_session(CREDS.session_id)
        entry = next(e for e in doc.tool_trail if e.tool_call_id == "q")
        if change == "text":
            kwargs["assistant_text"] += " A new assertion."
        elif change == "caption":
            accum = kwargs["accum"]
            accum._answer_tables = [
                replace(t, caption="Changed population") for t in accum.answer_tables
            ]
        elif change == "preview":
            changed = replace(
                entry, result_preview=replace(entry.result_preview, preview_rows=[["Sales", 999]])
            )
            doc.tool_trail[doc.tool_trail.index(entry)] = changed
        elif change == "failed":
            doc.tool_trail[doc.tool_trail.index(entry)] = replace(entry, status="error")
        elif change == "removed":
            doc.tool_trail.remove(entry)
        elif change == "scope":
            context = CURRENT_DELIVERY.get()
            context.credentials = replace(CREDS, column_scope=frozenset({"hr.employee.Id"}))
        elif change == "unrelated":
            doc.tool_trail.append(replace(entry, tool_call_id="unrelated"))
        return await finish(**kwargs)

    loop._finish = mutate
    result = await run(loop)
    approved = change in {"none", "unrelated"}
    assert (result.review["status"] == "approved") == approved
    assert bool(result.answer_tables) == approved
    assert len(judge.briefs) == 1
    assert judge.salvage_calls == int(not approved)
    # Contract mismatch is not misreported as a new semantic judge rejection.
    assert not any(name == "loop_answer_judge_refused" for name, _ in events)
    if approved:
        assert result.assistant_text == "There are 120 employees."
    else:
        assert "120" not in result.assistant_text
