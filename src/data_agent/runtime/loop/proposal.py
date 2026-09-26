"""Evidence eligibility and durable review state for one complete proposed answer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .answer_judge import JudgeVerdict


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class ReviewState:
    question_refusals: dict[str, str] = field(default_factory=dict)
    delivery_version: str = ""
    delivery_status: str = ""
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
            state.delivery_version = ""
            state.delivery_status = ""
            state.feedback = ""
            state.result_ids = ()
            state.assumptions_before_refusal = ()
        state.scope_hash = scope_hash
        return state

    def reject(self, verdict: JudgeVerdict, version: str, assumptions=()) -> None:
        if not self.violation:
            self.assumptions_before_refusal = tuple(assumptions or ())
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


# These successful discovery receipts describe values/samples, not completed answers.
# Used for model-facing discovery guidance; all known ineligible citations are ignored.
SUPPORTING_LOOKUP_TOOLS = frozenset({"resolveValues", "sampleRows"})


@dataclass(frozen=True)
class EvidenceAssessment:
    deliverables: list[dict[str, Any]]
    references: tuple[str, ...]
    errors: tuple[str, ...]
    ignored_references: tuple[str, ...]

    @property
    def feedback(self) -> str | None:
        return "\n\n".join(self.errors) or None


def assess_deliverable_evidence(state, args, trail) -> EvidenceAssessment:
    """Validate all references using ONLY the caller's current-turn, scope-filtered trail."""
    eligible = {e.tool_call_id: e for e in trail if evidence_kind(e)}
    known = {e.tool_call_id: e for e in trail}
    errors, refs, extras = [], [], []

    def label(ref):
        # Quote and bound model-authored identifiers; never interpolate raw control text.
        return json.dumps(ref, ensure_ascii=True, default=str)[:180]

    def resolve(ref):
        if not isinstance(ref, str):
            return None, f"An evidence reference {label(ref)} is not a result ID string."
        if ref in eligible:
            return ref, None
        entry = known.get(ref)
        if entry is not None:
            # Failed work and control/discovery receipts are not affirmative evidence.
            # Their existence is valid; excluding them requires no agent repair.
            extras.append(ref)
            return None, None
        # Legacy answer tools allowed unambiguous tool names. Exact execution IDs remain preferred.
        matches = [key for key, e in eligible.items() if e.tool_name == ref]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return (
                None,
                f"An evidence reference {label(ref)} is ambiguous. Use an exact returned result_id.",
            )
        # Missing and inaccessible are deliberately indistinguishable.
        return None, (
            f"An evidence reference {label(ref)} is unavailable in the accessible current-turn evidence. "
            "Use a successful current-turn result_id."
        )

    supplied = args.get("evidence", [])
    if not isinstance(supplied, list):
        errors.append("evidence must be a list of result IDs.")
        supplied = []
    for ref in supplied:
        ident, error = resolve(ref)
        if error:
            errors.append(error)
        elif ident is not None:
            refs.append(ident)
    deliverables = args.get("deliverables", [])
    if not isinstance(deliverables, list):
        errors.append("deliverables must be a list.")
        deliverables = []
    explicit = {d.get("intent_id"): d for d in deliverables if isinstance(d, dict)}
    rows = []
    intents = state.intents if state else ()
    for key in explicit:
        if key not in {i.intent_id for i in intents}:
            errors.append(
                f"A deliverable references undeclared intent {label(key)}. Declare it before finalizing."
            )
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
        if not isinstance(named, list):
            errors.append(f"result_ids for {intent.intent_id} must be a list.")
            named = []
        valid = []
        limitations = []
        for ref in named:
            if isinstance(ref, str) and ref in blocking_refs:
                limitations.append({"result_id": ref, "reason_code": intent.reason_code})
            elif isinstance(ref, str) and ref in eligible:
                valid.append(ref)
            else:
                ident, error = resolve(ref)
                if error or ident is not None:
                    errors.append(
                        f"Evidence binding for {intent.intent_id}: "
                        + (error or f"Use the exact result_id instead of {label(ref)}.")
                    )
        # Evidence kinds come from actual receipts, not optional model annotations.
        # A mistaken evidence_type hint must not discard a valid result binding.
        rows.append(
            {
                "intent_id": intent.intent_id,
                "request": intent.description,
                "status": intent.status,
                "proposed_answer": d.get("answer", args.get("answer", "")),
                "evidence": [{"result_id": r, "kind": evidence_kind(eligible[r])} for r in valid],
                **({"limitations": limitations} if limitations else {}),
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
    return EvidenceAssessment(
        rows, tuple(dict.fromkeys(refs)), tuple(errors), tuple(dict.fromkeys(extras))
    )


def normalize_proposal_args(args: dict[str, Any]) -> dict[str, Any]:
    """Default omitted selections; inferred table references still require validation."""
    normalized = dict(args)
    normalized.setdefault("capability_refs", [])
    if "evidence" not in normalized:
        tables = normalized.get("tables")
        normalized["evidence"] = list(
            dict.fromkeys(
                table["result_id"]
                for table in (tables if isinstance(tables, list) else [])
                if isinstance(table, dict) and isinstance(table.get("result_id"), str)
            )
        )
    return normalized


def validate_proposal_args(args: Any) -> str | None:
    from jsonschema import Draft202012Validator

    from data_agent.runtime.mcp.tool_schema import FINALIZE_ANSWER_SCHEMA

    error = next(Draft202012Validator(FINALIZE_ANSWER_SCHEMA["parameters"]).iter_errors(args), None)
    return ("Invalid finalizeAnswer arguments: " + error.message[:400]) if error else None


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
