"""Request-local review budget and typed dependency failures at the loop boundary."""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError

from .answer_judge import APPROVED
from .proposal import ReviewState, fingerprint


@dataclass
class DeliveryContext:
    credentials: object
    review_seconds: float = 30.0
    turn_index: int | None = None


CURRENT_DELIVERY: ContextVar[DeliveryContext | None] = ContextVar("delivery", default=None)


class DependencyFailureError(Exception):
    def __init__(self, dependency, reason, retryable):
        super().__init__(reason)
        self.dependency, self.reason, self.retryable = dependency, reason, retryable

    def to_dict(self):
        return {
            "code": "DEPENDENCY_FAILURE",
            "dependency": self.dependency,
            "reason": self.reason,
            "retryable": self.retryable,
        }


def dependency_failure(exc, dependency):
    if isinstance(exc, (httpx.TimeoutException, APITimeoutError, TimeoutError)):
        return DependencyFailureError(dependency, "timeout", True)
    if isinstance(exc, (httpx.TransportError, APIConnectionError)):
        return DependencyFailureError(dependency, "connection", True)
    if isinstance(exc, (httpx.HTTPStatusError, APIStatusError)):
        status = exc.response.status_code
        reason = (
            "access_denied"
            if status in {401, 403}
            else "configuration"
            if status in {400, 404, 422}
            else "service"
        )
        return DependencyFailureError(dependency, reason, status in {408, 429} or status >= 500)
    return None


def delivery_boundary(method):
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        context = DeliveryContext(kwargs["credentials"])
        token = CURRENT_DELIVERY.set(context)
        try:
            try:
                return await method(self, *args, **kwargs)
            except DependencyFailureError as failure:
                if context.turn_index is None:
                    raise
                from .turn_accumulators import TurnAccumulators

                self._observer("loop_dependency_failed", failure.to_dict())
                # No raw exception text or unreviewed partial artifacts are exposed.
                return await self._finish(
                    session_id=kwargs["session_id"],
                    turn_index=context.turn_index,
                    status="done",
                    exit_label="runtime_fallback",
                    tool_calls_made=0,
                    assistant_text="I couldn't complete this request because a required service failed. "
                    + (
                        "Please try again."
                        if failure.retryable
                        else "The service configuration or access needs attention."
                    ),
                    accum=TurnAccumulators(),
                    provenance=frozenset(),
                    failure=failure.to_dict(),
                )
        finally:
            CURRENT_DELIVERY.reset(token)

    return wrapped


async def review_once(loop, brief):
    import asyncio

    context = CURRENT_DELIVERY.get()
    available = context.review_seconds if context else 30.0
    if available <= 0:
        return APPROVED
    started = time.monotonic()
    try:
        return await asyncio.wait_for(
            loop._answer_judge.review(brief),
            timeout=min(available, getattr(loop._answer_judge, "timeout_seconds", 30.0)),
        )
    finally:
        if context:
            context.review_seconds = max(0.0, context.review_seconds - (time.monotonic() - started))


def delivery_version(text, accum, pending=None):
    return fingerprint(
        {
            "text": text or "",
            "tables": [t.to_doc() for t in accum.answer_tables],
            "cards": accum.capability_cards,
            "assumptions": accum.assumptions,
            "pending": pending,
        }
    )


def partial_delivery_text(doc, turn_index, accum):
    """Describe missing deliveries without claiming a prepared option was displayed."""
    from data_agent.runtime.session.models import live_analysis_state

    state = live_analysis_state(doc, turn_index)
    if not state:
        # Component labels describe what is actually included, not invented intent
        # bindings or a claim that a prepared widget has verified its data.
        lines = []
        for index, card in enumerate(accum.capability_judge_context, 1):
            label = card.get("description") or f"Requested view {index}"
            lines.append(f"{label}: The prepared view is included.")
        for index, table in enumerate(accum.answer_tables, 1):
            lines.append(f"{table.caption or f'Requested table {index}'}: The result is shown.")
        if lines:
            lines.append("Other requested parts could not be verified for this answer.")
        return "\n".join(lines)
    if len(state.intents) < 2:
        return ""
    trail = {e.tool_call_id: e for e in doc.tool_trail if e.turn_index == turn_index}
    lines = []
    for intent in state.intents:
        entry = trail.get(intent.evidence_tool_call_id)
        delivered = False
        if entry:
            if entry.capability_terminal:
                delivered = any(
                    c.get("name") == entry.tool_name for c in accum.capability_cards or ()
                )
            else:
                sql = accum.result_sql_by_call_id.get(entry.tool_call_id)
                delivered = bool(sql and any(t.sql == sql for t in accum.answer_tables))
        outcome = (
            "The result is shown." if delivered else "This part was not completed for display."
        )
        lines.append(f"{intent.description}: {outcome}")
    return "\n".join(lines)


async def review_delivery(loop, session_id, turn_index, text, accum, checkpoint):
    """Review the exact delivered content; primary approved proposals reuse their receipt."""
    context = CURRENT_DELIVERY.get()
    if context is None or loop._answer_judge is None:
        return {"status": "disabled"}, False
    if not getattr(loop._answer_judge, "enabled", True):
        return {"status": "disabled"}, True
    from data_agent.runtime.context.scope_filter import compute_scope_hash
    from data_agent.runtime.session.models import live_analysis_state

    doc = await loop._session_store.get_or_create_session(session_id)
    state = ReviewState.restore(
        doc.review_states.get(str(turn_index)), compute_scope_hash(context.credentials.column_scope)
    )
    pending = checkpoint.pending_question if checkpoint else None
    version = delivery_version(text, accum, pending)
    if (
        state.delivery_version == version
        and state.delivery_status == "approved"
        and (not state.violation or pending and state.site != "ask_user")
    ):
        return {"status": "approved"}, False
    site = (
        "ask_user"
        if pending
        else "exit_capability"
        if accum.capability_cards
        else "exit_table"
        if accum.has_answer_tables
        else "exit_prose"
    )
    questions = [m.content for m in doc.messages if m.role == "user" and m.turn_index == turn_index]
    calls = 0
    question_version = fingerprint(
        {
            "question": (pending or {}).get("question", ""),
            "options": (pending or {}).get("options") or [],
        }
    )
    # A refusal from a proposal remains binding even if a fallback's prose is approved.
    outstanding = bool(state.violation) and not pending
    if outstanding:
        loop._observer("loop_delivery_review", {"site": site, "status": "rejected", "calls": 0})
        return {"status": "rejected"}, True
    status = "rejected" if outstanding or bool(pending and state.question_refusals) else "exhausted"
    for _ in range(2):
        if context.review_seconds <= 0:
            break
        calls += 1
        try:
            results, anchor, trail = await loop._judge_results(
                session_id, turn_index, context.credentials.column_scope
            )
            brief = loop._judge_brief(
                site,
                question=questions[0] if questions else "",
                accum=accum,
                analysis_state=live_analysis_state(doc, turn_index),
                draft=text or "",
                results=results,
                date_anchor=anchor,
                designated_tables=tuple((t.caption, t.sql) for t in accum.answer_tables),
                pending_question=(pending or {}).get("question", ""),
                pending_options=tuple((pending or {}).get("options") or ()),
            )
            from .proposal import selected_components

            current_trail = [e for e in trail if e.turn_index == turn_index]
            table_ids = [
                ident
                for ident, sql in accum.result_sql_by_call_id.items()
                if any(t.sql == sql for t in accum.answer_tables)
            ]
            components = selected_components(
                {
                    "capability_refs": [c.get("name") for c in accum.capability_cards or ()],
                    "tables": [{"result_id": ident} for ident in table_ids],
                },
                current_trail,
                accum.result_sql_by_call_id,
            )
            brief = replace(
                brief,
                clarification_answers=tuple(questions[1:]),
                selected_components=tuple(components),
            )
            from .judge_evidence import enrich_brief

            brief = await enrich_brief(
                brief,
                trail=trail,
                turn_index=turn_index,
                session_id=session_id,
                store=loop._session_store,
                catalog_provider=loop._judge_catalog,
                credentials=context.credentials,
                analysis_state=live_analysis_state(doc, turn_index),
            )
            verdict = await review_once(loop, brief)
        except Exception:
            loop._observer("loop_answer_judge_failed", {"reason": "delivery_review_failed"})
            continue
        if not verdict.approved:
            if pending:
                state.question_refusals[question_version] = verdict.violation
            else:
                state.site = site
                state.reject(verdict, version, accum.assumptions)
            status = "rejected"
            break
        if verdict.reviewed:
            status = "rejected" if outstanding else "approved"
            if pending:
                state.question_refusals.clear()
            break
    state.delivery_version, state.delivery_status = version, status
    await loop._session_store.write_review_state(session_id, turn_index, state.to_doc())
    loop._observer("loop_delivery_review", {"site": site, "status": status, "calls": calls})
    return {"status": status, "attempts": calls}, status == "rejected"
