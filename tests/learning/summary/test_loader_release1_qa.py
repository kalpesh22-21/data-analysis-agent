"""QA sweep of the Release-1 loader slice: enforcement denials (§2.4/§2.5) and the
`answerWithTable` designation lift (§2.6).

The builder's own cases live in `test_loader.py`; these are the ones it did not
write, each chosen because it is a shape the LIVE Release-1 loop produces and the
implementation's correctness on it is not obvious from the code:

  * an enforcement denial standing BETWEEN a genuine failure and its fix — the skip
    must not consume the fix, or the friction signal disappears with it;
  * a session whose only non-ok entries are enforcement denials — the whole point of
    the slice is that its `SessionSignals` are indistinguishable from a session that
    never tripped a gate at all, so it is asserted AGAINST such a session rather than
    against hand-written zeros;
  * the model-authored shapes of `answerWithTable.args` that the runtime tolerates
    and this loader must not raise on.
"""

from __future__ import annotations

import unicodedata

import pytest

from data_agent.learning.candidate.signals import SessionSignals
from data_agent.learning.summary import load_session_summary
from data_agent.learning.summary.loader import ENFORCEMENT_ERROR_CODES, INFRA_ERROR_CODES
from data_agent.learning.triage import triage

from .helpers import make_doc, make_job, make_message, make_trail_entry

HEADCOUNT_SQL = "SELECT count(*) FROM hr.employee WHERE department = 'Analytics'"
PAYROLL_SQL = "SELECT sum(gross_pay) FROM payroll.payroll_fact WHERE toYear(pay_period) = 2025"

# Codes a runQuery/runBlueprint can come back with where the model's OWN WORK was judged
# and found wanting — the friction both readers exist to record. Derived from
# `denial_mapping._DENIAL_TABLE` by asking, of each entry, "was a verdict formed on the
# query or the blueprint?", not copied from a list.
#
# `CLICKHOUSE_UNAVAILABLE` used to sit here and no longer does (H8). It is INFRA, not
# substantive: the warehouse was down, so nothing judged the SQL. It keeps ONE of the two
# behaviours below — see `test_an_infra_outage_is_friction_but_not_a_corrected_blueprint`.
SUBSTANTIVE_DATA_CODES = frozenset({
    "COLUMN_SCOPE_VIOLATION",
    "SCRATCH_SESSION_VIOLATION",
    "CLICKHOUSE_QUERY_ERROR",
    "CARTESIAN_JOIN_FORBIDDEN",
    "TABLE_NOT_FOUND",
    "DATABASE_NOT_ALLOWED",
    "PARSE_FAILED_CLOSED",
})


async def _load(store, doc):
    return await load_session_summary(doc, store, job=make_job(doc.session_id))


def test_no_substantive_failure_code_is_ever_classified_as_enforcement():
    """The one invariant the whole split rests on, stated as a set property so a code
    added to `ENFORCEMENT_ERROR_CODES` later cannot quietly switch off a real signal.
    A per-code fixture only covers the codes someone remembered to write one for."""
    assert ENFORCEMENT_ERROR_CODES.isdisjoint(SUBSTANTIVE_DATA_CODES)


def test_no_substantive_failure_code_is_ever_classified_as_infra():
    """The H8 twin. `INFRA_ERROR_CODES` suppresses blueprint usages, so a substantive
    code leaking into it would silently stop recording blueprints that really did produce
    wrong answers — the same class of loss as the enforcement invariant above, through
    the other set."""
    assert INFRA_ERROR_CODES.isdisjoint(SUBSTANTIVE_DATA_CODES)


def _denied_blueprint_then_ok_query(error_code):
    """One `runBlueprint` refused with `error_code`, then a clean `runQuery` — the shape
    that exercises BOTH readers at once (the pair and the usage) off one trail."""
    return make_doc(
        tool_trail=[
            make_trail_entry(turn_index=0, tool_call_id="bp", tool_name="runBlueprint",
                             args={"blueprint_id": "bp-x"}, status="denied",
                             error_code=error_code),
            make_trail_entry(turn_index=0, tool_call_id="ok", tool_name="runQuery",
                             args={"sql": HEADCOUNT_SQL}, status="ok"),
        ],
    )


@pytest.mark.parametrize("error_code", sorted(SUBSTANTIVE_DATA_CODES))
async def test_every_substantive_code_still_counts_as_friction(store, error_code):
    """The other half of the same invariant, exercised through the real loader: each
    of these still yields a failed→fixed pair AND a `corrected` blueprint usage,
    i.e. behaviour identical to before the slice."""
    summary = await _load(store, _denied_blueprint_then_ok_query(error_code))
    assert [p.failed_tool_call_ref for p in summary.failed_fixed_sql] == ["bp"]
    assert [u.outcome for u in summary.blueprint_usages] == ["corrected"]
    assert SessionSignals.from_summary(summary).corrected_blueprint is True


@pytest.mark.parametrize("error_code", sorted(INFRA_ERROR_CODES))
async def test_an_infra_outage_is_friction_but_not_a_corrected_blueprint(store, error_code):
    """H8, pinned as the ASYMMETRY it is — the same trail, the two readers disagreeing.

    An outage still pairs failed→fixed (the analyst really did hit a wall and re-run: that
    is friction they lived through), but emits NO blueprint usage at all, because nothing
    ever judged the blueprint's SQL. Before this, `CLICKHOUSE_UNAVAILABLE` on a
    `runBlueprint` produced `outcome="corrected"` → `SessionSignals.corrected_blueprint`
    → a 0.5 inbox ranking penalty and a triage K4 keep, all from a warehouse being down.

    Emitting `accepted` instead would be the opposite conflation (a blueprint that never
    ran accruing a positive record), so the usage list is EMPTY, not merely non-corrected.

    Parametrized over the whole derived set rather than one code, for the reason the
    enforcement suite is: a per-code fixture only covers what someone remembered.
    """
    summary = await _load(store, _denied_blueprint_then_ok_query(error_code))
    assert summary.blueprint_usages == ()
    assert SessionSignals.from_summary(summary).corrected_blueprint is False
    assert [p.failed_tool_call_ref for p in summary.failed_fixed_sql] == ["bp"]
    assert SessionSignals.from_summary(summary).failed_fixed_count == 1


# --- §2.4: an enforcement denial must not stand between a failure and its fix ---


async def test_an_enforcement_denial_between_a_failure_and_its_fix_still_pairs(store):
    """The genuine failure keeps its fix even when a refused call sits between them.

    `_failed_fixed_pairs` scans FORWARD from each failure for the next ok runQuery.
    The enforcement skip is a `continue` on the OUTER loop (the refused call is not a
    failure), not a break in the forward scan — so `q_bad` must still reach `q_good`.
    A skip written into the inner scan instead would look identical on every
    single-failure fixture and would silently drop this pair.
    """
    doc = make_doc(
        tool_trail=[
            make_trail_entry(turn_index=0, tool_call_id="q_bad", tool_name="runQuery",
                             args={"sql": "SELECT no_such_column FROM hr.employee"},
                             status="error", error_code="CLICKHOUSE_QUERY_ERROR"),
            make_trail_entry(turn_index=0, tool_call_id="gated", tool_name="runQuery",
                             args={"sql": "SELECT 1"}, status="denied",
                             error_code="ANALYSIS_STATE_INVALID"),
            make_trail_entry(turn_index=0, tool_call_id="q_good", tool_name="runQuery",
                             args={"sql": HEADCOUNT_SQL}, status="ok"),
        ],
    )
    summary = await _load(store, doc)

    assert [(p.failed_tool_call_ref, p.fixed_tool_call_ref) for p in summary.failed_fixed_sql] == [
        ("q_bad", "q_good")
    ]
    assert summary.failed_fixed_sql[0].fixed_sql == HEADCOUNT_SQL


async def test_an_enforcement_denied_blueprint_between_two_real_failures_pairs_both(store):
    """Two genuine failures, one refusal in the middle: two pairs, both pointing at
    the single later ok runQuery. The refusal contributes no third pair."""
    doc = make_doc(
        tool_trail=[
            make_trail_entry(turn_index=0, tool_call_id="f1", tool_name="runQuery",
                             args={"sql": "SELECT bad1"}, status="error",
                             error_code="CLICKHOUSE_QUERY_ERROR"),
            make_trail_entry(turn_index=0, tool_call_id="gated", tool_name="runBlueprint",
                             args={"blueprint_id": "bp-headcount"}, status="denied",
                             error_code="BLUEPRINT_DEFINITION_NOT_READ"),
            make_trail_entry(turn_index=0, tool_call_id="f2", tool_name="runQuery",
                             args={"sql": "SELECT bad2"}, status="error",
                             error_code="CLICKHOUSE_QUERY_ERROR"),
            make_trail_entry(turn_index=0, tool_call_id="ok", tool_name="runQuery",
                             args={"sql": HEADCOUNT_SQL}, status="ok"),
        ],
    )
    summary = await _load(store, doc)
    assert [p.failed_tool_call_ref for p in summary.failed_fixed_sql] == ["f1", "f2"]


# --- the slice's actual promise: enforcement noise is invisible to the signals ---


def _release1_trail(*, with_enforcement_noise: bool):
    """The same successful Release-1 turn, once clean and once with every
    enforcement gate the model routinely trips on the way to the same answer."""
    trail = []
    if with_enforcement_noise:
        trail += [
            make_trail_entry(turn_index=0, tool_call_id="st_bad",
                             tool_name="updateAnalysisState", args={"intents": []},
                             status="denied", error_code="ANALYSIS_STATE_INVALID"),
            make_trail_entry(turn_index=0, tool_call_id="bp_gated", tool_name="runBlueprint",
                             args={"blueprint_id": "bp-headcount"}, status="denied",
                             error_code="BLUEPRINT_DEFINITION_NOT_READ"),
        ]
    trail += [
        make_trail_entry(turn_index=0, tool_call_id="get", tool_name="getBlueprint",
                         args={"blueprint_id": "bp-headcount"}, status="ok"),
        make_trail_entry(turn_index=0, tool_call_id="bp_run", tool_name="runBlueprint",
                         args={"blueprint_id": "bp-headcount"}, status="ok"),
    ]
    if with_enforcement_noise:
        trail += [
            make_trail_entry(turn_index=0, tool_call_id="ans_blocked",
                             tool_name="answerWithTable", args={"sql": HEADCOUNT_SQL},
                             status="denied",
                             error_code="FINALIZATION_BLOCKED_PENDING_INTENTS"),
            make_trail_entry(turn_index=0, tool_call_id="st_ok",
                             tool_name="updateAnalysisState",
                             args={"intents": [{"id": "i1", "status": "completed"}]},
                             status="ok"),
        ]
    trail.append(
        make_trail_entry(turn_index=0, tool_call_id="ans", tool_name="answerWithTable",
                         args={"answer": "1,204 people.", "sql": HEADCOUNT_SQL},
                         status="ok")
    )
    return trail


async def test_an_enforcement_only_session_signals_exactly_like_a_clean_one(store):
    """THE slice's headline claim, asserted as an equality against the clean session
    rather than against remembered zeros: after Release 1 a healthy session trips
    these gates as a matter of course, and the inbox must not rank it below an
    identical session that happened not to.

    `SessionSignals` is the durable stamp — the summary is gone by the time a human
    opens the inbox — so a difference here is permanent.
    """
    messages = [make_message(0, "user", "how many people are in Analytics?"),
                make_message(0, "assistant", "1,204 people.")]
    clean = await _load(store, make_doc("sess-clean", messages=messages,
                                        tool_trail=_release1_trail(with_enforcement_noise=False)))
    noisy = await _load(store, make_doc("sess-noisy", messages=messages,
                                        tool_trail=_release1_trail(with_enforcement_noise=True)))

    assert SessionSignals.from_summary(noisy) == SessionSignals.from_summary(clean)
    # ... and the stamp is the CLEAN one, not two matching wrong values.
    assert SessionSignals.from_summary(noisy) == SessionSignals(
        accepted_signal="no_correction", turn_count=1, failed_fixed_count=0,
        askuser_count=0, corrected_blueprint=False,
    )
    # The triage verdict — the other reader of the same two collections — agrees.
    assert triage(noisy) == triage(clean)
    # The noise is still THERE: the projection stays faithful, only the inferences drop.
    assert len(noisy.tool_calls) > len(clean.tool_calls)
    assert [u.tool_call_ref for u in noisy.blueprint_usages] == ["bp_run"]


# --- §2.6: model-authored `answerWithTable.args` shapes -----------------------


async def test_a_valid_top_level_sql_survives_an_entirely_malformed_tables_array(store):
    """Every `tables` item unusable — a non-str sql, a blank placeholder, a bare
    string, `None`, a blueprint-only item this loader cannot resolve to SQL — and one
    good top-level `sql`. The top-level designation is the answer's only record and
    must survive; nothing may raise."""
    doc = make_doc(
        tool_trail=[
            make_trail_entry(
                turn_index=0, tool_call_id="ans", tool_name="answerWithTable",
                args={"answer": "here", "sql": HEADCOUNT_SQL, "blueprint_id": "",
                      "tables": [
                          {"sql": 17},
                          {"sql": ""},
                          {"sql": "   \n\t "},
                          {"sql": None, "caption": "empty placeholder"},
                          {"blueprint_id": "bp-headcount"},
                          "not-a-mapping",
                          None,
                          [{"sql": "SELECT nested"}],
                      ]},
                status="ok",
            ),
        ],
    )
    summary = await _load(store, doc)
    assert [(a.tool_call_ref, a.sql, a.blueprint_id) for a in summary.answer_sqls] == [
        ("ans", HEADCOUNT_SQL, None)
    ]


async def test_two_answer_calls_in_one_session_dedupe_across_the_pair(store):
    """Multi-turn: the model answers, the user asks a follow-up, the model answers
    again re-showing one of the same tables. The repeat keeps the FIRST call's ref
    (that is the one a reviewer would open), and the follow-up's new table is added."""
    doc = make_doc(
        messages=[make_message(0, "user", "headcount for Analytics?"),
                  make_message(0, "assistant", "1,204."),
                  make_message(1, "user", "and their payroll?"),
                  make_message(1, "assistant", "Here are both.")],
        tool_trail=[
            make_trail_entry(turn_index=0, tool_call_id="ans1", tool_name="answerWithTable",
                             args={"sql": HEADCOUNT_SQL}, status="ok"),
            make_trail_entry(
                turn_index=1, tool_call_id="ans2", tool_name="answerWithTable",
                args={"sql": "", "tables": [
                    {"sql": HEADCOUNT_SQL, "caption": "headcount (again)"},
                    {"sql": PAYROLL_SQL, "caption": "payroll", "blueprint_id": "bp-pay"},
                ]},
                status="ok",
            ),
        ],
    )
    summary = await _load(store, doc)
    assert [(a.tool_call_ref, a.sql) for a in summary.answer_sqls] == [
        ("ans1", HEADCOUNT_SQL),
        ("ans2", PAYROLL_SQL),
    ]
    assert summary.answer_sqls[1].blueprint_id == "bp-pay"


async def test_dedupe_is_exact_string_equality_so_near_variants_both_survive(store):
    """PINS the intended semantics, which is the conservative one: dedupe is exact
    `str` equality, NOT normalization. A whitespace or Unicode-composition variant is
    a DIFFERENT string to the embedding endpoint and to the extractor's model, and
    this loader has no SQL-equivalence oracle — collapsing them would be a claim it
    cannot make. Two near-identical evidence lines cost tokens; a wrongly dropped one
    could cost the answer's only SQL.
    """
    nfc = "SELECT dept FROM hr.employee WHERE name = 'José'"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd  # same text, different code points
    spaced = "SELECT  dept FROM hr.employee WHERE name = 'José'"
    trailing = nfc + "\n"

    doc = make_doc(
        tool_trail=[
            make_trail_entry(
                turn_index=0, tool_call_id="ans", tool_name="answerWithTable",
                args={"sql": nfc, "tables": [{"sql": nfd}, {"sql": spaced},
                                             {"sql": trailing}, {"sql": nfc}]},
                status="ok",
            ),
        ],
    )
    summary = await _load(store, doc)
    # `tables` is read BEFORE the top-level pair (`_designations` mirrors the
    # runtime's precedence), so the array's four come first and the flat `nfc`
    # duplicate of the array's fourth is what the dedupe drops.
    assert [a.sql for a in summary.answer_sqls] == [nfd, spaced, trailing, nfc]


async def test_an_ok_answer_table_with_no_designation_at_all_is_not_an_error(store):
    """A prose-only answer (`resolve_designations` calls it "an ordinary, legitimate
    call") designates nothing — an empty tuple, not a crash and not a blank entry."""
    doc = make_doc(
        tool_trail=[
            make_trail_entry(turn_index=0, tool_call_id="ans", tool_name="answerWithTable",
                             args={"answer": "No rows matched.", "sql": "",
                                   "blueprint_id": "", "tables": []},
                             status="ok"),
        ],
    )
    summary = await _load(store, doc)
    assert summary.answer_sqls == ()
