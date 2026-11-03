"""Bind explicitly cited delivered evidence without pretending to execute it again."""

from copy import deepcopy

from data_agent.runtime.composite.analysis_state import (
    AnalysisStateRejectedError,
    previous_result_reference,
)
from data_agent.runtime.context import scope_filter

from .proposal import evidence_kind


async def bind_historical_evidence(store, session_id, turn_index, column_scope, arguments):
    doc = await store.get_or_create_session(session_id)
    # Both the delivered answer and its original receipt must remain authorized.
    delivered = set()
    source_sql = {}
    for message in scope_filter.filter_messages(doc.messages, column_scope):
        if (
            message.turn_index < turn_index
            and message.role == "assistant"
            and not message.failure
            and (message.review or {}).get("status") in {"approved", "disabled"}
        ):
            document = message.answer_document or {}
            cited = {
                ref
                for section in document.get("sections", [])
                for ref in section.get("evidence_refs", [])
            }
            delivered.update(cited & document.get("sources", {}).keys())
            source_sql.update(
                {
                    ref: source.get("sql")
                    for ref, source in document.get("sources", {}).items()
                    if ref in cited and isinstance(source.get("sql"), str)
                }
            )
    references = set(arguments.get("evidence", []))
    for section in arguments.get("sections", []):
        references.update(section.get("evidence_refs", []))
    for deliverable in arguments.get("deliverables", []):
        references.update(deliverable.get("result_ids", []))
    mapping = {}
    existing = {entry.tool_call_id for entry in doc.tool_trail}
    scoped = {
        entry.tool_call_id: entry
        for entry in scope_filter.filter_trail(tuple(doc.tool_trail), column_scope)
    }
    for entry in scoped.values():
        if (
            entry.turn_index >= turn_index
            or entry.tool_call_id not in references & delivered
            or evidence_kind(entry) != "warehouse"
        ):
            continue
        origin = (entry.model_response or {}).get("reused_from_result_id")
        source = scoped.get(origin) if origin in delivered else entry
        try:
            reference = previous_result_reference(
                source,
                turn_index,
                scope_filter.compute_scope_hash(column_scope),
            )
        except AnalysisStateRejectedError:
            # Leave an ineligible reference unchanged for ordinary grounding rejection.
            continue
        target = reference.tool_call_id
        mapping[entry.tool_call_id] = target
        if target not in existing:
            await store.append_trail_entry(session_id, reference)
            existing.add(target)
    # Only schema-defined reference positions, never prose/SQL/column names.
    result = deepcopy(arguments)
    result["evidence"] = [mapping.get(ref, ref) for ref in result.get("evidence", [])]
    for section in result.get("sections", []):
        section["evidence_refs"] = [
            mapping.get(ref, ref) for ref in section.get("evidence_refs", [])
        ]
        presentation = section.get("presentation", {})
        if "result_id" in presentation:
            presentation["result_id"] = mapping.get(
                presentation["result_id"], presentation["result_id"]
            )
    for table in result.get("tables", []):
        if "result_id" in table:
            table["result_id"] = mapping.get(table["result_id"], table["result_id"])
    for deliverable in result.get("deliverables", []):
        deliverable["result_ids"] = [
            mapping.get(ref, ref) for ref in deliverable.get("result_ids", [])
        ]
    return result, {target: source_sql[ref] for ref, target in mapping.items() if ref in source_sql}
