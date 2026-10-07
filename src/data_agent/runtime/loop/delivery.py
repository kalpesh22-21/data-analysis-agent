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
    review_seconds: float = 90.0
    repair_reserve_seconds: float = 30.0
    turn_index: int | None = None
    terminal_reserve_seconds: float = 0.0
    terminal_review_version: str = ""
    terminal_review_status: str = ""
    partial_review_attempted: bool = False
    review_unavailable: bool = False
    review_failure_reason: str = ""
    resilient_exit: bool = False
    corrected_answer: str | None = None


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
        per_call = getattr(self._answer_judge, "timeout_seconds", 30.0)
        configured = self._answer_judge_review_budget_seconds
        total = configured if configured is not None else 3 * per_call
        context = DeliveryContext(
            kwargs["credentials"],
            review_seconds=total,
            repair_reserve_seconds=min(per_call, total / 3),
            terminal_reserve_seconds=min(per_call, total / 3),
        )
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


async def review_once(loop, brief, *, repair=False, terminal=False):
    import asyncio

    context = CURRENT_DELIVERY.get()
    per_call = getattr(loop._answer_judge, "timeout_seconds", 30.0)
    available = (
        max(
            0.0,
            context.review_seconds
            - (0.0 if terminal else context.terminal_reserve_seconds)
            - (0.0 if repair or terminal else context.repair_reserve_seconds),
        )
        if context
        else per_call
    )
    if available <= 0:
        return APPROVED
    started = time.monotonic()
    try:
        try:
            verdict = await asyncio.wait_for(
                loop._answer_judge.review(brief), timeout=min(available, per_call),
            )
        except TimeoutError:
            verdict = replace(APPROVED, failure_reason="timeout")
        if verdict.failure_reason == "timeout":
            factory = getattr(loop._answer_judge, "timeout_retry", None)
            retry = factory() if callable(factory) else None
            # Borrow unused repair reserve for timeout recovery, but retain the
            # terminal reserve on non-terminal calls. All elapsed time is charged
            # below, once, including a failed retry.
            remaining = (
                context.review_seconds - (0.0 if terminal else context.terminal_reserve_seconds)
                - (time.monotonic() - started)
                if context else getattr(retry, "timeout_seconds", 0.0)
            )
            if retry is not None and remaining > 0:
                loop._observer("loop_answer_judge_timeout_retry", {"site": brief.site})
                try:
                    verdict = await asyncio.wait_for(
                        retry.review(brief), timeout=min(remaining, retry.timeout_seconds),
                    )
                except TimeoutError:
                    verdict = replace(APPROVED, failure_reason="timeout")
            if verdict.failure_reason == "timeout":
                if context:
                    context.review_unavailable = True
                    context.review_failure_reason = "timeout"
                raise TimeoutError("Judge review timed out")
        if context and verdict.approved and not verdict.reviewed:
            context.review_unavailable = True
            context.review_failure_reason = verdict.failure_reason or "unreviewed"
        return verdict
    except Exception as exc:
        if context:
            context.review_unavailable = True
            context.review_failure_reason = (
                "timeout" if isinstance(exc, TimeoutError) else "review_failed"
            )
        raise
    finally:
        if context:
            elapsed = time.monotonic() - started
            context.review_seconds = max(0.0, context.review_seconds - elapsed)
            if terminal:
                context.terminal_reserve_seconds = max(
                    0.0, context.terminal_reserve_seconds - elapsed
                )
            if repair:
                context.repair_reserve_seconds = max(0.0, context.repair_reserve_seconds - elapsed)
            elif not terminal:
                context.repair_reserve_seconds = max(
                    0.0, context.repair_reserve_seconds - max(0.0, elapsed - available)
                )


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
        pending
        and state.delivery_version == version
        and state.delivery_status in {"approved", "rejected", "exhausted"}
    ):
        # The question already passed the askUser repair gate. Re-reviewing it at
        # finish time cannot re-round and used to turn a pause into a generic decline.
        return {"status": state.delivery_status}, False
    if (
        state.delivery_version == version
        and state.delivery_status == "approved"
        and (not state.violation or pending and state.site != "ask_user")
    ):
        return {"status": "approved"}, False
    if (
        not pending
        and state.delivery_version == version
        and state.delivery_status == "exhausted"
        and not state.violation
    ):
        loop._observer("loop_delivery_review", {"status": "exhausted", "calls": 0})
        return {"status": "exhausted"}, False
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
    if not outstanding and not pending and context.terminal_review_version == version:
        state.delivery_version = version
        state.delivery_status = context.terminal_review_status
        await loop._session_store.write_review_state(session_id, turn_index, state.to_doc())
        loop._observer(
            "loop_delivery_review", {"site": site, "status": state.delivery_status, "calls": 1}
        )
        return {"status": state.delivery_status, "attempts": 1}, False
    if outstanding:
        loop._observer("loop_delivery_review", {"site": site, "status": "rejected", "calls": 0})
        return {"status": "rejected"}, True
    if context.resilient_exit and not pending:
        # Salvage already got its bounded opportunity. Do not start another full
        # review of the fallback, or turn unavailable review into an approval.
        loop._observer("loop_delivery_review", {"site": site, "status": "exhausted", "calls": 0})
        return {"status": "exhausted", "attempts": 0}, False
    status = "rejected" if outstanding or bool(pending and state.question_refusals) else "exhausted"
    if context.review_seconds > 0:
        calls = 1
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
                allow_partial_answer=not pending,
                allow_prose_correction=not pending,
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
            # This request is ending (including a pause whose resume gets a new
            # context). No further agent repair can use its reserves, so final
            # delivery may spend the remaining aggregate budget. Prior explicit
            # rejections are still handled above; this is not a new repair round.
            verdict = await review_once(loop, brief, terminal=True)
        except Exception:
            loop._observer("loop_answer_judge_failed", {"reason": "delivery_review_failed"})
        else:
            if not pending and verdict.reviewed:
                from .partial_answer import capture_partial

                snapshot = capture_partial(verdict, brief, accum, current_trail, turn_index)
                if snapshot:
                    state.approved_partial = snapshot
            if not pending and verdict.corrected_answer is not None:
                from .prose_correction import validate_correction

                error = validate_correction(
                    verdict,
                    original=text or "",
                    provenance=await loop._compute_turn_provenance_union(session_id, turn_index),
                    turn_sql=accum.sql_executed,
                    assumptions=accum.assumptions or (),
                    question=questions[0] if questions else "",
                    has_evidence=bool(results or accum.has_answer_tables or accum.capability_cards),
                    declined_clarification=False,
                )
                if error:
                    from .answer_judge import JudgeVerdict

                    verdict = JudgeVerdict(
                        False, "unsupported_by_evidence", error, reviewed=True, repair_type="prose"
                    )
                else:
                    context.corrected_answer = verdict.corrected_answer
                    version = delivery_version(verdict.corrected_answer, accum)
                    loop._observer("loop_answer_judge_prose_corrected", {"site": site})
            if not verdict.approved:
                if pending:
                    state.question_refusals[question_version] = verdict.violation
                else:
                    state.site = site
                    state.reject(verdict, version, accum.assumptions)
                status = "rejected"
            elif verdict.reviewed:
                status = "rejected" if outstanding else "approved"
                if pending:
                    state.question_refusals.clear()
    state.delivery_version, state.delivery_status = version, status
    state.delivery_reason = (
        (context.review_failure_reason or "review_unavailable") if status == "exhausted" else ""
    )
    await loop._session_store.write_review_state(session_id, turn_index, state.to_doc())
    loop._observer("loop_delivery_review", {"site": site, "status": status, "calls": calls})
    return {"status": status, "attempts": calls}, status == "rejected"
