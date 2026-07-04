"""Adversarial QA on the S5 leakage gate (invariant #1 — the global-store
entity-free guarantee, D58/D17).

We do NOT re-test the happy path. We attack the load-bearing invariant "a GLOBAL
(blueprint / global_knowledge) candidate that carries an entity is NEVER rated
`pass`". Two families:

  * strict-xfail repros — a REAL hole where an entity-bearing global candidate is
    rated `pass`. Each xfail asserts the SECURE behavior (result != "pass"); when
    the builder closes the hole the xfail flips to XPASS (strict) and fails CI.
  * passing hardening — the DECISION combine logic (regex OR semantic ⇒ not pass)
    is fail-safe under attack; pinned so a regression can never open it.

The injected semantic scanner is a scripted double (NO real LLM), per contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.candidate.verdicts import EntityHit, LeakageVerdict
from data_agent.learning.leakage import LeakageGateStage
from data_agent.learning.leakage.scanner import (
    NullSemanticEntityScanner,
    SemanticScanRequest,
    SemanticScanResult,
)

from .helpers import ctx, envelope, global_knowledge_envelope

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "learning"


# --- scripted doubles ---------------------------------------------------------


@dataclass
class PerfectScanner:
    """A semantic scanner that WOULD catch `probe` in any field it is actually
    handed. Used to prove a `pass` is NOT scanner-quality but a gate FIELD-COVERAGE
    gap: if even a perfect scanner cannot see the field, the gate never routed it."""

    probe: str = "E12345"
    seen_fields: list[str] | None = None

    async def scan(self, request: SemanticScanRequest) -> SemanticScanResult:
        if self.seen_fields is not None:
            self.seen_fields.extend(request.text_by_field.keys())
        for fieldname, text in request.text_by_field.items():
            if self.probe in text:
                return SemanticScanResult(
                    classification="leak",
                    hits=(EntityHit(field=fieldname, kind="employee_code", span=self.probe),),
                )
        return SemanticScanResult(classification="clean", hits=())


@dataclass
class AlwaysCleanScanner:
    """Always reports clean — isolates the REGEX layer's behavior."""

    async def scan(self, request: SemanticScanRequest) -> SemanticScanResult:
        return SemanticScanResult(classification="clean", hits=())


async def _run(env, scanner, store=None):
    store = store or InMemoryCandidateStore()
    gate = LeakageGateStage(candidate_store=store, semantic_scanner=scanner)
    return await gate.process(env, ctx()), store


def _verdict(result) -> LeakageVerdict:
    return LeakageVerdict.from_doc(result.envelope.entity_scan)


# =============================================================================
# STRICT-XFAIL REPROS — real holes (assert the SECURE behavior; currently fails)
# =============================================================================


async def test_global_knowledge_entity_in_nonstatement_field_starves_backstop():
    """A global_knowledge whose `statement` is clean but whose `structured` dict
    carries an employee code. `payload` is free-form (`{"type":"object"}`), so this
    key is representable. A PERFECT scanner is wired; it still cannot see the field
    because the gate never selects it — proving a FIELD-COVERAGE gap, not scanner
    quality."""
    seen: list[str] = []
    scanner = PerfectScanner(probe="E12345", seen_fields=seen)
    base = envelope("clean")
    gk = replace(
        base,
        candidate_id="candidate::gk-structured::0",
        type="global_knowledge",
        payload={
            "statement": "employees above the pay band are flagged for review",
            "knowledge_type": "business_rule",
            "structured": {"example_employee": "E12345", "example_dept": "0420"},
        },
    )
    result, _ = await _run(gk, scanner)
    verdict = _verdict(result)

    # The perfect scanner was never even handed the entity-bearing field.
    assert "structured" not in seen  # sanity: the gate starved the backstop
    # SECURE behavior (what a fix must achieve): an entity-bearing global candidate
    # is never `pass`. Currently FAILS (verdict == "pass") -> strict xfail.
    assert verdict.result != "pass"


async def test_entity_in_blueprint_notes_never_passes():
    """Q1 rework: an employee code hiding in `notes` (extractor free text that lands
    with the artifact) with a CLEAN intent must not slip through as `pass`. The gate
    now enumerates `notes` as a blueprint content surface."""
    base = envelope("clean")
    bp = replace(base, payload={**base.payload, "notes": "reusable report for employee E12345"})
    result, _ = await _run(bp, NullSemanticEntityScanner())
    verdict = _verdict(result)
    assert verdict.result != "pass"
    assert any(h.field == "notes" for h in verdict.hits)


async def test_entity_in_generalization_sql_template_literal_never_passes():
    """Q1 rework: a hardcoded region literal that survived as a role=inline into the
    AST-rewritten `generalization.sql_template` lands verbatim in the global artifact,
    so the template string itself is scanned — an entity there is not `pass`."""
    base = envelope("clean")
    gen = {
        "sql_template": "SELECT sum(gross_pay) FROM payroll.f WHERE region = 'EMEA'",
        "uses": [],
        "uses_rules": [],
        "node_templates": [],
        "result_grain": {"columns": [], "verifiable": False},
        "static_validation": {
            "explain_ok": True, "binds_to_subset_uses": True, "dag_ok": True,
            "read_only_select": True, "outcome": "ok", "reason": None,
        },
        "canonical_ast_norm": "x",
    }
    bp = replace(base, payload={**base.payload, "generalization": gen})
    result, _ = await _run(bp, NullSemanticEntityScanner())
    verdict = _verdict(result)
    assert verdict.result != "pass"
    assert any(h.field == "generalization.sql_template" for h in verdict.hits)


async def test_lowercase_region_passes_default_regex_only_gate():
    """global_knowledge statement carrying a lowercase 'emea' region with the
    PRODUCTION-DEFAULT null scanner (regex only). Multi-char region tokens
    (EMEA/APAC/LATAM/NAWEST/NAEAST) can be case-folded without the NA/US/EU
    false-positive risk, so this is a fixable regex gap."""
    gk = global_knowledge_envelope(
        statement="the standard headcount rule applies across the emea region"
    )
    result, _ = await _run(gk, NullSemanticEntityScanner())
    verdict = _verdict(result)
    # SECURE behavior: a region token is an entity family; not `pass`. Currently
    # FAILS (regex is case-sensitive) -> strict xfail.
    assert verdict.result != "pass"


# =============================================================================
# PASSING HARDENING — the DECISION combine logic is fail-safe (pin it)
# =============================================================================


async def test_regex_hit_with_clean_scanner_never_passes():
    """The combine is an OR: a regex hit (intent carries E12345) with a scanner that
    says `clean` must NOT pass. Fails open would be a Blocker; it does not."""
    result, _ = await _run(envelope("leaking"), AlwaysCleanScanner())
    verdict = _verdict(result)
    assert verdict.result != "pass"
    assert verdict.result == "quarantine"  # blueprint + no user_fact => hold
    assert any(h.field == "intent" for h in verdict.hits)


async def test_scanner_leak_with_clean_regex_never_passes():
    """The reverse: an entity the regex misses (unicode name) but the scanner flags
    as a `leak` must NOT pass — the semantic layer's finding is honored."""

    @dataclass
    class LeakScanner:
        async def scan(self, request: SemanticScanRequest) -> SemanticScanResult:
            return SemanticScanResult(
                classification="leak",
                hits=(EntityHit(field="intent", kind="person", span="José García"),),
            )

    base = envelope("clean")
    bp = replace(base, payload={**base.payload, "intent": "total earnings for José García"})
    result, _ = await _run(bp, LeakScanner())
    verdict = _verdict(result)
    assert verdict.result != "pass"
    # a regex-clean but scanner-flagged blueprint quarantines (no user_fact signal)
    assert verdict.result == "quarantine"


async def test_entity_only_in_result_signature_is_caught():
    """An entity nested in `result_signature` (not in `intent`) is still scanned —
    the gate serializes result_signature canonically before scanning."""
    base = envelope("clean")
    sig = json.loads(json.dumps(base.payload["result_signature"]))
    sig["invariants"] = ["single-row scalar for employee E12345"]
    bp = replace(
        base,
        payload={**base.payload, "intent": "total earnings for a department", "result_signature": sig},
    )
    result, _ = await _run(bp, AlwaysCleanScanner())
    verdict = _verdict(result)
    assert verdict.result != "pass"
    assert any(h.field == "result_signature" for h in verdict.hits)


async def test_perfect_scanner_does_catch_entity_in_statement():
    """Contrast to the starvation xfail: when the entity is in `statement` (a field
    the gate DOES select), the semantic backstop is consulted and the global
    candidate is rejected. Proves the starvation hole is field-coverage, not that
    the scanner is never called."""
    scanner = PerfectScanner(probe="E12345")
    gk = global_knowledge_envelope(statement="the rule triggered for E12345 last cycle")
    result, _ = await _run(gk, scanner)
    verdict = _verdict(result)
    assert verdict.result == "reject"  # hard entity in a hard-reject global target
