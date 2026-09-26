"""Exact judge-approved subsets, retained independently of rejected full answers."""

from copy import deepcopy
from dataclasses import replace

from data_agent.runtime.answer_scrub import scrub_answer_prose
from data_agent.runtime.composite.answer_with_table import AnswerTable, clean_answer_text
from data_agent.runtime.context.scope_filter import compute_scope_hash
from data_agent.runtime.session.models import live_analysis_state

from .answer_rules import first_match
from .proposal import ReviewState, evidence_kind, fingerprint, selected_components
from .turn_accumulators import TurnAccumulators


def evidence_version(trail, turn_index):
    # Conservative invalidation: any new successful evidence needs fresh review,
    # including evidence that could contradict a previously approved finding.
    return fingerprint(
        [e.to_doc() for e in trail if e.turn_index == turn_index and evidence_kind(e)]
    )


def capture_partial(verdict, brief, accum, trail, turn_index):
    """Validate structure/provenance; the judge is responsible for factual support."""
    part = verdict.partial_answer
    if not brief.allow_partial_answer or not verdict.reviewed or verdict.approved:
        return {}
    if not isinstance(part, dict) or set(part) != {
        "answer",
        "evidence",
        "table_result_ids",
        "capability_refs",
        "unfinished",
    }:
        return {}
    answer = part["answer"]
    if not isinstance(answer, str) or not answer.strip():
        return {}
    for key in ("evidence", "table_result_ids", "capability_refs", "unfinished"):
        value = part[key]
        if (
            not isinstance(value, list)
            or len(value) > 100
            or any(not isinstance(x, str) or not x.strip() for x in value)
        ):
            return {}
    if not part["evidence"] or not part["unfinished"]:
        return {}
    known = {e.tool_call_id: e for e in trail if e.turn_index == turn_index}
    if not set(part["evidence"]) <= known.keys():
        return {}
    eligible = {ref: e for ref, e in known.items() if evidence_kind(e)}
    # Match finalization's extra-citation policy: known control/failed receipts
    # are ignored, but do not substitute for successful affirmative support.
    part = {**part, "evidence": list(dict.fromkeys(r for r in part["evidence"] if r in eligible))}
    # References must have been available to the judge, not merely in storage.
    visible = {r.get("tool_call_id") for r in brief.results}
    if not part["evidence"] or not set(part["evidence"]) <= visible:
        return {}
    text = answer + "\n\nUnfinished: " + " ".join(part["unfinished"])
    provenance = set()
    for ref in part["evidence"]:
        columns = eligible[ref].provenance
        if columns is None:
            provenance = None
            break
        provenance.update(columns)
    provenance = frozenset(provenance) if provenance is not None else None
    scrubbed, redactions = scrub_answer_prose(text, provenance=provenance)
    if (
        len(text) > 20000
        or clean_answer_text(text) != text
        or redactions
        or scrubbed != text
        or any(ord(c) < 32 and c not in "\n\r\t" for c in text)
    ):
        return {}
    rule = first_match(text, accum.sql_executed, brief.question, has_alternative_evidence=True)
    if rule and rule.name != "no_evidence":
        return {}
    tables, cards = [], []
    for ref in dict.fromkeys(part["table_result_ids"]):
        matches = [
            c
            for c in brief.selected_components
            if c.get("kind") == "table" and c.get("result_id") == ref
        ]
        if len(matches) != 1 or ref not in part["evidence"]:
            return {}
        found = [t for t in accum.answer_tables if t.sql == matches[0].get("sql")]
        if len(found) != 1:
            return {}
        tables.append(found[0].to_doc())
    for name in dict.fromkeys(part["capability_refs"]):
        matches = [
            c
            for c in brief.selected_components
            if c.get("kind") == "capability" and c.get("capability_ref") == name
        ]
        found = [c for c in accum.capability_cards or () if c.get("name") == name]
        if len(matches) != 1 or len(found) != 1 or matches[0]["result_id"] not in part["evidence"]:
            return {}
        cards.append(dict(found[0]))
    return deepcopy(
        {
            "answer": text,
            "tables": tables,
            "cards": cards,
            "evidence": part["evidence"],
            "unfinished": part["unfinished"],
            "evidence_version": evidence_version(trail, turn_index),
            "provenance": sorted(provenance) if provenance is not None else None,
            "original_violation": verdict.violation,
        }
    )


def restore_partial(snapshot):
    provenance = snapshot["provenance"]
    provenance = frozenset(tuple(p) for p in provenance) if provenance is not None else None
    tables = [AnswerTable(**t, provenance=provenance) for t in snapshot["tables"]]
    accum = TurnAccumulators(
        answer_tables=tables, sql=[t.sql for t in tables], capability_cards=snapshot["cards"]
    )
    accum.select_capabilities([c["name"] for c in snapshot["cards"]])
    return snapshot["answer"], accum, provenance


async def recover_partial(loop, session_id, turn_index, accum, draft):
    """One terminal review using existing evidence, or reuse an unchanged approval."""
    from .delivery import CURRENT_DELIVERY, delivery_version, review_once
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
        snapshot = state.approved_partial
        if snapshot and snapshot.get("evidence_version") != evidence_version(current, turn_index):
            snapshot = state.approved_partial = {}
        if not snapshot:
            # No queries, no model-agent turn, and at most one judge call here.
            if (
                not any(evidence_kind(e) for e in current)
                or context.review_seconds <= 0
                or context.partial_review_attempted
            ):
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
            context.terminal_review_version = delivery_version(draft, accum)
            context.terminal_review_status = "exhausted"
            verdict = await review_once(loop, brief, terminal=True)
            if verdict.reviewed:
                if verdict.approved and not state.violation:
                    context.terminal_review_status = "approved"
                elif not verdict.approved:
                    state.reject(verdict, context.terminal_review_version, accum.assumptions or ())
                    await loop._session_store.write_review_state(
                        session_id, turn_index, state.to_doc()
                    )
            snapshot = capture_partial(verdict, brief, accum, current, turn_index)
        if not snapshot:
            return None
        text, recovered, provenance = restore_partial(snapshot)
        state.approved_partial = snapshot
        # This receipt approves ONLY the exact subset. Archive the rejected full
        # answer's reason in the snapshot, never treat it as approval of that answer.
        state.violation = ""
        state.delivery_version = delivery_version(text, recovered)
        state.delivery_status = "approved"
        await loop._session_store.write_review_state(session_id, turn_index, state.to_doc())
        loop._observer(
            "loop_partial_answer_recovered", {"evidence_count": len(snapshot["evidence"])}
        )
        return text, recovered, provenance
    except Exception:
        loop._observer("loop_partial_answer_failed", {"reason": "review_unavailable"})
        return None
