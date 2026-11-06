"""Execution provenance copied from successful runBlueprint results, never caller claims."""
from copy import deepcopy

EXECUTION_KEYS = (
    "blueprint_id", "sql", "terminal_sql", "bound_slots", "omitted_slots",
    "uses_rules", "resolved_rule_bindings", "window_anchor", "window_end", "verify",
)


def blueprint_execution_receipt(tool_name, status, full):
    if tool_name != "runBlueprint" or status != "ok" or not isinstance(full, dict):
        return None
    if not isinstance(full.get("blueprint_id"), str) or not full["blueprint_id"]:
        return None
    if not isinstance(full.get("terminal_sql"), str) or not full["terminal_sql"].strip():
        return None
    return {**{key: deepcopy(full[key]) for key in EXECUTION_KEYS if key in full},
            "semantic_review": "pending"}
