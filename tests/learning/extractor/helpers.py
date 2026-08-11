"""Shared builders for the Slice-3 extractor/candidate tests.

A `ScriptedModelClient`-backed `LearningExtractor` (NO real LLM), plus builders
for `SessionSummary`, raw `emit_candidates` candidate dicts, and the worked
payroll blueprint (design §3.1).
"""

from __future__ import annotations

from typing import Any

from data_agent.learning.extractor import ExtractorConfig, LearningExtractor
from data_agent.learning.extractor.schema import EXTRACTOR_TOOL_NAME, SEARCH_CORPUS_TOOL_NAME
from data_agent.learning.priorart import PriorArtCard
from data_agent.learning.summary.models import SessionSummary, ToolCallSummary, TurnSummary
from data_agent.learning.triage import TriageVerdict
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient

# The design §3.1 payroll worked example — 4 literal predicates, one per role.
PAYROLL_SQL = (
    "SELECT sum(gross_pay) AS total_earnings "
    "FROM payroll.payroll_fact "
    "WHERE department = '0420' "
    "AND toYear(pay_period) = 2025 "
    "AND record_type = 'EARNING' "
    "AND region = 'NA'"
)

KEEP_VERDICT = TriageVerdict(decision="keep", reason="K1", target_hints=("blueprint",))


def make_tool_call(
    ref: str = "tc1",
    sql: str = PAYROLL_SQL,
    *,
    status: str = "ok",
    tool_name: str = "runQuery",
    turn_index: int = 0,
    result_columns: tuple[str, ...] = ("total_earnings",),
) -> ToolCallSummary:
    return ToolCallSummary(
        turn_index=turn_index, tool_call_ref=ref, tool_name=tool_name,
        args={"sql": sql}, sql=sql, status=status, error_code=None,
        provenance=frozenset(), result_columns=result_columns, result_row_count=1,
        result_full_ref=None, full_result_loaded=False,
    )


def make_turn(
    turn_index: int = 0,
    *,
    user_nl: str | None = "how much did Analytics earn in 2025?",
    assistant_text: str | None = None,
    tool_call_refs: tuple[str, ...] = ("tc1",),
) -> TurnSummary:
    return TurnSummary(
        turn_index=turn_index, user_nl=user_nl, assistant_text=assistant_text,
        tool_call_refs=tool_call_refs,
    )


def make_summary(
    *,
    tool_calls: tuple[ToolCallSummary, ...] | None = None,
    turns: tuple[TurnSummary, ...] = (),
    accepted_signal: str | None = "no_correction",
    session_id: str = "sess-1",
    trace_id: str = "trace-1",
    content_hash: str = "hash-1",
    user_id: str = "user-1",
) -> SessionSummary:
    return SessionSummary(
        session_id=session_id, user_id=user_id, scope_ref="scope-abc", trace_id=trace_id,
        content_hash=content_hash,
        turns=turns,
        tool_calls=tool_calls if tool_calls is not None else (make_tool_call(),),
        blueprint_usages=(), askuser_exchanges=(), failed_fixed_sql=(),
        accepted_signal=accepted_signal,
    )


# --- parameterization entry builders -----------------------------------------


# Sentinel for `param_slot(binds_to=...)`: absent ⇒ derive the FQ default, explicit
# `None` ⇒ emit a NULL `binds_to`. The two are different candidates now (a windowed
# slot must declare null; every other type must declare a column), so the builder
# cannot use `None` to mean "unspecified" the way it did before plan §3a.
_DERIVE_BINDS_TO = "<derive>"


def param_slot(
    column: str,
    *,
    name: str | None = None,
    slot_type: str = "entity",
    required: bool = True,
    optional_pattern: str | None = None,
    binds_to: str | None = _DERIVE_BINDS_TO,
    value: str = "X",
    table: str = "payroll.payroll_fact",
) -> dict[str, Any]:
    return {
        "locator": {"table": table, "column": column, "value": value},
        "role": "slot",
        "slot": {
            "name": name or column,
            "type": slot_type,
            "binds_to": f"{table}.{column}" if binds_to == _DERIVE_BINDS_TO else binds_to,
            "required": required,
            "optional_pattern": optional_pattern,
        },
    }


def param_inline(column: str, *, why: str = "defines the metric", value: str = "X",
                 table: str = "payroll.payroll_fact") -> dict[str, Any]:
    return {"locator": {"table": table, "column": column, "value": value},
            "role": "inline", "why": why}


def param_rule(column: str, rule_id: str | None, *, value: str = "X",
               table: str = "payroll.payroll_fact") -> dict[str, Any]:
    return {"locator": {"table": table, "column": column, "value": value},
            "role": "rule", "rule_id": rule_id}


def payroll_parameterization() -> list[dict[str, Any]]:
    """The design §3.1 plan: department+year slots, record_type inline, region
    optional-slot — one entry per literal predicate (D97 totality)."""
    return [
        param_slot("department", slot_type="entity", value="0420"),
        param_slot("pay_period", name="year", slot_type="period", value="2025"),
        param_inline("record_type", why="defines the metric 'earnings'", value="EARNING"),
        param_slot("region", slot_type="entity", required=False,
                   optional_pattern="TRUE", value="NA"),
    ]


def evidence_item(*, turn_ref: int = 0, tool_call_ref: str = "tc1",
                  quote: str = "total earnings for Analytics in 2025") -> dict[str, Any]:
    return {"turn_ref": turn_ref, "tool_call_ref": tool_call_ref, "quote": quote}


def blueprint_raw(
    *,
    parameterization: list[dict[str, Any]] | None = None,
    source_refs: tuple[str, ...] = ("tc1",),
    accepted_signal: str = "no_correction",
    evidence: list[dict[str, Any]] | None = None,
    intent: str = "total earnings for a department in a given year",
    kind: str = "single",
    resolves: dict[str, str] | None = None,
    result_signature: dict[str, Any] | None = None,
    confidence: float = 0.9,
    proposed_action: str = "new",
    depends_on: list[str] | None = None,
    entity_self_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "blueprint",
        "confidence": confidence,
        "evidence": [evidence_item()] if evidence is None else evidence,
        "rationale": "reusable department-earnings report",
        "proposed_action": proposed_action,
        "entity_self_check": entity_self_check or {"contains_entities": False, "found": []},
        "depends_on": depends_on or [],
        "payload": {
            "intent": intent,
            "kind": kind,
            "resolves": resolves or {"earnings": "payroll.payroll_fact.gross_pay"},
            "source_tool_call_refs": list(source_refs),
            "accepted_signal": accepted_signal,
            "parameterization": (
                payroll_parameterization() if parameterization is None else parameterization
            ),
            "result_signature": result_signature,
            "notes": "",
        },
    }


# --- scripted model client -> LearningExtractor ------------------------------


def scripted_turn(candidates: list[dict[str, Any]]) -> ModelTurnResult:
    """One well-formed `emit_candidates` tool call carrying *candidates*."""
    return ModelTurnResult(
        tool_calls=[ToolCallRequest(id="call_1", name=EXTRACTOR_TOOL_NAME,
                                    arguments={"candidates": candidates})]
    )


def search_turn(query: str = "headcount by department",
                *, kinds: list[str] | None = None,
                call_id: str = "call_s1") -> ModelTurnResult:
    """One `searchCorpus` tool call (plan §3a) — the model asking before emitting."""
    args: dict[str, Any] = {"query": query}
    if kinds is not None:
        args["kinds"] = kinds
    return ModelTurnResult(
        tool_calls=[ToolCallRequest(id=call_id, name=SEARCH_CORPUS_TOOL_NAME, arguments=args)]
    )


def make_extractor(
    turns: list[ModelTurnResult],
    *,
    known_rules: frozenset[str] = frozenset(),
    max_retries: int = 2,
    prior_art: object | None = None,
    max_search_calls: int = 3,
) -> LearningExtractor:
    client = ScriptedModelClient(turns)
    return LearningExtractor(
        client,
        config=ExtractorConfig(
            max_retries=max_retries,
            known_rules=known_rules,
            max_search_calls=max_search_calls,
        ),
        prior_art=prior_art,  # type: ignore[arg-type]
    )


def emit_extractor(
    candidates: list[dict[str, Any]],
    *,
    known_rules: frozenset[str] = frozenset(),
    prior_art: object | None = None,
) -> LearningExtractor:
    """Extractor scripted to emit exactly *candidates* in one tool call."""
    return make_extractor(
        [scripted_turn(candidates)], known_rules=known_rules, prior_art=prior_art
    )


def prior_art_card(
    id_: str,
    *,
    intent: str = "",
    tier: str = "mcp",
    status: str = "validated",
    kind: str = "blueprint",
    verified: bool | None = True,
    drift_status: str = "clean",
    result_grain: tuple[str, ...] = (),
    uses_rules: tuple[str, ...] = (),
    similarity: float = 0.0,
    model_matched: bool = True,
) -> PriorArtCard:
    """A `PriorArtCard` for the extractor tests (mirrors the priorart suite's builder)."""
    return PriorArtCard(
        id=id_,
        kind=kind,  # type: ignore[arg-type]
        tier=tier,  # type: ignore[arg-type]
        status=status,
        verified=verified,
        drift_status=drift_status,
        intent=intent or id_,
        result_grain=result_grain,
        uses_rules=uses_rules,
        structural_key="",
        embedding_model="all-mpnet-base-v2",
        similarity=similarity,
        model_matched=model_matched,
    )


def malformed_turn(text: str = "here are your candidates") -> ModelTurnResult:
    """A response that is NOT an emit_candidates tool call (free text only)."""
    return ModelTurnResult(assistant_text=text, tool_calls=[])
