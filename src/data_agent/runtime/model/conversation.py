"""Rebuild original response batches without widening the scope-filtered transcript."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from .client import ModelTurnResult


def assign_unique_call_ids(
    result: ModelTurnResult,
    used_ids: set[str],
    observer: Callable[[str, dict[str, Any]], None] | None = None,
) -> ModelTurnResult:
    """Give every execution a unique receipt ID, even when a provider reuses IDs.

    Reserve IDs from this whole batch before generating replacements. References
    in arguments still name previously received receipts and must not be rewritten.
    """
    reserved = used_ids | {call.id for call in result.tool_calls}
    calls = []
    changed = False
    for call in result.tool_calls:
        if not call.id or call.id in used_ids:
            original = call.id
            base = original or "call"
            suffix = 1
            ident = f"{base}#{suffix}"
            while ident in reserved:
                suffix += 1
                ident = f"{base}#{suffix}"
            reserved.add(ident)
            call = replace(call, id=ident)
            changed = True
            if observer is not None:
                observer(
                    "loop_tool_call_id_reminted",
                    {
                        "old_id": original,
                        "new_id": ident,
                        "tool_name": call.name,
                        "reason": "collision" if original else "empty_id",
                    },
                )
        used_ids.add(call.id)
        calls.append(call)
    # Preserve opaque reasoning verbatim. Evidence arguments continue to reference
    # original receipts; neither opaque text nor arguments are safe to rewrite.
    return replace(result, tool_calls=calls) if changed else result


def conversation_call_ids(messages: list[dict[str, Any]]) -> set[str]:
    """Reserve synthetic, assistant-side and tool-side IDs, including withheld pairs."""
    ids = set()
    for message in messages:
        if isinstance(message.get("tool_call_id"), str):
            ids.add(message["tool_call_id"])
        for call in message.get("tool_calls", []) or []:
            if isinstance(call.get("id"), str):
                ids.add(call["id"])
    return ids


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
