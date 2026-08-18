"""The S5 leakage-gate GUARDRAIL span (D58/D25, design §10).

Emitted once per scanned candidate. D25-safe by construction: the ONLY attributes set are the
verdict label, non-PII counts, the scanned-field labels and the scanner provenance string —
never an entity span, quote or payload text.
"""

from __future__ import annotations

from typing import Any

from openinference.semconv.trace import OpenInferenceSpanKindValues
from opentelemetry.trace import Tracer

from data_agent.runtime.observability.tracing import span


def leakage_span(
    tracer: Tracer,
    *,
    candidate_id: str,
    result: str,
    hit_count: int,
    scanned_fields: tuple[str, ...],
    scanner: str,
) -> Any:
    """One leakage-gate decision (`learning.leakage`, GUARDRAIL).

    SHAPE-only (D25): the verdict label, the hit COUNT, the scanned-field labels and the scanner
    provenance — never an entity span or payload text.
    """
    return span(
        tracer,
        "learning.leakage",
        OpenInferenceSpanKindValues.GUARDRAIL,
        {
            "learning.candidate_id": candidate_id,
            "learning.leakage.result": result,
            "learning.leakage.hit_count": hit_count,
            "learning.leakage.scanned_fields": ",".join(scanned_fields),
            "learning.leakage.scanner": scanner,
        },
    )
