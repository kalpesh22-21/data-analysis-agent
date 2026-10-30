"""Resolve document handles against persisted approvals and the caller's current scope."""

from copy import deepcopy

from data_agent.runtime.context.scope_filter import filter_messages, filter_trail

_UNAVAILABLE = "The selected answer data is unavailable."


def _messages(doc, credentials):
    return [
        m
        for m in filter_messages(doc.messages, credentials.column_scope)
        if m.role == "assistant" and m.answer_document
    ]


async def resolve_table(doc, table_ref, credentials, store):
    # Visibility and explicit designation are checked before any result dereference.
    selected = None
    for message in reversed(_messages(doc, credentials)):
        document = message.answer_document
        for rid, source in document.get("sources", {}).items():
            if source.get("table_ref") != table_ref:
                continue
            if any(
                section.get(field, {}).get("result_id") == rid
                for section in document.get("sections", [])
                for field in ("supporting_table", "fallback_table")
            ) or any(
                section.get("presentation", {}).get("kind") == "table"
                and section["presentation"].get("result_id") == rid
                for section in document.get("sections", [])
            ):
                selected = (message.turn_index, rid)
                break
        if selected:
            break
    if selected is None:
        raise PermissionError(_UNAVAILABLE)
    for entry in filter_trail(doc.tool_trail, credentials.column_scope, current_turn_index=None):
        if (
            (entry.turn_index, entry.tool_call_id) != selected
            or entry.status != "ok"
            or entry.error_code
        ):
            continue
        if entry.tool_name == "runQuery" and isinstance(entry.args.get("sql"), str):
            sql = entry.args["sql"]
            return {"sql": sql, "column_formats": deepcopy(source.get("column_formats", {}))}
        if entry.tool_name == "runBlueprint" and entry.authoritative and entry.result_full_ref:
            full = await store.read_full_result(credentials.session_id, entry.result_full_ref)
            if isinstance(full, dict) and isinstance(full.get("terminal_sql"), str):
                sql = full["terminal_sql"]
                return {"sql": sql, "column_formats": deepcopy(source.get("column_formats", {}))}
    raise PermissionError(_UNAVAILABLE)


def resolve_action(doc, answer_id, capability_ref, credentials):
    for message in reversed(_messages(doc, credentials)):
        document = message.answer_document
        if document.get("answer_id") != answer_id:
            continue
        if not any(
            action.get("capability_ref") == capability_ref
            for section in document.get("sections", [])
            for action in section.get("actions", [])
        ):
            break
        for card in message.prepared_actions or []:
            if card.get("name") == capability_ref:
                return deepcopy(card)
    raise PermissionError("The prepared action is unavailable.")
