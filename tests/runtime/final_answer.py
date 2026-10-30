"""Explicit final proposals for scripted runtime conversations.

This constructs a model response; it never invents evidence or changes the loop.
Tests exercising invalid bare completions should use ModelTurnResult directly.
"""

from itertools import count

from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient

_ids = count()


def answer_arguments(*, takeaway, evidence=(), tables=(), capability_refs=(), **kwargs):
    """Build an explicit document proposal for scripted scenarios, with no evidence inference."""
    sections = [
        {
            "id": f"table-{i}",
            "title": t.get("caption") or "Results",
            "evidence_refs": [t["result_id"]],
            "presentation": {"kind": "table", "result_id": t["result_id"]},
            "caveats": [],
            "actions": [],
            **({"table_reason": "Separate requested result."} if len(tables) > 1 else {}),
        }
        for i, t in enumerate(tables)
    ]
    if capability_refs:
        sections.append(
            {
                "id": "actions",
                "evidence_refs": list(evidence),
                "presentation": {"kind": "text", "markdown": ""},
                "caveats": [],
                "actions": [
                    {"capability_ref": ref, "label": "Open option"} for ref in capability_refs
                ],
            }
        )
    return {"takeaway": takeaway, "sections": sections, "evidence": list(evidence), **kwargs}


def final_answer(*, assistant_text, evidence=(), tables=(), capability_refs=(), **kwargs):
    kwargs.pop("tool_calls", None)
    return ModelTurnResult(
        tool_calls=[
            ToolCallRequest(
                id=f"final_answer_{next(_ids)}",
                name="finalizeAnswer",
                arguments=answer_arguments(
                    takeaway=assistant_text,
                    evidence=evidence,
                    tables=tables,
                    capability_refs=capability_refs,
                ),
            )
        ],
        **kwargs,
    )


async def work_trail(store, session_id):
    """Read/discovery entries, excluding the explicit answer confirmation."""
    return [
        e
        for e in await store.load_trail(session_id)
        if e.tool_name not in {"answerWithText", "answerWithTable"}
    ]


class BlueprintAnswerModel(ScriptedModelClient):
    """Select the resumed receipt exposed in context, whose ID is minted at runtime."""

    async def send_turn(self, messages, tools):
        import json
        from dataclasses import replace

        result = await super().send_turn(messages, tools)
        if "__visible_blueprint__" not in json.dumps([c.arguments for c in result.tool_calls]):
            return result
        receipts = [
            json.loads(m["content"])
            for m in messages
            if m.get("role") == "tool" and m.get("content", "").startswith("{")
        ]
        refs = [
            r["result_id"]
            for r in receipts
            if r.get("tool_name") == "runBlueprint" and r.get("status") == "ok"
        ]
        assert refs, "The resumed blueprint must be visible to the model before selection."
        return replace(
            result,
            tool_calls=[
                replace(
                    c,
                    arguments=json.loads(
                        json.dumps(c.arguments).replace("__visible_blueprint__", refs[-1])
                    ),
                )
                for c in result.tool_calls
            ],
        )


def recovery_sections(result_ids=(), *, titles=(), capability_refs=(), evidence=()):
    """Construct explicit recovery sections for scripted model responses."""
    names = {item['result_id']: item['caption'] for item in titles}
    return answer_arguments(
        takeaway="", evidence=evidence,
        tables=[{"result_id": ref, "caption": names.get(ref)} for ref in result_ids],
        capability_refs=capability_refs,
    )["sections"]
