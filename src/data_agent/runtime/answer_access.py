"""Resolve document handles against persisted approvals and the caller's current scope."""

from copy import deepcopy

from data_agent.runtime.context.scope_filter import filter_messages


def _messages(doc, credentials):
    return [
        m
        for m in filter_messages(doc.messages, credentials.column_scope)
        if m.role == "assistant" and m.answer_document
    ]


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
