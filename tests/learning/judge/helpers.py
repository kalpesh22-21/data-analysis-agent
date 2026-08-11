"""Shared builders for the coverage-judge tests (plan §3b).

A `ScriptedModelClient`-backed `CoverageJudge` — NO real LLM anywhere in this suite.

**Read `test_judge_limits_qa.py` before trusting anything built here.** A scripted
client proves that a verdict is routed, guarded, recorded and honoured. It cannot prove
that the verdict is CORRECT, because the thing under test on that axis is a language
model's judgement and there is no model in this suite. Every assertion below is about
plumbing.
"""

from __future__ import annotations

from typing import Any

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.judge import JUDGE_TOOL_NAME, CoverageJudge, JudgeConfig
from data_agent.learning.priorart import InMemoryPriorArtIndex, PriorArtCard
from data_agent.learning.summary.models import SessionSummary, ToolCallSummary, TurnSummary
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient

CANON_SQL = (
    "SELECT sum(AnnualSalary) AS total FROM dbpcm_warehouse.employee "
    "WHERE Department = 'Analytics'"
)


def make_summary(
    *,
    session_id: str = "sess-1",
    content_hash: str = "hash-1",
    question: str = "what did Analytics earn in total?",
    sql: str | None = CANON_SQL,
    turns: tuple[TurnSummary, ...] | None = None,
    tool_calls: tuple[ToolCallSummary, ...] | None = None,
) -> SessionSummary:
    if turns is None:
        turns = (
            TurnSummary(
                turn_index=0, user_nl=question, assistant_text=None, tool_call_refs=("tc1",)
            ),
        )
    if tool_calls is None:
        tool_calls = (
            ToolCallSummary(
                turn_index=0,
                tool_call_ref="tc1",
                tool_name="runQuery",
                args={"sql": sql},
                sql=sql,
                status="ok",
                error_code=None,
                provenance=frozenset(),
                result_columns=("total",),
                result_row_count=1,
                result_full_ref=None,
                full_result_loaded=False,
            ),
        )
    return SessionSummary(
        session_id=session_id,
        user_id="u1",
        scope_ref="scope-1",
        trace_id="trace-1",
        content_hash=content_hash,
        turns=turns,
        tool_calls=tool_calls,
        blueprint_usages=(),
        askuser_exchanges=(),
        failed_fixed_sql=(),
        accepted_signal="no_correction",
    )


def card(
    id_: str = "bp::abc",
    *,
    intent: str = "total earnings for a department",
    tier: str = "mcp",
    status: str = "validated",
    similarity: float = 0.85,
    model_matched: bool = True,
    kind: str = "blueprint",
) -> PriorArtCard:
    return PriorArtCard(
        id=id_,
        kind=kind,  # type: ignore[arg-type]
        tier=tier,  # type: ignore[arg-type]
        status=status,
        verified=True,
        drift_status="clean",
        intent=intent,
        result_grain=(),
        uses_rules=(),
        structural_key="",
        embedding_model="all-mpnet-base-v2",
        similarity=similarity,
        model_matched=model_matched,
    )


def verdict_turn(
    verdict: str = "duplicate",
    *,
    covered_by: str = "bp::abc",
    reason: str = "the corpus blueprint computes the same total at the same grain",
    confidence: float = 0.95,
    tool_name: str = JUDGE_TOOL_NAME,
    arguments: Any = None,
) -> ModelTurnResult:
    """One well-formed `record_coverage` tool call, or whatever *arguments* says."""
    if arguments is None:
        arguments = {
            "verdict": verdict,
            "covered_by": covered_by,
            "reason": reason,
            "confidence": confidence,
        }
    return ModelTurnResult(
        tool_calls=[ToolCallRequest(id="call_j1", name=tool_name, arguments=arguments)]
    )


def free_text_turn(text: str = "I think this is a duplicate") -> ModelTurnResult:
    """A response that is NOT a tool call — the malformed case."""
    return ModelTurnResult(assistant_text=text, tool_calls=[])


class BoomModelClient:
    """A `ModelClient` whose `send_turn` raises — the provider-outage case."""

    def __init__(self) -> None:
        self.calls = 0

    async def send_turn(self, messages: list[dict], tools: list[dict]) -> ModelTurnResult:
        self.calls += 1
        raise RuntimeError("provider exploded")


def make_judge(
    turns: list[ModelTurnResult],
    *,
    cards: list[PriorArtCard] | None = None,
    scores: dict[tuple[str, str], float] | None = None,
    index_fails: bool = False,
    audit: InMemoryAuditStore | None = None,
    config: JudgeConfig | None = None,
    model_client: Any = None,
    tracer: object | None = None,
) -> tuple[CoverageJudge, ScriptedModelClient | Any, InMemoryAuditStore, InMemoryPriorArtIndex]:
    """A judge over a scripted model, an in-memory audit store and a fake index."""
    client = model_client if model_client is not None else ScriptedModelClient(turns)
    store = audit if audit is not None else InMemoryAuditStore()
    index = InMemoryPriorArtIndex(cards or [], scores=scores or {}, fail=index_fails)
    judge = CoverageJudge(
        client,
        store,
        prior_art=index,
        config=config or JudgeConfig(),
        tracer=tracer,
    )
    return judge, client, store, index


def make_envelope(
    *,
    candidate_id: str = "candidate::hash-1::0",
    intent: str = "total earnings for a department",
    content_hash: str = "hash-1",
) -> CandidateEnvelope:
    return CandidateEnvelope(
        candidate_id=candidate_id,
        type="blueprint",
        status=CandidateStatus.EXTRACTED,
        payload={
            "intent": intent,
            "kind": "single",
            "parameterization": [
                {"role": "slot", "slot": {"name": "department", "type": "entity"}}
            ],
            "generalization": {
                "sql_template": (
                    "SELECT sum(AnnualSalary) FROM dbpcm_warehouse.employee "
                    "WHERE Department = {department}"
                ),
                "result_grain": {"columns": ["Department"]},
                "uses_rules": ["earnings_only"],
            },
        },
        source_session="sess-1",
        source_trace="trace-1",
        evidence_refs=(),
        extractor_rationale="",
        entity_scan={"result": "pass", "hits": []},
        confidence=0.9,
        proposed_action="new",
        depends_on=(),
        content_hash=content_hash,
    )
