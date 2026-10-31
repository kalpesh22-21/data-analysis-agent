"""Isolated refresh work and atomic publication of a reviewed answer revision."""

import json
from copy import deepcopy
from uuid import uuid4

from data_agent.runtime.context.scope_filter import filter_messages

from .memory_store import InMemorySessionStore
from .models import TrailEntry, TurnMessage
from .store import CASMismatchError


class RefreshRejectedError(Exception):
    """The previous answer remains intact; no refreshed document can be published."""


class RefreshAccessChangedError(RefreshRejectedError):
    """The UI must reload scope-filtered history before displaying this answer."""


def find_answer(doc, answer_id, scope):
    for message in reversed(filter_messages(doc.messages, scope)):
        if (
            message.role == "assistant"
            and (message.answer_document or {}).get("answer_id") == answer_id
        ):
            return message
    raise PermissionError("The answer is unavailable under the current access scope.")


def apply_refresh(doc, *, answer_id, expected_revision, message, trail):
    """Called inside the store's CAS mutation; never overwrite another refresh."""
    for index in range(len(doc.messages) - 1, -1, -1):
        current = doc.messages[index]
        saved = current.answer_document or {}
        if saved.get("answer_id") != answer_id:
            continue
        if saved.get("revision") != expected_revision:
            raise CASMismatchError("This answer has changed. Reload before refreshing again.")
        document = message.answer_document or {}
        if (
            document.get("answer_id") != answer_id
            or document.get("revision") != expected_revision + 1
            or message.turn_index != current.turn_index
            or (message.review or {}).get("status") != "approved"
        ):
            raise ValueError("Refresh publication requires the next reviewed answer revision.")
        doc.messages[index] = deepcopy(message)
        doc.tool_trail.extend(deepcopy(trail))
        return
    raise CASMismatchError("The answer is no longer available.")


class RefreshStore(InMemorySessionStore):
    """Conversation writes are private; result blobs use fresh IDs and normal TTLs."""

    def __init__(self, store, original, target):
        super().__init__()
        self.backing = store
        self.prefix = "refresh_" + uuid4().hex + "_"
        seed = deepcopy(original)
        seed.messages = [m for m in seed.messages if m.turn_index < target.turn_index]
        seed.tool_trail = [e for e in seed.tool_trail if e.turn_index < target.turn_index]
        seed.pause_checkpoint = None
        seed.analysis_state = None
        seed.review_states = {}
        self._docs[seed.session_id] = seed
        self.turn_index = target.turn_index

    async def write_full_result(self, session_id, result_id, result_full):
        return await self.backing.write_full_result(
            session_id, self.prefix + result_id, result_full
        )

    async def read_full_result(self, session_id, result_full_ref):
        return await self.backing.read_full_result(session_id, result_full_ref)

    async def publication(self, session_id, answer_id, expected_revision):
        staged = await self.get_or_create_session(session_id)
        message = staged.messages[-1]
        if (
            message.role != "assistant"
            or message.turn_index != self.turn_index
            or (message.review or {}).get("status") != "approved"
            or not message.answer_document
        ):
            raise RefreshRejectedError(
                "Refresh did not produce a reviewed answer. The saved answer is unchanged."
            )
        entries = [e for e in staged.tool_trail if e.turn_index == self.turn_index]
        ids = {e.tool_call_id: self.prefix + e.tool_call_id for e in entries}

        def remap(value, field=None):
            # Rewrite reference fields only; a business value may equal a tool ID.
            if field in {"result_preview", "prepared_actions", "slot_bindings"}:
                return deepcopy(value)
            if isinstance(value, str):
                if field in {
                    "tool_call_id",
                    "result_id",
                    "answer_result_id",
                    "evidence",
                    "evidence_refs",
                }:
                    return ids.get(value, value)
                return value
            if isinstance(value, list):
                return [remap(v, field) for v in value]
            if isinstance(value, dict):
                result = {
                    ids.get(k, k) if field == "sources" else k: remap(v, k)
                    for k, v in value.items()
                }
                if field == "tool_calls":
                    result["id"] = ids.get(result.get("id"), result.get("id"))
                    function = result.get("function", {})
                    if isinstance(function.get("arguments"), str):
                        try:
                            function["arguments"] = json.dumps(
                                remap(json.loads(function["arguments"]))
                            )
                        except ValueError:
                            pass
                return result
            return value

        message = TurnMessage.from_doc(remap(message.to_doc()))
        document = message.answer_document
        document.update(answer_id=answer_id, revision=expected_revision + 1)
        trail = [TrailEntry.from_doc(remap(e.to_doc())) for e in entries]
        return message, trail
