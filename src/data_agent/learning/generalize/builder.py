"""Builder — the S3 blueprint PLAN + accepted SQL → `BlueprintGeneralization`.

Pure and deterministic (NO LLM). Orchestrates: AST-rewrite (`rewrite`), transitive
`uses` (the reused D69/D87 provenance extractor), `result_grain` (from the plan's
`result_signature.grain`), static validation (`validate`), and the pinned
`canonical_ast_norm` (`canonical`). Every failure path is IN-BAND: it produces a
`BlueprintGeneralization` whose `static_validation.outcome == "fail_to_review"`
(D52/D97) — this function never raises for a bad candidate and never auto-promotes.
"""

from __future__ import annotations

from typing import Any

from ...sqlparse import ProvenanceExtractionError, extract_column_provenance
from ..candidate.generalization import (
    BlueprintGeneralization,
    NodeTemplate,
    ResultGrainStamp,
    StaticValidation,
)
from .canonical import canonical_ast_norm
from .rewrite import RewriteError, rewrite_sql_to_template
from .validate import (
    REASON_UNREWRITABLE,
    REASON_WHEN_COMPOSITE,
    check_dag,
    check_read_only_select,
    decide_outcome,
)


def _result_grain(payload: dict[str, Any]) -> ResultGrainStamp:
    grain = ((payload.get("result_signature") or {}).get("grain")) or {}
    return ResultGrainStamp(
        columns=tuple(grain.get("columns", []) or []),
        verifiable=bool(grain.get("verifiable", True)),
    )


def _uses_rules(parameterization: list[dict[str, Any]]) -> tuple[str, ...]:
    """The resolved catalog rule ids for role=rule locators (D48 input), sorted +
    de-duplicated. A plan-level fact (independent of any single node)."""
    rules = {
        p["rule_id"]
        for p in parameterization
        if p.get("role") == "rule" and p.get("rule_id")
    }
    return tuple(sorted(rules))


def _slot_binds_to(parameterization: list[dict[str, Any]]) -> tuple[str, ...]:
    binds: list[str] = []
    for p in parameterization:
        if p.get("role") == "slot":
            slot = p.get("slot") or {}
            if slot.get("binds_to"):
                binds.append(slot["binds_to"])
    return tuple(binds)


def _provenance_uses(
    templates: list[str], catalog_schema: dict[str, dict[str, str]]
) -> tuple[bool, tuple[str, ...]]:
    """Transitive `uses` across the template(s) via the D69/D87 extractor. Returns
    `(ok, uses)`; `ok=False` (a provenance failure) drives `explain_ok=False`."""
    uses: set[str] = set()
    for template in templates:
        try:
            pairs = extract_column_provenance(template, catalog_schema)
        except ProvenanceExtractionError:
            return False, ()
        for table, column in pairs:
            uses.add(f"{table}.{column}")
    return True, tuple(sorted(uses))


def _fail_to_review(
    payload: dict[str, Any],
    parameterization: list[dict[str, Any]],
    reason: str,
) -> BlueprintGeneralization:
    """An in-band fail-to-review generalization — no guessed template, no canonical
    hash input (S6 fail-soft: an empty `canonical_ast_norm` skips the hard key)."""
    return BlueprintGeneralization(
        sql_template=None,
        uses=(),
        uses_rules=_uses_rules(parameterization),
        node_templates=(),
        result_grain=_result_grain(payload),
        static_validation=StaticValidation(
            explain_ok=False,
            binds_to_subset_uses=False,
            dag_ok=False,
            read_only_select=False,
            outcome="fail_to_review",
            reason=reason,
        ),
        canonical_ast_norm="",
    )


def _accepted_sql_for_single(
    payload: dict[str, Any], sql_by_ref: dict[str, str | None]
) -> str | None:
    """The accepted single-blueprint SQL: the last `source_tool_call_ref` that
    resolves to a non-empty SQL (the final accepted runQuery of the turn)."""
    resolved = [
        sql_by_ref.get(ref)
        for ref in payload.get("source_tool_call_refs", []) or []
        if sql_by_ref.get(ref)
    ]
    return resolved[-1] if resolved else None


def generalize_blueprint(
    payload: dict[str, Any],
    sql_by_ref: dict[str, str | None],
    catalog_schema: dict[str, dict[str, str]],
) -> BlueprintGeneralization:
    """Deterministically enrich a blueprint PLAN into a `BlueprintGeneralization`.

    `sql_by_ref` maps a `tool_call_ref` → its accepted SQL (from the session trail,
    `SessionSummary.tool_calls[*].sql`). `catalog_schema` is the D69 `database.table`
    → `{column: type}` catalog the provenance extractor qualifies against.
    """
    parameterization: list[dict[str, Any]] = list(payload.get("parameterization", []) or [])
    composes: list[dict[str, Any]] = list(payload.get("composes", []) or [])
    kind = payload.get("kind")
    is_composite = kind == "composite" or bool(composes)

    if is_composite:
        return _generalize_composite(payload, parameterization, composes, sql_by_ref, catalog_schema)
    return _generalize_single(payload, parameterization, sql_by_ref, catalog_schema)


def _generalize_single(
    payload: dict[str, Any],
    parameterization: list[dict[str, Any]],
    sql_by_ref: dict[str, str | None],
    catalog_schema: dict[str, dict[str, str]],
) -> BlueprintGeneralization:
    accepted_sql = _accepted_sql_for_single(payload, sql_by_ref)
    if not accepted_sql:
        return _fail_to_review(payload, parameterization, REASON_UNREWRITABLE)
    try:
        sql_template = rewrite_sql_to_template(accepted_sql, parameterization, strict=True)
    except RewriteError:
        return _fail_to_review(payload, parameterization, REASON_UNREWRITABLE)

    provenance_ok, uses = _provenance_uses([sql_template], catalog_schema)
    binds = _slot_binds_to(parameterization)
    uses_set = set(uses)
    binds_ok = provenance_ok and all(b in uses_set for b in binds)
    read_only = check_read_only_select(sql_template)
    outcome, reason = decide_outcome(
        explain_ok=provenance_ok,
        binds_to_subset_uses=binds_ok,
        dag_ok=True,
        read_only_select=read_only,
    )
    return BlueprintGeneralization(
        sql_template=sql_template,
        uses=uses,
        uses_rules=_uses_rules(parameterization),
        node_templates=(),
        result_grain=_result_grain(payload),
        static_validation=StaticValidation(
            explain_ok=provenance_ok,
            binds_to_subset_uses=binds_ok,
            dag_ok=True,
            read_only_select=read_only,
            outcome=outcome,
            reason=reason,
        ),
        canonical_ast_norm=canonical_ast_norm(sql_template),
    )


def _generalize_composite(
    payload: dict[str, Any],
    parameterization: list[dict[str, Any]],
    composes: list[dict[str, Any]],
    sql_by_ref: dict[str, str | None],
    catalog_schema: dict[str, dict[str, str]],
) -> BlueprintGeneralization:
    # A `when`-bearing composite cannot promote 1:1 onto `Blueprint` (the S3 plan's
    # `when` is a bare string; `NodeTemplate` has no `when` field) — reject to review
    # rather than emit a half-typed template (§1 / §11.6).
    if any(node.get("when") is not None for node in composes):
        return _fail_to_review(payload, parameterization, REASON_WHEN_COMPOSITE)

    ordered = sorted(composes, key=lambda n: n.get("order", 0))
    node_templates: list[NodeTemplate] = []
    try:
        for node in ordered:
            ref = node.get("source_tool_call_ref")
            accepted_sql = sql_by_ref.get(ref) if ref else None
            if not accepted_sql:
                return _fail_to_review(payload, parameterization, REASON_UNREWRITABLE)
            # Lenient: a top-level param whose literal is absent from THIS node's SQL
            # is skipped (a node references only a subset of the shared params).
            template = rewrite_sql_to_template(accepted_sql, parameterization, strict=False)
            node_templates.append(NodeTemplate(order=int(node["order"]), sql_template=template))
    except RewriteError:
        return _fail_to_review(payload, parameterization, REASON_UNREWRITABLE)

    templates = [n.sql_template for n in node_templates]
    provenance_ok, uses = _provenance_uses(templates, catalog_schema)
    binds = _slot_binds_to(parameterization)
    uses_set = set(uses)
    binds_ok = provenance_ok and all(b in uses_set for b in binds)
    read_only = provenance_ok and all(check_read_only_select(t) for t in templates)
    dag_ok = check_dag(composes)
    outcome, reason = decide_outcome(
        explain_ok=provenance_ok,
        binds_to_subset_uses=binds_ok,
        dag_ok=dag_ok,
        read_only_select=read_only,
    )
    return BlueprintGeneralization(
        sql_template=None,
        uses=uses,
        uses_rules=_uses_rules(parameterization),
        node_templates=tuple(node_templates),
        result_grain=_result_grain(payload),
        static_validation=StaticValidation(
            explain_ok=provenance_ok,
            binds_to_subset_uses=binds_ok,
            dag_ok=dag_ok,
            read_only_select=read_only,
            outcome=outcome,
            reason=reason,
        ),
        canonical_ast_norm=canonical_ast_norm(None, tuple(node_templates)),
    )
