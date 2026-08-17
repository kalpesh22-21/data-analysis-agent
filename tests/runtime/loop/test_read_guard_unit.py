"""Unit tests for `loop/read_guard.py::ReadGuard` — the repeated-idempotent-read
guard's STATE and DECISION, exercised through its own interface rather than through
a whole scripted turn.

These are the fast, exhaustive complement to the e2e suites
(`test_repeated_idempotent_read_guard.py`, `test_trimmed_read_refetch.py`,
`test_blueprint_definition_gate.py`), which stay the proof that the loop wires the
decision to the right effects — the marker `TrailEntry`, the append-then-emit order,
the budget accounting. Here we pin the decision itself: the seeding rules, the
per-round reset, the trim-aware exemption and its cap, the only-`ok` recording
contract, and the exact event payloads.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.loop.read_guard import (
    _MAX_TRIMMED_READ_REFETCHES,
    IDEMPOTENT_READ_TOOLS,
    ReadGuard,
    idempotent_read_signature,
)

_DB = "dbpcm_warehouse"
_TABLE = "employee"
_SCHEMA_ARGS = {"database": _DB, "table": _TABLE}
# `context/assembly.py::IDEMPOTENT_READ_ALREADY_SERVED_CODE`, re-typed here only as
# a value the caller passes in: `data_free` is the guard's parameter, and the guard
# deliberately does not import the assembly constant (it is a leaf).
_MARKER_IS_DATA_FREE = True


class _Recorder:
    """Records every `(event, payload)` the guard emits, in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def payloads(self, event: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.events if name == event]


def _guard(readable: frozenset[str] | None = None) -> tuple[ReadGuard, _Recorder]:
    """A guard with its round already begun — the loop always calls `begin_round`
    before dispatching a batch, so a test that skipped it would be testing a state
    the production path never reaches."""
    recorder = _Recorder()
    guard = ReadGuard(recorder)
    guard.begin_round(readable if readable is not None else frozenset())
    return guard, recorder


# --- classification basics --------------------------------------------------


def test_first_read_is_never_declined() -> None:
    guard, recorder = _guard()
    decision = guard.classify("getTableSchema", _SCHEMA_ARGS)
    assert decision.is_idempotent_read is True
    assert decision.declined is False
    assert decision.signature == idempotent_read_signature("getTableSchema", _SCHEMA_ARGS)
    assert recorder.events == []


def test_non_idempotent_tool_is_not_a_read_and_is_never_declined() -> None:
    """`runQuery` is deliberately excluded: a repeat may be a distinct legitimate
    step. `signature is None` IS "not a read" — the two can never drift, because
    `is_idempotent_read` is derived from it."""
    guard, _ = _guard()
    sql = {"sql": "SELECT 1"}
    guard.classify("runQuery", sql)
    repeat = guard.classify("runQuery", sql)
    assert repeat.is_idempotent_read is False
    assert repeat.signature is None
    assert repeat.declined is False


def test_repeat_of_a_readable_served_read_is_declined() -> None:
    guard, recorder = _guard(readable=frozenset({"call-1"}))
    first = guard.classify("getTableSchema", _SCHEMA_ARGS)
    guard.record_served(first, "call-1")
    guard.begin_round(frozenset({"call-1"}))

    repeat = guard.classify("getTableSchema", _SCHEMA_ARGS)
    assert repeat.declined is True
    # No exemption event: the serving result is still readable, so the guard fires.
    assert recorder.events == []


def test_different_args_are_a_different_signature() -> None:
    guard, _ = _guard(readable=frozenset({"call-1"}))
    first = guard.classify("getTableSchema", _SCHEMA_ARGS)
    guard.record_served(first, "call-1")
    guard.begin_round(frozenset({"call-1"}))

    other = guard.classify("getTableSchema", {"database": _DB, "table": "payroll"})
    assert other.declined is False


def test_key_order_does_not_change_the_signature() -> None:
    """The signature canonicalizes key order, so a model that emits the same args in
    a different order still deduplicates."""
    guard, _ = _guard(readable=frozenset({"call-1"}))
    first = guard.classify("getTableSchema", {"database": _DB, "table": _TABLE})
    guard.record_served(first, "call-1")
    guard.begin_round(frozenset({"call-1"}))

    repeat = guard.classify("getTableSchema", {"table": _TABLE, "database": _DB})
    assert repeat.declined is True


def test_every_tool_in_the_set_is_classified_as_a_read() -> None:
    """Uniform across `IDEMPOTENT_READ_TOOLS` — a per-tool carve-out is exactly what
    the guard's contract forbids."""
    guard, _ = _guard()
    for tool_name in sorted(IDEMPOTENT_READ_TOOLS):
        assert guard.classify(tool_name, {"probe": tool_name}).is_idempotent_read is True


# --- trail seeding ----------------------------------------------------------


def test_prior_read_seeds_both_the_signature_and_its_serving_pointer() -> None:
    """Seeding from the persisted trail is what lets a fresh window (a budget-cap
    `continue`, or just the D45 per-round-trip rebuild) recognize a repeat it did
    not itself serve."""
    guard, recorder = _guard(readable=frozenset({"prior-1"}))
    guard.observe_prior_read("getTableSchema", _SCHEMA_ARGS, "prior-1", data_free=False)

    decision = guard.classify("getTableSchema", _SCHEMA_ARGS)
    assert decision.declined is True
    # The pointer was seeded too, and `prior-1` is readable — so no exemption.
    assert recorder.events == []


def test_data_free_marker_seeds_seen_but_never_becomes_the_serving_pointer() -> None:
    """A guard-marker entry proves the signature was served but holds NO data, so it
    must not become the pointer the readability test follows — otherwise a window
    whose only surviving entry is the marker would look "still readable" and the
    model would be nudged toward a result it cannot read."""
    guard, recorder = _guard(readable=frozenset({"marker-1"}))
    guard.observe_prior_read(
        "getTableSchema", _SCHEMA_ARGS, "marker-1", data_free=_MARKER_IS_DATA_FREE
    )

    decision = guard.classify("getTableSchema", _SCHEMA_ARGS)
    # Seen (so it IS a repeat) but with no serving pointer → the exemption fires with
    # `no_readable_source` and the repeat is re-dispatched.
    assert decision.declined is False
    assert recorder.names() == ["loop_trimmed_read_refetch_allowed"]
    assert recorder.payloads("loop_trimmed_read_refetch_allowed")[0]["reason"] == (
        "no_readable_source"
    )


def test_a_real_read_after_a_marker_installs_the_pointer() -> None:
    """The trail walk visits entries in order; whichever real read is seen last owns
    the pointer, and the marker never disturbs it."""
    guard, recorder = _guard(readable=frozenset({"real-1"}))
    guard.observe_prior_read("getTableSchema", _SCHEMA_ARGS, "real-1", data_free=False)
    guard.observe_prior_read("getTableSchema", _SCHEMA_ARGS, "marker-2", data_free=True)

    assert guard.classify("getTableSchema", _SCHEMA_ARGS).declined is True
    assert recorder.events == []


def test_non_read_trail_entries_are_ignored_by_the_seed() -> None:
    guard, _ = _guard()
    guard.observe_prior_read("runQuery", {"sql": "SELECT 1"}, "prior-q", data_free=False)
    assert guard.classify("runQuery", {"sql": "SELECT 1"}).declined is False


# --- emulation seeding ------------------------------------------------------


def test_emulation_seeding_dedupes_a_model_recall_of_the_swept_listing() -> None:
    """The whole point of the discovery sweep: a model re-call of
    `listDatabases`/`listTables` is served locally instead of re-dispatched."""
    signature = idempotent_read_signature("listTables", {"database": _DB})
    guard, recorder = _guard(readable=frozenset({"emulated-1"}))
    guard.seed_emulation({signature}, {signature: "emulated-1"})

    assert guard.classify("listTables", {"database": _DB}).declined is True
    assert recorder.events == []


def test_emulation_signature_without_a_readable_pointer_is_exempted() -> None:
    """If the synthetic pair is not readable (it was not pinned, or the sweep seeded
    no pointer), the exemption re-dispatches rather than nudging about something
    absent — the same rule as any other read."""
    signature = idempotent_read_signature("listDatabases", {})
    guard, recorder = _guard(readable=frozenset())
    guard.seed_emulation({signature}, {signature: "emulated-1"})

    decision = guard.classify("listDatabases", {})
    assert decision.declined is False
    assert recorder.payloads("loop_trimmed_read_refetch_allowed")[0]["reason"] == (
        "result_not_readable_in_window"
    )


# --- per-round reset --------------------------------------------------------


def test_second_identical_call_in_one_batch_is_declined_not_exempted() -> None:
    """`readable_tool_call_ids` describes the window BEFORE this batch ran, so a read
    dispatched moments ago is necessarily absent from it. Treating that as "trimmed
    away" would re-dispatch the second of two identical calls in one batch — exactly
    the duplicate the guard exists to collapse."""
    guard, recorder = _guard(readable=frozenset())
    first = guard.classify("getTableSchema", _SCHEMA_ARGS)
    guard.record_served(first, "call-1")

    repeat = guard.classify("getTableSchema", _SCHEMA_ARGS)
    assert repeat.declined is True
    assert recorder.events == []


def test_next_round_reopens_the_exemption_when_the_result_was_trimmed() -> None:
    """"Served this round" means "will be readable next round" — so once the next
    round begins and the pointer is NOT readable, the exemption is available again."""
    guard, recorder = _guard(readable=frozenset())
    first = guard.classify("getTableSchema", _SCHEMA_ARGS)
    guard.record_served(first, "call-1")
    guard.begin_round(frozenset())  # `call-1` did not survive the fit

    repeat = guard.classify("getTableSchema", _SCHEMA_ARGS)
    assert repeat.declined is False
    assert recorder.names() == ["loop_trimmed_read_refetch_allowed"]


# --- the trim-aware exemption and its cap -----------------------------------


def test_exemption_flips_the_decline_and_reports_the_missing_source() -> None:
    guard, recorder = _guard(readable=frozenset({"someone-else"}))
    guard.observe_prior_read("getTableSchema", _SCHEMA_ARGS, "call-1", data_free=False)

    decision = guard.classify("getTableSchema", _SCHEMA_ARGS)
    assert decision.declined is False
    payload = recorder.payloads("loop_trimmed_read_refetch_allowed")[0]
    assert payload["reason"] == "result_not_readable_in_window"
    assert payload["refetch_count"] == 0
    assert payload["refetch_cap"] == _MAX_TRIMMED_READ_REFETCHES


def test_exemption_is_capped_per_signature_per_window() -> None:
    """`_MAX_TRIMMED_READ_REFETCHES` grants, then the guard resumes: fetched →
    trimmed → re-fetched → trimmed is in aggregate the waste the guard exists to
    prevent, and past the cap the cheap nudge is strictly better."""
    guard, recorder = _guard(readable=frozenset())
    guard.observe_prior_read("getTableSchema", _SCHEMA_ARGS, "call-1", data_free=False)

    for granted in range(_MAX_TRIMMED_READ_REFETCHES):
        guard.begin_round(frozenset())
        decision = guard.classify("getTableSchema", _SCHEMA_ARGS)
        assert decision.declined is False, f"exemption {granted} should have been granted"
        assert recorder.payloads("loop_trimmed_read_refetch_allowed")[granted][
            "refetch_count"
        ] == granted

    guard.begin_round(frozenset())
    capped = guard.classify("getTableSchema", _SCHEMA_ARGS)
    assert capped.declined is True
    assert len(recorder.payloads("loop_trimmed_read_refetch_allowed")) == (
        _MAX_TRIMMED_READ_REFETCHES
    )
    capped_payloads = recorder.payloads("loop_trimmed_read_refetch_capped")
    assert len(capped_payloads) == 1
    assert capped_payloads[0]["refetch_count"] == _MAX_TRIMMED_READ_REFETCHES
    assert capped_payloads[0]["deduped"] is False


def test_the_capped_event_fires_on_every_post_cap_round() -> None:
    """The capped event is the one worth ALERTING on — it means the turn is
    thrashing — so it must be emitted on EVERY post-cap round, not once per window.
    A one-shot signal would make a turn that keeps re-reading a dropped result look
    like a single blip in the trace, understating exactly the condition the event
    exists to surface. The decline itself is unaffected: past the cap the guard
    resumes for good."""
    guard, recorder = _guard(readable=frozenset())
    guard.observe_prior_read("getTableSchema", _SCHEMA_ARGS, "call-1", data_free=False)

    for _ in range(_MAX_TRIMMED_READ_REFETCHES):
        guard.begin_round(frozenset())
        guard.classify("getTableSchema", _SCHEMA_ARGS)

    guard.begin_round(frozenset())
    assert guard.classify("getTableSchema", _SCHEMA_ARGS).declined is True
    assert len(recorder.payloads("loop_trimmed_read_refetch_capped")) == 1

    # A fresh round with the pointer still unreadable: the cap still bites, AND it
    # says so again.
    guard.begin_round(frozenset())
    assert guard.classify("getTableSchema", _SCHEMA_ARGS).declined is True
    capped_payloads = recorder.payloads("loop_trimmed_read_refetch_capped")
    assert len(capped_payloads) == 2
    # No further exemption was spent, so the second report reads identically.
    assert capped_payloads[1]["refetch_count"] == _MAX_TRIMMED_READ_REFETCHES
    assert len(recorder.payloads("loop_trimmed_read_refetch_allowed")) == (
        _MAX_TRIMMED_READ_REFETCHES
    )


def test_the_cap_is_per_signature_not_shared() -> None:
    other_args = {"database": _DB, "table": "payroll"}
    guard, recorder = _guard(readable=frozenset())
    guard.observe_prior_read("getTableSchema", _SCHEMA_ARGS, "call-1", data_free=False)
    guard.observe_prior_read("getTableSchema", other_args, "call-2", data_free=False)

    for _ in range(_MAX_TRIMMED_READ_REFETCHES + 1):
        guard.begin_round(frozenset())
        guard.classify("getTableSchema", _SCHEMA_ARGS)
    assert len(recorder.payloads("loop_trimmed_read_refetch_capped")) == 1

    guard.begin_round(frozenset())
    # The OTHER signature has spent nothing, so its first exemption is still there.
    assert guard.classify("getTableSchema", other_args).declined is False
    assert recorder.payloads("loop_trimmed_read_refetch_allowed")[-1]["table"] == "payroll"


def test_a_re_fetch_repoints_at_the_freshest_serving_entry() -> None:
    """After an exemption the model re-fetches for real; the new entry is where the
    result lives now, so the pointer must move off the stale trimmed one — otherwise
    every later round would keep granting exemptions against a dead id."""
    guard, recorder = _guard(readable=frozenset())
    guard.observe_prior_read("getTableSchema", _SCHEMA_ARGS, "stale-1", data_free=False)

    exempted = guard.classify("getTableSchema", _SCHEMA_ARGS)
    guard.record_served(exempted, "fresh-2")
    guard.begin_round(frozenset({"fresh-2"}))

    assert guard.classify("getTableSchema", _SCHEMA_ARGS).declined is True
    assert len(recorder.payloads("loop_trimmed_read_refetch_allowed")) == 1


# --- the only-`ok` recording contract ---------------------------------------


def test_an_unrecorded_read_is_not_already_served() -> None:
    """The caller records ONLY `status == "ok"` results, so a denied/errored read is
    never "already served" and a legitimate retry after a transient failure is never
    suppressed."""
    guard, recorder = _guard(readable=frozenset())
    guard.classify("getTableSchema", _SCHEMA_ARGS)  # dispatched, then failed
    guard.begin_round(frozenset())

    retry = guard.classify("getTableSchema", _SCHEMA_ARGS)
    assert retry.declined is False
    # Not a repeat at all — so not an exemption either, and nothing is emitted.
    assert recorder.events == []


def test_record_served_on_a_non_read_decision_is_a_no_op() -> None:
    guard, _ = _guard()
    decision = guard.classify("runQuery", {"sql": "SELECT 1"})
    guard.record_served(decision, "call-1")
    assert guard.classify("runQuery", {"sql": "SELECT 1"}).declined is False


# --- event payloads (D25) ---------------------------------------------------


def test_exemption_payload_carries_catalog_safe_identifiers_only() -> None:
    guard, recorder = _guard(readable=frozenset())
    guard.observe_prior_read("getTableSchema", _SCHEMA_ARGS, "call-1", data_free=False)
    guard.classify("getTableSchema", _SCHEMA_ARGS)

    payload = recorder.payloads("loop_trimmed_read_refetch_allowed")[0]
    assert payload["tool_name"] == "getTableSchema"
    assert payload["deduped"] is False
    assert payload["dedup_target"] == f"{_DB}.{_TABLE}"
    assert payload["database"] == _DB
    assert payload["table"] == _TABLE


def test_explain_query_sql_never_reaches_an_exemption_payload() -> None:
    """D25: free-form args are never placed on a span, so an `explainQuery` read
    identifies as the empty target rather than by its query text."""
    sql = "SELECT secret FROM dbpcm_warehouse.payroll"
    guard, recorder = _guard(readable=frozenset())
    guard.observe_prior_read("explainQuery", {"sql": sql}, "call-1", data_free=False)
    guard.classify("explainQuery", {"sql": sql})

    payload = recorder.payloads("loop_trimmed_read_refetch_allowed")[0]
    assert payload["dedup_target"] == ""
    assert not any("secret" in str(value) for value in payload.values())


def test_get_blueprint_exemption_payload_carries_the_blueprint_id() -> None:
    guard, recorder = _guard(readable=frozenset())
    guard.observe_prior_read("getBlueprint", {"id": "bp-headcount"}, "call-1", data_free=False)
    guard.classify("getBlueprint", {"id": "bp-headcount"})

    payload = recorder.payloads("loop_trimmed_read_refetch_allowed")[0]
    assert payload["blueprint_id"] == "bp-headcount"
    assert payload["dedup_target"] == "bp-headcount"


def test_a_non_get_blueprint_id_arg_never_leaks_onto_a_payload() -> None:
    """The `id` surfacing is matched on the TOOL NAME, so a future guarded tool that
    happens to take an `id` cannot start leaking a free-form value."""
    guard, recorder = _guard(readable=frozenset())
    guard.observe_prior_read("listTables", {"id": "free-form"}, "call-1", data_free=False)
    guard.classify("listTables", {"id": "free-form"})

    payload = recorder.payloads("loop_trimmed_read_refetch_allowed")[0]
    assert "blueprint_id" not in payload
    assert payload["dedup_target"] == ""
