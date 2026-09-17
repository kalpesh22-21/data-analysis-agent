"""Preparation deduplication covers successful, failed and resumed real-loop calls."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from data_agent.runtime.capabilities.client import CapabilityDefinition, ToolParam
from data_agent.runtime.capabilities.preparation import PreparationCache
from data_agent.runtime.capabilities.tools import GetCapabilityTool, PresentCapabilityCardTool
from data_agent.runtime.dispatch.tool_dispatcher import ToolResult, _build_preview
from data_agent.runtime.loop.answer_judge import JudgeVerdict
from data_agent.runtime.loop.turn_accumulators import TurnAccumulators
from tests.runtime.test_harness_improvements import CREDS, Judge, batch, build, call, run


async def test_real_handler_reload_and_prose_repair_reuse_preparation():
    name = "get_employee_payroll_totals"
    definition = CapabilityDefinition(
        name,
        "1",
        "data_widget",
        "View employee paystubs",
        (ToolParam("employees", "Employee", "employee", True, None, None, None, "all"),),
        {"preamble_url": "ember:PayrollTotalsCl"},
    )

    class Client:
        hydrates = 0

        async def get_definition(self, *args, **kwargs):
            return definition

        async def hydrate(self, *args, **kwargs):
            self.hydrates += 1
            return {
                "name": name,
                "arguments": {
                    "filters": {"employees": []},
                    "unresolved_entities": {"employees": ["Venkat"]},
                    "has_unresolved_entities": True,
                },
            }

    client = Client()

    def load(ident):
        return call("getCapabilityTool", ident, tool_name=name)

    def prep(ident):
        return call(name, ident, employees=["Venkat"])

    def answer(ident, text):
        return call(
            "finalizeAnswer", ident, answer=text, tables=[], capability_refs=[name], evidence=["p1"]
        )

    judge = Judge(
        [
            JudgeVerdict(
                False,
                "unsupported_environment_claim",
                "Do not invent multiple matches.",
                True,
                repair_type="prose",
            ),
            JudgeVerdict(True, reviewed=True),
        ]
    )
    loop, store, model, _, events = build(
        [
            batch(load("l1")),
            batch(prep("p1")),
            batch(load("l2"), prep("p2")),
            batch(answer("f1", "Select the employee; there are multiple matches.")),
            batch(load("l3"), prep("p3")),
            batch(
                answer(
                    "f2",
                    "Select the employee in the paystub view. I have not determined the latest paystub.",
                )
            ),
        ],
        judge=judge,
    )

    def register(definition):
        # Exercise handler replacement on reload, a stronger case than app.py's
        # existing-name fast path; the guard must belong to the turn, not handler.
        loop._runtime_tools[name] = PresentCapabilityCardTool(client=client, definition=definition)
        return True

    loop._runtime_tools["getCapabilityTool"] = GetCapabilityTool(
        client=client,
        hydrate=register,
        visible_names=set(),
    )
    out = await run(loop, "Venkat's last paystub")
    assert out.status == "done"
    assert client.hydrates == 1
    assert len(out.capability_cards) == 1
    assert len(judge.briefs) == 2
    assert sum(e == "loop_repeated_capability_call_guarded" for e, _ in events) == 2
    assert any("do not reload or prepare them again" in str(c.messages) for c in model.calls)
    trail = await store.load_trail(CREDS.session_id)
    for entry in [e for e in trail if e.tool_name == name][1:]:
        payload = await store.read_full_result(CREDS.session_id, entry.result_full_ref)
        assert payload["reused_from_result_id"] == "p1"
        assert "does not establish zero, one, or multiple matches" in payload["next_step"]


class Preparation:
    repeat_guard_eligible = True

    def __init__(self, failed=False):
        self.calls = []
        self.failed = failed

    async def run(self, args, *unused, **kwargs):
        self.calls.append(deepcopy(args))
        if self.failed:
            return ToolResult(
                "error",
                "show_profile",
                "CAPABILITY_UNAVAILABLE",
                True,
                "Unavailable; use the warehouse for supported data.",
                frozenset(),
                None,
                None,
            )
        payload = {
            "name": "show_profile",
            "prepared": True,
            "capability_ref": "show_profile",
            "arguments": {"employees": args.get("employees", [])},
            "_agent_evidence": {"kind": "data_widget", "description": "Employee profile"},
        }
        return ToolResult(
            "ok",
            "show_profile",
            None,
            None,
            None,
            frozenset(),
            _build_preview(payload, 20),
            payload,
        )


def prepare(ident, employee="Venkat"):
    return call("show_profile", ident, employees=[employee])


def finish(failed=False, evidence="p1"):
    return call(
        "finalizeAnswer",
        "finish",
        answer="I don't have any information to answer your question."
        if failed
        else "Select the employee in the displayed option.",
        tables=[],
        capability_refs=[] if failed else ["show_profile"],
        evidence=[] if failed else [evidence],
    )


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("resume", [False, True])
async def test_same_arguments_never_repeat_provider_even_after_failure_or_resume(failed, resume):
    tool = Preparation(failed)
    steps = [batch(prepare("p1"))]
    if resume:
        steps.append(
            batch(
                call("askUser", "pause", question="Which period?", options=["Current", "Previous"])
            )
        )
    steps.extend([batch(prepare("p2"), prepare("p3")), batch(finish(failed))])
    loop, store, _, _, events = build(steps, extra={"show_profile": tool})
    out = await run(loop, "Show Venkat's employee profile")
    if resume:
        assert out.status == "paused_ask_user"
        out = await loop.resume(session_id=CREDS.session_id, credentials=CREDS, answer="Current")
    assert out.status == "done"
    assert len(tool.calls) == 1
    assert len(out.capability_cards or []) == (0 if failed else 1)
    trail = [e for e in await store.load_trail(CREDS.session_id) if e.tool_name == "show_profile"]
    assert len(trail) == 3
    assert [e.status for e in trail] == ["error" if failed else "ok"] * 3
    assert sum(e == "loop_repeated_capability_call_guarded" for e, _ in events) == 2
    receipt = await store.read_full_result(CREDS.session_id, trail[1].result_full_ref)
    assert receipt["reused_from_result_id"] == "p1"
    assert receipt["prepared"] is not failed


@pytest.mark.parametrize("failed", [False, True])
async def test_changed_arguments_and_new_turn_can_call_provider(failed):
    tool = Preparation(failed)
    loop, _, _, _, _ = build(
        [
            batch(prepare("p1"), prepare("p2", "Jane")),
            batch(finish(failed)),
            batch(prepare("p4")),
            batch(finish(failed, "p4")),
        ],
        extra={"show_profile": tool},
    )
    await run(loop, "Show Venkat and Jane's employee profiles")
    await run(loop, "Show Venkat's employee profile")
    assert tool.calls == [
        {"employees": ["Venkat"]},
        {"employees": ["Jane"]},
        {"employees": ["Venkat"]},
    ]


def card(employee="Venkat", **extra):
    return {"name": "show_profile", "arguments": {"employees": [employee]}, **extra}


def test_equivalent_binding_dedup_preserves_different_people_and_targets():
    accum = TurnAccumulators(
        capability_cards=[
            card(metadata={"title": "First"}),
            card(metadata={"title": "Second"}),
            card(),
            card("Jane"),
            card(metadata={"arguments": {"links": ["other-destination"]}}),
        ]
    )
    accum.select_capabilities(["show_profile"])
    assert len(accum.capability_cards) == 3
    assert accum.capability_cards[0]["metadata"]["title"] == "First"
    assert accum.capability_cards_deduped == 2
    accum.clear_capabilities()
    assert accum.capability_cards is None


@pytest.mark.parametrize("change", ["scope", "turn"])
async def test_prior_attempts_are_not_reused_across_scope_or_turn(change):
    entry = SimpleNamespace(
        turn_index=0,
        status="error",
        capability_terminal=False,
        error_code="CAPABILITY_UNAVAILABLE",
        model_response={"scope_hash": "old"},
        tool_name="show_profile",
        args={"employees": ["Venkat"]},
    )
    cache = PreparationCache(
        None,
        "session",
        1 if change == "turn" else 0,
        "new" if change == "scope" else "old",
        [entry],
    )
    assert await cache.lookup("show_profile", entry.args) is None


@pytest.mark.parametrize("broken", [False, True])
async def test_missing_stored_result_does_not_allow_identical_provider_retry(broken):
    class Store:
        async def read_full_result(self, *args):
            if broken:
                raise RuntimeError("private store error")
            return None

    entry = SimpleNamespace(
        turn_index=0,
        status="ok",
        capability_terminal=True,
        error_code=None,
        model_response={"scope_hash": "scope"},
        tool_name="show_profile",
        args={"employees": ["Venkat"]},
        result_full_ref="expired",
        tool_call_id="first",
        provenance=frozenset(),
    )
    cache = PreparationCache(Store(), "session", 0, "scope", [entry])
    result = await cache.lookup("show_profile", entry.args)
    assert result.status == "error"
    assert result.retryable is False
    assert result.result_full["prepared"] is False
    assert "private" not in result.user_message
    assert await cache.lookup("show_profile", entry.args) is not None


async def test_reused_receipt_keeps_refusal_exclusion_and_original_evidence():
    tool = Preparation()
    args = {"employees": ["Venkat"]}
    original = await tool.run(args)
    cache = PreparationCache(None, "session", 0, "scope", [])
    cache.record("show_profile", args, original, "first")
    accum = TurnAccumulators()
    accum.note_capability_card(original)
    accum.select_capabilities(["show_profile"])
    accum.exclude_components([{"kind": "capability", "capability_ref": "show_profile"}])
    repeated = await cache.lookup("show_profile", {**args, "serves_intents": ["i2"]})
    accum.note_capability_card(repeated)
    accum.select_capabilities(["show_profile"])
    assert accum.capability_cards is None
    assert "reused_from_result_id" not in original.result_full
    assert len(tool.calls) == 1


def test_only_ui_preparation_handlers_opt_in():
    assert PresentCapabilityCardTool.repeat_guard_eligible is True
    from data_agent.runtime.dispatch.tool_envelope import RuntimeToolBase

    assert not getattr(RuntimeToolBase, "repeat_guard_eligible", False)
