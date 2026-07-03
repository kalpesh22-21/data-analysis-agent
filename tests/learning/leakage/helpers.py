"""Shared builders for the S5 leakage-gate tests (Layer-1, no real LLM)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.leakage.scanner import (
    SemanticScanRequest,
    SemanticScanResult,
)
from data_agent.learning.stage import StageContext
from data_agent.learning.triage import TriageVerdict

from ..extractor.helpers import make_summary

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "learning"

KEEP_VERDICT = TriageVerdict(decision="keep", reason="K1", target_hints=("blueprint",))


def load_mixed() -> dict[str, Any]:
    with (_FIXTURES / "s3_candidates_mixed.json").open() as fh:
        return json.load(fh)


def envelope(kind: str) -> CandidateEnvelope:
    """Load the `clean` or `leaking` blueprint candidate from the S5 fixture."""
    return CandidateEnvelope.from_doc(load_mixed()[kind])


def global_knowledge_envelope(*, statement: str) -> CandidateEnvelope:
    """A synthetic `global_knowledge` candidate (the fixture has only blueprints)."""
    base = envelope("clean")
    from dataclasses import replace

    return replace(
        base,
        candidate_id="candidate::hash-gk::0",
        type="global_knowledge",
        payload={"statement": statement, "knowledge_type": "business_rule"},
    )


def ctx() -> StageContext:
    return StageContext(summary=make_summary(), verdict=KEEP_VERDICT)


@dataclass
class ScriptedSemanticScanner:
    """A deterministic `SemanticEntityScanner` double — returns a fixed
    classification (and optional extra hits). NO real LLM."""

    classification: str = "clean"
    extra_hits: tuple = ()
    calls: list[SemanticScanRequest] | None = None

    async def scan(self, request: SemanticScanRequest) -> SemanticScanResult:
        if self.calls is not None:
            self.calls.append(request)
        return SemanticScanResult(
            classification=self.classification, hits=tuple(self.extra_hits)
        )
