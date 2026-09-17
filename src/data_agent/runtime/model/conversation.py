"""Rebuild original response batches without widening the scope-filtered transcript."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from .client import ModelTurnResult


def assign_unique_call_ids(result: ModelTurnResult, used_ids: set[str]) -> ModelTurnResult:
    """Give every execution a unique receipt ID, even when a provider reuses IDs.

    Reserve IDs from this whole batch before generating replacements. References
    in arguments still name previously received receipts and must not be rewritten.
    """
    reserved = used_ids | {call.id for call in result.tool_calls}
    calls = []
    changed = False
    for call in result.tool_calls:
        if not call.id or call.id in used_ids:
            ident = "call_" + uuid.uuid4().hex
            while ident in reserved:
                ident = "call_" + uuid.uuid4().hex
            reserved.add(ident)
            call = replace(call, id=ident)
            changed = True
        used_ids.add(call.id)
        calls.append(call)
    # Opaque provider metadata may refer to original IDs; don't replay it with
    # renamed calls. Ordinary responses retain their original metadata.
    return replace(result, tool_calls=calls, reasoning_metadata={}) if changed else result


def restore_response_batches(
    messages: list[dict[str, Any]], *, scope_hash: str, use_reasoning_metadata: bool
) -> list[dict[str, Any]]:
    outputs = {m["tool_call_id"]: m for m in messages if m.get("role") == "tool"}
    envelopes = {
        m["_model_response"]["id"]: m["_model_response"]
        for m in messages
        if isinstance(m.get("_model_response"), dict)
    }
    by_call = {
        call["id"]: envelope
        for envelope in envelopes.values()
        for call in envelope.get("tool_calls", [])
        if call.get("id") in outputs
    }
    restored = []
    emitted = set()
    grouped_ids = set()
    for message in messages:
        envelope = message.get("_model_response")
        if not envelope and message.get("role") == "assistant":
            envelope = next(
                (by_call[c["id"]] for c in message.get("tool_calls", []) if c.get("id") in by_call),
                None,
            )
        if not envelope:
            if message.get("role") == "tool" and message.get("tool_call_id") in grouped_ids:
                continue
            restored.append(message)
            continue
        key = envelope["id"]
        if key in emitted:
            continue
        emitted.add(key)
        calls = envelope.get("tool_calls", [])
        present = [c for c in calls if c.get("id") in outputs]
        grouped_ids.update(c["id"] for c in present)
        # A missing or withheld result can imply narrowed permissions. Never replay
        # opaque reasoning or original prose for only part of its original batch.
        safe = (
            envelope.get("scope_hash") == scope_hash
            and len(present) == len(calls)
            and all("result withheld" not in str(outputs[c["id"]].get("content")) for c in present)
        )
        assistant = {
            "role": "assistant",
            "content": envelope.get("content") if safe else None,
            "tool_calls": present,
        }
        if safe and use_reasoning_metadata:
            assistant.update(
                {
                    k: v
                    for k, v in envelope.get("reasoning_metadata", {}).items()
                    if k in {"reasoning_content", "reasoning", "reasoning_details"}
                }
            )
        if present:
            restored.append(assistant)
            restored.extend(outputs[c["id"]] for c in present)
    return restored
