"""The injected LLM semantic-scan seam for the S5 leakage gate (D58).

The regex layer catches KNOWN entity shapes; this second layer catches entities no pattern
anticipated and classifies WHY an entity is present — an accidental leak versus a legitimate
user-specific fact to reroute. That judgement is an LLM call in production, so it is an
INJECTED seam and Layer-1 tests drive a scripted double. The default is the null scanner
(regex-only), so an unwired gate still runs and fails safe: no reroute, and every entity hit
becomes a reject or a quarantine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from ..candidate.verdicts import EntityHit

# The scanner's classification of the candidate's entity posture:
#   clean     — no semantic entity concern (regex may still have found hits)
#   leak      — an entity that does NOT belong (accidental global leak)
#   user_fact — an entity that is a legitimate per-user fact (=> reroute target)
SemanticClass = Literal["clean", "leak", "user_fact"]


@dataclass(frozen=True)
class SemanticScanRequest:
    """What the gate hands the semantic scanner.

    The candidate type plus the per-field text already selected for scanning — never the raw
    envelope.
    """

    candidate_type: str
    text_by_field: dict[str, str]


@dataclass(frozen=True)
class SemanticScanResult:
    """The scanner's verdict: the entities it found (additive to the regex hits)
    plus its classification (which drives reroute vs. reject/quarantine)."""

    classification: SemanticClass = "clean"
    hits: tuple[EntityHit, ...] = field(default_factory=tuple)


class SemanticEntityScanner(Protocol):
    """The injected semantic-scan seam. Production wires an LLM-backed impl; tests
    wire a scripted double. Must be pure w.r.t. its input (no side effects)."""

    async def scan(self, request: SemanticScanRequest) -> SemanticScanResult: ...


class NullSemanticEntityScanner:
    """The default (unwired) scanner: always `clean`, no hits.

    The gate then runs regex-only and fails safe — an entity hit with no `user_fact` signal can
    never become a reroute, only a reject (global) or a quarantine (blueprint).
    """

    async def scan(self, request: SemanticScanRequest) -> SemanticScanResult:
        return SemanticScanResult(classification="clean", hits=())
