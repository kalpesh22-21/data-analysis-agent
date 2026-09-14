"""Explicit final proposals for scripted runtime conversations.

This constructs a model response; it never invents evidence or changes the loop.
Tests exercising invalid bare completions should use ModelTurnResult directly.
"""

from itertools import count

from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest

_ids = count()


def final_answer(*, assistant_text, evidence=(), tables=(), capability_refs=(), **kwargs):
    kwargs.pop("tool_calls", None)
    return ModelTurnResult(
        tool_calls=[
            ToolCallRequest(
                id=f"final_answer_{next(_ids)}",
                name="finalizeAnswer",
                arguments={
                    "answer": assistant_text,
                    "evidence": list(evidence),
                    "tables": list(tables),
                    "capability_refs": list(capability_refs),
                },
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
