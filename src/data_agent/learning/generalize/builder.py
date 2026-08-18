"""Builder — the S3 blueprint PLAN + accepted SQL → `BlueprintGeneralization`.

Pure and deterministic (NO LLM). Orchestrates the AST rewrite, the transitive `uses` from
the D69/D87 provenance extractor, `result_grain`, static validation and the pinned
`canonical_ast_norm`. Every failure path is IN-BAND: it produces a `BlueprintGeneralization`
whose `static_validation.outcome == "fail_to_review"` (D52/D97). This module never raises
for a bad candidate and never auto-promotes.
"""

from __future__ import annotations

import logging
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
    REASON_DAG,
    REASON_UNREWRITABLE,
    REASON_WHEN_COMPOSITE,
    check_dag,
    check_read_only_select,
    decide_outcome,
)

_logger = logging.getLogger(__name__)


def _result_grain(payload: dict[str, Any]) -> ResultGrainStamp:
    """The declared grain, read defensively.

    `result_signature`/`grain`/`columns` are model-authored and rehydrated from the candidate
    store, so a wrong JSON type on any of the three must degrade to the empty grain rather than
    `AttributeError` out of a module contracted never to raise. Runs on EVERY path, including
    `_fail_to_review` — the error path must not have its own error path.
    """
    signature = payload.get("result_signature")
    grain = signature.get("grain") if isinstance(signature, dict) else None
    if not isinstance(grain, dict):
        grain = {}
    columns = grain.get("columns") or []
    if not isinstance(columns, list):
        columns = []
    return ResultGrainStamp(
        # Element-wise too: a non-string element (`["a", ["b"]]`) is carried into the
        # landed seed and only fails at `ResultGrain.parse` during landing. Dropped
        # here instead — same degrade-in-band posture as the three container checks
        # above. (`tuple("department")` — a STRING where the list belongs — would have
        # been ten single-character columns, a bogus D56 grain rather than a skipped
        # one; that is the container check on the line above.)
        columns=tuple(c for c in columns if isinstance(c, str)),
        verifiable=bool(grain.get("verifiable", True)),
    )


def _uses_rules(parameterization: list[dict[str, Any]]) -> tuple[str, ...]:
    """The resolved catalog rule ids for role=rule locators (D48 input), sorted + de-duplicated.

    A plan-level fact, independent of any single node.
    """
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
    """Transitive `uses` across the template(s) via the D69/D87 extractor.

    Returns `(ok, uses)`; `ok=False` (a provenance failure) drives `explain_ok=False`.
    """
    uses: set[str] = set()
    for template in templates:
        try:
            pairs = extract_column_provenance(template, catalog_schema)
        except ProvenanceExtractionError:
            return False, ()
        for table, column in pairs:
            uses.add(f"{table}.{column}")
    return True, tuple(sorted(uses))


def _absent_or_str(value: Any) -> bool:
    """Absent (or explicitly null) means "not declared"; anything present must be a string.

    A wrong JSON type is evidence about the prompt and should show up in a verdict, not be
    normalized away.
    """
    return value is None or isinstance(value, str)


def _plan_params_ok(raw_params: Any) -> bool:
    """Is `parameterization` shaped the way its readers assume?

    DERIVED FROM THE READERS, not from a remembered field list: for every consumer reachable
    from `generalize_blueprint` (success path AND `_fail_to_review` path), the OPERATION forces
    the requirement — `.get()` ⇒ dict (`entry`, `locator`, `slot`); membership or `sorted()` ⇒
    str (`locator.column`, `slot.binds_to`, `rule_id`); string concatenation ⇒ str
    (`slot.name`). `role` and `locator.value` are deliberately unconstrained, because no reader
    does anything partial with them: the first is only ever `==`-compared and the second is
    `str()`-ed, both total on every type.

    Where the driving operation is "must be hashable", the gate requires `str`: it is the domain
    type and it is outcome-equivalent — a hashable non-string matched nothing and already ended
    in `fail_to_review`, it just gets there by verdict now. Checked ONCE, here, which is what
    makes this module's "never raises for a bad candidate" contract true, so a NEW read added to
    any reader belongs in this docstring before it belongs in the code.
    """
    if not isinstance(raw_params, list):
        return False
    for param in raw_params:
        if not isinstance(param, dict):
            return False
        if not _absent_or_str(param.get("rule_id")):
            return False
        locator = param.get("locator")
        if locator is not None and (
            not isinstance(locator, dict) or not _absent_or_str(locator.get("column"))
        ):
            return False
        slot = param.get("slot")
        if slot is not None and (
            not isinstance(slot, dict)
            or not _absent_or_str(slot.get("name"))
            or not _absent_or_str(slot.get("binds_to"))
        ):
            return False
    return True


def _canonical_or_empty(
    sql_template: str | None, node_templates: tuple[NodeTemplate, ...] = ()
) -> str:
    """The S6 hash input, or `""` when the template will not normalize.

    `canonical_ast_norm` PARSES the template, and it is the last thing this module does on a
    path that has already decided what it thinks of the candidate — so a template the recipe
    chokes on used to raise `sqlglot.ParseError` out of a function contracted never to raise,
    taking the whole stage (and, on the consumer path, the session) with it. The empty string is
    the documented S6 fail-soft: no hash input, no hard key, the candidate is reviewed rather
    than deduplicated.
    """
    try:
        return canonical_ast_norm(sql_template, node_templates)
    except Exception:
        _logger.warning(
            "S4 canonical_ast_norm failed for a %s template; hashing is skipped "
            "(fail-soft) and the candidate is reviewed rather than deduplicated",
            "composite" if sql_template is None else "single",
            exc_info=True,
        )
        return ""


def _fail_to_review(
    payload: dict[str, Any],
    parameterization: list[dict[str, Any]],
    reason: str,
) -> BlueprintGeneralization:
    """An in-band fail-to-review generalization.

    No guessed template, and no canonical hash input (S6 fail-soft: an empty
    `canonical_ast_norm` skips the hard key).
    """
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


def fail_to_review_generalization(
    payload: dict[str, Any], reason: str = REASON_UNREWRITABLE
) -> BlueprintGeneralization:
    """The in-band `fail_to_review` generalization, for a caller OUTSIDE this module.

    `GeneralizeStage` needs it for its defensive catch: the stage's contract is that S4 never
    raises, and the only way to keep that true for an unanticipated fault is to stamp the same
    verdict an anticipated one gets. Deliberately takes no `parameterization` — an unanticipated
    fault is no reason to trust the plan enough to derive `uses_rules` from it.
    """
    return _fail_to_review(payload, [], reason)


def _accepted_sql_for_single(
    payload: dict[str, Any], sql_by_ref: dict[str, str | None]
) -> str | None:
    """The accepted SQL: the last `source_tool_call_ref` that resolves to a non-empty query.

    Refs are model-authored: a non-list `source_tool_call_refs` is not iterable and
    `dict.get(<unhashable>)` raises `TypeError`, so both are read as "no accepted SQL" (⇒
    `unrewritable`, the same in-band verdict a dangling ref gets) rather than as an exception.
    """
    refs = payload.get("source_tool_call_refs") or []
    if not isinstance(refs, list):
        return None
    resolved = [
        sql_by_ref.get(ref) for ref in refs if isinstance(ref, str) and sql_by_ref.get(ref)
    ]
    return resolved[-1] if resolved else None


def generalize_blueprint(
    payload: dict[str, Any],
    sql_by_ref: dict[str, str | None],
    catalog_schema: dict[str, dict[str, str]],
) -> BlueprintGeneralization:
    """Deterministically enrich a blueprint PLAN into a `BlueprintGeneralization`.

    `sql_by_ref` maps a `tool_call_ref` → its accepted SQL; `catalog_schema` is the D69
    `database.table` → `{column: type}` catalog the provenance extractor qualifies against.

    The two PLAN collections are shape-gated here, once, so nothing downstream re-checks:
    `parameterization` per `_plan_params_ok`, and `composes` as a list (`check_dag` is the
    deeper structural gate). Everything INSIDE `payload` is model-authored and rehydrated from
    the store, so a wrong JSON type anywhere in it must be an in-band `fail_to_review`, never an
    exception.
    """
    raw_params = payload.get("parameterization") or []
    raw_composes = payload.get("composes") or []
    if not isinstance(raw_composes, list):
        return _fail_to_review(payload, [], REASON_DAG)
    if not _plan_params_ok(raw_params):
        # No trustworthy role plan ⇒ no faithful rewrite is possible (a dropped or
        # mis-read predicate is the D97 silent-wrong-answer class), so: unrewritable.
        return _fail_to_review(payload, [], REASON_UNREWRITABLE)
    parameterization: list[dict[str, Any]] = list(raw_params)
    composes: list[dict[str, Any]] = list(raw_composes)
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
        canonical_ast_norm=_canonical_or_empty(sql_template),
    )


def _generalize_composite(
    payload: dict[str, Any],
    parameterization: list[dict[str, Any]],
    composes: list[dict[str, Any]],
    sql_by_ref: dict[str, str | None],
    catalog_schema: dict[str, dict[str, str]],
) -> BlueprintGeneralization:
    # THE STRUCTURAL GATE, FIRST. Same derivation as `_plan_params_ok` — below this
    # line the raw nodes are read THREE ways, and `check_dag` is what makes each read
    # total: `node.get("when")` and `node.get("source_tool_call_ref")` need every node
    # to be a DICT (AttributeError otherwise); `sorted(composes, key=lambda n:
    # n["order"])` needs `order` PRESENT (KeyError) and MUTUALLY COMPARABLE — a plan
    # mixing `0` and `"1"` raises `TypeError: '<' not supported between 'str' and
    # 'int'`; and `NodeTemplate(order=node["order"])` needs it to be an int. `check_dag`
    # never raises and proves exactly that: every node is a dict with a unique integer
    # `order`, well-typed `feeds_from`/`consumes`/`output`, and resolvable edges and
    # consume refs. Gating here rather than stamping `dag_ok` at the end also means no
    # part of the DAG is trusted before it is validated.
    #
    # Empty `composes` reaches this branch when the plan says `kind: "composite"` and
    # declares no nodes: a DAG with no steps. It would otherwise sail through with zero
    # templates and an `outcome="ok"` stamp, then land as a blueprint with neither a
    # `sql_template` nor a `composes` (which the corpus loader refuses) — so it is a
    # DAG fault, reviewed here rather than discovered at the landing write.
    if not composes or not check_dag(composes):
        return _fail_to_review(payload, parameterization, REASON_DAG)

    # A `when`-bearing composite cannot promote 1:1 onto `Blueprint` (the S3 plan's
    # `when` is a bare string; `NodeTemplate` has no `when` field) — reject to review
    # rather than emit a half-typed template (§1 / §11.6).
    if any(node.get("when") is not None for node in composes):
        return _fail_to_review(payload, parameterization, REASON_WHEN_COMPOSITE)

    ordered = sorted(composes, key=lambda n: n["order"])
    node_templates: list[NodeTemplate] = []
    try:
        for node in ordered:
            ref = node.get("source_tool_call_ref")
            # `source_tool_call_ref` is a PLAN-only field (the runtime never sees it),
            # so `check_dag` says nothing about it — and `dict.get(<unhashable>)` raises
            # TypeError, so a list/dict ref would escape. A non-string ref resolves to
            # no accepted SQL, which is already the unrewritable path below.
            accepted_sql = sql_by_ref.get(ref) if isinstance(ref, str) and ref else None
            if not accepted_sql:
                return _fail_to_review(payload, parameterization, REASON_UNREWRITABLE)
            # Lenient: a top-level param whose literal is absent from THIS node's SQL
            # is skipped (a node references only a subset of the shared params).
            template = rewrite_sql_to_template(accepted_sql, parameterization, strict=False)
            # `node["order"]` is a plain int here — the gate above proved it.
            node_templates.append(NodeTemplate(order=node["order"], sql_template=template))
    except RewriteError:
        return _fail_to_review(payload, parameterization, REASON_UNREWRITABLE)

    templates = [n.sql_template for n in node_templates]
    provenance_ok, uses = _provenance_uses(templates, catalog_schema)
    binds = _slot_binds_to(parameterization)
    uses_set = set(uses)
    binds_ok = provenance_ok and all(b in uses_set for b in binds)
    read_only = provenance_ok and all(check_read_only_select(t) for t in templates)
    dag_ok = True  # proven by the gate at the top; a False here returned already
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
        canonical_ast_norm=_canonical_or_empty(None, tuple(node_templates)),
    )
