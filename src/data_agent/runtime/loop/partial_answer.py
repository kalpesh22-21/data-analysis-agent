"""Exact judge-approved subsets, retained independently of rejected full answers."""

from copy import deepcopy
from dataclasses import replace

from data_agent.runtime.answer_scrub import scrub_answer_prose
from data_agent.runtime.composite.answer_with_table import AnswerTable, clean_answer_text
from data_agent.runtime.context.scope_filter import compute_scope_hash
from data_agent.runtime.session.models import live_analysis_state

from .answer_rules import first_match
from .proposal import ReviewState, evidence_kind, extra_evidence, fingerprint, selected_components
from .turn_accumulators import TurnAccumulators


def evidence_version(trail, turn_index):
    # Detect evidence changes before deciding whether an approval can be reused.
    return fingerprint(
        [e.to_doc() for e in trail if e.turn_index == turn_index and evidence_kind(e)]
    )


def capture_partial(verdict, brief, accum, trail, turn_index, *, observer=None):
    if not brief.allow_partial_answer or not verdict.reviewed or verdict.approved or verdict.partial_answer is None:
        return {}
    return _validate_response(
        verdict.partial_answer,
        brief,
        accum,
        trail,
        turn_index,
        original_violation=verdict.violation,
        observer=observer,
    )


def _validate_response(
    part,
    brief,
    accum,
    trail,
    turn_index,
    *,
    outcome="partial",
    original_violation="",
    observer=None,
):
    """Validate structure/provenance; the judge is responsible for factual support."""

    def invalid(reason, **details):
        if observer:
            observer("loop_recovery_validation_failed", {"reason": reason, **details})
        return {}

    if not isinstance(part, dict) or set(part) != {
        "answer",
        "evidence",
        "table_result_ids",
        "capability_refs",
        "unfinished",
    }:
        return invalid("invalid_partial_shape")
    answer = part["answer"]
    if not isinstance(answer, str) or not answer.strip():
        return invalid("empty_answer")
    for key in ("evidence", "table_result_ids", "capability_refs", "unfinished"):
        value = part[key]
        if (
            not isinstance(value, list)
            or len(value) > 100
            or any(not isinstance(x, str) or not x.strip() for x in value)
        ):
            return invalid("invalid_reference_list")
    if (
        outcome != "none"
        and not part["evidence"]
        or outcome == "partial"
        and not part["unfinished"]
    ):
        return invalid("missing_evidence_or_unfinished")
    known = {e.tool_call_id: e for e in trail if e.turn_index == turn_index}
    if not set(part["evidence"]) <= known.keys():
        return invalid("unknown_evidence_id")
    eligible = {ref: e for ref, e in known.items() if evidence_kind(e)}
    # Match finalization's extra-citation policy: known control/failed receipts
    # are ignored, but do not substitute for successful affirmative support.
    part = {**part, "evidence": list(dict.fromkeys(r for r in part["evidence"] if r in eligible))}
    # References must have been available to the judge, not merely in storage.
    visible = {r.get("tool_call_id") for r in brief.results}
    if (outcome != "none" and not part["evidence"]) or not set(part["evidence"]) <= visible:
        return invalid("evidence_not_visible")
    text = answer + (
        "\n\nUnfinished: " + " ".join(part["unfinished"]) if part["unfinished"] else ""
    )
    provenance = set()
    for ref in part["evidence"]:
        columns = eligible[ref].provenance
        if columns is None:
            provenance = None
            break
        provenance.update(columns)
    provenance = frozenset(provenance) if provenance is not None else None
    from .column_context import business_values

    receipts = [
        row[0]
        for result in brief.results
        if result.get("tool_call_id") in part["evidence"]
        for row in (result.get("result_preview") or {}).get("preview_rows", [])
        if isinstance(row, list)
        and len(row) == 1
        and isinstance(row[0], dict)
        and row[0].get("result_id") == result.get("tool_call_id")
        and "context_key" in row[0]
        and isinstance(row[0].get("rows"), list)
        and all(isinstance(value, dict) for value in row[0]["rows"])
    ]
    values = business_values(receipts)
    scrubbed, redactions = scrub_answer_prose(text, provenance=provenance, business_values=values)
    if (
        len(text) > 20000
        or clean_answer_text(text) != text
        or redactions
        or scrubbed != text
        or any(ord(c) < 32 and c not in "\n\r\t" for c in text)
    ):
        return invalid("unsafe_answer_text")
    rule = first_match(
        text,
        accum.sql_executed,
        brief.question,
        has_alternative_evidence=True,
        business_values=values,
        has_tables=bool(part["table_result_ids"]),
    )
    # Verbosity is a judge signal, never a reason to discard an approved answer.
    if rule and rule.name not in {"no_evidence", "verbose_answer"}:
        return invalid("answer_guard", rule=rule.name)
    tables, cards = [], []
    for ref in dict.fromkeys(part["table_result_ids"]):
        matches = [
            c
            for c in brief.selected_components
            if c.get("kind") == "table" and c.get("result_id") == ref
        ]
        if len(matches) != 1 or ref not in part["evidence"]:
            return invalid("invalid_table_reference")
        found = [t for t in accum.answer_tables if t.sql == matches[0].get("sql")]
        if len(found) != 1:
            return invalid("table_candidate_missing")
        tables.append(found[0].to_doc())
    for name in dict.fromkeys(part["capability_refs"]):
        matches = [
            c
            for c in brief.selected_components
            if c.get("kind") == "capability" and c.get("capability_ref") == name
        ]
        found = [c for c in accum.capability_cards or () if c.get("name") == name]
        if len(matches) != 1 or len(found) != 1 or matches[0]["result_id"] not in part["evidence"]:
            return invalid("invalid_capability_reference")
        cards.append(dict(found[0]))
    return deepcopy(
        {
            "answer": text,
            "completion": outcome,
            "request_context": [brief.question, *brief.clarification_answers],
            "evidence_receipts": {
                e.tool_call_id: fingerprint(e.to_doc())
                for e in trail
                if e.turn_index == turn_index and evidence_kind(e)
            },
            "tables": tables,
            "cards": cards,
            "extra_evidence": extra_evidence(
                part["evidence"],
                list(eligible.values()),
                selected=part["table_result_ids"]
                + [
                    c["result_id"]
                    for c in brief.selected_components
                    if c.get("capability_ref") in part["capability_refs"]
                ],
                sql_by_id={
                    **{key: run.terminal_sql for key, run in accum.blueprint_runs.items()},
                    **accum.result_sql_by_call_id,
                },
            ),
            "evidence": part["evidence"],
            "unfinished": part["unfinished"],
            "evidence_version": evidence_version(trail, turn_index),
            "provenance": sorted(provenance) if provenance is not None else None,
            "original_violation": original_violation,
        }
    )


def restore_partial(snapshot):
    provenance = snapshot["provenance"]
    provenance = frozenset(tuple(p) for p in provenance) if provenance is not None else None
    tables = [AnswerTable(**t, provenance=provenance) for t in snapshot["tables"]]
    accum = TurnAccumulators(
        answer_tables=tables, sql=[t.sql for t in tables], capability_cards=snapshot["cards"]
    )
    accum.extra_evidence = snapshot.get("extra_evidence")
    accum.select_capabilities([c["name"] for c in snapshot["cards"]])
    return snapshot["answer"], accum, provenance


async def recover_partial(loop, session_id, turn_index, accum, draft):
    import asyncio

    from .delivery import CURRENT_DELIVERY

    context = CURRENT_DELIVERY.get()
    if context:
        context.resilient_exit = True
    timeout = getattr(loop._answer_judge, "timeout_seconds", 30.0)
    try:
        return await asyncio.wait_for(
            _recover_partial(loop, session_id, turn_index, accum, draft),
            timeout=2 * timeout,
        )
    except TimeoutError:
        if context:
            context.review_unavailable = True
        loop._observer("loop_partial_answer_failed", {"reason": "exit_deadline"})
        return None


async def _recover_partial(loop, session_id, turn_index, accum, draft):
    """One terminal review using existing evidence, or reuse an unchanged approval."""
    from .delivery import CURRENT_DELIVERY, delivery_version
    from .judge_evidence import enrich_brief

    context = CURRENT_DELIVERY.get()
    if not context or not loop._answer_judge or not getattr(loop._answer_judge, "enabled", True):
        return None
    doc = await loop._session_store.get_or_create_session(session_id)
    state = ReviewState.restore(
        doc.review_states.get(str(turn_index)), compute_scope_hash(context.credentials.column_scope)
    )
    try:
        results, anchor, trail = await loop._judge_results(
            session_id,
            turn_index,
            context.credentials.column_scope,
            exclude_result_ids=frozenset(c["result_id"] for c in state.excluded_components),
        )
        current = [e for e in trail if e.turn_index == turn_index]
        # All candidates come from successful scoped executions, never judge-authored SQL.
        for entry in current:
            if evidence_kind(entry) != "warehouse" or entry.provenance is None:
                continue
            sql = accum.result_sql_by_call_id.get(entry.tool_call_id) or entry.args.get("sql")
            if sql:
                accum.result_sql_by_call_id[entry.tool_call_id] = sql
                if not any(t.sql == sql for t in accum.answer_tables):
                    accum._answer_tables.append(AnswerTable(sql=sql, provenance=entry.provenance))

        snapshot = state.approved_partial
        questions = [
            m.content for m in doc.messages if m.role == "user" and m.turn_index == turn_index
        ]
        if snapshot:
            receipts = {
                e.tool_call_id: fingerprint(e.to_doc()) for e in current if evidence_kind(e)
            }
            old = snapshot.get("evidence_receipts", {})
            changed = snapshot.get("evidence_version") != evidence_version(current, turn_index)
            changed = changed or snapshot.get("request_context", questions) != questions
            # Mutated or removed support invalidates the approval without an LLM decision.
            if old and any(receipts.get(ref) != old.get(ref) for ref in snapshot["evidence"]):
                snapshot = {}
            elif changed:
                try:
                    impact = await loop._answer_judge.partial_relevance(
                        snapshot, results, questions
                    )
                except Exception:
                    impact = "unclear"
                loop._observer("loop_partial_relevance", {"reason": impact})
                if impact != "unrelated":
                    snapshot = {}
        if not snapshot:
            if context.partial_review_attempted:
                loop._observer("loop_partial_answer_failed", {"reason": "already_attempted"})
                return None
            context.partial_review_attempted = True
            brief = loop._judge_brief(
                "exit_capability"
                if accum.capability_cards
                else "exit_table"
                if accum.has_answer_tables
                else "exit_prose",
                question=next(
                    (
                        m.content
                        for m in doc.messages
                        if m.role == "user" and m.turn_index == turn_index
                    ),
                    "",
                ),
                accum=accum,
                analysis_state=live_analysis_state(doc, turn_index),
                draft=draft or "The full request could not be completed.",
                results=results,
                date_anchor=anchor,
                designated_tables=tuple((t.caption, t.sql) for t in accum.answer_tables),
            )
            brief = replace(
                brief,
                allow_partial_answer=True,
                terminal_partial_review=True,
                previous_rejection=state.feedback or state.violation,
                clarification_answers=tuple(
                    m.content
                    for m in doc.messages
                    if m.role == "user" and m.turn_index == turn_index
                )[1:],
                selected_components=tuple(
                    selected_components(
                        {
                            "tables": [
                                {"result_id": ref}
                                for ref, sql in accum.result_sql_by_call_id.items()
                                if any(t.sql == sql for t in accum.answer_tables)
                            ],
                            "capability_refs": [
                                c.get("name") for c in accum.capability_cards or ()
                            ],
                        },
                        current,
                        accum.result_sql_by_call_id,
                    )
                ),
            )
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
            response = await loop._answer_judge.salvage(brief)
            outcome = response.pop("outcome")
            captions = response.pop("captions", [])
            for caption in captions:
                ref, text = caption["result_id"], caption["caption"]
                if ref not in response["table_result_ids"]:
                    loop._observer("loop_salvage_caption_omitted", {"reason": "caption_not_selected"})
                    continue
                entry = next((e for e in current if e.tool_call_id == ref), None)
                if entry is None:
                    continue  # The mandatory reference validator below rejects it.
                clean, count = scrub_answer_prose(text, provenance=entry.provenance)
                if (
                    count
                    or clean != text
                    or first_match(
                        text, accum.sql_executed, brief.question, has_alternative_evidence=True
                    )
                ):
                    loop._observer("loop_salvage_caption_omitted", {"reason": "invalid_caption"})
                    text = None
                accum._answer_tables = [
                    replace(t, caption=text) if t.sql == accum.result_sql_by_call_id.get(ref) else t
                    for t in accum.answer_tables
                ]
            if (
                outcome == "full"
                and response["unfinished"]
                or outcome == "none"
                and any(response[k] for k in ("evidence", "table_result_ids", "capability_refs"))
            ):
                loop._observer("loop_partial_answer_failed", {"reason": "invalid_salvage_outcome"})
                return None
            snapshot = _validate_response(
                response,
                brief,
                accum,
                current,
                turn_index,
                outcome=outcome,
                observer=loop._observer,
            )
            if snapshot:
                loop._observer("loop_salvage_approved", {"status": outcome})
        if not snapshot:
            return None  # Validation already emitted the precise reason.
        text, recovered, provenance = restore_partial(snapshot)
        context.recovery_completion = snapshot.get("completion", "partial")
        loop._observer("loop_recovery_validated", {"status": "approved"})
        state.approved_partial = snapshot
        # This receipt approves ONLY the exact subset. Archive the rejected full
        # answer's reason in the snapshot, never treat it as approval of that answer.
        state.violation = ""
        state.delivery_version = delivery_version(text, recovered)
        state.delivery_status = "approved"
        state.delivery_reason = ""
        await loop._session_store.write_review_state(session_id, turn_index, state.to_doc())
        loop._observer(
            "loop_partial_answer_recovered",
            {
                "evidence_count": len(snapshot["evidence"]),
                "text_length": len(text),
                "table_count": len(recovered.answer_tables),
                "card_count": len(recovered.capability_cards or ()),
            },
        )
        return text, recovered, provenance
    except Exception as exc:
        from jsonschema import ValidationError

        reason = "review_unavailable"
        if isinstance(exc, TimeoutError):
            reason = "exit_deadline"
        elif isinstance(exc, ValidationError):
            reason = "schema_validation"
        elif isinstance(exc, ValueError) and exc.args and exc.args[0] in {
            "recovery_evidence_budget", "non_reasoning_transport_unavailable",
            "invalid_recovery_response", "caption_not_selected", "invalid_caption",
        }:
            reason = exc.args[0]
        loop._observer(
            "loop_partial_answer_failed",
            {"reason": reason, "exception_type": type(exc).__name__},
        )
        return None
