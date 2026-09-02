"""Builder — the S3 blueprint PLAN + accepted SQL → `BlueprintGeneralization`.

Pure and deterministic (NO LLM). Orchestrates the AST rewrite, the transitive `uses` from
the D69/D87 provenance extractor, `result_grain`, static validation and the pinned
`canonical_ast_norm`. Every failure path is IN-BAND: it produces a `BlueprintGeneralization`
whose `static_validation.outcome == "fail_to_review"` (D52/D97). This module never raises
for a bad candidate and never auto-promotes.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ...runtime.blueprint.models import TABLE_CONSUME_REF
from ...runtime.blueprint.template import SCRATCH_DB
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
    ambiguous_frozen_date_literals,
    check_dag,
    check_no_frozen_date_literal,
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
    rules = {p["rule_id"] for p in parameterization if p.get("role") == "rule" and p.get("rule_id")}
    return tuple(sorted(rules))


def _slot_binds_to(parameterization: list[dict[str, Any]]) -> tuple[str, ...]:
    binds: list[str] = []
    for p in parameterization:
        if p.get("role") == "slot":
            slot = p.get("slot") or {}
            if slot.get("binds_to"):
                binds.append(slot["binds_to"])
    return tuple(binds)


# A name shaped like a MATERIALIZED scratch table (`s_<sid>_<suffix>`), which a declared
# placeholder must never be. Traced end to end and such a name is inert — every use is either a
# membership key or the identifier `_rewrite_scratch_tables` REPLACES, so it never reaches
# warehouse SQL — but refusing it keeps the corpus honest: a promoted blueprint whose
# placeholder masquerades as another session's table is a lie to whoever audits it later, and
# an intermediate should carry a semantic name (`emp_earnings`) anyway.
#
# Refusing here means the name is simply NOT declared, so it falls through to the ordinary D64
# fail-closed path rather than being special-cased — the conservative direction.
_MATERIALIZED_SCRATCH_NAME = re.compile(r"^s_.+_.+$")


def _declared_scratch(node: dict[str, Any] | None) -> frozenset[str]:
    """The scratch placeholders *node* consumes AS TABLES — the `$n` (not `$n.name`) consumes.

    These name intermediates the blueprint produces itself one node earlier, so they are exempt
    from the extractor's session-ownership check: there is no session, and nothing is
    materialized until the DAG runs. Anything not named here still fail-closes.
    """
    if not isinstance(node, dict):
        return frozenset()
    consumes = node.get("consumes")
    if not isinstance(consumes, dict):
        return frozenset()
    return frozenset(
        str(placeholder)
        for placeholder, ref in consumes.items()
        if TABLE_CONSUME_REF.match(str(ref))
        and not _MATERIALIZED_SCRATCH_NAME.match(str(placeholder))
    )


def _scratch_column_schema(
    node: dict[str, Any] | None,
    templates_by_order: dict[Any, str],
) -> dict[str, dict[str, str]]:
    """`{scratch.<placeholder>: {column: TEXT}}` for *node*'s TABLE consumes.

    MIRRORS `compiler._scratch_schema_for_node`, which does exactly this at LOAD time, and
    reuses its `_template_output_columns` reader so the two cannot disagree about what columns a
    materialized scratch table carries. The producer's SQL comes from the TEMPLATES, not from the
    plan node — an S3 `composes` entry carries `source_tool_call_ref` and never SQL, and reading
    it there registered the placeholder with zero columns, which fails to qualify identically.
    """
    from ...runtime.blueprint.compiler import _template_output_columns

    if not isinstance(node, dict):
        return {}
    schema: dict[str, dict[str, str]] = {}
    for placeholder, ref in (node.get("consumes") or {}).items():
        match = TABLE_CONSUME_REF.match(str(ref))
        if match is None:
            continue
        columns = _template_output_columns(templates_by_order.get(int(match.group(1))))
        schema[f"{SCRATCH_DB}.{placeholder}"] = {c: "TEXT" for c in columns}
    return schema


def _catalog_table_names(catalog_schema: dict[str, dict[str, str]]) -> frozenset[str]:
    """The BARE table names the catalog declares — what an alias key can collide with."""
    return frozenset(key.split(".", 1)[1] for key in catalog_schema if "." in key)


def _provenance_uses(
    templates: list[str],
    catalog_schema: dict[str, dict[str, str]],
    *,
    per_node: list[tuple[frozenset[str], dict[str, dict[str, str]]]] | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """Transitive `uses` across the template(s) via the D69/D87 extractor.

    Returns `(ok, uses)`; `ok=False` (a provenance failure) drives `explain_ok=False`.

    *per_node* supplies, per template, the scratch placeholders that template DECLARES it
    consumes and the columns each carries. Without it a composite whose consumer reads
    `scratch.<name>` stamped `explain_ok=False, uses=()` — the extractor fail-closes on any
    `scratch.*` with no bound session (D64) — and with an empty footprint the loader then also
    refused the PRODUCER for reading a warehouse table outside it. So a shape the executor,
    `check_dag` and the corpus loader all support could never be promoted.

    ⚠ SCRATCH COLUMNS ARE EXCLUDED FROM `uses`. The footprint is a claim about the WAREHOUSE,
    and a scratch table is this blueprint's own intermediate — including it would inflate the
    declared footprint with names no access check can mean anything about. That is the
    D69/OQ-4 scope-honesty split the compiler makes at load time, made the same way here.
    """
    uses: set[str] = set()
    for index, template in enumerate(templates):
        declared, scratch_schema = per_node[index] if per_node else (frozenset(), {})
        # ⚠ A PLACEHOLDER MAY NOT SHADOW A CATALOG TABLE NAME, and this is the FAIL-OPEN one.
        # `_build_alias_map` keys aliases by BARE table name, so `scratch.payroll` and
        # `dbpcm_warehouse.payroll` collide and the last source wins. When scratch wins, every
        # warehouse column reference is attributed to the scratch table and then dropped by the
        # scope-honesty filter below — `uses=()` with `outcome="ok"`, an UNDERSTATED footprint,
        # which is the direction that silently passes any scope check. When the warehouse wins,
        # the producer's model-chosen aliases are written into `database.table.column` keys for
        # columns that do not exist.
        #
        # `consumes` keys are model-authored on the mined path, so the shadowing name is free to
        # an attacker and reachable by accident. Undeclaring it drops the reference back onto the
        # ordinary D64 fail-closed path.
        shadowed = {name for name in declared if name in _catalog_table_names(catalog_schema)}
        if shadowed:
            _logger.info(
                "generalize: scratch placeholder(s) %s shadow a catalog table name — refusing "
                "to exempt them, so the candidate fails to review rather than under-declaring "
                "its footprint",
                sorted(shadowed),
            )
            declared = declared - shadowed
        schema = {**catalog_schema, **scratch_schema} if scratch_schema else catalog_schema
        try:
            pairs = extract_column_provenance(template, schema, declared_scratch=declared)
        except ProvenanceExtractionError:
            return False, ()
        for table, column in pairs:
            if table.split(".", 1)[0] == SCRATCH_DB:
                continue
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
            # VACUOUS on this path: no template was produced, so there is no frozen date
            # literal to have found. False here would name a fault this candidate does not
            # have, and `reason` above already carries the one it does. DELIBERATELY
            # divergent from the four all-False neighbours, which are equally unchecked
            # here — do not "fix" either side to match the other. Nothing reads the
            # individual flags (routing, render and the scheduler all read `outcome`), so
            # the only audience is a human reading the stamp, and to them False on a
            # never-run check reads as a finding.
            date_literal_ok=True,
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
    resolved = [sql_by_ref.get(ref) for ref in refs if isinstance(ref, str) and sql_by_ref.get(ref)]
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
        return _generalize_composite(
            payload, parameterization, composes, sql_by_ref, catalog_schema
        )
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
    date_literal_ok = check_no_frozen_date_literal(sql_template)
    date_warnings = ambiguous_frozen_date_literals(sql_template)
    outcome, reason = decide_outcome(
        explain_ok=provenance_ok,
        binds_to_subset_uses=binds_ok,
        dag_ok=True,
        read_only_select=read_only,
        date_literal_ok=date_literal_ok,
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
            date_literal_ok=date_literal_ok,
            outcome=outcome,
            reason=reason,
            date_literal_warnings=date_warnings,
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
    by_order = {n.get("order"): n for n in composes if isinstance(n, dict)}
    templates_by_order = {t.order: t.sql_template for t in node_templates}
    provenance_ok, uses = _provenance_uses(
        templates,
        catalog_schema,
        per_node=[
            (
                _declared_scratch(by_order.get(t.order)),
                _scratch_column_schema(by_order.get(t.order), templates_by_order),
            )
            for t in node_templates
        ],
    )
    binds = _slot_binds_to(parameterization)
    uses_set = set(uses)
    binds_ok = provenance_ok and all(b in uses_set for b in binds)
    read_only = provenance_ok and all(check_read_only_select(t) for t in templates)
    # EVERY node template: a composite's top-level `sql_template` is None by construction, so
    # the node templates are the only SQL this candidate carries — one frozen date in one node
    # ages the whole blueprint.
    date_literal_ok = all(check_no_frozen_date_literal(t) for t in templates)
    date_warnings = tuple(
        dict.fromkeys(
            warning
            for template in templates
            for warning in ambiguous_frozen_date_literals(template)
        )
    )
    dag_ok = True  # proven by the gate at the top; a False here returned already
    outcome, reason = decide_outcome(
        explain_ok=provenance_ok,
        binds_to_subset_uses=binds_ok,
        dag_ok=dag_ok,
        read_only_select=read_only,
        date_literal_ok=date_literal_ok,
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
            date_literal_ok=date_literal_ok,
            outcome=outcome,
            reason=reason,
            date_literal_warnings=date_warnings,
        ),
        canonical_ast_norm=_canonical_or_empty(None, tuple(node_templates)),
    )
