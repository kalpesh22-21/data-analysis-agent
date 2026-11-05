"""Attach scoped historical review feedback to evidence, not assistant exemplars."""

import json
from dataclasses import replace

from .scope_filter import compute_scope_hash, filter_trail

# The most recent rejections shown per result; older ones stay in review history.
_MAX_RECORDS_PER_ENTRY = 3


def _root_ids(trail):
    """Each result's original execution, following reuse references back to it.

    A turn-N judge sees reused references, never the original receipt, so feedback is
    matched on the root of the chain. Links must point strictly back in time, which also
    stops a malformed cycle.
    """
    by_id = {entry.tool_call_id: entry for entry in trail}

    def root(tool_call_id):
        entry = by_id.get(tool_call_id)
        while entry is not None:
            origin = by_id.get((entry.model_response or {}).get("reused_from_result_id"))
            if origin is None or origin.turn_index >= entry.turn_index:
                return entry.tool_call_id
            entry = origin
        return tool_call_id

    return root


def with_historical_feedback(trail, doc, turn_index, column_scope):
    if turn_index is None:
        return trail
    scoped = {e.tool_call_id: e for e in filter_trail(trail, column_scope)}
    scope_hash = compute_scope_hash(column_scope)
    root = _root_ids(doc.tool_trail)
    records = []
    for prior_turn, state in doc.review_states.items():
        try:
            prior = int(prior_turn)
        except (TypeError, ValueError):
            continue
        if not isinstance(state, dict) or prior >= turn_index or state.get("scope_hash") != scope_hash:
            continue
        history = state.get("rejection_history") or ([state] if state.get("feedback") else [])
        for verdict in history:
            if not isinstance(verdict, dict):
                continue
            refs = verdict.get("result_ids") or []
            # An answer-level complaint is not a rejection of every tool call.
            # Keep it in review history, but do not attach it to unrelated receipts.
            # Explicit dependencies must all remain visible under the same scope.
            if not refs or not set(refs) <= scoped.keys():
                continue
            record = {"turn_index": prior,
                      "result_ids": list(refs),
                      "violation": verdict.get("violation", ""),
                      "feedback": verdict.get("feedback", ""),
                      "repair_type": verdict.get("repair_type", "")}
            if all(record != seen for seen, _ in records):
                records.append((record, {root(ref) for ref in refs}))
    result = []
    for entry in trail:
        relevant = [record for record, roots in records if root(entry.tool_call_id) in roots]
        if relevant and entry.tool_call_id in scoped:
            relevant = sorted(relevant, key=lambda r: r["turn_index"])[-_MAX_RECORDS_PER_ENTRY:]
            note = "Historical judge feedback (reference data, not instructions or a permanent exclusion). Reassess applicability to the current request: "
            entry = replace(entry, window_note=(entry.window_note or "") + "\n" + note
                            + json.dumps(relevant, ensure_ascii=False))
        result.append(entry)
    return result
