"""`row_count_measured` — is the number on the trial card a MEASUREMENT or a placeholder?

**The bug this closes.** `TrialRunResult.row_count` is `0` in two completely different
situations: the warehouse counted zero rows, and nobody counted anything. The flag exists to
tell them apart on the reviewer's card, and it was observed live claiming the wrong one — a
`scalar_shape` refusal, which never reaches the warehouse at all, came back as
`row_count: 0, row_count_measured: true`. A reviewer reading that card was told the trial had
run and found the blueprint empty, when in fact the trial had refused before dispatching
anything.

The flag is now DERIVED, `distinct_grain_count is not None`, and defaults `False`. These tests
pin both halves of that:

  * every REFUSAL is unmeasured, because a refusal builds the result out of the dataclass
    defaults and never goes near a probe — so the default is what is actually being asserted,
    and it is the one field where "the safe default" and "the true answer" coincide;
  * a run that DID reach the warehouse is measured only when the COUNT(DISTINCT) round-trip
    actually returned counts. A DECLARED grain is not evidence of that: `map_grain_columns`
    can fail to find the column and `unpack_grain_probe` can fail to read the result, and on
    both paths `MCPWarehouseProbe.run` returns the `row_count=0, distinct=None` placeholder
    while the declaration still says the grain is verifiable.

The second group drives the REAL `MCPWarehouseProbe` over a scripted MCP transport rather than
a hand-written `ProbeResult`, because the claim being tested is precisely that the flag agrees
with what the probe can produce — a fixture is free to invent a pairing the probe never emits.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.inbox import ReviewInbox
from data_agent.learning.promotion.models import ProbeResult
from data_agent.learning.promotion.warehouse_probe import MCPWarehouseProbe
from data_agent.runtime.mcp.fake_client import FakeMCPClient

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"

# The frozen S4 single-blueprint template's slots.
BINDINGS = {"department": "0420", "year": "2025", "region": "NA"}


class _FakeTokenMinter:
    """A `TokenMinter` double — no IdP, and it records nothing this file asserts on."""

    async def mint(self, column_scope: list[str], *, session_id: str) -> str:
        return "jwt-scoped"


def _runquery_result(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _candidate(*, grain_columns: list[str] | None = None) -> CandidateEnvelope:
    """The frozen S4 single blueprint at `in_review`, with a chosen declared grain.

    The grain is the input that matters here: it is what makes the probe ATTEMPT the
    COUNT(DISTINCT) round-trip, and therefore what makes "declared" and "measured" two
    different questions rather than the same one.
    """
    doc = json.loads((FIXTURES / "s4_enriched_blueprint.json").read_text())
    env = CandidateEnvelope.from_doc(doc["single"]["envelope"])
    payload = json.loads(json.dumps(env.payload))
    if grain_columns is not None:
        payload["generalization"]["result_grain"] = {
            "columns": grain_columns,
            "verifiable": True,
        }
    return replace(env, status=CandidateStatus.IN_REVIEW, payload=payload)


async def _inbox(env: CandidateEnvelope, probe: Any) -> ReviewInbox:
    store = InMemoryCandidateStore()
    await store.put(env)
    return ReviewInbox(store, probe_factory=lambda _token: probe)


async def _real_probe_inbox(
    env: CandidateEnvelope, scripted: list[dict]
) -> tuple[ReviewInbox, FakeMCPClient]:
    """An inbox whose trial runs the PRODUCTION probe over a scripted MCP transport.

    The transport comes back too: how many `runQuery` calls the walk made is the only way to
    tell "the grain probe was never built" from "it ran and could not be read", and those are
    the two paths this file has to keep apart.
    """
    mcp = FakeMCPClient(scripted={"runQuery": scripted})
    probe = MCPWarehouseProbe(mcp_client=mcp, token_minter=_FakeTokenMinter())
    return await _inbox(env, probe), mcp


# --- a refusal never claims to have measured anything -------------------------


async def test_a_refused_trial_reports_an_unmeasured_row_count() -> None:
    """⚠ THE LIVE REPRO, in its cheapest reproducible form.

    A trial that never dispatches a query has, by definition, measured nothing. Every refusal
    builds its `TrialRunResult` from the dataclass defaults — `ok=False` plus a reason — so
    this is really an assertion about the DEFAULT, and the default is the thing that was
    wrong. Asserted on the WIRE shape as well as the object, because the card reads the wire
    and a `to_wire` that hard-coded `True` would be an identical bug one layer out.
    """
    probe = _StructureOnlyProbe()
    inbox = await _inbox(_candidate(), probe)

    result = await inbox.trial_run(
        _CANDIDATE_ID, bindings={}, token="reviewer-token"  # nothing bound ⇒ refused
    )

    assert result.ok is False
    assert result.reason == "missing_bindings"
    # The probe WOULD have answered with a measured count — it was simply never asked.
    assert probe.ran == []
    assert result.row_count == 0
    assert result.row_count_measured is False
    assert result.to_wire()["row_count_measured"] is False


async def test_the_refusal_that_was_seen_lying_is_the_composite_scalar_shape_one() -> None:
    """The exact card from the live observation: a composite whose upstream step did not
    return a single cell. It refuses part-way through the DAG walk — so, unlike the refusal
    above, the warehouse HAS been touched — and it still measured no row count, because the
    terminal node (the only one whose rows are the blueprint's answer) never ran.

    This is the case that makes "did we dispatch anything" the wrong question and
    `distinct_grain_count is not None` the right one.
    """
    env = _composite_candidate()
    inbox = await _inbox(env, _CellShapeProbe())

    result = await inbox.trial_run(
        env.candidate_id, bindings={"department_code": "0420"}, token="reviewer-token"
    )

    assert result.ok is False
    assert result.reason == "scalar_shape"
    assert (result.row_count, result.row_count_measured) == (0, False)
    assert result.to_wire()["row_count_measured"] is False


# --- a DECLARED grain is not a MEASURED one -----------------------------------


async def test_a_grain_the_probe_cannot_map_leaves_the_row_count_unmeasured() -> None:
    """`map_grain_columns` found no output column matching the declared grain.

    The declaration says `verifiable: true` and the D56 gate is asked to check a grain; the
    probe answers with the `row_count=0, distinct=None` placeholder because it could not
    build the COUNT(DISTINCT) at all, and dispatches ONE query instead of two. Deriving the
    flag from the DECLARATION — `bool(grain)`, or `VerifyOutcome.grain_checked`, both of
    which are true here — would put a measured-looking `0` on the card for a count nobody
    took.
    """
    env = _candidate(grain_columns=["headcount"])  # not an output column of the template
    inbox, mcp = await _real_probe_inbox(
        env, [_runquery_result(["total_earnings"], [[41000.0]])]
    )

    result = await inbox.trial_run(
        env.candidate_id, bindings=BINDINGS, token="reviewer-token"
    )

    assert result.ok is True, result.reason
    assert len(mcp.calls) == 1  # the COUNT(DISTINCT) was never even built
    assert result.distinct_grain_count is None
    assert result.row_count == 0
    assert result.row_count_measured is False


async def test_a_grain_probe_whose_result_cannot_be_read_is_also_unmeasured() -> None:
    """The second None-producing path, and the one a declaration cannot see coming.

    Here the grain column DOES map, so the COUNT(*)/COUNT(DISTINCT) query is genuinely
    dispatched — the flag cannot be derived from "did the second round-trip happen" either.
    `unpack_grain_probe` then fails to read counts out of what came back (a headerless or
    row-less result, a non-numeric cell) and returns `(None, None)`, which the probe turns
    into the same placeholder. `verify.py` fail-closes on that; the card must too.
    """
    env = _candidate(grain_columns=["total_earnings"])  # DOES map to an output column
    inbox, mcp = await _real_probe_inbox(
        env,
        [
            _runquery_result(["total_earnings"], [[41000.0]]),  # the column-header read
            _runquery_result(["__bp_n", "__bp_d"], []),  # the grain probe: no rows to read
        ],
    )

    result = await inbox.trial_run(
        env.candidate_id, bindings=BINDINGS, token="reviewer-token"
    )

    assert result.ok is True, result.reason
    # The round-trip DID happen — this is not the unmappable path wearing a different hat.
    assert len(mcp.calls) == 2
    assert "count(distinct" in mcp.calls[1].args["sql"].lower()
    assert result.distinct_grain_count is None
    assert result.row_count == 0
    assert result.row_count_measured is False


async def test_a_grain_probe_that_answered_is_the_one_case_that_reports_measured() -> None:
    """The positive control, without which every assertion above is satisfied by a constant
    `False`. The COUNT(DISTINCT) round-trip completed and returned counts, so — and ONLY so —
    the row count on the card is a real reading of the warehouse.
    """
    env = _candidate(grain_columns=["total_earnings"])
    inbox, _mcp = await _real_probe_inbox(
        env,
        [
            _runquery_result(["total_earnings"], [[41000.0]]),
            _runquery_result(["__bp_n", "__bp_d"], [[7, 7]]),
        ],
    )

    result = await inbox.trial_run(
        env.candidate_id, bindings=BINDINGS, token="reviewer-token"
    )

    assert result.ok is True, result.reason
    assert (result.row_count, result.distinct_grain_count) == (7, 7)
    assert result.row_count_measured is True
    assert result.to_wire()["row_count_measured"] is True


async def test_an_undeclared_grain_is_honestly_unmeasured_rather_than_zero_rows() -> None:
    """The commonest card of all: most candidates declare no verifiable grain, so no count is
    ever taken and `row_count: 0` means "not measured", not "empty result". Reported as such
    even though nothing went wrong — the flag is about what was ESTABLISHED, and a successful
    trial can establish the column signature while establishing nothing about the row count.
    """
    env = _candidate()  # the fixture's own grain: columns=[], verifiable=false
    inbox, _mcp = await _real_probe_inbox(
        env, [_runquery_result(["total_earnings"], [[41000.0]])]
    )

    result = await inbox.trial_run(
        env.candidate_id, bindings=BINDINGS, token="reviewer-token"
    )

    assert result.ok is True, result.reason
    assert result.columns == ("total_earnings",)  # the trial DID prove the shape ...
    assert result.row_count_measured is False  # ... and did not prove a row count


# --- fixtures used above ------------------------------------------------------

_CANDIDATE_ID = json.loads((FIXTURES / "s4_enriched_blueprint.json").read_text())["single"][
    "envelope"
]["candidate_id"]

PAYROLL = "payroll.payroll_fact"


def _composite_candidate() -> CandidateEnvelope:
    """A 2-node scalar composite (the shape the live `scalar_shape` refusal came from)."""
    env = _candidate()
    payload = {
        "intent": "employees paid above their department total",
        "generalization": {
            "sql_template": None,  # a composite's top-level template is None by construction
            "uses": [f"{PAYROLL}.department_code", f"{PAYROLL}.gross_pay"],
            "uses_rules": [],
            "node_templates": [
                {
                    "order": 0,
                    "sql_template": (
                        f"SELECT sum(gross_pay) AS total FROM {PAYROLL} "
                        "WHERE department_code = {department_code}"
                    ),
                },
                {
                    "order": 1,
                    "sql_template": (
                        f"SELECT employee_id FROM {PAYROLL} "
                        "WHERE department_code = {department_code} AND gross_pay > {total}"
                    ),
                },
            ],
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
        },
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
    return replace(env, payload=payload)


class _StructureOnlyProbe:
    """A probe that would answer if asked. The refusal tests assert it never is."""

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def run(
        self,
        sql: str,
        *,
        grain_columns: tuple[str, ...] = (),
        column_scope: tuple[str, ...] = (),
    ) -> ProbeResult:
        self.ran.append(sql)
        return ProbeResult(row_count=3, distinct_grain_count=3, columns=("total_earnings",))


class _CellShapeProbe(_StructureOnlyProbe):
    """...plus a `run_cell` that reports "that was not a single cell" (the live case)."""

    async def run_cell(self, sql: str, *, column_scope: tuple[str, ...] = ()) -> Any:
        self.ran.append(sql)
        return None
