"""Exercise judge timeout recovery through the real client and a simulated HTTP server."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI

from data_agent.runtime.loop.answer_judge import JUDGE_TOOL_NAME, AnswerJudge
from data_agent.runtime.loop.delivery import review_once
from data_agent.runtime.model.openai_client import OpenAIModelClient
from tests.runtime.loop.test_answer_judge_unit import _brief


@pytest.mark.parametrize("approved", [True, False])
async def test_timeout_retry_disables_thinking_on_http_request(approved):
    requests = []
    cancelled = False
    events = []

    async def handle(request):
        nonlocal cancelled
        requests.append(json.loads(request.content))
        assert request.url.path == "/v1/chat/completions"
        if len(requests) == 1:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled = True
        return httpx.Response(
            200,
            json={
                "id": "simulated-judge",
                "object": "chat.completion",
                "created": 0,
                "model": "judge",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "verdict",
                                    "type": "function",
                                    "function": {
                                        "name": JUDGE_TOOL_NAME,
                                        "arguments": json.dumps(
                                            {
                                                "approved": approved,
                                                "violation": ""
                                                if approved
                                                else "unsupported_by_evidence",
                                                "feedback": ""
                                                if approved
                                                else "Unsupported claim.",
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    async with AsyncOpenAI(
        api_key="simulation-only",
        base_url="https://judge.test/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as provider:
        client = OpenAIModelClient(provider, model="judge", thinking_token_budget=4096)
        # Allow SDK cold initialization before the deliberately stalled HTTP request.
        judge = AnswerJudge(client, 20000, timeout_seconds=2, timeout_retry_seconds=2)
        verdict = await review_once(
            SimpleNamespace(
                _answer_judge=judge, _observer=lambda event, payload: events.append(event)
            ),
            _brief(),
        )

    assert cancelled
    assert len(requests) == 2
    assert requests[0]["thinking_token_budget"] == 4096
    assert "chat_template_kwargs" not in requests[0]
    assert requests[1]["chat_template_kwargs"] == {"enable_thinking": False, "thinking": False}
    assert "thinking_token_budget" not in requests[1]
    assert requests[1]["messages"] == requests[0]["messages"]
    assert requests[1]["tools"] == requests[0]["tools"]
    assert verdict.reviewed and verdict.approved == approved
    assert events == ["loop_presentation_review_started", "loop_answer_judge_timeout_retry"]
    assert client._thinking_token_budget == 4096
    assert client._chat_template_kwargs == {}
