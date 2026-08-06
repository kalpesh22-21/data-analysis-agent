"""Unit tests for observability/progress_summarizer.py (Layer 1, all fakes).

A `ProgressSummarizer` over a FAKE `ModelClient`: it returns the trimmed
present-tense line the model produced, and fails soft (returns `None`) on a
model error, a timeout, or empty output — never raising into the caller.
"""

from __future__ import annotations

import asyncio

from data_agent.runtime.model.client import ModelTurnResult
from data_agent.runtime.observability.progress_summarizer import ProgressSummarizer


class _FakeModelClient:
    """Records the (messages, tools) it saw and returns a canned line."""

    def __init__(self, text: str | None) -> None:
        self._text = text
        self.calls: list[tuple[list[dict], list[dict]]] = []

    async def send_turn(self, messages: list[dict], tools: list[dict]) -> ModelTurnResult:
        self.calls.append((messages, tools))
        return ModelTurnResult(assistant_text=self._text, tool_calls=[], usage={})


class _RaisingModelClient:
    async def send_turn(self, messages: list[dict], tools: list[dict]) -> ModelTurnResult:
        raise RuntimeError("model exploded")


class _SlowModelClient:
    def __init__(self, delay: float) -> None:
        self._delay = delay

    async def send_turn(self, messages: list[dict], tools: list[dict]) -> ModelTurnResult:
        await asyncio.sleep(self._delay)
        return ModelTurnResult(assistant_text="too late", tool_calls=[], usage={})


async def test_summarize_returns_trimmed_line() -> None:
    fake = _FakeModelClient("  Querying overtime pay by department  ")
    summarizer = ProgressSummarizer(fake)

    line = await summarizer.summarize("runQuery", {"sql": "SELECT 1"})

    assert line == "Querying overtime pay by department"
    # One round-trip, no tools advertised (tools=[]).
    assert len(fake.calls) == 1
    _messages, tools = fake.calls[0]
    assert tools == []


async def test_summarize_strips_wrapping_quotes() -> None:
    fake = _FakeModelClient('"Listing tables in the warehouse"')
    summarizer = ProgressSummarizer(fake)

    assert await summarizer.summarize("listTables", {}) == "Listing tables in the warehouse"


async def test_summarize_passes_tool_name_and_args_to_prompt() -> None:
    fake = _FakeModelClient("Resolving department codes")
    summarizer = ProgressSummarizer(fake)

    await summarizer.summarize("resolveValues", {"concept": "earnings", "period": "2026-01"})

    messages, _tools = fake.calls[0]
    user_content = messages[-1]["content"]
    assert "resolveValues" in user_content
    assert "earnings" in user_content
    assert "2026-01" in user_content


async def test_summarize_truncates_huge_arg_to_bound_tokens() -> None:
    fake = _FakeModelClient("Running a long query")
    summarizer = ProgressSummarizer(fake)
    huge_sql = "SELECT " + ("x" * 5000)

    await summarizer.summarize("runQuery", {"sql": huge_sql})

    messages, _tools = fake.calls[0]
    user_content = messages[-1]["content"]
    # The 5000-char arg must have been bounded well below its original size.
    assert len(user_content) < 2000


async def test_summarize_returns_none_on_model_error() -> None:
    summarizer = ProgressSummarizer(_RaisingModelClient())
    assert await summarizer.summarize("runQuery", {"sql": "SELECT 1"}) is None


async def test_summarize_returns_none_on_empty_output() -> None:
    assert await ProgressSummarizer(_FakeModelClient("")).summarize("runQuery", {}) is None
    assert await ProgressSummarizer(_FakeModelClient(None)).summarize("runQuery", {}) is None
    assert await ProgressSummarizer(_FakeModelClient("   ")).summarize("runQuery", {}) is None


async def test_summarize_returns_none_on_timeout() -> None:
    summarizer = ProgressSummarizer(_SlowModelClient(delay=5.0), timeout_seconds=0.01)
    assert await summarizer.summarize("runQuery", {"sql": "SELECT 1"}) is None
