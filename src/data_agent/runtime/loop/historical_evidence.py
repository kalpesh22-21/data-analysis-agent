"""Bind explicitly cited earlier-turn evidence without pretending to execute it again.

A finalizer that cites a result from an earlier turn is not sent back to run it again: the
result is bound as a current-turn reference, labelled as a reused snapshot with its
original retrieval time, provided the earlier turn's answer was delivered and the receipt
is still eligible (`previous_result_reference`). The result need not have been shown in
that answer; an evidence-only result of a delivered answer was reviewed with it.
"""

import re
from copy import deepcopy
from dataclasses import replace

from data_agent.runtime.composite.analysis_state import (
    AnalysisStateRejectedError,
    excluded_result_ids,
    previous_result_reference,
    resolve_previous_result,
)
from data_agent.runtime.composite.answer_with_table import blueprint_run_from_result
from data_agent.runtime.context import scope_filter
from data_agent.runtime.session.models import RECOVERY_BINDING

from .proposal import evidence_kind


async def bind_historical_evidence(
    store, session_id, turn_index, column_scope, arguments, *, bound_by=None
):
    doc = await store.get_or_create_session(session_id)
    # Both the delivered answer and its original receipt must remain authorized.
    excluded = excluded_result_ids(doc, turn_index)
    delivered = set()
    delivered_turns = set()
    source_sql = {}
    for message in scope_filter.filter_messages(doc.messages, column_scope):
        if (
            message.turn_index < turn_index
            and message.role == "assistant"
            and not message.failure
            and (message.review or {}).get("status") in {"approved", "disabled"}
        ):
            # Only a reviewed final answer vouches for its turn's other results: an askUser
            # pause is also persisted as approved, and a decline delivered none of them.
            if (
                message.answer_document
                and (message.review or {}).get("site") != "ask_user"
                and message.ship_disposition != "decline_only"
            ):
                delivered_turns.add(message.turn_index)
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
        if entry.tool_call_id not in excluded
    }
    for entry in scoped.values():
        if (
            entry.turn_index >= turn_index
            or entry.tool_call_id not in references
            or (entry.tool_call_id not in delivered and entry.turn_index not in delivered_turns)
            or evidence_kind(entry) != "warehouse"
        ):
            continue
        try:
            source = resolve_previous_result(
                entry, scoped.values(), turn_index, scope_filter.compute_scope_hash(column_scope)
            )
            reference = previous_result_reference(
                source,
                turn_index,
                scope_filter.compute_scope_hash(column_scope),
            )
        except AnalysisStateRejectedError:
            # Leave an ineligible reference unchanged for ordinary grounding rejection.
            continue
        target = reference.tool_call_id
        if entry.tool_call_id not in source_sql:
            # Not shown in a delivered answer, so its SQL is read from the receipt itself.
            sql = await _executed_sql(store, session_id, source)
            if sql is None:
                continue
            source_sql[entry.tool_call_id] = sql
        mapping[entry.tool_call_id] = target
        if target not in existing:
            if bound_by:
                reference = replace(
                    reference, model_response={**reference.model_response, "bound_by": bound_by}
                )
            await store.append_trail_entry(session_id, reference)
            existing.add(target)
    # A reference updateAnalysisState(reuse_previous_result=true) already bound into this
    # turn is cited as is, but a reused blueprint carries no inline SQL: resolve its executed
    # SQL from the same authorized origin, never by running anything.
    current_sql = {}
    for ref in references:
        entry = scoped.get(ref)
        origin = (entry.model_response or {}).get("reused_from_result_id") if entry else None
        if entry is None or entry.turn_index != turn_index or not origin:
            continue
        sql = source_sql.get(origin) or await _executed_sql(store, session_id, entry)
        if sql:
            current_sql[ref] = sql
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
    return result, {
        **{target: source_sql[ref] for ref, target in mapping.items() if ref in source_sql},
        **current_sql,
    }


async def _executed_sql(store, session_id, source):
    """The SQL a receipt executed: a query's own SQL, or a verified blueprint's terminal SQL."""
    if source.tool_name == "runQuery":
        sql = source.args.get("sql")
        return sql if isinstance(sql, str) and sql.strip() else None
    if not source.result_full_ref:
        return None
    try:
        full = await store.read_full_result(session_id, source.result_full_ref)
    except Exception:  # noqa: BLE001 - a store failure means "not reusable", never a failed turn
        return None
    captured = blueprint_run_from_result(full)
    return captured[1].terminal_sql if captured else None


# Recovery offers the latest answer plus the earlier answers that share the most words
# with this request, so "the time-off ranking from earlier" can still reach its turn.
_RECOVERY_EXTRA_ANSWERS = 2
_RECOVERY_MAX_RESULTS = 6
_WORD = re.compile(r"[a-z0-9]+")
_COMMON_WORDS = frozenset({
    "the", "and", "for", "from", "with", "that", "this", "these", "those", "show", "what",
    "which", "how", "many", "much", "are", "was", "were", "has", "have", "had", "you", "your",
    "all", "any", "each", "use", "using", "used", "earlier", "previous", "prior", "same",
    "saved", "result", "results", "answer", "again", "without", "refresh", "refreshing",
    "them", "please", "into", "only", "not", "but", "can", "its", "their", "then", "than",
    "also", "just", "give", "tell", "list", "return", "shown", "showed", "now",
})


def _words(*texts):
    words = {w for t in texts if isinstance(t, str) for w in _WORD.findall(t.lower())}
    return {w[:-1] if len(w) > 4 and w.endswith("s") else w
            for w in words if len(w) > 2 and w not in _COMMON_WORDS}


async def bind_recovery_history(store, session_id, turn_index, column_scope):
    """Offer earlier delivered sources when recovery has no new data.

    The latest delivered answer is always offered, then up to two earlier answers whose
    request, takeaway and section titles share words with this turn's request. This is
    a candidate set, not approval for the new request: the recovery judge must assess
    relevance, receipt timestamps and historical labels are preserved, and candidates
    the recovered answer does not cite stay hidden from later context.
    """
    doc = await store.get_or_create_session(session_id)
    messages = sorted((
        m for m in scope_filter.filter_messages(doc.messages, column_scope)
        if m.turn_index < turn_index and m.role == "assistant" and not m.failure
        and (m.review or {}).get("status") in {"approved", "disabled"}
        and (m.answer_document or {}).get("sources")
    ), key=lambda m: m.turn_index, reverse=True)
    if not messages:
        return {}
    requests = {}
    for m in doc.messages:
        if m.role == "user":
            requests.setdefault(m.turn_index, []).append(m.content)
    asked = _words(*requests.get(turn_index, ()))

    def overlap(message):
        document = message.answer_document
        return len(asked & _words(*requests.get(message.turn_index, ()), document.get("takeaway"),
                                  *(s.get("title") for s in document.get("sections", []))))

    # A stable sort keeps the more recent answer first among equal overlaps.
    related = [m for m in sorted(messages[1:], key=overlap, reverse=True) if overlap(m)]
    refs = []
    for message in [messages[0], *related[:_RECOVERY_EXTRA_ANSWERS]]:
        document = message.answer_document
        refs += [ref for section in document.get("sections", [])
                 for ref in section.get("evidence_refs", []) if ref in document["sources"]]
    refs = list(dict.fromkeys(refs))[:_RECOVERY_MAX_RESULTS]
    _, sql = await bind_historical_evidence(
        store, session_id, turn_index, column_scope, {"evidence": refs}, bound_by=RECOVERY_BINDING
    )
    return sql
