"""A18 over real MCP HTTP + agent run/resume/review, with fixture data/models."""

import json

import pytest

from tests.integration.test_scope_recovery_cross_repo import API, rig  # noqa: F401

pytestmark = pytest.mark.skipif(not API, reason="Set CLICKHOUSE_API_REPO for cross-repo HTTP test")


@pytest.mark.parametrize("mode", ["normal", "resume", "correction", "clear"])
async def test_business_caveats_survive_delivery_and_history(rig, mode):  # noqa: F811
    from data_agent.runtime.app import _stream_turn
    from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
    from data_agent.runtime.loop.answer_judge import JudgeVerdict
    from data_agent.runtime.observability.progress import ProgressEmitter
    from data_agent.runtime.session_history import project_history
    from tests.runtime.test_answer_document import final, section
    from tests.runtime.test_harness_improvements import (
        CATALOG,
        Judge,
        batch,
        build,
        call,
        discovery,
        query,
    )

    state, client, manager, creds, store, _ = rig
    creds = await manager.credentials(creds)
    first = "Headcount counts distinct employees with status Active or On Leave"
    paraphrase = "Headcount is current employees (Active plus On Leave)."
    ui = "Only the top 20 of 211 departments are shown in the chart; the full result is available in the table/CSV"
    data = "Only 2026 records are included."
    selected = section()
    selected["caveats"] = [first, ui, data]
    steps = [
        discovery(),
        query(),
        batch(
            call(
                "recordAssumptions",
                "a",
                assumptions=[paraphrase, "Use the table's paging/CSV to view all rows."],
            )
        ),
    ]
    judge = Judge()
    if mode == "correction":
        replacement = {
            "takeaway": "Employees by department.",
            "sections": [selected],
            "evidence": ["q"],
            "assumptions": [paraphrase],
        }
        judge = Judge(
            [
                JudgeVerdict(
                    True, reviewed=True, repair_type="presentation", corrected_document=replacement
                )
            ]
        )
        steps.append(final(section()))
    else:
        if mode == "clear":
            selected["caveats"] = [ui, data]
            steps.append(batch(call("recordAssumptions", "clear", assumptions=[])))
        steps.append(final(selected))
    if mode == "resume":
        steps.insert(
            0,
            batch(
                call(
                    "askUser", "ask", question="Which department?", options=["Sales", "Engineering"]
                )
            ),
        )
    loop, *_ = build(steps, judge, store=store)
    loop._tool_dispatcher = ToolDispatcher(client, CATALOG)
    if mode == "resume":
        paused = await loop.run(
            session_id=creds.session_id,
            credentials=creds,
            user_message="How many employees in my chosen department?",
        )
        assert paused.status == "paused_ask_user"

    async def turn():
        if mode == "resume":
            return await loop.resume(session_id=creds.session_id, credentials=creds, answer="Sales")
        return await loop.run(
            session_id=creds.session_id,
            credentials=creds,
            user_message="How many employees by department?",
        )

    frames = [frame async for frame in _stream_turn(turn, ProgressEmitter())]
    result = next(
        json.loads(frame.split("data: ", 1)[1])
        for frame in frames
        if frame.startswith("event: result")
    )
    document = result["answer_document"]
    assert [c for s in document["sections"] for c in s["caveats"]] == (
        [data] if mode == "clear" else [first, data]
    )
    assert ui not in json.dumps(document)
    assert "paging/CSV" not in json.dumps(document)
    saved = await store.get_or_create_session(creds.session_id)
    assert saved.messages[-1].answer_document == document
    history = project_history(saved.messages, saved.tool_trail, creds.column_scope, None)
    assert history["turns"][-1]["answer_document"] == document
    assert state["executions"] == 1  # No SQL rerun for assumption/presentation repair.
