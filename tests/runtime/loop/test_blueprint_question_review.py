"""A runtime-authored slot question takes the same repair gate as askUser."""

import json

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.dispatch.tool_dispatcher import ToolPause
from data_agent.runtime.loop.answer_judge import JudgeVerdict
from data_agent.runtime.observability.tracing import guardrail_observer
from tests.runtime import test_harness_improvements as h


async def setup(spent=False):
    pause = ToolPause(
        reason="blueprint_slot",
        pending_question={"question": "Which employee?"},
        blueprint_id="bp",
        slot_bindings_json=json.dumps({"employee": "A4I8"}),
    )
    judge = h.Judge(
        [
            JudgeVerdict(
                False,
                "non_contextual_question",
                "The employee is already established; use the successful query.",
                reviewed=True,
            )
        ]
    )
    steps = [
        h.discovery(),
        h.query(),
        h.batch(h.call("getBlueprint", "bpdef", id="bp")),
        h.batch(h.call("runBlueprint", "bp-run", id="bp", slot_bindings={"employee": "A4I8"})),
    ]
    if not spent:
        steps.append(h.batch(h.finish()))
    loop, store, model, _, events = h.build(
        steps,
        judge,
        extra={
            "getBlueprint": h.Tool("getBlueprint", {"found": True}),
            "runBlueprint": h.Tool("runBlueprint", pause=pause),
        },
    )
    if spent:
        await store.claim_finalization_block(h.CREDS.session_id, 0, 1, "ask_user_judge")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    observer = guardrail_observer(provider.get_tracer("test"))
    loop._observer = lambda name, data: (events.append((name, data)), observer(name, data))
    outcome = await h.run(loop)
    return outcome, store, model, judge, events, exporter


async def test_blueprint_question_rejection_rerounds_with_evidence_and_exports_refusal():
    outcome, store, model, judge, events, exporter = await setup()
    assert outcome.status == "done"
    assert outcome.assistant_text == "There are 120 employees."
    assert model.calls_made == 5  # the last round only occurs after question rejection
    assert judge.briefs[0].site == "ask_user"
    assert any(r["tool_call_id"] == "q" for r in judge.briefs[0].results)
    assert "That question was NOT sent." in str(model.calls[-1])
    assert any(name == "loop_ask_user_judge_refused" for name, _ in events)
    names = [span.name for span in exporter.get_finished_spans()]
    assert "loop_ask_user_judge_refused" in names
    assert "loop_finalization_block_spent" in names
    doc = await store.get_or_create_session(h.CREDS.session_id)
    assert doc.pause_checkpoint is None
    assert len([e for e in doc.tool_trail if e.tool_call_id == "bp-run"]) == 1


async def test_spent_blueprint_question_allowance_keeps_a_resumable_checkpoint():
    outcome, store, model, judge, events, exporter = await setup(spent=True)
    assert outcome.status == "paused_ask_user"
    assert outcome.review["status"] == "rejected"
    assert (
        outcome.assistant_text
        != "I don't have enough verified information to answer your question."
    )
    doc = await store.get_or_create_session(h.CREDS.session_id)
    assert not doc.pause_checkpoint.consumed
    assert doc.pause_checkpoint.reason == "blueprint_slot"
    assert doc.pause_checkpoint.blueprint_id == "bp"
    assert json.loads(doc.pause_checkpoint.slot_bindings_json)["employee"] == "A4I8"
    assert len(judge.briefs) == 1
    assert any(
        span.name == "loop_ask_user_judge_exhausted" for span in exporter.get_finished_spans()
    )
