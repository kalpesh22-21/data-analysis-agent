"""Bounded, authorized evidence for the single post-execution semantic review.

No extra warehouse/model calls, no inference of intent bindings or company-wide access.
The caller must scope-filter the trail before passing it here.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

import sqlglot
from sqlglot import exp

from data_agent.runtime.dispatch.tool_dispatcher import resolve_catalog

from .answer_judge import evidence_ids

_METADATA_KEYS = (
    "description",
    "grain",
    "grain_verifiable",
    "primary_key",
    "primary_key_note",
    "row_scope",
    "schema_notes",
    "join_keys",
    "temporal",
    "measures",
    "rules",
    "default_filters",
    "ambiguities",
)
_EXECUTION_KEYS = (
    "blueprint_id",
    "sql",
    "terminal_sql",
    "bound_slots",
    "omitted_slots",
    "uses_rules",
    "resolved_rule_bindings",
    "window_anchor",
    "window_end",
    "verify",
)


def catalog_context(catalog, executions, column_scope):
    """All authorized table rules, even when the SQL omitted their predicates.

    Catalog exports are scope independent. Omit a section referencing a denied column
    rather than accidentally publishing that column via a rule or join description.
    Explicit omission markers distinguish access restrictions from absent documentation.
    """
    dependencies: dict[str, set[str]] = {}
    missing = []
    for sql in executions:
        try:
            tree = sqlglot.parse_one(sql, dialect="clickhouse")
            ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
            for table in tree.find_all(exp.Table):
                if not table.db and table.name in ctes:
                    continue
                candidates = [
                    key
                    for key in catalog.schema
                    if key == f"{table.db}.{table.name}"
                    or (not table.db and key.rsplit(".", 1)[-1] == table.name)
                ]
                if len(candidates) != 1:
                    missing.append("table_documentation_unresolved")
                    continue
                key = candidates[0]
                names = dependencies.setdefault(key, set())
                names.update(
                    c.name for c in tree.find_all(exp.Column) if c.name in catalog.schema[key]
                )
                if any(
                    isinstance(e, exp.Star) or (isinstance(e, exp.Column) and e.is_star)
                    for select in tree.find_all(exp.Select)
                    for e in select.expressions
                ):
                    names.update(catalog.schema[key])
        except (sqlglot.errors.ParseError, ValueError):
            missing.append("sql_dependencies_unresolved")
    tables = []
    for key, needed in sorted(dependencies.items()):
        allowed = {
            c for c in catalog.schema[key] if not column_scope or f"{key}.{c}" in column_scope
        }
        if not allowed:
            missing.append("table_documentation_outside_scope")
            continue
        raw = catalog.documentation_for(key)
        if not raw:
            tables.append({"table": key, "documentation_unavailable": True})
            continue
        denied = set(catalog.schema[key]) - allowed
        # Include cross-table references in the access check too.
        denied.update(
            c
            for t, cols in catalog.schema.items()
            for c in cols
            if column_scope and f"{t}.{c}" not in column_scope and c not in allowed
        )

        def safe(value, denied=denied):
            tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", json.dumps(value)))
            return not (tokens & denied)

        result: dict[str, Any] = {"table": key, "omitted_for_scope": []}
        for name in _METADATA_KEYS:
            value = raw.get(name)
            if value is None:
                continue
            if name == "rules" and isinstance(value, list):
                result[name] = []
                for rule in value:
                    if not safe(rule):
                        result["omitted_for_scope"].append("rule")
                    elif rule not in result[name]:
                        result[name].append(rule)
            elif safe(value):
                result[name] = value
            else:
                result["omitted_for_scope"].append(name)
        # Rule/join/grain columns matter even when absent from the query.
        tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", json.dumps(result)))
        needed.update(tokens & allowed)
        columns = raw.get("columns", {})
        result["columns"] = {
            c: columns.get(c, {"type": catalog.schema[key][c], "documentation_unavailable": True})
            for c in sorted(needed & allowed)
            if safe(columns.get(c, {}))
        }
        if any(not safe(columns.get(c, {})) for c in needed & allowed):
            result["omitted_for_scope"].append("column_documentation")
        tables.append(result)
    return {"tables": tables, "limitations": sorted(set(missing))}


def coverage_package(brief, results, analysis_state, column_scope):
    """Only declared associations; never infer that an unrelated result answers a part."""
    available = {r["tool_call_id"] for r in results}
    assigned = set()
    parts = []
    for intent in getattr(analysis_state, "intents", ()):
        proposed = next(
            (d for d in brief.deliverables if d.get("intent_id") == intent.intent_id), {}
        )
        refs = evidence_ids(proposed.get("evidence", ()))
        bound = getattr(intent, "evidence_tool_call_id", None)
        if bound and bound not in refs:
            refs.append(bound)
        refs.extend(
            r["tool_call_id"]
            for r in results
            if intent.intent_id in r.get("serves_intents", ()) and r["tool_call_id"] not in refs
        )
        assigned.update(refs)
        parts.append(
            {
                "intent_id": intent.intent_id,
                "requested_outcome": intent.description,
                "status": intent.status,
                "proposed_answer": proposed.get("proposed_answer", proposed.get("answer")),
                "disclosed_limitations": proposed.get("limitations", []),
                "result_ids": refs,
                "unavailable_result_ids": [r for r in refs if r not in available],
                "binding_status": "explicit" if refs else "unassigned",
                "limitation_reason": intent.reason_code,
            }
        )
    return {
        "parts": parts,
        "assignment_mode": "declared_intents" if parts else "single_request_without_ledger",
        "unassigned_result_ids": sorted(available - assigned),
        "user_requested_scope": {
            "question": brief.question,
            "clarifications": list(brief.clarification_answers),
        },
        "caller_access": {
            "column_allowlist_active": bool(column_scope),
            "row_access": "warehouse-enforced; effective row population not supplied",
            "company_wide_completeness": "unknown",
        },
    }


async def enrich_brief(
    brief, *, trail, turn_index, session_id, store, catalog_provider, credentials, analysis_state
):
    current = {e.tool_call_id: e for e in trail if e.turn_index == turn_index and e.status == "ok"}
    results = [dict(r) for r in brief.results]
    sqls = []
    referenced = set(brief.referenced_result_ids) | set(brief.designated_tool_call_ids)
    referenced.update(c.get("result_id") for c in brief.selected_components)
    for part in brief.deliverables:
        referenced.update(evidence_ids(part.get("evidence", ())))
    blueprints = set()
    uses_rules = []
    for result in results:
        entry = current.get(result.get("tool_call_id"))
        if entry is None:
            continue
        full = None
        if entry.tool_name in {"runBlueprint", "getHelpCenterDocument"} and entry.result_full_ref:
            try:
                full = await store.read_full_result(session_id, entry.result_full_ref)
            except Exception:
                pass  # Missing evidence is marked below, never inferred.
        if entry.tool_name == "runQuery":
            result["execution"] = {"sql": entry.args.get("sql"), "semantic_review": "pending"}
        elif entry.tool_name == "runBlueprint":
            result["execution"] = {
                **(
                    {k: full[k] for k in _EXECUTION_KEYS if k in full}
                    if isinstance(full, dict)
                    else {"execution_metadata_unavailable": True}
                ),
                "requested_slot_bindings": entry.args.get("slot_bindings", {}),
                "semantic_review": "pending",
            }
            blueprints.add(entry.args.get("id"))
        elif entry.tool_name == "getHelpCenterDocument" and entry.result_full_ref:
            if isinstance(full, dict):
                result["document"] = full
            else:
                result["document_unavailable"] = True
        execution = result.get("execution", {})
        uses_rules.extend(execution.get("uses_rules", ()))
        sql = execution.get("sql", [])
        if not referenced or entry.tool_call_id in referenced:
            sqls.extend([sql] if isinstance(sql, str) else sql if isinstance(sql, list) else [])
    # Matching definitions carry uses_rules and optional-slot semantics. All have
    # already passed the same scope filter as result rows.
    definitions = []
    for entry in current.values():
        if entry.tool_name == "getBlueprint" and entry.args.get("id") in blueprints:
            if entry.result_preview:
                definitions.extend(entry.result_preview.preview_rows)
    package = coverage_package(brief, results, analysis_state, credentials.column_scope)
    package["blueprint_definitions"] = definitions
    try:
        catalog = (
            await resolve_catalog(catalog_provider, credentials)
            if catalog_provider is not None and sqls
            else None
        )
        package["catalog"] = (
            catalog_context(catalog, sqls, credentials.column_scope)
            if catalog
            else {"unavailable": True}
        )
    except Exception:
        package["catalog"] = {"unavailable": True}
    rules = {}
    for table in package["catalog"].get("tables", ()):
        for rule in table.get("rules", ()):
            if isinstance(rule, dict) and rule.get("id"):
                rules.setdefault(rule["id"], []).append(
                    {"table": table["table"], "definition": rule}
                )
    expanded = []
    for rule in uses_rules:
        if isinstance(rule, str):
            matches = rules.get(rule, [])
            item = {
                "reference": rule,
                "matches": matches,
                "unavailable": not matches,
                "ambiguous": len(matches) > 1,
            }
        else:
            item = {"definition": rule}
        if item not in expanded:
            expanded.append(item)
    package["blueprint_rules"] = expanded
    return replace(brief, results=tuple(results), evidence_package=package)
