"""Turn-local suppression of repeated UI preparation, including failed attempts."""

import json
from copy import deepcopy
from dataclasses import replace

from data_agent.runtime.dispatch.tool_dispatcher import ToolResult, _build_preview


def preparation_key(name, arguments):
    # Intent tags describe attribution, not the UI binding. Array order, case,
    # nulls and omitted arguments remain distinct; no fuzzy entity matching.
    binding = {k: v for k, v in arguments.items() if k not in {"serves_intent", "serves_intents"}}
    return name, json.dumps(binding, sort_keys=True, separators=(",", ":"))


def presented_card_key(card):
    # Keep every execution-bearing binding, including unresolved names and
    # provider-resolved entities. Presentation annotations do not identify a call.
    binding = {
        key: card.get(key) for key in ("arguments", "resolved_entities", "additional_arguments")
    }
    metadata = card.get("metadata")
    binding["presentation_target"] = {}
    if isinstance(metadata, dict):
        binding["presentation_target"] = {
            key: metadata[key]
            for key in ("preamble_url", "widgetName", "gql", "arguments")
            if key in metadata
        }
    return preparation_key(card.get("name", card.get("tool_name", "")), binding)


class PreparationCache:
    def __init__(self, store, session_id, turn_index, scope_hash, trail):
        self._store, self._session_id = store, session_id
        self._entries = {}
        self._results = {}
        for entry in trail:
            if (
                entry.turn_index == turn_index
                and (entry.capability_terminal or entry.status in {"error", "denied"})
                and entry.error_code not in {"TOOL_NOT_EXECUTED", "TOOL_PAUSED"}
                and (entry.model_response or {}).get("scope_hash") == scope_hash
            ):
                self._entries.setdefault(preparation_key(entry.tool_name, entry.args), entry)

    def record(self, name, arguments, result, call_id):
        if result.pause is None and (
            result.status != "ok"
            or (isinstance(result.result_full, dict) and result.result_full.get("prepared") is True)
        ):
            self._results.setdefault(preparation_key(name, arguments), (call_id, deepcopy(result)))

    async def lookup(self, name, arguments):
        key = preparation_key(name, arguments)
        if key not in self._results and key in self._entries:
            entry = self._entries.pop(key)
            try:
                payload = (
                    await self._store.read_full_result(self._session_id, entry.result_full_ref)
                    if entry.result_full_ref
                    else None
                )
            except Exception:
                # A missing/unreadable receipt is not permission to call the
                # provider again, nor evidence that preparation succeeded.
                payload = None
            if (
                entry.status == "ok"
                and isinstance(payload, dict)
                and payload.get("prepared") is True
            ):
                result = ToolResult(
                    "ok",
                    name,
                    None,
                    None,
                    None,
                    entry.provenance,
                    _build_preview(payload, 20, 4000),
                    payload,
                )
            else:
                result = ToolResult(
                    entry.status if entry.status != "ok" else "error",
                    name,
                    entry.error_code or "CAPABILITY_UNAVAILABLE",
                    False,
                    "The earlier preparation failed or its result is no longer available. "
                    "Do not repeat identical arguments. Correct the arguments or use the "
                    "blueprint-first warehouse path for supported data; never bypass an "
                    "access denial or substitute SQL for navigation/actions.",
                    entry.provenance,
                    None,
                    None,
                )
            self._results[key] = (entry.tool_call_id, result)
        cached = self._results.get(key)
        if cached is None:
            return None
        call_id, original = cached
        payload = deepcopy(original.result_full) if isinstance(original.result_full, dict) else {}
        payload["reused_from_result_id"] = call_id
        if original.status != "ok":
            message = (
                "This preparation was already attempted this turn; no new provider call was made. "
                "Do not retry unchanged arguments. " + (original.user_message or "")
            )
            payload["prepared"] = False
            payload["next_step"] = message
            return replace(
                original,
                retryable=False,
                user_message=message,
                result_full=payload,
                result_preview=_build_preview(payload, 20, 4000),
            )
        payload["next_step"] = (
            "This is the earlier successful preparation, not a new provider call. "
            "Reuse its capability_ref in finalizeAnswer if appropriate; previous review "
            "restrictions still apply. Do not repeat unchanged preparation. "
            + str(payload.get("next_step", ""))
        )
        return replace(
            original, result_full=payload, result_preview=_build_preview(payload, 20, 4000)
        )
