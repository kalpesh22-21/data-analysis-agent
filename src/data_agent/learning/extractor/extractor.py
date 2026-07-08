"""LearningExtractor — the RAG-grounded, structured-output extractor (S3, D31).

Turns one KEEP-triaged `SessionSummary` into zero-or-more typed candidate
envelopes via a FORCED tool call (no free text, D31), retrying on a malformed
response. The model client is INJECTED (the runtime `ModelClient` seam) so
Layer-1 tests drive a deterministic `ScriptedModelClient` — no real LLM in unit
tests. The extractor emits a PLAN only (never SQL, D35); the AST rewrite is S4.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from data_agent.runtime.model.client import ModelClient, begin_turn_client

from ..summary.models import SessionSummary
from ..triage import TriageVerdict
from .models import Decline, ExtractedCandidate, ExtractionResult
from .schema import (
    SLOT_TYPE_ENUM,
    SchemaMismatchError,
    build_extractor_tool,
    parse_candidates,
)
from .validation import REASON_MALFORMED, to_candidate

_logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are the offline learning extractor. Given an ACCEPTED analytics session, "
    "emit zero or more typed learning candidates by calling the emit_candidates tool. "
    "Rules: (1) emit a PLAN, never SQL. (2) Every candidate MUST cite >=1 evidence "
    "quote (turn_ref + tool_call_ref) from the session; no evidence => do not emit it. "
    "(3) For a blueprint, the payload MUST include `kind` ('single'|'composite') and "
    "`parameterization` with exactly one entry per literal predicate of the accepted "
    "SQL, each classified slot|rule|inline (there is NO drop role; a caller-specific "
    "predicate is an OPTIONAL slot with an optional_pattern; a metric-defining predicate "
    "is inline; a catalog-rule-resolvable predicate is rule with an EXISTING rule_id). "
    "(4) For each parameterization entry, `locator.table` is the 'database.table' and "
    "`locator.column` is the BARE column. A slot's `binds_to` MUST be the "
    "FULLY-QUALIFIED 'database.table.column' (= locator.table + '.' + locator.column, "
    "e.g. 'dbpcm_warehouse.employee.Department'), NEVER a bare column, and must lie "
    "within the columns the SQL touches; each slot MUST include `name`, `type`, "
    "`binds_to`, and `required` (true|false). (5) A slot's `type` MUST be exactly one of: "
    f"{', '.join(SLOT_TYPE_ENUM)}. A free-text filter value (e.g. a department name) is "
    "'entity', NOT 'enum'; use 'enum' ONLY for a small closed set you ALSO provide in "
    "`enum_values` (an enum slot without enum_values is invalid). (6) The aggregated "
    "metric column (e.g. the argument "
    "of sum(...)) is NOT a predicate — put it in `resolves` (a JSON OBJECT/map, e.g. "
    "{'total salary': 'dbpcm_warehouse.employee.AnnualSalary'}, NEVER a list), never in "
    "parameterization. "
    "(7) Emit NO `result_signature` (null) for a single scalar aggregate (sum(...) with "
    "only a WHERE filter and no GROUP BY); set it only when the SQL has a GROUP BY whose "
    "grouped columns appear in the SELECT output. (8) intent and result_signature must "
    "be ENTITY-FREE (no literal values)."
)


@dataclass(frozen=True)
class ExtractorConfig:
    max_retries: int = 2
    known_rules: frozenset[str] = frozenset()


class LearningExtractor:
    def __init__(
        self,
        model_client: ModelClient,
        *,
        config: ExtractorConfig | None = None,
    ) -> None:
        self._model_client = model_client
        self._config = config or ExtractorConfig()

    async def extract(
        self, summary: SessionSummary, verdict: TriageVerdict
    ) -> ExtractionResult:
        """Run the forced-structured-output call + validation. Returns the
        structurally-valid candidates + the declines (each with a reason code)."""
        raw_candidates = await self._call_model_with_retry(summary, verdict)

        candidates: list[ExtractedCandidate] = []
        declines: list[Decline] = []
        for raw in raw_candidates:
            if not isinstance(raw, dict):
                declines.append(Decline("unknown", REASON_MALFORMED, "candidate is not an object"))
                continue
            outcome = to_candidate(raw, summary, known_rules=self._config.known_rules)
            if isinstance(outcome, ExtractedCandidate):
                candidates.append(outcome)
            else:
                declines.append(outcome)
        return ExtractionResult(candidates=tuple(candidates), declines=tuple(declines))

    async def _call_model_with_retry(
        self, summary: SessionSummary, verdict: TriageVerdict
    ) -> list[dict]:
        client = begin_turn_client(self._model_client)
        tools = [build_extractor_tool()]
        messages = self._build_messages(summary, verdict)
        last_exc: SchemaMismatchError | None = None
        # 1 initial attempt + max_retries retries on a malformed (non-tool-call)
        # response — D31 retry-on-mismatch. A persistent malformed response raises
        # (→ the consumer leaves the message un-acked → reclaim → dead-letter).
        for attempt in range(self._config.max_retries + 1):
            result = await client.send_turn(messages, tools)
            try:
                return parse_candidates(result)
            except SchemaMismatchError as exc:
                last_exc = exc
                _logger.warning(
                    "extractor structured-output mismatch (attempt %d/%d): %s",
                    attempt + 1, self._config.max_retries + 1, exc,
                )
                messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was not a valid emit_candidates tool "
                            "call. Respond by calling emit_candidates with a 'candidates' array."
                        ),
                    },
                ]
        assert last_exc is not None
        raise last_exc

    def _build_messages(
        self, summary: SessionSummary, verdict: TriageVerdict
    ) -> list[dict]:
        # Entity-bearing summary is fine here — the extractor is IN-boundary and
        # pre-leakage-gate (S5). Serialized compactly for the model.
        payload = {
            "session_id": summary.session_id,
            "accepted_signal": summary.accepted_signal,
            "triage": {"decision": verdict.decision, "reason": verdict.reason,
                       "target_hints": list(verdict.target_hints)},
            "turns": [
                {"turn_index": t.turn_index, "user_nl": t.user_nl,
                 "assistant_text": t.assistant_text}
                for t in summary.turns
            ],
            "tool_calls": [
                {"tool_call_ref": tc.tool_call_ref, "turn_index": tc.turn_index,
                 "tool_name": tc.tool_name, "sql": tc.sql, "status": tc.status,
                 "result_columns": list(tc.result_columns)}
                for tc in summary.tool_calls
            ],
            "askuser_exchanges": [
                {"question": ex.question, "answer": ex.answer}
                for ex in summary.askuser_exchanges
            ],
            "failed_fixed_sql": [
                {"failed_sql": ff.failed_sql, "fixed_sql": ff.fixed_sql}
                for ff in summary.failed_fixed_sql
            ],
        }
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
