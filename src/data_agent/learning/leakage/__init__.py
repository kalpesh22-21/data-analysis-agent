# leakage — the S5 leakage gate (Track B, Slice 5; D58/D17).
# A `CandidateStage` (GUARDRAIL span) that writes the authoritative `LeakageVerdict`
# into `envelope.entity_scan`. Regex/NER + an injected LLM semantic scan.
from .gate import PENDING_ENTITY_SCAN, LeakageGateStage, settle_entity_scan
from .scanner import (
    NullSemanticEntityScanner,
    SemanticClass,
    SemanticEntityScanner,
    SemanticScanRequest,
    SemanticScanResult,
)

__all__ = [
    "PENDING_ENTITY_SCAN",
    "LeakageGateStage",
    "NullSemanticEntityScanner",
    "SemanticClass",
    "SemanticEntityScanner",
    "SemanticScanRequest",
    "SemanticScanResult",
    "settle_entity_scan",
]
