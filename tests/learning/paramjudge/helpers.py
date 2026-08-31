"""Fixtures for the parameterization judge (design §D, phase D-1)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from data_agent.learning.audit.memory_audit_store import InMemoryAuditStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.paramjudge import ParameterizationJudge, ParamJudgeConfig
from data_agent.learning.paramjudge.schema import PARAM_JUDGE_TOOL_NAME
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"

# The defect the whole judge exists for: a metric-defining literal made fillable. Filling
# `record_type` with 'DDUCT' returns total DEDUCTIONS under an intent that says "earnings".
OVER_SLOTTED_SQL = (
    "SELECT sum(gross_pay) AS total_earnings FROM payroll.payroll_fact "
    "WHERE department = {department} AND record_type = {record_type}"
)


def _entry(column: str, value: str, role: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "locator": {"table": "payroll.payroll_fact", "column": column, "value": value},
        "role": role,
        "slot": None,
        "rule_id": None,
        "why": None,
    }
    entry.update(extra)
    return entry


def clean_blueprint(
    *, outcome: str = "ok", entries: list[dict[str, Any]] | None = None
) -> CandidateEnvelope:
    """A blueprint that PASSED static validation — the judge's whole population.

    `outcome` is a parameter because the skip it drives is load-bearing: a `fail_to_review`
    candidate already carries a deterministic complaint with a reason tag the writer routes on,
    and paying a model to add a second opinion risks the two disagreeing about why it is in a
    queue.
    """
    base = json.loads((FIXTURES / "envelopes_each_reason.json").read_text())[
        "leakage_near_miss"
    ]
    env = CandidateEnvelope.from_doc(base)
    if entries is None:
        entries = [
            _entry(
                "department",
                "0420",
                "slot",
                slot={
                    "name": "department",
                    "type": "entity",
                    "binds_to": "payroll.payroll_fact.department",
                    "required": True,
                },
            ),
            _entry(
                "record_type",
                "EARNING",
                "slot",
                slot={
                    "name": "record_type",
                    "type": "entity",
                    "binds_to": "payroll.payroll_fact.record_type",
                    "required": True,
                },
            ),
        ]
    payload = {
        **env.payload,
        "intent": "total earnings for a department",
        "parameterization": entries,
        "source_tool_call_refs": ["tc1"],
        "generalization": {
            "sql_template": OVER_SLOTTED_SQL,
            "uses": ["payroll.payroll_fact.gross_pay"],
            "static_validation": {
                "explain_ok": True,
                "binds_to_subset_uses": True,
                "dag_ok": True,
                "read_only_select": True,
                "date_literal_ok": True,
                "outcome": outcome,
                "reason": None if outcome == "ok" else "frozen_date_literal",
            },
        },
    }
    return replace(env, status=CandidateStatus.CANDIDATE, payload=payload)


def verdict_turn(
    verdict: str = "revise",
    *,
    feedback: str = "record_type defines the metric named in the intent; re-role to inline",
    confidence: float = 0.9,
    findings: list[dict[str, Any]] | None = None,
    tool_name: str = PARAM_JUDGE_TOOL_NAME,
    arguments: Any = None,
) -> ModelTurnResult:
    """One well-formed `judge_parameterization` call, or whatever *arguments* says."""
    if arguments is None:
        arguments = {
            "verdict": verdict,
            "feedback": feedback,
            "confidence": confidence,
            "findings": (
                findings
                if findings is not None
                else [
                    {
                        "class": "A",
                        "criterion": "slot_should_be_inline",
                        "entry_index": 1,
                        "note": "record_type = EARNING is the metric definition",
                    }
                ]
            ),
        }
    return ModelTurnResult(
        tool_calls=[ToolCallRequest(id="pj1", name=tool_name, arguments=arguments)]
    )


class ScriptedModelClient:
    """Replays queued turns; records what it was asked."""

    def __init__(self, turns: list[ModelTurnResult]) -> None:
        self._turns = list(turns)
        self.calls: list[tuple[list[dict], list[dict]]] = []

    async def send_turn(self, messages: list[dict], tools: list[dict]) -> ModelTurnResult:
        self.calls.append((messages, tools))
        return self._turns.pop(0) if self._turns else ModelTurnResult(tool_calls=[])


class BoomModelClient:
    """A client whose `send_turn` raises — the provider-outage case."""

    def __init__(self, exc: BaseException | None = None) -> None:
        self.calls = 0
        self._exc = exc or RuntimeError("provider exploded")

    async def send_turn(self, messages: list[dict], tools: list[dict]) -> ModelTurnResult:
        self.calls += 1
        raise self._exc


def make_judge(
    turns: list[ModelTurnResult],
    *,
    store: InMemoryAuditStore | None = None,
    client: Any = None,
) -> tuple[ParameterizationJudge, InMemoryAuditStore, Any]:
    audit = store if store is not None else InMemoryAuditStore()
    model = client if client is not None else ScriptedModelClient(turns)
    judge = ParameterizationJudge(
        model_client=model,
        audit_store=audit,
        config=ParamJudgeConfig(model="test-model", timeout_seconds=5.0),
    )
    return judge, audit, model
