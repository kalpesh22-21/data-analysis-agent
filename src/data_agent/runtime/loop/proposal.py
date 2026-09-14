"""Evidence eligibility and durable review state for one complete proposed answer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .answer_judge import JudgeVerdict


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class ReviewState:
    calls: int = 0
    repaired: bool = False
    answer_version: str = ""
    scope_hash: str = ""
    violation: str = ""
    site: str = "exit_prose"
    feedback: str = ""
    intent_id: str = ""
    result_ids: tuple[str, ...] = ()
    repair_type: str = ""
    approved_version: str = ""
    assumptions_before_refusal: tuple[str, ...] = ()
    excluded_components: tuple[dict[str, Any], ...] = ()

    def to_doc(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)

    @classmethod
    def restore(cls, value: Any, scope_hash: str) -> ReviewState:
        if not isinstance(value, dict):
            return cls(scope_hash=scope_hash)
        state = cls(**{k: v for k, v in value.items() if k in cls.__dataclass_fields__})
        state.excluded_components = tuple(state.excluded_components)
        state.result_ids = tuple(state.result_ids)
        state.assumptions_before_refusal = tuple(state.assumptions_before_refusal)
        state.calls = min(2, max(0, int(state.calls)))
        if state.scope_hash != scope_hash:
            state.excluded_components = ()
            state.approved_version = ""
            state.feedback = ""
            state.result_ids = ()
            state.assumptions_before_refusal = ()
        state.scope_hash = scope_hash
        return state

    def reject(self, verdict: JudgeVerdict, version: str, assumptions=()) -> None:
        if not self.violation:
            self.assumptions_before_refusal = tuple(assumptions)
        self.violation, self.feedback = verdict.violation, verdict.feedback
        self.intent_id, self.result_ids = verdict.intent_id, verdict.result_ids
        self.repair_type = verdict.repair_type
        self.answer_version = version
        self.approved_version = ""


def evidence_kind(entry) -> str | None:
    if entry.status != "ok" or entry.error_code:
        return None
    if entry.tool_name == "runQuery" or (entry.tool_name == "runBlueprint" and entry.authoritative):
        return "warehouse"
    if entry.tool_name == "getHelpCenterDocument":
        preview = entry.result_preview
        payload = (
            preview.preview_rows[0][0]
            if preview and preview.preview_rows and preview.preview_rows[0]
            else None
        )
        if (
            isinstance(payload, dict)
            and payload.get("found") is True
            and isinstance(payload.get("content"), str)
            and payload["content"].strip()
        ):
            return "product"
        return None
    if entry.tool_name in {"getTableSchema", "listTables", "listDatabases", "searchKnowledge"}:
        return "catalog"
    if entry.capability_terminal:
        return "capability"
    if entry.tool_name == "getCapabilityTool":
        preview = entry.result_preview
        payload = (
            preview.preview_rows[0][0]
            if preview and preview.preview_rows and preview.preview_rows[0]
            else None
        )
        return "capability" if isinstance(payload, dict) and payload.get("found") is True else None
    return None


def deliverable_evidence(state, args, trail) -> tuple[list[dict[str, Any]], str | None]:
    eligible = {e.tool_call_id: e for e in trail if evidence_kind(e)}

    # Legacy answerWithText accepted tool names. Resolve only an unambiguous name;
    # new finalizeAnswer always advertises execution IDs.
    def resolve(ref):
        if ref in eligible:
            return ref
        matches = [key for key, e in eligible.items() if e.tool_name == ref]
        return matches[0] if len(matches) == 1 else None

    supplied = args.get("evidence", [])
    if not isinstance(supplied, list):
        return [], "evidence must be a list of result IDs."
    refs = []
    for ref in supplied:
        ident = resolve(ref) if isinstance(ref, str) else None
        if ident is None:
            return (
                [],
                "An evidence reference is missing, unsuccessful, ambiguous, or outside your access. Use a successful current-turn result_id.",
            )
        refs.append(ident)
    deliverables = args.get("deliverables", [])
    if not isinstance(deliverables, list):
        return [], "deliverables must be a list."
    explicit = {d.get("intent_id"): d for d in deliverables if isinstance(d, dict)}
    rows = []
    intents = state.intents if state else ()
    if any(key not in {i.intent_id for i in intents} for key in explicit):
        return [], "A deliverable references an undeclared intent. Declare it before finalizing."
    for intent in intents:
        d = explicit.get(intent.intent_id, {})
        named = d.get(
            "result_ids", [intent.evidence_tool_call_id] if intent.evidence_tool_call_id else []
        )
        blocking_refs = {
            e.tool_call_id
            for e in trail
            if intent.status == "blocked"
            and e.tool_call_id == intent.evidence_tool_call_id
            and e.status != "ok"
        }
        if not isinstance(named, list) or any(
            r not in eligible and r not in blocking_refs for r in named
        ):
            return (
                [],
                f"Provide accessible successful result IDs for {intent.intent_id}, or disclose its missing evidence.",
            )
        expected = d.get("evidence_type")
        if (
            expected
            and named
            and any(evidence_kind(eligible[r]) != expected for r in named if r in eligible)
        ):
            return (
                [],
                f"Evidence for {intent.intent_id} has the wrong type; preserve other supported parts and correct this binding.",
            )
        rows.append(
            {
                "intent_id": intent.intent_id,
                "request": intent.description,
                "status": intent.status,
                "proposed_answer": d.get("answer", args.get("answer", "")),
                "evidence": [
                    {"result_id": r, "kind": evidence_kind(eligible[r])}
                    for r in named
                    if r in eligible
                ],
                **(
                    {
                        "limitations": [
                            {"result_id": r, "reason_code": intent.reason_code}
                            for r in named
                            if r in blocking_refs
                        ]
                    }
                    if blocking_refs
                    else {}
                ),
            }
        )
    if not intents:
        rows.append(
            {
                "intent_id": None,
                "proposed_answer": args.get("answer", ""),
                "evidence": [{"result_id": r, "kind": evidence_kind(eligible[r])} for r in refs],
            }
        )
    return rows, None


def validate_proposal_args(args: Any) -> str | None:
    from jsonschema import Draft202012Validator

    from data_agent.runtime.mcp.tool_schema import FINALIZE_ANSWER_SCHEMA

    error = next(Draft202012Validator(FINALIZE_ANSWER_SCHEMA["parameters"]).iter_errors(args), None)
    return ("Invalid finalizeAnswer arguments: " + error.message[:400]) if error else None


def context_catalog_entries(messages, allowed_ids, turn_index):
    """Scoped emulated discovery is valid catalogue evidence even without a trail write."""
    from data_agent.runtime.session.models import ResultPreview, TrailEntry

    entries = []
    for message in messages:
        if message.get("role") != "tool" or message.get("tool_call_id") not in allowed_ids:
            continue
        try:
            payload = json.loads(message.get("content", ""))
            if payload.get("status") != "ok" or payload.get("tool_name") not in {
                "listDatabases",
                "listTables",
            }:
                continue
            entries.append(
                TrailEntry(
                    turn_index=turn_index,
                    tool_call_id=message["tool_call_id"],
                    tool_name=payload["tool_name"],
                    args={},
                    status="ok",
                    error_code=None,
                    provenance=frozenset(),
                    result_preview=ResultPreview.from_doc(payload["result_preview"]),
                    result_full_ref=None,
                    ts="",
                )
            )
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
    return entries


def selected_components(args, trail, result_sql_by_call_id=None):
    """Only successful current-scope executions selected in this exact proposal."""
    result_sql_by_call_id = result_sql_by_call_id or {}
    components = []
    refs = args.get("capability_refs", [])
    refs = refs if isinstance(refs, list) else []
    tables = args.get("tables", [])
    tables = tables if isinstance(tables, list) else []
    for entry in trail:
        if entry.status != "ok" or entry.error_code:
            continue
        if (
            any(isinstance(t, dict) and t.get("result_id") == entry.tool_call_id for t in tables)
            and evidence_kind(entry) == "warehouse"
        ):
            components.append(
                {
                    "kind": "table",
                    "result_id": entry.tool_call_id,
                    "sql": result_sql_by_call_id.get(entry.tool_call_id)
                    or result_sql_by_call_id.get(entry.args.get("blueprint_id"))
                    or entry.args.get("sql"),
                    "blueprint_id": entry.args.get("blueprint_id")
                    if entry.tool_name == "runBlueprint"
                    else None,
                }
            )
        if entry.capability_terminal and entry.tool_name in refs:
            components.append(
                {
                    "kind": "capability",
                    "result_id": entry.tool_call_id,
                    "capability_ref": entry.tool_name,
                }
            )
    return components


def omit_components(state, verdict, components):
    """All-or-nothing validation; never guess a target from prose or a tool name."""
    if verdict.repair_type != "omit_component" or not verdict.result_ids:
        return False
    targets = []
    for ref in dict.fromkeys(verdict.result_ids):
        matches = [c for c in components if c["result_id"] == ref]
        if len(matches) != 1:
            return False
        target = matches[0]
        if (
            verdict.violation in {"capability_coverage_gap", "capability_intent_mismatch"}
            and target["kind"] != "capability"
        ):
            return False
        # Current UI selection uses capability names / resolved SQL, so aliases
        # shared by multiple selected executions cannot be removed independently.
        alias = "capability_ref" if target["kind"] == "capability" else "sql"
        if target.get(alias) and sum(c.get(alias) == target[alias] for c in components) != 1:
            return False
        targets.append(target)
    state.excluded_components = tuple([*state.excluded_components, *targets])
    return True


def filter_excluded_components(args, excluded):
    """Filter selection and evidence before dispatch/persistence and final review."""
    if not excluded:
        return args
    ids = {c["result_id"] for c in excluded}
    names = {c["capability_ref"] for c in excluded if c["kind"] == "capability"}
    sqls = {c.get("sql") for c in excluded if c.get("sql")}
    blueprints = {c.get("blueprint_id") for c in excluded if c.get("blueprint_id")}

    def rejected_table(t):
        if not isinstance(t, dict):
            return False
        ref = t.get("result_id")
        if isinstance(ref, str):
            return ref in ids
        return any(
            isinstance(t.get(key), str) and t[key] in values
            for key, values in (("sql", sqls), ("blueprint_id", blueprints))
        )

    result = dict(args)
    if rejected_table(result):
        for key in ("sql", "blueprint_id", "result_id"):
            result.pop(key, None)
        result["tables"] = []
    if isinstance(result.get("tables"), list):
        result["tables"] = [t for t in result["tables"] if not rejected_table(t)]
    if isinstance(result.get("capability_refs"), list):
        result["capability_refs"] = [
            r for r in result["capability_refs"] if not isinstance(r, str) or r not in names
        ]
    if isinstance(result.get("evidence"), list):
        result["evidence"] = [
            r
            for r in result["evidence"]
            if not isinstance(r, str) or (r not in ids and r not in names)
        ]
    if isinstance(result.get("deliverables"), list):
        result["deliverables"] = [
            {
                **d,
                "result_ids": [
                    r for r in d["result_ids"] if not isinstance(r, str) or r not in ids
                ],
            }
            if isinstance(d, dict) and isinstance(d.get("result_ids"), list)
            else d
            for d in result["deliverables"]
        ]
    return result


def omission_nudge(excluded):
    return (
        "\nRebuild the answer from the remaining evidence. This is an omission repair, NOT a request "
        "to reword or advertise the rejected component. The UI will NOT receive these components. "
        "Remove them and all related delivery/access claims from the revised answer: "
        + json.dumps(excluded)
        + ". Use remaining successful evidence to answer the supported parts and explicitly disclose "
        "what remains unanswered. Do not say an excluded option is displayed, presented, or available to use. An empty query result supports only that no matching records were returned "
        "by THAT query within accessible data; it does not prove no such employees exist or that another "
        "data source has no records. Say the excluded part could not be provided or verified, not that "
        "it was searched or contains no data. Do not re-present an excluded card or table. Do not rerun a successful "
        "query just to reconstruct its existing result. The next judge call validates the complete partial answer."
    )
