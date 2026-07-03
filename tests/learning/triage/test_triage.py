"""S2-triage-deterministic-keep-skip (D100, design §3) — K1–K4 keep predicates,
the skip_* reason codes, and target_hints (matrix rows T1–T8).
"""

from __future__ import annotations

from data_agent.learning.summary.models import (
    AskUserExchange,
    BlueprintUsage,
    FailedFixedSql,
    SessionSummary,
    ToolCallSummary,
)
from data_agent.learning.triage import TriageVerdict, triage


def _tc(tool_name="runQuery", status="ok", *, ref="c1", turn=0):
    return ToolCallSummary(
        turn_index=turn, tool_call_ref=ref, tool_name=tool_name,
        args={}, sql=None, status=status, error_code=None, provenance=frozenset(),
        result_columns=(), result_row_count=None, result_full_ref=None,
        full_result_loaded=False,
    )


def _summary(
    *,
    tool_calls=(),
    accepted_signal=None,
    failed_fixed_sql=(),
    askuser_exchanges=(),
    blueprint_usages=(),
):
    return SessionSummary(
        session_id="s", user_id="u", scope_ref="sc", trace_id="t", content_hash="h",
        turns=(), tool_calls=tuple(tool_calls), blueprint_usages=tuple(blueprint_usages),
        askuser_exchanges=tuple(askuser_exchanges), failed_fixed_sql=tuple(failed_fixed_sql),
        accepted_signal=accepted_signal,
    )


# --- KEEP predicates --------------------------------------------------------


def test_k1_accepted_successful_query_keeps():
    # T1: accepted_signal set AND an ok runQuery ⇒ K1 keep, hint blueprint.
    v = triage(_summary(tool_calls=[_tc("runQuery", "ok")], accepted_signal="no_correction"))
    assert v.decision == "keep"
    assert v.reason == "K1"
    assert "blueprint" in v.target_hints


def test_k1_requires_both_acceptance_and_ok_call():
    # accepted but NO ok data call ⇒ not K1.
    v = triage(_summary(tool_calls=[_tc("runQuery", "error")], accepted_signal="explicit_confirm"))
    assert not (v.decision == "keep" and v.reason == "K1")


def test_k2_failed_fixed_pair_keeps_even_without_acceptance():
    # T2 / edge: a failed→fixed pair keeps even when accepted_signal is None.
    pair = FailedFixedSql(failed_tool_call_ref="bad", failed_sql="SELCT",
                          fixed_tool_call_ref="good", fixed_sql="SELECT 1")
    v = triage(_summary(
        tool_calls=[_tc("runQuery", "error", ref="bad"), _tc("runQuery", "ok", ref="good")],
        failed_fixed_sql=[pair], accepted_signal=None,
    ))
    assert v.decision == "keep"
    assert v.reason == "K2"
    assert "global_knowledge" in v.target_hints
    assert "blueprint" in v.target_hints


def test_k3_answered_askuser_keeps():
    # T3: an answered askUser exchange ⇒ K3.
    ex = AskUserExchange(question_tool_call_ref="ask1", question="which dept?",
                         answer="sales", answer_turn_index=1)
    v = triage(_summary(tool_calls=[_tc("askUser", "ok", ref="ask1")], askuser_exchanges=[ex]))
    assert v.decision == "keep"
    assert v.reason == "K3"
    assert "user_knowledge" in v.target_hints
    assert "global_knowledge" in v.target_hints


def test_k3_unanswered_askuser_does_not_keep():
    ex = AskUserExchange(question_tool_call_ref="ask1", question="which dept?",
                         answer=None, answer_turn_index=None)
    v = triage(_summary(tool_calls=[_tc("askUser", "ok", ref="ask1")], askuser_exchanges=[ex]))
    assert v.decision == "skip"


def test_k4_corrected_blueprint_keeps():
    # T4: a corrected blueprint usage ⇒ K4.
    bp = BlueprintUsage(tool_call_ref="bp", blueprint_id="bp-x", status="error", outcome="corrected")
    v = triage(_summary(tool_calls=[_tc("runBlueprint", "error", ref="bp")], blueprint_usages=[bp]))
    assert v.decision == "keep"
    assert v.reason == "K4"
    assert "blueprint" in v.target_hints


def test_keep_precedence_is_k1_first():
    # When multiple predicates fire, the reason is the first-listed slug (K1).
    pair = FailedFixedSql(failed_tool_call_ref="bad", failed_sql="x",
                          fixed_tool_call_ref="good", fixed_sql="y")
    v = triage(_summary(
        tool_calls=[_tc("runQuery", "ok")], accepted_signal="no_correction",
        failed_fixed_sql=[pair],
    ))
    assert v.decision == "keep"
    assert v.reason == "K1"


# --- SKIP reason codes ------------------------------------------------------


def test_skip_no_tool_calls_for_chat_only():
    # T5: greeting-/chat-only session (no tool calls at all).
    v = triage(_summary(tool_calls=[]))
    assert v.decision == "skip"
    assert v.reason == "skip_no_tool_calls"
    assert v.target_hints == ()


def test_skip_all_failed_when_every_query_failed():
    # T6: every data query failed and none was fixed.
    v = triage(_summary(tool_calls=[_tc("runQuery", "error"), _tc("runQuery", "denied", ref="c2")]))
    assert v.decision == "skip"
    assert v.reason == "skip_all_failed"


def test_skip_no_acceptance_for_unaccepted_one_shot():
    # T7: a successful query but accepted_signal None and no K2–K4 signal.
    v = triage(_summary(tool_calls=[_tc("runQuery", "ok")], accepted_signal=None))
    assert v.decision == "skip"
    assert v.reason == "skip_no_acceptance"


def test_skip_no_tool_calls_takes_precedence_over_others():
    v = triage(_summary(tool_calls=[]))
    assert v.reason == "skip_no_tool_calls"


# --- T8: verdict shape + hints ----------------------------------------------


def test_verdict_is_typed_and_hints_deduped_in_order():
    # K2 + K3 both fire: hints extend then de-dup preserving order; reason=K2 (K1 false).
    pair = FailedFixedSql(failed_tool_call_ref="bad", failed_sql="x",
                          fixed_tool_call_ref="good", fixed_sql="y")
    ex = AskUserExchange(question_tool_call_ref="ask1", question="q", answer="a", answer_turn_index=1)
    v = triage(_summary(
        tool_calls=[_tc("runQuery", "error", ref="bad"), _tc("runQuery", "ok", ref="good"),
                    _tc("askUser", "ok", ref="ask1")],
        failed_fixed_sql=[pair], askuser_exchanges=[ex],
    ))
    assert isinstance(v, TriageVerdict)
    assert v.decision == "keep"
    assert v.reason == "K2"
    # de-dup preserves first-seen order: K2 adds (global_knowledge, blueprint),
    # K3 adds (user_knowledge, global_knowledge) → global_knowledge not repeated.
    assert v.target_hints == ("global_knowledge", "blueprint", "user_knowledge")
    assert len(v.target_hints) == len(set(v.target_hints))
