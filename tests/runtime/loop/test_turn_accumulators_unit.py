"""Unit tests for `loop/turn_accumulators.py` — the turn window's answer
accumulators, exercised through their own interface rather than through a whole
scripted turn.

These are the fast, exhaustive complement to the suites that stay the proof that
the LOOP wires the folds to the right POSITIONS — `test_multi_table_answer.py`
(over-claim correction, whole-list resume seed, uncovered intents),
`test_record_assumptions.py` (the resume seed and the `[] -> None` fork),
`test_loop_resume_dag.py` (enrichment surviving an approval-resume, the
forwarding-hole regression, the mid-loop pause envelope) and
`test_answer_shape_gate.py` (the seeded counter). Pinned HERE is what the object
itself guarantees: that a seed is copied rather than aliased, what each fold does
and refuses to do, and what the exits read.

THE FOUR ASYMMETRIES WORTH STATING UP FRONT, because most of what follows turns
on one of them:

  1. ASSUMPTIONS ACCUMULATE, THE ANSWER TABLES REPLACE. A second
     `recordAssumptions` appends (deduped); a second `answerWithTable` supersedes
     the WHOLE previous set — the model changed its mind about which query is the
     answer, and a turn has one answer.
  2. ...BUT ONLY WHEN IT DESIGNATES SOMETHING. A later call that resolves to
     nothing leaves the earlier good set intact, so a malformed retry cannot
     silently drop the user's table.
  3. THE ENVELOPE PREFERS THE TABLES AND DISCARDS THE TURN-LEVEL ENRICHMENT the
     moment one exists — including when the derived values are `None`. That
     badge-loss is the over-claim correction, not a bug.
  4. `result_sql_by_call_id` IS THE ONE ACCUMULATOR WITH NO SEED. It is a
     window-local telemetry signal, never a refusal, so an evidence call from an
     earlier window simply does not contribute.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.composite.answer_with_table import (
    VERIFICATION_EMPTY_STATUS,
    AnswerTable,
    BlueprintRun,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolResult
from data_agent.runtime.loop.turn_accumulators import (
    AnswerEnvelope,
    TurnAccumulators,
    accumulate_enrichment,
    answer_envelope,
    capture_terminal_sql,
)


def _ok(tool_name: str, result_full: Any = None) -> ToolResult:
    return ToolResult(
        status="ok",
        tool_name=tool_name,
        error_code=None,
        retryable=None,
        user_message=None,
        provenance=None,
        result_preview=None,
        result_full=result_full,
    )


def _refused(tool_name: str, result_full: Any = None) -> ToolResult:
    """A call the loop REWROTE before the folds ran — a finalization refusal, the
    blueprint-not-run nudge, a denial. Every fold must ignore it."""
    return ToolResult(
        status="error",
        tool_name=tool_name,
        error_code="SOME_REFUSAL",
        retryable=True,
        user_message=None,
        provenance=None,
        result_preview=None,
        result_full=result_full,
    )


def _blueprint_result(
    blueprint_id: str = "bp-1",
    *,
    terminal_sql: str = "SELECT 1",
    sql: list[str] | None = None,
    status: str = "verified",
    row_count: int = 7,
) -> dict[str, Any]:
    return {
        "blueprint_id": blueprint_id,
        "terminal_sql": terminal_sql,
        "sql": sql if sql is not None else [terminal_sql],
        "status": status,
        "verify": {"grain_checked": True},
        "row_count": row_count,
    }


# --- seeded construction: COPIES, NOT ALIASES -------------------------------


def test_a_fresh_window_knows_nothing() -> None:
    """`run()`'s case: a brand-new turn. Every exit reads empty, and the shape
    gate is armed (no table has reached the user)."""
    accum = TurnAccumulators()

    assert accum.sql_executed is None
    assert accum.assumptions is None
    assert accum.has_answer_tables is False
    assert accum.blueprint_runs == {}
    assert accum.result_sql_by_call_id == {}
    assert accum.envelope() == AnswerEnvelope(
        answer_sql=None, blueprint_use=None, verification=None, answer_tables=None
    )


def test_every_seeded_container_is_copied_not_aliased() -> None:
    """THE SEED IS A SNAPSHOT. A caller that keeps mutating the list/dict it
    handed over — or reuses it for a second window — must not reach into this
    window's state. Mutate every source AFTER construction and assert isolation.
    """
    sql = ["SELECT a"]
    tables = [AnswerTable(sql="SELECT a")]
    runs = {"bp-1": BlueprintRun(terminal_sql="SELECT a", verification=None, slots={})}
    assumptions = ["headcount excludes contractors"]

    accum = TurnAccumulators(
        sql=sql,
        answer_tables=tables,
        blueprint_runs=runs,
        assumptions=assumptions,
    )

    sql.append("SELECT b")
    tables.append(AnswerTable(sql="SELECT b"))
    runs["bp-2"] = BlueprintRun(terminal_sql="SELECT b", verification=None, slots={})
    assumptions.append("invented later")

    assert accum.sql_executed == ["SELECT a"]
    assert accum.assumptions == ["headcount excludes contractors"]
    assert list(accum.blueprint_runs) == ["bp-1"]
    envelope = accum.envelope()
    assert envelope.answer_tables is not None
    assert [table["sql"] for table in envelope.answer_tables] == ["SELECT a"]


def test_the_two_single_object_seeds_are_copied_too() -> None:
    """`blueprint_use`/`verification` are only ever REBOUND by the enrichment fold
    today, so aliasing the caller's dict happens to be safe. "Happens to be safe"
    via a property of a function two modules away is what this copy retires."""
    blueprint_use = {"blueprint_id": "bp-1", "slots": {"dept": "eng"}}
    verification = {"passed": True, "method": "blueprint_gate"}

    accum = TurnAccumulators(blueprint_use=blueprint_use, verification=verification)

    blueprint_use["blueprint_id"] = "bp-tampered"
    verification["passed"] = False

    envelope = accum.envelope()
    assert envelope.blueprint_use == {"blueprint_id": "bp-1", "slots": {"dept": "eng"}}
    assert envelope.verification == {"passed": True, "method": "blueprint_gate"}


def test_the_answer_shape_seed_reads_the_designated_tables() -> None:
    """`has_answer_tables` is the `AnswerShapeCounter` seed: a resumed window that
    already put a grid in front of the user must not re-arm a gate whose whole job
    is to notice a missing one. An EMPTY seed is not a table."""
    assert TurnAccumulators(answer_tables=[]).has_answer_tables is False
    assert TurnAccumulators(answer_tables=None).has_answer_tables is False
    assert TurnAccumulators(answer_tables=[AnswerTable(sql="SELECT 1")]).has_answer_tables is True


# --- note_enrichment --------------------------------------------------------


def test_enrichment_folds_run_query_sql_deduped_in_first_occurrence_order() -> None:
    accum = TurnAccumulators()

    accum.note_enrichment("runQuery", {"sql": "SELECT a"}, _ok("runQuery"))
    accum.note_enrichment("runQuery", {"sql": "SELECT b"}, _ok("runQuery"))
    accum.note_enrichment("runQuery", {"sql": "SELECT a"}, _ok("runQuery"))

    assert accum.sql_executed == ["SELECT a", "SELECT b"]


def test_enrichment_rebinds_the_turn_level_chip_and_badge() -> None:
    """The tuple-rebind the free function returned for the caller to reassign is
    now internal: one call updates the SQL list AND both single-object fields."""
    accum = TurnAccumulators()

    accum.note_enrichment(
        "runBlueprint",
        {"slot_bindings": {"dept": "eng"}},
        _ok("runBlueprint", _blueprint_result("bp-1", terminal_sql="SELECT bp")),
    )

    assert accum.sql_executed == ["SELECT bp"]
    envelope = accum.envelope()
    assert envelope.blueprint_use == {"blueprint_id": "bp-1", "slots": {"dept": "eng"}}
    assert envelope.verification == {
        "passed": True,
        "method": "blueprint_gate",
        "grain_checked": True,
    }


def test_an_unverified_blueprint_does_not_clear_an_earlier_badge() -> None:
    """`blueprint_verification` returns `None` for an unverified result, and the
    fold keeps the PREVIOUS verification rather than overwriting with `None` —
    while the chip still moves to the newest run."""
    accum = TurnAccumulators()
    accum.note_enrichment(
        "runBlueprint",
        {"slot_bindings": {}},
        _ok("runBlueprint", _blueprint_result("bp-1", terminal_sql="SELECT one")),
    )
    accum.note_enrichment(
        "runBlueprint",
        {"slot_bindings": {}},
        _ok(
            "runBlueprint",
            _blueprint_result("bp-2", terminal_sql="SELECT two", status="raw"),
        ),
    )

    envelope = accum.envelope()
    assert envelope.blueprint_use is not None
    assert envelope.blueprint_use["blueprint_id"] == "bp-2"
    assert envelope.verification == {
        "passed": True,
        "method": "blueprint_gate",
        "grain_checked": True,
    }


def test_an_empty_blueprint_result_withdraws_the_verification_claim() -> None:
    """J6 through the accumulator: zero rows is an EXPLICIT `passed: False`, not
    `None` (a blueprint DID run) and not a green badge (the grain teeth read
    `0 == 0` and would pass for every blueprint alive)."""
    accum = TurnAccumulators()

    accum.note_enrichment(
        "runBlueprint",
        {"slot_bindings": {}},
        _ok("runBlueprint", _blueprint_result(row_count=0)),
    )

    envelope = accum.envelope()
    assert envelope.verification is not None
    assert envelope.verification["passed"] is False
    assert envelope.verification["status"] == VERIFICATION_EMPTY_STATUS


def test_enrichment_ignores_a_refused_call_and_every_other_tool() -> None:
    """The loop folds AFTER its `tool_result` rewrites, so a refused call arrives
    here already non-`ok`. Nothing it carried may reach the user's answer."""
    accum = TurnAccumulators()

    accum.note_enrichment("runQuery", {"sql": "SELECT refused"}, _refused("runQuery"))
    accum.note_enrichment(
        "runBlueprint",
        {"slot_bindings": {}},
        _refused("runBlueprint", _blueprint_result()),
    )
    accum.note_enrichment("getTableSchema", {"table": "employee"}, _ok("getTableSchema"))

    assert accum.sql_executed is None
    assert accum.envelope() == AnswerEnvelope(
        answer_sql=None, blueprint_use=None, verification=None, answer_tables=None
    )


def test_enrichment_folds_onto_a_seed_rather_than_replacing_it() -> None:
    """The approval-resume case: the window starts with the pre-pause SQL and adds
    to it, deduping against what it inherited."""
    accum = TurnAccumulators(sql=["SELECT seeded"])

    accum.note_enrichment("runQuery", {"sql": "SELECT seeded"}, _ok("runQuery"))
    accum.note_enrichment("runQuery", {"sql": "SELECT fresh"}, _ok("runQuery"))

    assert accum.sql_executed == ["SELECT seeded", "SELECT fresh"]


# --- capture_blueprint_run --------------------------------------------------


def test_capture_blueprint_run_records_sql_badge_and_slots_under_the_id() -> None:
    accum = TurnAccumulators()

    accum.capture_blueprint_run(
        "runBlueprint",
        _ok("runBlueprint", _blueprint_result("bp-1", terminal_sql="SELECT terminal")),
        arguments={"slot_bindings": {"dept": "eng"}},
    )

    run = accum.blueprint_runs["bp-1"]
    assert run.terminal_sql == "SELECT terminal"
    assert run.slots == {"dept": "eng"}
    assert run.verification is not None and run.verification["passed"] is True


def test_capture_blueprint_run_is_a_no_op_off_its_own_tool_or_on_a_refusal() -> None:
    accum = TurnAccumulators()

    accum.capture_blueprint_run("runQuery", _ok("runQuery", _blueprint_result()))
    accum.capture_blueprint_run(
        "runBlueprint", _refused("runBlueprint", _blueprint_result())
    )
    accum.capture_blueprint_run("runBlueprint", _ok("runBlueprint", None))

    assert accum.blueprint_runs == {}


def test_a_later_run_of_the_same_blueprint_supersedes_the_earlier_one() -> None:
    """The map is keyed by blueprint id, so the badge and the SQL always come from
    the SAME run — which is the pairing hazard `BlueprintRun` exists to close."""
    accum = TurnAccumulators()

    accum.capture_blueprint_run(
        "runBlueprint",
        _ok("runBlueprint", _blueprint_result("bp-1", terminal_sql="SELECT first")),
        arguments={"slot_bindings": {"dept": "eng"}},
    )
    accum.capture_blueprint_run(
        "runBlueprint",
        _ok(
            "runBlueprint",
            _blueprint_result("bp-1", terminal_sql="SELECT second", status="raw"),
        ),
        arguments={"slot_bindings": {"dept": "sales"}},
    )

    run = accum.blueprint_runs["bp-1"]
    assert run.terminal_sql == "SELECT second"
    assert run.slots == {"dept": "sales"}
    assert run.verification is None


# --- note_result_sql --------------------------------------------------------


def test_result_sql_reads_the_argument_for_run_query_and_result_full_for_a_blueprint() -> None:
    """The two branches read DIFFERENT places for the same fact and neither can
    serve the other: a `runQuery`'s query is the argument the model sent, a
    blueprint's is the terminal node's, which only `result_full` knows."""
    accum = TurnAccumulators()

    accum.note_result_sql("runQuery", "call-1", {"sql": "SELECT q"}, _ok("runQuery"))
    accum.note_result_sql(
        "runBlueprint",
        "call-2",
        {"id": "bp-1"},
        _ok("runBlueprint", _blueprint_result("bp-1", terminal_sql="SELECT terminal")),
    )

    assert dict(accum.result_sql_by_call_id) == {
        "call-1": "SELECT q",
        "call-2": "SELECT terminal",
    }


def test_result_sql_ignores_refusals_blank_sql_and_non_dict_arguments() -> None:
    accum = TurnAccumulators()

    accum.note_result_sql("runQuery", "call-1", {"sql": "SELECT q"}, _refused("runQuery"))
    accum.note_result_sql("runQuery", "call-2", {"sql": ""}, _ok("runQuery"))
    accum.note_result_sql("runQuery", "call-3", {"sql": 17}, _ok("runQuery"))
    accum.note_result_sql("runQuery", "call-4", "not-a-dict", _ok("runQuery"))
    accum.note_result_sql("getTableSchema", "call-5", {"table": "e"}, _ok("getTableSchema"))
    accum.note_result_sql("runBlueprint", "call-6", {}, _ok("runBlueprint", None))

    assert accum.result_sql_by_call_id == {}


# --- note_assumptions -------------------------------------------------------


def test_assumptions_accumulate_deduped_in_first_occurrence_order() -> None:
    """An answer is one answer, but assumptions are ADDITIVE — a second call adds
    to the first rather than replacing it."""
    accum = TurnAccumulators()

    accum.note_assumptions(
        "recordAssumptions",
        {"assumptions": ["excludes contractors", "  headcount is FTE  "]},
        _ok("recordAssumptions"),
    )
    accum.note_assumptions(
        "recordAssumptions",
        {"assumptions": ["excludes contractors", "as of the latest load"]},
        _ok("recordAssumptions"),
    )

    assert accum.assumptions == [
        "excludes contractors",
        "headcount is FTE",
        "as of the latest load",
    ]


def test_assumptions_ignore_a_refused_call_and_every_other_tool() -> None:
    accum = TurnAccumulators()

    accum.note_assumptions(
        "recordAssumptions", {"assumptions": ["refused"]}, _refused("recordAssumptions")
    )
    accum.note_assumptions("runQuery", {"assumptions": ["wrong tool"]}, _ok("runQuery"))

    assert accum.assumptions is None


def test_assumptions_fold_onto_a_seed_and_dedupe_against_it() -> None:
    """`resume()`'s case: the trail-rebuilt assumptions of an earlier window, plus
    whatever this window records, with no duplicate across the join."""
    accum = TurnAccumulators(assumptions=["excludes contractors"])

    accum.note_assumptions(
        "recordAssumptions",
        {"assumptions": ["excludes contractors", "as of the latest load"]},
        _ok("recordAssumptions"),
    )

    assert accum.assumptions == ["excludes contractors", "as of the latest load"]


# --- note_answer_tables -----------------------------------------------------


def test_the_last_designation_wins_over_the_whole_set() -> None:
    """Not an append: a second `answerWithTable` means the model changed its mind
    about which query is the answer, and that is as true of a SET as of a string.
    """
    accum = TurnAccumulators()

    accum.note_answer_tables(
        "answerWithTable",
        _ok("answerWithTable"),
        [AnswerTable(sql="SELECT first"), AnswerTable(sql="SELECT second")],
    )
    accum.note_answer_tables(
        "answerWithTable", _ok("answerWithTable"), [AnswerTable(sql="SELECT third")]
    )

    envelope = accum.envelope()
    assert envelope.answer_tables is not None
    assert [table["sql"] for table in envelope.answer_tables] == ["SELECT third"]


def test_a_designation_that_resolves_to_nothing_keeps_the_earlier_good_set() -> None:
    """A malformed retry cannot silently drop the user's table — and this is also
    what stops the shape gate re-arming on that retry."""
    accum = TurnAccumulators()
    accum.note_answer_tables(
        "answerWithTable", _ok("answerWithTable"), [AnswerTable(sql="SELECT good")]
    )

    accum.note_answer_tables("answerWithTable", _ok("answerWithTable"), [])

    assert accum.has_answer_tables is True
    envelope = accum.envelope()
    assert envelope.answer_sql == "SELECT good"


def test_answer_tables_ignore_a_refused_call_and_every_other_tool() -> None:
    """A finalization refusal or the blueprint-not-run nudge rewrote `tool_result`
    before this fold ran; the designation it carried must not reach the user."""
    accum = TurnAccumulators()

    accum.note_answer_tables(
        "answerWithTable", _refused("answerWithTable"), [AnswerTable(sql="SELECT refused")]
    )
    accum.note_answer_tables(
        "runQuery", _ok("runQuery"), [AnswerTable(sql="SELECT wrong tool")]
    )

    assert accum.has_answer_tables is False
    assert accum.envelope().answer_tables is None


def test_a_fresh_designation_replaces_a_seeded_one() -> None:
    """The resumed window inherits the pre-pause designation, and last-wins still
    applies across the pause."""
    accum = TurnAccumulators(answer_tables=[AnswerTable(sql="SELECT seeded")])

    accum.note_answer_tables(
        "answerWithTable", _ok("answerWithTable"), [AnswerTable(sql="SELECT fresh")]
    )

    assert accum.envelope().answer_sql == "SELECT fresh"


def test_has_answer_tables_transitions_only_on_a_resolved_designation() -> None:
    accum = TurnAccumulators()
    assert accum.has_answer_tables is False

    accum.note_answer_tables("answerWithTable", _ok("answerWithTable"), [])
    assert accum.has_answer_tables is False

    accum.note_answer_tables(
        "answerWithTable", _ok("answerWithTable"), [AnswerTable(sql="SELECT 1")]
    )
    assert accum.has_answer_tables is True

    # It NEVER falls back: a later empty designation leaves the set intact.
    accum.note_answer_tables("answerWithTable", _ok("answerWithTable"), [])
    assert accum.has_answer_tables is True


# --- the exits --------------------------------------------------------------


def test_the_empty_to_none_fork_on_both_list_exits() -> None:
    """`[] -> None` on `sql_executed` and `assumptions` alike, so the UI treats
    "no panel" and "empty panel" identically (contract §1 fork 1)."""
    empty = TurnAccumulators(sql=[], assumptions=[])
    assert empty.sql_executed is None
    assert empty.assumptions is None

    filled = TurnAccumulators(sql=["SELECT 1"], assumptions=["an assumption"])
    assert filled.sql_executed == ["SELECT 1"]
    assert filled.assumptions == ["an assumption"]


def test_the_envelope_falls_back_to_the_turn_level_enrichment_with_no_table() -> None:
    """The no-table case has no grid to over-claim on, so the fields mean what they
    have always meant: this turn's PROSE answer came from a verified blueprint —
    the enrichment the approval-resume seed exists to carry across a pause."""
    accum = TurnAccumulators(
        blueprint_use={"blueprint_id": "bp-1", "slots": {}},
        verification={"passed": True, "method": "blueprint_gate"},
    )

    assert accum.envelope() == AnswerEnvelope(
        answer_sql=None,
        blueprint_use={"blueprint_id": "bp-1", "slots": {}},
        verification={"passed": True, "method": "blueprint_gate"},
        answer_tables=None,
    )


def test_a_designated_table_discards_the_turn_level_chip_and_badge() -> None:
    """THE OVER-CLAIM CORRECTION. As soon as there IS a designated table the
    derived values win outright, INCLUDING when they are `None` — a verified
    blueprint that ran for part 1 may not badge a hand-written grid for part 2."""
    accum = TurnAccumulators(
        answer_tables=[AnswerTable(sql="SELECT hand written")],
        blueprint_use={"blueprint_id": "bp-1", "slots": {}},
        verification={"passed": True, "method": "blueprint_gate"},
    )

    envelope = accum.envelope()
    assert envelope.answer_sql == "SELECT hand written"
    assert envelope.blueprint_use is None
    assert envelope.verification is None
    assert envelope.answer_tables == [
        {"sql": "SELECT hand written", "caption": None, "blueprint_use": None, "verification": None}
    ]


def test_the_envelope_projects_the_first_table_and_rolls_verification_up() -> None:
    """The PRIMARY is the first item (the model's lead table), and `verification`
    is the conservative AND over every designated table."""
    verified = {"passed": True, "method": "blueprint_gate"}
    accum = TurnAccumulators(
        answer_tables=[
            AnswerTable(
                sql="SELECT lead",
                blueprint_use={"blueprint_id": "bp-1", "slots": {}},
                verification=dict(verified),
            ),
            AnswerTable(sql="SELECT second", verification=dict(verified)),
        ]
    )

    envelope = accum.envelope()
    assert envelope.answer_sql == "SELECT lead"
    assert envelope.blueprint_use == {"blueprint_id": "bp-1", "slots": {}}
    assert envelope.verification is not None and envelope.verification["passed"] is True

    mixed = TurnAccumulators(
        answer_tables=[
            AnswerTable(sql="SELECT lead", verification=dict(verified)),
            AnswerTable(sql="SELECT unverified"),
        ]
    )
    assert mixed.envelope().verification is None


def test_the_envelope_is_recomputed_from_current_state_at_every_call() -> None:
    """Six exits call it, one of them unconditionally on every dispatched tool
    call. Each must see the state as of ITS moment, never a cached earlier one."""
    accum = TurnAccumulators()
    assert accum.envelope().answer_sql is None

    accum.note_answer_tables(
        "answerWithTable", _ok("answerWithTable"), [AnswerTable(sql="SELECT late")]
    )
    assert accum.envelope().answer_sql == "SELECT late"


# --- the two module-level functions the resume path shares ------------------


def test_the_windowless_helpers_produce_exactly_what_the_folds_do() -> None:
    """`_resume_blueprint` runs `accumulate_enrichment` + `capture_terminal_sql`
    to BUILD the seeds of a window that does not exist yet. Sharing the functions
    is what stops the resumed answer drifting from an unpaused one — so the two
    routes must agree field for field."""
    result_full = _blueprint_result("bp-1", terminal_sql="SELECT bp")
    slots = {"dept": "eng"}

    seed_sql: list[str] = []
    seed_blueprint_use, seed_verification = accumulate_enrichment(
        "runBlueprint",
        {"slot_bindings": slots},
        _ok("runBlueprint", result_full),
        turn_sql=seed_sql,
        blueprint_use=None,
        verification=None,
    )
    seed_runs: dict[str, BlueprintRun] = {}
    capture_terminal_sql(
        "runBlueprint",
        _ok("runBlueprint", result_full),
        into=seed_runs,
        arguments={"slot_bindings": slots},
    )
    seeded = TurnAccumulators(
        sql=seed_sql,
        blueprint_runs=seed_runs,
        blueprint_use=seed_blueprint_use,
        verification=seed_verification,
    )

    in_window = TurnAccumulators()
    in_window.note_enrichment(
        "runBlueprint", {"slot_bindings": slots}, _ok("runBlueprint", result_full)
    )
    in_window.capture_blueprint_run(
        "runBlueprint",
        _ok("runBlueprint", result_full),
        arguments={"slot_bindings": slots},
    )

    assert seeded.sql_executed == in_window.sql_executed == ["SELECT bp"]
    assert seeded.envelope() == in_window.envelope()
    assert dict(seeded.blueprint_runs) == dict(in_window.blueprint_runs)


def test_answer_envelope_is_callable_on_a_bare_table_list() -> None:
    """The function stays public because the envelope shape is read by the pause
    path through `_pause_from_runtime_tool`'s `envelope=` parameter, which takes
    the value rather than the accumulator."""
    assert answer_envelope([], blueprint_use=None, verification=None) == AnswerEnvelope(
        answer_sql=None, blueprint_use=None, verification=None, answer_tables=None
    )
