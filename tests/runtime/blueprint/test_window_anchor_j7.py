"""J7 — a blueprint DECLARES its window anchor, and the runtime says so out loud.

WHAT WAS WRONG (confirmed live, L3). `bp-hires-per-month` anchors its trailing
window on `max(hire_date)` — the latest data on record — deliberately, so the
fixture does not decay to empty as wall-clock time passes. On this warehouse that
means a "last six months" run answers about 2021. The user means six CALENDAR
months, and since the date-anchor injection the model has a grounded "today", so
it ran the blueprint, judged the 2021 window unresponsive, re-derived the whole
thing with its own `toDate(today)`-anchored SQL, and completed the intent with
that unverified query as the evidence.

NOTHING WAS WRONG WITH THE BLUEPRINT. What was missing is that it never SAID which
anchor it uses, so the only way to find out was to read the rows — and reading the
rows is exactly what produced the wrong conclusion. The fix is therefore a
DECLARATION plus two surfaces, and deliberately NOT a behaviour change:

  1. `window_anchor: data | calendar`, an OPTIONAL corpus field. Absent = no claim,
     and the eight non-windowed blueprints stay byte-identical.
  2. `getBlueprint` renders the declaration — the read the model makes BEFORE
     running, and the only place a composed blueprint's anchor could ever surface
     (its per-node SQL is hidden).
  3. `runBlueprint` carries a one-line note on the result — the moment the wrong
     conclusion was actually drawn.

The tests below walk the value end to end, because it crosses seven layers and a
drop at any one of them is silent: seed -> loader validation -> neo4j property ->
`BlueprintDetail` -> `Blueprint` -> `result_full` -> `ToolResult` -> `TrailEntry`
-> the rendered tool message. `authoritative` lost its marker on the resume path
exactly this way.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.compiler import (
    dag_properties,
    validate_blueprint_dag,
)
from data_agent.runtime.blueprint.executor import (
    BlueprintExecutor,
    ExecCompleted,
    _stamp_window_anchor,
)
from data_agent.runtime.blueprint.models import (
    DATA_ANCHORED_RESULT_NOTE,
    DATA_ANCHORED_RESULT_NOTE_TEMPLATE,
    WINDOW_ANCHOR_GLOSS,
    WINDOW_ANCHORS,
    Blueprint,
    BlueprintParseError,
    window_anchor_declaration,
)
from data_agent.runtime.blueprint.tool import (
    blueprint_outcome_to_tool_result,
    window_note_for_result,
)
from data_agent.runtime.context.budget import _render_entry
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult
from data_agent.runtime.loop.agent_loop import _tool_trail_entry_to_canonical
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.corpus_loader import (
    _UPSERT_BLUEPRINT,
    BlueprintSeed,
    CorpusLoadError,
)
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.tools import GetBlueprintTool
from data_agent.runtime.retrieval.vector_index import (
    FakeVectorIndex,
    _coerce_window_anchor,
    map_blueprint_detail_record,
)
from data_agent.runtime.session.models import ResultPreview, TrailEntry

_E = "dbpcm_warehouse.employee"
_CODE_COL = f"{_E}.employee_code"
_HIRE_COL = f"{_E}.hire_date"
_STATUS_COL = f"{_E}.employee_status"

CATALOG = CatalogHandle(
    {
        _E: {
            "employee_code": "String",
            "hire_date": "Nullable(Date)",
            "employee_status": "Nullable(String)",
        }
    }
)

# A data-anchored trailing window, structurally identical to the canon blueprint's
# (the `max(hire_date)` anchor subquery is the thing being declared).
_HIRES_SQL = (
    "SELECT toStartOfMonth(hire_date) AS month, "
    "COUNT(DISTINCT employee_code) AS hires "
    "FROM dbpcm_warehouse.employee "
    "WHERE hire_date >= ("
    "  SELECT toStartOfMonth(max(hire_date)) FROM dbpcm_warehouse.employee"
    ") - INTERVAL {window_months} MONTH "
    "GROUP BY toStartOfMonth(hire_date)"
)

_WINDOW_SLOT: dict[str, Any] = {
    "name": "window_months",
    "type": "relative_window",
    "required": True,
    "min_value": 1,
    "max_value": 36,
}


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s-j7", jwt="jwt-secret", column_scope=scope)


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _detail(
    *, window_anchor: str | None, result_grain: list[str] | None = None
) -> BlueprintDetail:
    return BlueprintDetail(
        id="bp-hires-per-month",
        intent="New hires per month over a trailing window of the last N months",
        slots_summary="window_months",
        uses=frozenset({_CODE_COL, _HIRE_COL, _STATUS_COL}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        slots=[_WINDOW_SLOT],
        sql_template=_HIRES_SQL,
        result_grain=["month"] if result_grain is None else result_grain,
        window_anchor=window_anchor,
    )


def _seed(**overrides: Any) -> BlueprintSeed:
    base: dict[str, Any] = {
        "id": "bp-hires-per-month",
        "intent": "New hires per month over a trailing window",
        "slots_summary": "window_months",
        "uses": [_CODE_COL, _HIRE_COL, _STATUS_COL],
        "slots": [dict(_WINDOW_SLOT)],
        "sql_template": _HIRES_SQL,
        "result_grain": ["month"],
    }
    base.update(overrides)
    return BlueprintSeed(**base)


def _executor(mcp: FakeMCPClient, detail: BlueprintDetail) -> BlueprintExecutor:
    index = FakeVectorIndex()
    index.add_detail(detail)
    return BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=index)


async def _run(detail: BlueprintDetail) -> ExecCompleted:
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["month", "hires"], [["2021-06-01", 4]]),  # the node
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),  # grain probe
            ]
        }
    )
    outcome = await _executor(mcp, detail).execute(
        blueprint_id=detail.id, slot_bindings={"window_months": 6}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    return outcome


async def _run_terminal(
    detail: BlueprintDetail,
    columns: list[str],
    rows: list[list[Any]],
    *,
    truncated: bool = False,
) -> ExecCompleted:
    """One single-node run whose TERMINAL result is exactly *columns*/*rows* — the input
    the concrete window is derived from. The grain probe is scripted CONSISTENT
    (total == distinct) so verify always passes and every assertion below is about the
    window derivation rather than about the D56 gate."""
    node = _rq(columns, rows)
    node["truncated"] = truncated
    mcp = FakeMCPClient(
        scripted={"runQuery": [node, _rq(["__bp_n", "__bp_d"], [[len(rows), len(rows)]])]}
    )
    outcome = await _executor(mcp, detail).execute(
        blueprint_id=detail.id, slot_bindings={"window_months": 6}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    return outcome


# The note as the model actually receives it for the fixture's `2021-06-01` window,
# written out longhand rather than formatted — the wording IS the deliverable, and a
# test that builds it from the same template it is checking asserts nothing.
_CONCRETE_NOTE = (
    "Window is data-anchored: it counts back from the latest data on record, which "
    "ends 2021-06-01 — not from today's date. Present it as 'as of the latest data "
    "(2021-06-01)' and state in prose when that differs from the calendar period the "
    "user asked about; do not re-derive with a calendar-anchored query."
)


# ---------------------------------------------------------------------------
# 1. The parse layer — the closed set, and what "absent" means
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("anchor", sorted(WINDOW_ANCHORS))
def test_a_declared_anchor_round_trips_through_the_parse_layer(anchor: str) -> None:
    """Every value in the closed set parses to itself. Parametrized over the set
    rather than over two literals, so a third anchor cannot be added to the
    vocabulary without this test covering it."""
    blueprint = Blueprint.parse(
        id="bp-x", intent="i", sql_template="SELECT 1", window_anchor=anchor
    )
    assert blueprint.window_anchor == anchor


def test_an_absent_anchor_parses_to_none_and_is_the_default() -> None:
    """ABSENT IS A DISTINCT STATE, not a synonym for `calendar`. A blueprint that
    declares nothing makes no claim, and `None` is what every surfacing site keys
    off to stay silent — collapsing it to a default anchor would put a sentence the
    author never wrote in front of the model on eight blueprints."""
    parsed = Blueprint.parse(id="bp-x", intent="i", sql_template="SELECT 1")
    assert parsed.window_anchor is None
    assert Blueprint(id="bp-x", intent="i").window_anchor is None
    assert (
        Blueprint.parse(id="bp-x", intent="i", sql_template="SELECT 1", window_anchor=None
    ).window_anchor is None)


@pytest.mark.parametrize("bad", ["date", "DATA", "today", "", 1, True, [], {"a": 1}])
def test_an_unknown_anchor_is_rejected_rather_than_dropped(bad: Any) -> None:
    """A present-but-unknown value is an AUTHORING ERROR and fails loud.

    Dropping it would be the worse of the two failures: `window_anchor: date` would
    load looking declared, surface nothing, and put the model straight back to
    inferring the anchor from the rows — the exact failure this field exists to
    stop, now with a YAML line that says it was handled.

    `True` and `[]` are in the list on purpose. `x not in <frozenset>` hashes `x`,
    so an unhashable value would raise `TypeError` past every
    `except BlueprintParseError` on the read path; and `isinstance(True, int)` is
    the bool-subclass trap the rest of this module guards everywhere.
    """
    with pytest.raises(BlueprintParseError, match="window_anchor"):
        Blueprint.parse(id="bp-x", intent="i", sql_template="SELECT 1", window_anchor=bad)


def test_every_anchor_in_the_closed_set_has_a_gloss_by_construction() -> None:
    """`WINDOW_ANCHORS` is DERIVED from `WINDOW_ANCHOR_GLOSS`, so "accepted at load"
    and "renderable to the model" are the same set and cannot drift apart. The
    sibling `SLOT_TYPES`/`SLOT_TYPE_GLOSS` pair is written the other way round and
    needs a parity test to hold it together; this asserts the derivation instead."""
    assert WINDOW_ANCHORS == frozenset(WINDOW_ANCHOR_GLOSS)
    for anchor in WINDOW_ANCHORS:
        declaration = window_anchor_declaration(anchor)
        assert declaration is not None
        assert declaration.startswith(f"{anchor} — ")


@pytest.mark.parametrize("bad", [None, "date", "", 7, ["data"]])
def test_the_declaration_helper_is_total_and_never_prints_a_bare_value(bad: Any) -> None:
    """Read-side depth: a corrupt stored anchor renders as NOTHING rather than as a
    bare enum the model would have to interpret, and never raises into a tool
    result."""
    assert window_anchor_declaration(bad) is None


# ---------------------------------------------------------------------------
# 2. The loader — the seed accepts it, validates it, and stores it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("anchor", sorted(WINDOW_ANCHORS))
def test_the_seed_carries_the_anchor_to_the_node_property(anchor: str) -> None:
    """`dag_properties` is what the upsert spreads, so a field it does not carry is
    a field the graph never sees — no matter how well it parsed."""
    props = dag_properties(_seed(window_anchor=anchor))
    assert props["window_anchor"] == anchor
    assert "$window_anchor" in _UPSERT_BLUEPRINT


def test_an_undeclared_anchor_writes_null_so_a_re_seed_clears_a_stale_claim() -> None:
    """`None`, not `""`. neo4j REMOVES a property set to null, which is what makes a
    re-seed that DROPS the declaration actually retract it; an empty string would
    leave a third state (`""`) that matches no anchor and is not absent either."""
    assert dag_properties(_seed())["window_anchor"] is None
    assert dag_properties(_seed(window_anchor=""))["window_anchor"] is None


@pytest.mark.parametrize("anchor", sorted(WINDOW_ANCHORS))
def test_write_time_validation_accepts_a_declared_anchor(anchor: str) -> None:
    validate_blueprint_dag(_seed(window_anchor=anchor))


def test_write_time_validation_rejects_an_unknown_anchor_for_every_write_path() -> None:
    """`validate_blueprint_dag` runs over EVERY seed in `load_corpus`'s pre-write
    pass — fixture seed, MCP-export hydration and the learning landing writer alike
    — so this one gate is why the read side never has to ask whether a stored anchor
    is renderable."""
    with pytest.raises(CorpusLoadError, match="window_anchor"):
        validate_blueprint_dag(_seed(window_anchor="yesterday"))


# ---------------------------------------------------------------------------
# 3. The read path — neo4j record -> BlueprintDetail
# ---------------------------------------------------------------------------


def test_the_keyed_fetch_selects_and_maps_the_stored_anchor() -> None:
    from data_agent.runtime.retrieval.vector_index import _GET_BLUEPRINT_QUERY

    assert "b.window_anchor AS window_anchor" in _GET_BLUEPRINT_QUERY
    detail = map_blueprint_detail_record(
        {"id": "bp-x", "uses": [], "window_anchor": "data"}
    )
    assert detail.window_anchor == "data"


@pytest.mark.parametrize("stored", [None, "", "date", 3, True])
def test_a_corrupt_stored_anchor_degrades_to_undeclared_rather_than_failing_the_fetch(
    stored: Any,
) -> None:
    """The read-side backstop, coerced against the CLOSED set rather than merely
    type-checked. The write side already refuses an unknown anchor, so this only
    fires for a hand-edited or foreign-written node — and there the honest reading
    is "this blueprint declares nothing", not "print whatever is stored"."""
    assert _coerce_window_anchor(stored) is None
    assert map_blueprint_detail_record(
        {"id": "bp-x", "uses": [], "window_anchor": stored}
    ).window_anchor is None


def test_a_blueprint_detail_without_the_field_still_constructs() -> None:
    """Additive: every existing `BlueprintDetail(...)` call site — and every
    pre-J7 stored node — is unchanged."""
    assert map_blueprint_detail_record({"id": "bp-x", "uses": []}).window_anchor is None


# ---------------------------------------------------------------------------
# 4. getBlueprint — the read the model makes BEFORE running
# ---------------------------------------------------------------------------


async def _get_blueprint(detail: BlueprintDetail) -> dict[str, Any]:
    index = FakeVectorIndex()
    index.add_detail(detail)
    result = await GetBlueprintTool(vector_index=index).run(
        {"id": detail.id}, _creds(scope=frozenset(detail.uses or frozenset()))
    )
    assert result.status == "ok"
    assert isinstance(result.result_full, dict)
    return result.result_full


async def test_get_blueprint_renders_a_data_anchor_declaration() -> None:
    """The line the model reads before it decides whether this blueprint answers a
    calendar question. Self-describing on purpose — a bare `"data"` would be a token
    the model has to guess the meaning of, and guessing is what J7 is about."""
    found = await _get_blueprint(_detail(window_anchor="data"))
    assert found["window_anchor"] == (
        "data — this blueprint's window counts back from the latest data on record, "
        "not from today's date."
    )


async def test_get_blueprint_renders_a_calendar_anchor_declaration() -> None:
    """`bp-hires-in-range`'s case. The gloss is written to be TRUE of that shape —
    the window is bound to the dates the caller supplies — and deliberately does not
    claim the blueprint anchors on today, because nothing in it reads the clock."""
    found = await _get_blueprint(_detail(window_anchor="calendar"))
    assert found["window_anchor"] == (
        "calendar — this blueprint's window is bound to the calendar dates you "
        "supply, not to the latest data on record."
    )


async def test_get_blueprint_omits_the_key_entirely_when_nothing_is_declared() -> None:
    """The additive claim, which is the one most easily broken: an undeclared
    blueprint's FOUND shape must not gain a `window_anchor: null` — a null there
    reads as "declared, and the declaration is nothing"."""
    found = await _get_blueprint(_detail(window_anchor=None))
    assert "window_anchor" not in found


# ---------------------------------------------------------------------------
# 5. runBlueprint — the moment the wrong conclusion was drawn
# ---------------------------------------------------------------------------


async def test_a_data_anchored_run_stamps_the_anchor_on_the_result() -> None:
    outcome = await _run(_detail(window_anchor="data"))
    assert outcome.result_full["window_anchor"] == "data"


async def test_the_dag_finalize_path_stamps_the_anchor_the_same_way() -> None:
    """The SECOND result-build site. Two `result_full` dicts assembled in two
    functions is exactly how the single-node and DAG paths came to describe the same
    `verify` gate differently (J6) — they share `_stamp_window_anchor` for the same
    reason, and this is the assertion that keeps them sharing it. It matters here
    because a COMPOSED blueprint's per-node SQL is hidden from the model, so the
    declaration is the ONLY way its anchor can ever be seen.
    """
    detail = BlueprintDetail(
        id="bp-composed-window",
        intent="A composed windowed blueprint",
        slots_summary="",
        uses=frozenset({_CODE_COL, _HIRE_COL}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        sql_template=None,
        composes=[
            {
                "order": 0,
                "output": {"anchor_month": "scalar"},
                "sql_template": (
                    "SELECT toStartOfMonth(max(hire_date)) AS anchor_month "
                    "FROM dbpcm_warehouse.employee"
                ),
            },
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {"anchor_month": "$0.anchor_month"},
                "output": {},
                "sql_template": (
                    "SELECT toStartOfMonth(hire_date) AS month, "
                    "COUNT(DISTINCT employee_code) AS hires "
                    "FROM dbpcm_warehouse.employee WHERE hire_date >= {anchor_month} "
                    "GROUP BY toStartOfMonth(hire_date)"
                ),
            },
        ],
        result_grain=["month"],
        window_anchor="data",
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["anchor_month"], [["2021-06-01"]]),  # node 0 -> scalar
                _rq(["month", "hires"], [["2021-06-01", 4]]),  # node 1 -> terminal
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),  # grain probe
            ]
        }
    )

    outcome = await _executor(mcp, detail).execute(
        blueprint_id=detail.id, slot_bindings={}, credentials=_creds()
    )

    assert isinstance(outcome, ExecCompleted)
    assert outcome.result_full["window_anchor"] == "data"
    # J7-anchor: the terminal node's own rows name the window, so a COMPOSED blueprint
    # gets the concrete date too — it is derived from the result, not from the SQL.
    assert outcome.result_full["window_end"] == "2021-06-01"
    assert outcome.result_full["window_start"] == "2021-06-01"
    mapped = blueprint_outcome_to_tool_result(outcome)
    assert mapped is not None
    assert mapped.window_note == _CONCRETE_NOTE


async def test_a_data_anchored_run_stamps_the_concrete_window_from_the_rows() -> None:
    """J7-anchor, the whole point. The static note said the window was data-anchored
    but not WHICH dates it covered, so whether the result answered the question asked
    stayed an inference off the rows — and 2-of-3 live runs inferred it wrong and
    re-derived with calendar SQL anyway. The executor already holds the rows; the max
    of the grain column IS the window end, so it is stamped rather than left to be read.

    The rows are deliberately OUT OF ORDER: the derivation is a max over the column,
    not "the last row", which a `GROUP BY` without an `ORDER BY` does not promise."""
    outcome = await _run_terminal(
        _detail(window_anchor="data"),
        ["month", "hires"],
        [["2021-04-01", 2], ["2021-06-01", 4], ["2021-05-01", 3]],
    )
    assert outcome.result_full["window_end"] == "2021-06-01"
    assert outcome.result_full["window_start"] == "2021-04-01"


async def test_the_note_names_the_date_so_responsiveness_is_read_not_inferred() -> None:
    """The delivered sentence, word for word. `window_end` is only worth stamping if it
    reaches the model, and this is the surface it reaches it on."""
    result = blueprint_outcome_to_tool_result(await _run(_detail(window_anchor="data")))
    assert result is not None
    assert result.window_note == _CONCRETE_NOTE
    assert _CONCRETE_NOTE == DATA_ANCHORED_RESULT_NOTE_TEMPLATE.format(
        window_end="2021-06-01"
    )


async def test_a_datetime_valued_anchor_column_is_stored_as_a_date() -> None:
    """ClickHouse hands back a `DateTime` grain column as `YYYY-MM-DD hh:mm:ss`. The
    date part is the window end; the zero time is noise that would read, in the note,
    as a precision the window does not have."""
    outcome = await _run_terminal(
        _detail(window_anchor="data"), ["month", "hires"], [["2021-06-01 00:00:00", 4]]
    )
    assert outcome.result_full["window_end"] == "2021-06-01"
    mapped = blueprint_outcome_to_tool_result(outcome)
    assert mapped is not None
    assert mapped.window_note == _CONCRETE_NOTE


@pytest.mark.parametrize(
    ("case", "grain", "columns", "rows", "truncated"),
    [
        # `bp-hires-projection`'s shape: a data-anchored window with NO declared grain,
        # so there is no column that means "the window" and nothing to take a max over.
        ("no declared grain", [], ["month", "hires"], [["2021-06-01", 4]], False),
        # More than one grain column: one of them may be the window and the other a
        # dimension, and which is which is not knowable here. Guessing is the failure.
        (
            "multi-column grain",
            ["month", "hires"],
            ["month", "hires"],
            [["2021-06-01", 4]],
            False,
        ),
        # A truncated result's max ROW is not the max VALUE — the rows are a prefix.
        ("truncated", ["month"], ["month", "hires"], [["2021-06-01", 4]], True),
        # Nothing to take a max over. (The J6a empty-result note carries this case.)
        ("empty result", ["month"], ["month", "hires"], [], False),
        # A grain column that is not a date at all (a `period` key, a label).
        ("non-date anchor", ["month"], ["month", "hires"], [["June 2021", 4]], False),
        # A well-formed-looking value that is not a real date.
        ("impossible date", ["month"], ["month", "hires"], [["2021-13-45", 4]], False),
        # A NULL in the anchor column (`Nullable(Date)` is the warehouse's own type for
        # `hire_date`). The whole derivation fails — NOT "skip the NULL and take the max
        # of the rest", which would name a window end while silently dropping rows the
        # answer contains.
        ("null anchor value", ["month"], ["month", "hires"], [["2021-06-01", 4], [None, 2]], False),
        # A row shorter than the column list. Nothing in the pipeline promises this, but
        # the derivation indexes into every row and an IndexError here would crash a turn
        # that the pre-slice code answered.
        ("short row", ["month"], ["month", "hires"], [["2021-06-01", 4], []], False),
    ],
)
async def test_the_concrete_window_fails_closed_to_the_static_note(
    case: str,
    grain: list[str],
    columns: list[str],
    rows: list[list[Any]],
    truncated: bool,
) -> None:
    """Every case where the rows do not LICENSE a date. A wrong date in the note is
    strictly worse than no date — it would be a specific, confident, checkable claim
    about coverage that the model would then present to the user — so each of these
    degrades to exactly the pre-slice behaviour: the anchor string, no window keys,
    and the static note byte-for-byte."""
    outcome = await _run_terminal(
        _detail(window_anchor="data", result_grain=grain),
        columns,
        rows,
        truncated=truncated,
    )
    assert outcome.result_full["window_anchor"] == "data", case
    assert "window_end" not in outcome.result_full, case
    assert "window_start" not in outcome.result_full, case
    mapped = blueprint_outcome_to_tool_result(outcome)
    assert mapped is not None
    assert mapped.window_note == DATA_ANCHORED_RESULT_NOTE, case
    assert mapped.window_note == (
        "Window is data-anchored: it counts back from the latest data on record, "
        "not from today's date. Present it as 'as of the latest data' — do not "
        "re-derive with a calendar-anchored query."
    ), case


async def test_a_calendar_anchored_run_gains_no_window_dates() -> None:
    """No behaviour change for the anchor that was never the problem: a calendar
    window's bounds ARE the slot values the caller supplied, already known to the
    model, and stamping the rows' own extent there would invite it to describe the
    data's coverage as the window it asked for."""
    outcome = await _run_terminal(
        _detail(window_anchor="calendar"), ["month", "hires"], [["2021-06-01", 4]]
    )
    assert outcome.result_full["window_anchor"] == "calendar"
    assert "window_end" not in outcome.result_full
    mapped = blueprint_outcome_to_tool_result(outcome)
    assert mapped is not None
    assert mapped.window_note is None


def test_an_unmappable_grain_column_stamps_the_anchor_only() -> None:
    """A declared grain column with no matching output column.

    Asserted directly on the writer because that is the shortest statement of the rule,
    but it is NOT an unreachable branch: a `result_grain` declaring `verifiable: false`
    skips the D56 mapping entirely (executor `_verify`, the `not grain.verifiable`
    arm) and reaches the stamp with a grain that was never mapped. On a `verifiable:
    true` grain the gate happens to refuse the result first (`distinct=None` →
    VERIFY_FAILED), and the window derivation must fail closed on its OWN terms rather
    than inherit that ordering."""
    blueprint = Blueprint.parse(
        id="bp-x",
        intent="i",
        slots=[dict(_WINDOW_SLOT)],
        sql_template=_HIRES_SQL,
        result_grain=["not_an_output_column"],
        window_anchor="data",
    )
    result_full: dict[str, Any] = {}
    _stamp_window_anchor(
        result_full,
        blueprint,
        template_sql=_HIRES_SQL,
        columns=["month", "hires"],
        rows=[["2021-06-01", 4]],
        truncated=False,
    )
    assert result_full == {"window_anchor": "data"}


def test_an_undeclared_anchor_stamps_nothing_at_all() -> None:
    """The additive guarantee at the writer itself: rows that WOULD yield a window end
    do not put one on a blueprint that declared no anchor."""
    blueprint = Blueprint.parse(
        id="bp-x",
        intent="i",
        slots=[dict(_WINDOW_SLOT)],
        sql_template=_HIRES_SQL,
        result_grain=["month"],
    )
    result_full: dict[str, Any] = {}
    _stamp_window_anchor(
        result_full,
        blueprint,
        template_sql=_HIRES_SQL,
        columns=["month", "hires"],
        rows=[["2021-06-01", 4]],
        truncated=False,
    )
    assert result_full == {}


async def test_an_undeclared_blueprints_result_is_byte_identical_to_before() -> None:
    """No phantom key on the eight blueprints that declare nothing — this dict is
    persisted behind a D46 pointer and rehydrated by `GET /session/history`."""
    outcome = await _run(_detail(window_anchor=None))
    assert "window_anchor" not in outcome.result_full


async def test_the_tool_result_carries_the_note_only_for_a_data_anchored_window() -> None:
    """The note is deliberately NARROW. A `calendar` blueprint's window is the dates
    the caller asked for, so the model's own reading is already right and a note
    would be per-run tokens buying nothing; the note exists to stop ONE failure, not
    to annotate windows in general."""
    data_result = blueprint_outcome_to_tool_result(await _run(_detail(window_anchor="data")))
    assert data_result is not None
    assert data_result.window_note == _CONCRETE_NOTE

    for anchor in (None, "calendar"):
        other = blueprint_outcome_to_tool_result(await _run(_detail(window_anchor=anchor)))
        assert other is not None
        assert other.window_note is None


@pytest.mark.parametrize(
    "result_full", [None, [], "data", {}, {"window_anchor": "calendar"}, {"window_anchor": 1}]
)
def test_the_note_derivation_fails_closed_on_any_other_shape(result_full: Any) -> None:
    assert window_note_for_result(result_full) is None


@pytest.mark.parametrize(
    "window_end",
    [
        None,
        17,
        True,
        "",
        "9999-99-99",  # right shape, not a date on any calendar
        "2021-02-30",  # right shape, not a date in that month
        "2021-6-1",  # unpadded
        "20210601",  # `date.fromisoformat` accepts this; the note must not
        "\u0662\u0660\u0662\u0661-\u0660\u0666-\u0660\u0661",  # Arabic-Indic digits: `\d` matches them
        "2021-06-01 00:00:00",  # the STAMP is a bare date; a time here means poisoned
        "2021-06-01'; DROP",
    ],
)
def test_a_poisoned_window_end_degrades_to_the_static_note(window_end: Any) -> None:
    """`result_full` is persisted behind a D46 pointer and re-read on the D45 resume
    path, so by the time the note is derived the window end is UNTRUSTED JSON — and it
    is interpolated verbatim into a sentence the model reads as fact. Validated by what
    the note needs (a real `YYYY-MM-DD` calendar date) rather than by rejecting the bad
    values anyone happened to think of: shape check THEN `date.fromisoformat`, because
    each alone passes values the other catches."""
    result_full = {"window_anchor": "data", "window_end": window_end}
    assert window_note_for_result(result_full) == DATA_ANCHORED_RESULT_NOTE


def test_a_missing_window_end_is_the_ordinary_pre_slice_case_not_a_poisoning() -> None:
    """Every fail-closed derivation lands here, plus every result written before this
    slice existed. It is the SAME static note, and that is deliberate."""
    assert window_note_for_result({"window_anchor": "data"}) == DATA_ANCHORED_RESULT_NOTE
    assert window_note_for_result({"window_anchor": "data", "window_end": "2021-06-01"}) == (
        _CONCRETE_NOTE
    )


def test_the_note_rides_the_shared_mapper_so_a_resumed_run_cannot_lose_it() -> None:
    """`blueprint_outcome_to_tool_result` is the ONE `ExecOutcome` -> `ToolResult`
    mapping, used by a first call and by the loop's mid-DAG resume alike. Deriving
    the note anywhere else is how the resume path silently lost the `authoritative`
    marker before this function existed."""
    import inspect

    from data_agent.runtime.loop import agent_loop

    source = inspect.getsource(agent_loop)
    assert "window_note=tool_result.window_note" in source
    assert source.count("window_note=tool_result.window_note") == 2, (
        "both TrailEntry construction sites (live dispatch and blueprint resume) must "
        "carry the note, or a resumed blueprint describes its window differently"
    )


# ---------------------------------------------------------------------------
# 6. The persisted channel — TrailEntry -> render -> the model's tool message
# ---------------------------------------------------------------------------


def _entry(**overrides: Any) -> TrailEntry:
    base: dict[str, Any] = {
        "turn_index": 0,
        "tool_call_id": "c1",
        "tool_name": "runBlueprint",
        "args": {"id": "bp-hires-per-month"},
        "status": "ok",
        "error_code": None,
        "provenance": frozenset(),
        "result_preview": ResultPreview(
            columns=["month", "hires"], row_count=1, truncated=False, preview_rows=[["2021-06", 4]]
        ),
        "result_full_ref": "ref-1",
        "ts": "2026-08-17T00:00:00Z",
    }
    base.update(overrides)
    return TrailEntry(**base)


def test_the_note_survives_the_persist_reload_round_trip() -> None:
    """It has to be PERSISTED to arrive: `_render_entry` cannot see the corpus, so a
    D45 replay re-emits the identical tool message only if the note is on the
    document. Same argument that put `authoritative` on this dataclass."""
    entry = _entry(window_note=DATA_ANCHORED_RESULT_NOTE)
    doc = entry.to_doc()
    assert doc["window_note"] == DATA_ANCHORED_RESULT_NOTE
    assert TrailEntry.from_doc(json.loads(json.dumps(doc))).window_note == (
        DATA_ANCHORED_RESULT_NOTE
    )


def test_a_pre_j7_document_loads_unchanged() -> None:
    """A document written before the field existed has no key at all, and a
    non-string one must not reach the renderer, which prints it at the model."""
    doc = _entry().to_doc()
    del doc["window_note"]
    assert TrailEntry.from_doc(doc).window_note is None
    doc["window_note"] = 17
    assert TrailEntry.from_doc(doc).window_note is None


def test_the_renderer_carries_the_note_and_omits_it_otherwise() -> None:
    rendered = _render_entry(_entry(window_note=DATA_ANCHORED_RESULT_NOTE), preview_row_count=5)
    assert rendered["window_note"] == DATA_ANCHORED_RESULT_NOTE
    assert "window_note" not in _render_entry(_entry(), preview_row_count=5)


def test_the_model_facing_tool_message_carries_the_note_on_its_own_key() -> None:
    """Its OWN key beside `note`, not appended to it. The two are INDEPENDENT — a
    blueprint that failed verify still ran a data-anchored window — and the J6a
    empty-result sentence is a careful piece of wording that concatenation would
    blur. `note` says whether to TRUST the rows; `window_note` says what period they
    COVER."""
    rendered = _render_entry(
        _entry(authoritative=True, window_note=DATA_ANCHORED_RESULT_NOTE), preview_row_count=5
    )
    _assistant, tool_message = _tool_trail_entry_to_canonical(rendered)
    content = json.loads(tool_message["content"])

    assert content["window_note"] == DATA_ANCHORED_RESULT_NOTE
    # The J6a notes are untouched, word for word.
    assert content["note"] == (
        "Verified blueprint result — authoritative; do not re-derive with "
        "additional queries."
    )
    assert content["authoritative"] is True


def test_the_note_reaches_the_model_even_when_the_result_is_not_authoritative() -> None:
    """The independence, asserted rather than assumed. Nesting the note inside the
    `authoritative` branch would drop it on exactly the results the model is most
    likely to second-guess — which is the behaviour J7 exists to correct."""
    rendered = _render_entry(_entry(window_note=DATA_ANCHORED_RESULT_NOTE), preview_row_count=5)
    _assistant, tool_message = _tool_trail_entry_to_canonical(rendered)
    content = json.loads(tool_message["content"])

    assert content["window_note"] == DATA_ANCHORED_RESULT_NOTE
    assert "note" not in content
    assert "authoritative" not in content


def test_a_runquery_tool_message_is_byte_identical_to_before() -> None:
    """The additive claim at the last layer: every non-blueprint entry renders
    exactly as it always did."""
    rendered = _render_entry(_entry(tool_name="runQuery"), preview_row_count=5)
    _assistant, tool_message = _tool_trail_entry_to_canonical(rendered)
    assert set(json.loads(tool_message["content"])) == {
        "status",
        "error_code",
        "user_message",
        "result_preview",
    }


def test_the_tool_result_default_leaves_every_other_tool_alone() -> None:
    assert (
        ToolResult(
            status="ok",
            tool_name="runQuery",
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=frozenset(),
            result_preview=None,
            result_full=None,
        ).window_note
        is None
    )


# ---------------------------------------------------------------------------
# 7. The corpus itself — what the three hires blueprints actually declare
# ---------------------------------------------------------------------------


def _fixture_blueprints() -> dict[str, dict[str, Any]]:
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "fixtures" / "corpus" / "blueprints.yaml"
    return {d["id"]: d for d in yaml.safe_load(path.read_text())}


def test_the_two_max_hire_date_anchored_blueprints_declare_data() -> None:
    """Declared BECAUSE the template anchors on `max(hire_date)` — the assertion
    pairs the claim with the SQL that makes it true, so a future re-anchoring of
    either template fails here instead of shipping a declaration that lies."""
    corpus = _fixture_blueprints()
    for bp_id in ("bp-hires-per-month", "bp-hires-projection"):
        entry = corpus[bp_id]
        assert entry["window_anchor"] == "data", bp_id
        assert "max(hire_date)" in entry["sql_template"], bp_id


def test_the_explicit_range_blueprint_declares_calendar_and_reads_no_clock() -> None:
    """`bp-hires-in-range` binds both bounds from the caller's explicit ISO dates, so
    its window IS the calendar period asked for — which is what makes it the right
    blueprint for a calendar question and worth declaring rather than leaving
    silent. It must contain NO anchor subquery: a `calendar` declaration over a
    `max()`-anchored template would be the same lie in the other direction."""
    entry = _fixture_blueprints()["bp-hires-in-range"]
    assert entry["window_anchor"] == "calendar"
    assert "{hire_window_start}" in entry["sql_template"]
    assert "{hire_window_end}" in entry["sql_template"]
    assert "max(" not in entry["sql_template"]
    assert "today" not in entry["sql_template"].lower()


def test_no_other_blueprint_declares_an_anchor() -> None:
    """Absent = no claim, and it stays that way for the eight blueprints with no
    window. A declaration on a non-windowed blueprint would be a sentence in front
    of the model describing something that does not exist."""
    declared = {
        bid: entry["window_anchor"]
        for bid, entry in _fixture_blueprints().items()
        if "window_anchor" in entry
    }
    assert declared == {
        "bp-hires-per-month": "data",
        "bp-hires-in-range": "calendar",
        "bp-hires-projection": "data",
    }


def test_every_declared_anchor_in_the_corpus_is_in_the_closed_set() -> None:
    """The mirror is hand-maintained against the MCP canon, so a typo lands as a
    silent nothing at the render sites unless something checks the vocabulary."""
    for bid, entry in _fixture_blueprints().items():
        if "window_anchor" in entry:
            assert entry["window_anchor"] in WINDOW_ANCHORS, bid


def test_the_prompt_states_the_standing_rule_the_two_surfaces_instantiate() -> None:
    """The prompt carries only what no runtime check can enforce — the model's
    decision to re-derive. `getBlueprint` and the result note carry the specifics."""
    from data_agent.runtime.prompts import AGENT_SYSTEM_PROMPT

    assert "DATA-ANCHORED blueprint window ends at the latest data on record" in (
        AGENT_SYSTEM_PROMPT
    )
    assert "never re-run it calendar-anchored" in AGENT_SYSTEM_PROMPT
