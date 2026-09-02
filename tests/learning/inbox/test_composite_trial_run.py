"""The reviewer's trial run over a COMPOSITE candidate — a DAG, not a single template.

Reproduced live on a minted 2-node scalar composite: the button did nothing useful and the card
had no inputs. `ReviewInbox.trial_run` read `generalization.sql_template` and nothing else, and a
composite's top-level template is `None` BY CONSTRUCTION (`generalize/builder.py`) — its SQL is
one template per node under `node_templates`. So every DAG the loop or the minting page has ever
produced answered `no_template`, and `_template_parts` returned `()`, which is a card with no
slot chips at all.

The tests below pin the walk against the runtime executor's rules, because that is the thing a
trial is supposed to predict: bind slots plus upstream scalar `consumes`, run every node in
order, gate the TERMINAL node on D56 — and refuse, out loud, the two shapes a probe cannot do
honestly (a table intermediate, and an upstream node that does not return a single cell).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.inbox import ReviewInbox
from data_agent.learning.inbox.models import _template_parts
from data_agent.learning.promotion.models import ProbeResult

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"

PAYROLL = "payroll.payroll_fact"
USES = ("payroll.payroll_fact.department_code", "payroll.payroll_fact.gross_pay")

# The repro shape: step 0 computes a department total, step 1 filters against it. The slot
# `department_code` is in BOTH steps — the case the card's chip de-duplication exists for.
NODE_0 = (
    f"SELECT sum(gross_pay) AS total FROM {PAYROLL} "
    "WHERE department_code = {department_code}"
)
NODE_1 = (
    f"SELECT employee_id, gross_pay FROM {PAYROLL} "
    "WHERE department_code = {department_code} AND gross_pay > {total}"
)


def _generalization(node_templates: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    return {
        "sql_template": None,  # a composite's top-level template is None by construction
        "uses": list(USES),
        "uses_rules": [],
        "node_templates": node_templates,
        "result_grain": {"columns": [], "verifiable": False},
        "static_validation": {
            "explain_ok": True,
            "binds_to_subset_uses": True,
            "dag_ok": True,
            "read_only_select": True,
            "outcome": "ok",
            "reason": None,
        },
        "canonical_ast_norm": "SELECT 1 AS c",
        **over,
    }


def _scalar_composite(**over: Any) -> dict[str, Any]:
    """The payload of the minted 2-node scalar composite this slice was written for."""
    payload = {
        "intent": "employees paid above their department total",
        "generalization": _generalization(
            [
                {"order": 0, "sql_template": NODE_0},
                {"order": 1, "sql_template": NODE_1},
            ]
        ),
        "composes": [
            {
                "order": 0,
                "node_kind": "query",
                "feeds_from": [],
                "consumes": {},
                "output": {"total": "scalar"},
            },
            {
                "order": 1,
                "node_kind": "query",
                "feeds_from": [0],
                "consumes": {"total": "$0.total"},
                "output": {"rows": "scalar"},
            },
        ],
    }
    payload.update(over)
    return payload


async def _inbox(payload: dict[str, Any], probe: Any) -> tuple[ReviewInbox, CandidateEnvelope]:
    base = json.loads((FIXTURES / "envelopes_each_reason.json").read_text())[
        "leakage_near_miss"
    ]
    env = replace(
        CandidateEnvelope.from_doc(base),
        status=CandidateStatus.IN_REVIEW,
        payload=payload,
    )
    store = InMemoryCandidateStore()
    await store.put(env)
    # `probe_factory` is the injection point the surface already has for exactly this: the
    # reviewer's token builds the probe, so a fake token builds a fake probe over the same path.
    return ReviewInbox(store, probe_factory=lambda _token: probe), env


class _FakeProbe:
    """A structure oracle plus the `ScalarCellProbe` port, recording every SQL it is handed.

    Both halves are needed to walk a DAG: `run` answers the D56 triple for the terminal node,
    `run_cell` answers the single upstream cell that gets bound into the consumer.
    """

    def __init__(
        self,
        *,
        cells: tuple[Any, ...] = (),
        result: ProbeResult | None = None,
        raises: Exception | None = None,
        cell_raises: Exception | None = None,
    ) -> None:
        self._cells = list(cells)
        # THE DEFAULT MIRRORS WHAT THE REAL PROBE ANSWERS FOR THIS PAYLOAD. Every
        # `_generalization` here declares `result_grain: {columns: []}`, and
        # `_result_grain_columns` (`inbox/inbox.py`) derives the probe's `grain_columns`
        # from that COLUMN LIST alone — it never reads `verifiable`, so a payload with
        # non-empty columns and `verifiable: false` WOULD still be probed. With `columns`
        # empty, `MCPWarehouseProbe.run` is handed `grain_columns=()`, never issues the
        # COUNT(DISTINCT) round-trip, and returns the `row_count=0, distinct=None`
        # placeholder with only the column header read. The previous default paired a
        # non-zero `row_count` with `distinct=None`, which no code path in the probe can
        # produce (every path that leaves `distinct=None` also returns `row_count=0`) — it
        # made the card assert a measured row count against a fixture the warehouse could
        # never hand back.
        self._result = result or ProbeResult(
            row_count=0, distinct_grain_count=None, columns=("employee_id", "gross_pay")
        )
        # What `run` raises INSTEAD of answering — a warehouse that rejected the query. The
        # real one quotes the failing SQL back, which is why this is a fixture at all.
        self._raises = raises
        # The same, for the scalar read — kept separate so a test can fail the CONSUMER while
        # the producer succeeds, which is the only arrangement in which a cell exists to leak.
        self._cell_raises = cell_raises
        self.ran: list[str] = []  # every SQL, in the order the walk dispatched it
        self.cell_sqls: list[str] = []
        self.scopes: list[tuple[str, ...]] = []

    async def run(
        self,
        sql: str,
        *,
        grain_columns: tuple[str, ...] = (),
        column_scope: tuple[str, ...] = (),
    ) -> ProbeResult:
        self.ran.append(sql)
        self.scopes.append(column_scope)
        if self._raises is not None:
            raise self._raises
        return self._result

    async def run_cell(self, sql: str, *, column_scope: tuple[str, ...] = ()) -> Any:
        self.ran.append(sql)
        self.cell_sqls.append(sql)
        self.scopes.append(column_scope)
        if self._cell_raises is not None:
            raise self._cell_raises
        return self._cells.pop(0) if self._cells else None


class _StructureOnlyProbe:
    """The probe an offline/dev inbox falls back to: no `run_cell`, so no DAG."""

    async def run(
        self,
        sql: str,
        *,
        grain_columns: tuple[str, ...] = (),
        column_scope: tuple[str, ...] = (),
    ) -> ProbeResult:
        return ProbeResult(row_count=0, distinct_grain_count=None, columns=())


# --- the walk ----------------------------------------------------------------


async def test_a_two_node_scalar_composite_runs_and_reports_the_terminal_shape() -> None:
    """⚠ THE REPRO. Before this slice the same candidate answered `no_template`.

    Both steps run, in order, and what comes back describes the TERMINAL node — the one whose
    rows are the blueprint's answer. That mirrors `executor._finalize`, which gates the final
    node's result and returns it; an intermediate's shape is not the blueprint's shape.
    """
    probe = _FakeProbe(cells=(41000,))
    inbox, env = await _inbox(_scalar_composite(), probe)

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.ok is True, result.reason
    assert len(probe.ran) == 2, "every node runs, not just the terminal one"
    # The TERMINAL node's columns — node 0 returns `total`, node 1 returns these.
    assert result.columns == ("employee_id", "gross_pay")
    # No verifiable grain is declared, so no COUNT(DISTINCT) ran and there is no row count
    # to report: the 0 is a placeholder, and the flag is what stops the card presenting it
    # as a measurement.
    assert result.row_count == 0
    assert result.row_count_measured is False
    # The declared footprint scopes EVERY node's read, not only the last.
    assert probe.scopes == [USES, USES]


async def test_the_upstream_cell_reaches_the_downstream_sql() -> None:
    """⚠ THE ONE THAT MAKES A COMPOSITE TRIAL MEAN ANYTHING.

    The promotion replay's `_pick_template` runs the TERMINAL node alone, which is right for a
    structure oracle over synthetic samples and wrong here: with no upstream run, `{total}` is
    unbound. A trial that faked it would ask the reviewer to type a number the blueprint exists
    to compute.

    Asserted on the SQL the probe was handed, because that is the only place the binding is
    observable — the value never appears in the result (structure, never values).
    """
    probe = _FakeProbe(cells=(41000,))
    inbox, env = await _inbox(_scalar_composite(), probe)

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.ok is True, result.reason
    upstream_sql, downstream_sql = probe.ran
    assert "sum(gross_pay)" in upstream_sql
    assert "41000" in downstream_sql, downstream_sql
    assert "{total}" not in downstream_sql
    # The reviewer's own value is bound into BOTH steps — one input, every occurrence.
    assert "'0420'" in upstream_sql and "'0420'" in downstream_sql
    # ...and the trial result carries neither value back.
    assert "41000" not in json.dumps(result.to_wire())


async def test_a_slot_missing_in_any_node_is_reported_before_anything_runs() -> None:
    """The required set spans ALL node templates, so a slot only the second step uses is still
    the reviewer's to supply — and nothing is dispatched until it is. Reported exactly as the
    single-template path reports it, because the card renders one list."""
    probe = _FakeProbe(cells=(41000,))
    inbox, env = await _inbox(_scalar_composite(), probe)

    result = await inbox.trial_run(
        env.candidate_id, bindings={}, token="reviewer-token"
    )

    assert result.ok is False
    assert result.reason == "missing_bindings"
    assert result.missing == ("department_code",)
    assert probe.ran == [], "a refused trial must not touch the warehouse"


async def test_an_upstream_consume_is_never_asked_of_the_reviewer() -> None:
    """`{total}` is filled by step 0, so it is not a missing binding — it is the edge. Offering
    it as an input would ask a human to hand-type an intermediate the DAG computes."""
    probe = _FakeProbe(cells=(41000,))
    inbox, env = await _inbox(_scalar_composite(), probe)

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.ok is True, result.reason


# --- the shapes a trial refuses rather than fakes ----------------------------


async def test_a_table_intermediate_is_refused_by_name() -> None:
    """A whole result passed downstream needs the D93 scratch materialization the probe has no
    side-channel for — and it is the same shape that cannot be promoted today. So it is refused
    with a reason the card maps, rather than half-run.

    The EXACT string matters: the frontend keys its explanation off it.
    """
    payload = _scalar_composite()
    payload["composes"][0]["output"] = {"total": "table"}
    payload["composes"][1]["consumes"] = {"total": "$0"}
    probe = _FakeProbe()
    inbox, env = await _inbox(payload, probe)

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.ok is False
    assert result.reason == "table_intermediate_unsupported"
    assert probe.ran == []


async def test_a_table_output_with_no_table_consume_is_refused_too() -> None:
    """The shape the loader forbids but a poisoned record can carry: a non-terminal node
    declaring a `table` output that nothing consumes as a table. It still cannot be scalar-passed
    — `executor._execute_dag` calls it UNSUPPORTED for the same reason."""
    payload = _scalar_composite()
    payload["composes"][0]["output"] = {"total": "table"}
    inbox, env = await _inbox(payload, _FakeProbe(cells=(41000,)))

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.reason == "table_intermediate_unsupported"


async def test_an_upstream_step_that_is_not_one_cell_fails_closed() -> None:
    """⚠ THE WRONG-ANSWER CLASS, borrowed verbatim from `executor._extract_scalar_output`.

    The D56 gate guards the TERMINAL node only, so an intermediate that fans out (or returns
    nothing, or a NULL) would bind an ARBITRARY cell downstream and the trial would report a
    verified shape over it. The probe answers `None` for anything that is not one non-NULL cell,
    and the trial stops there.
    """
    probe = _FakeProbe(cells=(None,))  # the probe's "that was not a single cell"
    inbox, env = await _inbox(_scalar_composite(), probe)

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.ok is False
    assert result.reason == "scalar_shape"
    assert "step 0" in result.detail
    assert len(probe.ran) == 1, "the consumer must not run on an unproven scalar"


async def test_a_probe_that_cannot_read_a_cell_says_so() -> None:
    """An offline/dev inbox falls back to the scheduler's structure-only probe. Binding a
    synthetic value into the consumer would report green for a query nobody ran, so the trial
    refuses with the reason instead."""
    inbox, env = await _inbox(_scalar_composite(), _StructureOnlyProbe())

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.ok is False
    assert result.reason == "no_scalar_probe"


async def test_an_empty_generalization_is_still_no_template() -> None:
    """Preserved for the genuinely-empty case: a knowledge candidate, or a fail-to-review
    blueprint whose generalization carries neither a template nor any nodes. `no_template` is
    what the card already explains; a composite reason there would name a DAG that is not
    there."""
    inbox, env = await _inbox(
        {"intent": "nothing to run", "generalization": _generalization([])},
        _FakeProbe(),
    )
    assert (await inbox.trial_run(env.candidate_id, bindings={}, token="t")).reason == (
        "no_template"
    )

    inbox, env = await _inbox({"statement": "a business rule"}, _FakeProbe())
    assert (await inbox.trial_run(env.candidate_id, bindings={}, token="t")).reason == (
        "no_template"
    )


async def test_a_composite_still_refuses_a_blank_token_and_an_empty_scope() -> None:
    """The two refusals the single path already carries, checked on this branch too: neither is
    something a second entry point may quietly opt out of."""
    inbox, env = await _inbox(_scalar_composite(), _FakeProbe(cells=(1,)))
    blank = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="  "
    )
    assert blank.reason == "no_token"

    payload = _scalar_composite()
    payload["generalization"]["uses"] = []
    inbox, env = await _inbox(payload, _FakeProbe(cells=(1,)))
    scoped = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )
    assert scoped.reason == "no_uses_scope"


async def test_a_damaged_dag_is_refused_rather_than_raising() -> None:
    """The payload is rehydrated JSON. A consume ref in neither grammar, a node template with no
    order, a dangling edge — all are damage, and damage is a reason string: this method is called
    from an endpoint a reviewer is looking at."""
    payload = _scalar_composite()
    payload["composes"][1]["consumes"] = {"total": "step zero please"}
    inbox, env = await _inbox(payload, _FakeProbe(cells=(1,)))
    assert (
        await inbox.trial_run(
            env.candidate_id, bindings={"department_code": "0420"}, token="t"
        )
    ).reason == "malformed_composite"

    payload = _scalar_composite()
    payload["generalization"]["node_templates"][0] = {"sql_template": NODE_0}
    inbox, env = await _inbox(payload, _FakeProbe(cells=(1,)))
    assert (
        await inbox.trial_run(
            env.candidate_id, bindings={"department_code": "0420"}, token="t"
        )
    ).reason == "malformed_composite"

    # A consume pointing FORWARD (or at a step that does not exist) resolves to nothing when the
    # consumer runs — refused rather than bound to whatever happened to be in hand.
    payload = _scalar_composite()
    payload["composes"][1]["consumes"] = {"total": "$7.total"}
    inbox, env = await _inbox(payload, _FakeProbe(cells=(1,)))
    assert (
        await inbox.trial_run(
            env.candidate_id, bindings={"department_code": "0420"}, token="t"
        )
    ).reason == "malformed_composite"


async def test_a_composite_the_minter_actually_produces_can_be_trialled() -> None:
    """⚠ THE END-TO-END REPRO, against a candidate nobody hand-wrote.

    Every other test here builds the payload by hand, which is exactly how a walk can be
    correct about a shape the pipeline does not emit — and that was the trap: `consumes` and
    `output` live on the S3 plan's `composes`, NOT on `generalization.node_templates`, which
    carries only `order` + `sql_template`. A walker keyed off the wrong field passes its own
    fixtures and finds nothing on a real row.

    So this one mints a 2-node scalar composite through the real minter (real generalize,
    leakage and writer stages) and trials the candidate that lands in the store.
    """
    from data_agent.learning.mint.models import MintNode

    from ..mint.test_mint_qa_composite import PAYROLL as MINT_PAYROLL
    from ..mint.test_mint_qa_composite import TOTAL_SQL, consuming_sql, mint

    _result, env = await mint(
        (
            MintNode(step_intent="the department total", output_name="total"),
            MintNode(step_intent="the share", output_name="share", feeds_from=(0,)),
        ),
        [TOTAL_SQL, consuming_sql("total", "share")],
    )
    store = InMemoryCandidateStore()
    await store.put(env)
    probe = _FakeProbe(cells=(41000,), result=ProbeResult(0, None, ("share",)))
    inbox = ReviewInbox(store, probe_factory=lambda _token: probe)

    # The card asks for exactly what the trial requires — the classified literal, and nothing
    # the DAG computes for itself.
    chips = {p["slot"] for p in _template_parts(env.payload) if "slot" in p}
    assert chips == {"department_code"}

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.ok is True, (result.reason, result.detail)
    assert len(probe.ran) == 2
    assert "41000" in probe.ran[1]
    assert MINT_PAYROLL in probe.ran[0]
    assert result.columns == ("share",)


# --- the card's inputs -------------------------------------------------------


def _rendered(parts: tuple[dict[str, str], ...]) -> str:
    """The parts joined back up the way the card appends them, slots re-wrapped in braces."""
    return "".join(
        "{" + p["slot"] + "}" if "slot" in p else p.get("text", "") for p in parts
    )


def test_the_card_parts_span_every_node_and_offer_each_slot_once() -> None:
    """Both steps are shown, because the trial runs both — and `department_code`, which both
    steps use, is one input: the trial binds one typed value to every occurrence of it, so a
    second box could only disagree with the first. The repeat is still RENDERED, as text, so the
    second step's SQL is the SQL that will run."""
    parts = _template_parts(_scalar_composite())

    chips = [p["slot"] for p in parts if "slot" in p]
    assert chips == ["department_code"]
    assert _rendered(parts) == f"{NODE_0}\n\n{NODE_1}"


def test_a_consume_placeholder_is_rendered_but_never_offered() -> None:
    """⚠ THE CARD AND THE TRIAL MUST AGREE ABOUT WHAT A HUMAN SUPPLIES.

    `{total}` is filled by step 0's result — `trial_run` subtracts each node's referenced
    `consumes` from its required set, exactly as `executor._node_bindings` does. A chip for it
    would be a box whose value the trial silently drops, which is the defect
    `tests/learning/mint/test_mint_qa_composite.py` already names for the single-template card.
    """
    parts = _template_parts(_scalar_composite())

    assert "total" not in {p["slot"] for p in parts if "slot" in p}
    assert "{total}" in _rendered(parts), "the token is still shown; only the input is withheld"


def test_the_card_parts_follow_node_order_not_document_order() -> None:
    """`order` IS the DAG order (`check_dag` refuses a forward edge), so a document that stored
    the nodes the other way round still renders the steps in the order they run."""
    payload = _scalar_composite()
    payload["generalization"]["node_templates"] = [
        {"order": 1, "sql_template": NODE_1},
        {"order": 0, "sql_template": NODE_0},
    ]
    rendered = _rendered(_template_parts(payload))
    assert rendered.index("sum(gross_pay)") < rendered.index("employee_id")


def test_a_damaged_node_list_renders_as_no_template_rather_than_raising() -> None:
    """Same posture as the single-template split: this is a listing projection over a rehydrated
    doc, so damage is emptiness, never an exception."""
    assert _template_parts({"generalization": {"node_templates": "not a list"}}) == ()
    assert _template_parts({"generalization": {"node_templates": [{"order": 0}]}}) == ()
    assert _template_parts({"generalization": {"node_templates": []}}) == ()
    # A malformed `composes` costs the wiring, never the render: every token falls back to
    # being offered once, which is the same answer a composite with no wiring would get.
    payload = _scalar_composite()
    payload["composes"] = "not a list"
    assert _rendered(_template_parts(payload)) == f"{NODE_0}\n\n{NODE_1}"


# --- QA: the DAG shapes the walk had not been driven over --------------------
#
# Everything above pins the 2-node line the slice was written for. What follows walks the
# shapes AROUND it — a fan-IN, a name collision, damage that does not look like damage — and
# each one is here because the code makes a decision about it that nothing was reading back.
# Where the trial and the CARD answer the same question differently, both answers are asserted
# in one test: a reviewer sees both, and a disagreement between them is a defect in the pair
# rather than in either one.

NODE_CAP = (
    f"SELECT max(gross_pay) AS cap FROM {PAYROLL} "
    "WHERE department_code = {department_code}"
)
NODE_BAND = (
    f"SELECT employee_id FROM {PAYROLL} WHERE department_code = {{department_code}} "
    "AND gross_pay > {total} AND gross_pay < {cap}"
)


def _diamond() -> dict[str, Any]:
    """Two independent producers feeding ONE consumer — a fan-in, not a chain."""
    payload = _scalar_composite()
    payload["generalization"]["node_templates"] = [
        {"order": 0, "sql_template": NODE_0},
        {"order": 1, "sql_template": NODE_CAP},
        {"order": 2, "sql_template": NODE_BAND},
    ]
    payload["composes"] = [
        {"order": 0, "consumes": {}, "output": {"total": "scalar"}, "feeds_from": []},
        {"order": 1, "consumes": {}, "output": {"cap": "scalar"}, "feeds_from": []},
        {
            "order": 2,
            "consumes": {"total": "$0.total", "cap": "$1.cap"},
            "output": {"rows": "scalar"},
            "feeds_from": [0, 1],
        },
    ]
    return payload


async def test_a_fan_in_binds_every_upstream_cell_into_the_one_consumer() -> None:
    """⚠ THE SHAPE THE `scalar_shape` GUARD IS MOST LIKELY TO OVER-REFUSE.

    That guard counts the distinct scalars taken OFF ONE STEP, because this trial reads one cell
    per step and cannot split a wide row. A fan-IN is the mirror image — several steps, one
    scalar each — and it is legal: the runtime executor binds one `consumes` entry per producer.
    Nothing was driving it, so a guard keyed on the consumer instead of the producer would have
    refused every diamond and passed the whole suite.

    Both cells reach the terminal SQL, each under its OWN placeholder — the fan-in's actual
    failure mode is binding one value twice.
    """
    probe = _FakeProbe(cells=(41000, 99000))
    inbox, env = await _inbox(_diamond(), probe)

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.ok is True, (result.reason, result.detail)
    assert len(probe.ran) == 3, "both producers and the consumer run"
    terminal = probe.ran[-1]
    assert "gross_pay > 41000" in terminal, terminal
    assert "gross_pay < 99000" in terminal, terminal
    # The card asks for the one thing the DAG does not compute for itself.
    chips = [p["slot"] for p in _template_parts(_diamond()) if "slot" in p]
    assert chips == ["department_code"]


async def test_a_slot_named_like_a_consume_is_still_the_reviewers_to_supply() -> None:
    """A NAME COLLISION ACROSS STEPS: `{total}` is a reviewer slot in step 0 and an upstream
    consume in step 1. The two are the same string and different things, and `consumes` is
    per-node, so each occurrence must be resolved against ITS OWN node's wiring.

    The failure this pins is a walk that hoisted `consumes` to the whole DAG: step 0's `{total}`
    would then be "already filled", nothing would ask the reviewer for it, and the step would
    fail to bind — or worse, bind step 1's cell into the step that produces it.
    """
    payload = _scalar_composite()
    payload["generalization"]["node_templates"] = [
        {
            "order": 0,
            "sql_template": (
                f"SELECT sum(gross_pay) AS total FROM {PAYROLL} "
                "WHERE department_code = {total}"
            ),
        },
        {"order": 1, "sql_template": NODE_1},
    ]
    probe = _FakeProbe(cells=(41000,))
    inbox, env = await _inbox(payload, probe)

    result = await inbox.trial_run(
        env.candidate_id,
        bindings={"department_code": "0420", "total": "TYPED"},
        token="reviewer-token",
    )

    assert result.ok is True, (result.reason, result.detail)
    producer, consumer = probe.ran
    assert "'TYPED'" in producer, "step 0's {total} is the reviewer's value"
    assert "41000" in consumer, "step 1's {total} is step 0's cell"
    assert "TYPED" not in consumer
    # ...and the card offers a box for exactly that one, under the same name.
    chips = {p["slot"] for p in _template_parts(payload) if "slot" in p}
    assert chips == {"department_code", "total"}


async def test_an_upstream_cell_is_bound_as_a_literal_not_interpolated() -> None:
    """⚠ THE INJECTION SEAM THIS SLICE OPENED.

    Before it, no warehouse VALUE was ever put back into SQL on this path — `WarehouseProbe`
    refuses to return one (D98). `run_cell` does, and the trial binds it into the next node's
    template, so a cell whose content is quote-shaped is now reaching a query builder.

    It goes through `bind_template`, the same typed-literal boundary the runtime uses (D10), so
    the value arrives INTACT and QUOTED: the embedded quote is ClickHouse-doubled and the
    newline is escaped, so nothing in the cell can end the literal or start a comment. Asserted
    on the SQL the probe was handed, because that is the only place it is observable.
    """
    probe = _FakeProbe(cells=("O'Brien\n-- drop",))
    inbox, env = await _inbox(_scalar_composite(), probe)

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.ok is True, (result.reason, result.detail)
    consumer = probe.ran[1]
    assert "'O''Brien" in consumer, consumer  # intact, and the quote is escaped
    assert "'O'Brien" not in consumer, "an unescaped quote would end the literal early"
    assert "\n" not in consumer, "a raw newline would put `-- drop` on its own line"
    assert consumer.endswith("'"), consumer  # the literal is still closed
    # The value the warehouse returned never comes back to the reviewer.
    assert "O''Brien" not in json.dumps(result.to_wire())


async def test_a_step_with_no_plan_entry_is_refused_not_asked_of_the_reviewer() -> None:
    """⚠ QA's O2, now refused: a DESYNC between the two halves of the join is damage.

    The wiring lives ONLY in `composes`. Drop the entry for step 1 and `{total}` stops being an
    edge and becomes an ordinary unbound slot — the card would grow a box for it, and a reviewer
    who typed a number would get a GREEN result for a two-step blueprint whose second step never
    saw the first one's output. That is the "green for a query nobody ran" shape
    `no_scalar_probe` refuses elsewhere, reached by damage that leaves nothing to notice.

    It is refusable because the producer's guarantee is a BIJECTION: `_generalize_composite`
    appends exactly one `NodeTemplate` per `composes` entry, roots included (their entry exists
    with an empty `consumes`), and nothing downstream drops payload keys. So a template with no
    wiring is a shape the pipeline cannot emit.
    """
    payload = _scalar_composite()
    payload["composes"] = [payload["composes"][0]]  # step 1's wiring is gone
    probe = _FakeProbe(cells=(41000,))
    inbox, env = await _inbox(payload, probe)

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )
    assert result.ok is False
    assert result.reason == "malformed_composite"
    assert "[1]" in result.detail and "no wiring" in result.detail, result.detail
    assert probe.ran == []

    # ...and typing the value the missing edge would have supplied does NOT buy a green run.
    probe = _FakeProbe(cells=(41000,))
    inbox, env = await _inbox(payload, probe)
    typed = await inbox.trial_run(
        env.candidate_id,
        bindings={"department_code": "0420", "total": "9"},
        token="reviewer-token",
    )
    assert typed.reason == "malformed_composite"
    assert probe.ran == []
    # ⚠ THE CARD STILL DISAGREES: it renders both steps and offers a box for `{total}`, because
    # a projection with no wiring cannot tell an edge from a slot. Pinned as a pair so a fix to
    # either one has to consider the other — the trial's refusal is the half that matters.
    chips = {p["slot"] for p in _template_parts(payload) if "slot" in p}
    assert chips == {"department_code", "total"}


async def test_wiring_for_a_step_that_has_no_sql_is_refused_too() -> None:
    """The desync in the other direction, and the wrong answer is worse: the step with no
    template may be the TERMINAL one, so the walk would gate an INTERMEDIATE's result and report
    `verify_passed` about the wrong node entirely."""
    payload = _scalar_composite()
    payload["generalization"]["node_templates"] = [{"order": 0, "sql_template": NODE_0}]
    probe = _FakeProbe(cells=(41000,))
    inbox, env = await _inbox(payload, probe)

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.reason == "malformed_composite"
    assert "no SQL" in result.detail, result.detail
    assert probe.ran == []


async def test_two_plan_entries_claiming_one_step_are_refused_like_two_templates() -> None:
    """The join key stops being a key on the WIRING side. A dict comprehension would silently
    keep the last entry — and with it whichever `consumes` happened to be written second. The two
    halves of one join cannot hold different standards, so this is refused exactly as duplicate
    template orders are."""
    payload = _scalar_composite()
    payload["composes"].append(
        {"order": 1, "consumes": {}, "output": {"rows": "scalar"}, "feeds_from": []}
    )
    probe = _FakeProbe(cells=(41000,))
    inbox, env = await _inbox(payload, probe)

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.reason == "malformed_composite"
    assert "step 1" in result.detail
    assert probe.ran == []


async def test_two_templates_claiming_one_step_are_refused_though_the_card_shows_both() -> None:
    """Duplicate `order`s: the join key stops being a key, so "step 0" names two SQLs and the
    walk cannot say which one produces step 0's cell. `_trial_nodes` refuses BY NAME.

    ⚠ THE CARD DOES NOT. `_composite_parts` sorts and renders both, so the reviewer reads a
    plausible two-step blueprint with a bindable box, presses the button and is told the
    composite is malformed. The refusal is the safe half; the render is the half that sets up
    the surprise, and it is pinned here so a fix to either one has to consider the other.
    """
    payload = _scalar_composite()
    payload["generalization"]["node_templates"] = [
        {"order": 0, "sql_template": NODE_0},
        {"order": 0, "sql_template": NODE_1},
    ]
    probe = _FakeProbe(cells=(41000,))
    inbox, env = await _inbox(payload, probe)

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.ok is False
    assert result.reason == "malformed_composite"
    assert "step 0" in result.detail
    assert probe.ran == []
    # The card, meanwhile, renders the pair as if it were a healthy DAG.
    assert _rendered(_template_parts(payload)) == f"{NODE_0}\n\n{NODE_1}"


async def test_a_step_with_an_empty_template_is_refused_though_the_card_hides_it() -> None:
    """A node whose `sql_template` is `""` — the shape a half-written rewrite leaves behind.

    The trial refuses the WHOLE composite by name, which is right: a step with no SQL cannot
    run, and running the others would report on a DAG that is not this one.

    ⚠ AND AGAIN THE CARD DISAGREES: `_composite_parts` drops a node with no template, so the
    reviewer is shown a tidy ONE-step blueprint — no gap, no marker — and then told step 1
    carries no SQL. Pinned as a pair for the same reason as the duplicate-order case.
    """
    payload = _scalar_composite()
    payload["generalization"]["node_templates"][1]["sql_template"] = ""
    probe = _FakeProbe(cells=(41000,))
    inbox, env = await _inbox(payload, probe)

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.ok is False
    assert result.reason == "malformed_composite"
    assert "step 1" in result.detail
    assert probe.ran == []
    # The dropped step leaves no trace on the card.
    assert _rendered(_template_parts(payload)) == NODE_0


async def test_a_one_step_composite_runs_through_the_terminal_probe_only() -> None:
    """A `node_templates` list of ONE. It is a composite by storage shape and a plain query by
    behaviour, and the walk must not treat "first" as "upstream": there is nothing downstream to
    feed, so the single node is the TERMINAL and goes through `run` — the D56 gate — rather than
    through `run_cell`, which would gate nothing and report a cell as a shape.
    """
    payload = _scalar_composite()
    payload["generalization"]["node_templates"] = [{"order": 0, "sql_template": NODE_0}]
    payload["composes"] = [
        {"order": 0, "consumes": {}, "output": {"total": "scalar"}, "feeds_from": []}
    ]
    probe = _FakeProbe(result=ProbeResult(0, None, ("total",)))
    inbox, env = await _inbox(payload, probe)

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.ok is True, (result.reason, result.detail)
    assert result.columns == ("total",)
    assert probe.cell_sqls == [], "the only node is the terminal, not an intermediate"
    assert len(probe.ran) == 1


async def test_a_composite_with_no_footprint_can_never_be_trialled() -> None:
    """⚠ THE LIVE SYMPTOM: `no_uses_scope` on a real minted composite, with a valid token.

    `uses` is `()` exactly when `_provenance_uses` FAILED (`generalize/builder.py`), which also
    stamps `explain_ok=False` — and that is precisely what routes a composite to a human instead
    of auto-landing it. So the candidate a reviewer most needs to trial is the one whose trial is
    refused before any SQL is built, and no token can change that.

    Both spellings reach it: an absent `uses` key and an empty list. Pinned as CURRENT behaviour,
    with the reason it deserves a second look recorded here rather than in a passing comment: the
    refusal's stated rationale is that an empty scope "would mint an UNRESTRICTED token", and on
    THIS path nothing is minted at all — `SuppliedTokenMinter.mint` ignores `column_scope` and
    hands back the reviewer's own token verbatim. The refusal may still be the policy we want; it
    is not the mechanism the comment describes.
    """
    for label, mutate in (
        ("absent", lambda gen: gen.pop("uses")),
        ("empty", lambda gen: gen.update(uses=[])),
    ):
        payload = _scalar_composite()
        payload["generalization"]["static_validation"]["explain_ok"] = False
        payload["generalization"]["static_validation"]["outcome"] = "fail_to_review"
        mutate(payload["generalization"])
        probe = _FakeProbe(cells=(41000,))
        inbox, env = await _inbox(payload, probe)

        result = await inbox.trial_run(
            env.candidate_id,
            bindings={"department_code": "0420"},
            token="a-real-token-the-reviewer-holds",
        )

        assert result.ok is False, label
        assert result.reason == "no_uses_scope", label
        assert probe.ran == [], label


async def test_a_non_list_uses_is_not_shredded_into_a_character_scope() -> None:
    """QA's D1, fixed: `uses` decides what a query is allowed to touch, and it was the one read
    on this path with no type guard.

    `tuple(generalization.get("uses") or [])` accepts any iterable, so a rehydrated STRING
    footprint was exploded into one single-character "column" per letter and handed to the probe
    as `column_scope` — and the trial then reported `ok=True`, having proved something about a
    scope nobody declared. Harmless under the trial's own `SuppliedTokenMinter` (which ignores
    scope), NOT harmless on the offline fallback to the scheduler's probe, whose minter posts
    that list to the IdP.

    Refused rather than salvaged: reading "the one column it names" would be this surface
    guessing at a corrupt footprint, which is what every other read here declines to do.
    """
    payload = _scalar_composite()
    payload["generalization"]["uses"] = "payroll.payroll_fact.gross_pay"
    probe = _FakeProbe(cells=(41000,))
    inbox, env = await _inbox(payload, probe)

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.ok is False
    assert result.reason == "no_uses_scope"
    assert "not a list" in result.detail, result.detail
    assert probe.scopes == [], "nothing was handed a character scope"


@pytest.mark.parametrize(
    "damaged", ["payroll.payroll_fact.gross_pay", {"a": 1}, ["ok.t.c", 7], ["ok.t.c", ""]]
)
async def test_the_same_footprint_guard_holds_on_the_single_template_path(damaged: Any) -> None:
    """The defect's other half. The same expression was written twice, so the fix is written
    once and both paths call it — a guard that protected only the new path would leave the
    older, more-travelled one shredding footprints."""
    base = json.loads((FIXTURES / "envelopes_each_reason.json").read_text())[
        "leakage_near_miss"
    ]
    env = replace(
        CandidateEnvelope.from_doc(base),
        status=CandidateStatus.IN_REVIEW,
        payload={
            "generalization": {
                "sql_template": f"SELECT department FROM {PAYROLL}",
                "uses": damaged,
            }
        },
    )
    store = InMemoryCandidateStore()
    await store.put(env)
    probe = _FakeProbe()
    inbox = ReviewInbox(store, probe_factory=lambda _token: probe)

    result = await inbox.trial_run(env.candidate_id, bindings={}, token="reviewer-token")

    assert result.ok is False
    assert result.reason == "no_uses_scope"
    assert probe.scopes == []


async def test_a_warehouse_error_never_carries_an_upstream_cell_back(caplog) -> None:
    """⚠ THE LEAK THE `bind_failed` BRANCH ALREADY WITHHELD, through the other door.

    A consumer's SQL has the upstream cell rendered into it as a literal, and a warehouse quotes
    the failing query back at you — `Cannot parse Date from String '…'`, `Syntax error near …`.
    Relaying that verbatim walks a governed value onto the reviewer's screen AND into this
    process's log, past the rule the trial's own docstring states ("structure, never values").
    Same shape as the token scrub already on this path, applied to the other secret the walk
    handles.
    """
    cell = "Aurelia Vasquez-Kowalczyk"  # long enough to substitute out of a message
    probe = _FakeProbe(
        cells=(cell,),
        raises=RuntimeError(
            f"Code: 53. DB::Exception: Cannot parse: SELECT ... AND name > '{cell}'"
        ),
    )
    inbox, env = await _inbox(_scalar_composite(), probe)

    with caplog.at_level("INFO", logger="data_agent.learning.inbox.inbox"):
        result = await inbox.trial_run(
            env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
        )

    assert result.ok is False
    assert result.reason == "warehouse_error"
    assert cell not in json.dumps(result.to_wire())
    assert cell not in caplog.text
    # The rest of the message survives, because a reason with no cause is what this whole
    # scrub-instead-of-withhold shape exists to avoid.
    assert "Cannot parse" in result.detail
    assert "[upstream value redacted]" in result.detail


async def test_a_numeric_cell_is_redacted_in_place_not_leaked(caplog) -> None:
    """The realistic shape: the intermediate is a SUM, and the warehouse quotes it back inside
    the query it rejected. A 5-digit aggregate is a governed value like any other — it is
    substituted out, and the message survives around it."""
    probe = _FakeProbe(
        cells=(41000,),
        raises=RuntimeError("Code: 43. DB::Exception: gross_pay > 41000 is not comparable"),
    )
    inbox, env = await _inbox(_scalar_composite(), probe)

    with caplog.at_level("INFO", logger="data_agent.learning.inbox.inbox"):
        result = await inbox.trial_run(
            env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
        )

    assert result.reason == "warehouse_error"
    assert "41000" not in json.dumps(result.to_wire())
    assert "41000" not in caplog.text
    assert "not comparable" in result.detail


async def test_a_cell_too_short_to_scrub_withholds_the_whole_message(caplog) -> None:
    """A cell of `5` cannot be substituted out of an error string — every byte offset, error
    code and column name containing a 5 would be mangled, and the reviewer would read a sentence
    that says something else. So the message is withheld WHOLE and the refusal says why.

    Fail-closed on the value, honest about the cost — the alternative was leaking short cells on
    the grounds that they are short. A 4+ character cell (an aggregate, a date, an id) is
    specific enough to substitute, and keeps its message: see the test above.
    """
    probe = _FakeProbe(cells=(5,), raises=RuntimeError("Code: 53. DB::Exception: near 5"))
    inbox, env = await _inbox(_scalar_composite(), probe)

    with caplog.at_level("INFO", logger="data_agent.learning.inbox.inbox"):
        result = await inbox.trial_run(
            env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
        )

    assert result.reason == "warehouse_error"
    assert "DB::Exception" not in result.detail
    assert "DB::Exception" not in caplog.text
    assert "withheld" in result.detail
    assert "step 1" in result.detail


async def test_a_first_step_failure_still_reports_the_warehouse_verbatim() -> None:
    """The scrub is scoped to what has actually been READ. Step 0 binds no upstream cell, so
    there is nothing to protect and the message comes back whole — a blanket withholding would
    have cost every composite the diagnostic the single-template path keeps."""
    probe = _FakeProbe(
        cell_raises=RuntimeError("Code: 60. DB::Exception: Unknown table payroll.x")
    )
    inbox, env = await _inbox(_scalar_composite(), probe)

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.reason == "warehouse_error"
    assert "Unknown table payroll.x" in result.detail


def test_the_probe_tells_transport_damage_apart_from_a_non_scalar_shape() -> None:
    """`_single_cell` answers two different questions and must not conflate them: `None` says
    THE BLUEPRINT did not return a scalar (`scalar_shape`); raising says THE RESULT could not be
    read at all (`warehouse_error`). Blaming the blueprint for transport damage sends a reviewer
    to rewrite SQL that is fine.

    And the rows are counted RAW: filtering unreadable rows out first made `["garbage", [5]]`
    collapse to one row and FAIL OPEN to the scalar 5 — an arbitrary cell bound downstream out
    of a payload nobody could parse.
    """
    import pytest as _pytest

    from data_agent.learning.promotion.warehouse_probe import (
        WarehouseProbeError,
        _single_cell,
    )

    assert _single_cell({"columns": ["t"], "rows": [[41000]]}) == 41000
    assert _single_cell({"columns": ["t"], "rows": [[None]]}) is None  # NULL — unbindable
    assert _single_cell({"rows": []}) is None  # 0 rows
    assert _single_cell({"rows": [[1], [2]]}) is None  # fan-out
    assert _single_cell({"rows": [[1, 2]]}) is None  # wide row
    assert _single_cell({"rows": ["garbage", [5]]}) is None, "must not fail open to 5"

    for damaged in (None, "not a dict", {"columns": []}, {"rows": "nope"}, {"rows": ["x"]}):
        with _pytest.raises(WarehouseProbeError):
            _single_cell(damaged)


async def test_one_step_consumed_as_two_scalars_is_refused() -> None:
    """⚠ THE GUARD THE FAN-IN TEST DOES NOT REACH. That one takes ONE scalar off each of TWO
    steps, which is legal. This takes TWO scalars off ONE step, which the runtime supports (it
    reads a wide row) and this trial cannot: `run_cell` returns a single cell, so the walk would
    have to bind that one value under BOTH names — the same number twice, silently, in a query
    the reviewer would then be told is verified.
    """
    payload = _scalar_composite()
    payload["generalization"]["node_templates"][1]["sql_template"] = (
        f"SELECT employee_id FROM {PAYROLL} WHERE department_code = {{department_code}} "
        "AND gross_pay > {total} AND gross_pay < {cap}"
    )
    payload["composes"][0]["output"] = {"total": "scalar", "cap": "scalar"}
    payload["composes"][1]["consumes"] = {"total": "$0.total", "cap": "$0.cap"}
    probe = _FakeProbe(cells=(41000, 99000))
    inbox, env = await _inbox(payload, probe)

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.ok is False
    assert result.reason == "scalar_shape"
    assert "step 0" in result.detail and "one cell per step" in result.detail, result.detail
    assert probe.ran == [], "refused on the declared shape, before any query"
