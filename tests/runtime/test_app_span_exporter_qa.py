"""QA adversarial companion for the Layer-3 Slice-2 observability/PII seam
(D-L3-5 / D25). Tests ONLY — new file, additive; never modifies `app.py`,
`tracing.py`, or the demo launcher.

The load-bearing security claim of this slice is: the `GET /_test/spans`
dump route is a TEST affordance that CANNOT be reached in production because
it is registered ONLY when a `span_exporter` is injected into `create_app`,
and production passes `span_exporter=None`. This module proves that claim
STRUCTURALLY (the route is absent from `app.routes`, not merely erroring),
then — with an exporter injected as the demo does — proves the D25 redaction
invariant actually holds over the REAL emitted spans, and that the PII-scan
used to prove it is non-vacuous (it can catch a planted leak).

Covered:
  * Default-OFF structural proof: `create_app(...)` WITHOUT `span_exporter`
    registers NO `/_test/spans` route (asserted over `app.routes`), and the
    endpoint 404s; WITH an exporter the route exists.
  * The PII boundary: a turn driven with a KNOWN sentinel in a SQL literal AND
    in a result cell — the `/_test/spans` dump contains the sentinel NOWHERE
    (D25 redaction holds on shipped spans); the AGENT span carries only a
    scope HASH, never the raw scope.
  * Non-vacuity meta-test: a span deliberately carrying the sentinel in an
    attribute IS caught by the exact same scan — the invariant can fail.
  * `configure_tracing` default path: `span_exporter=None` attaches zero extra
    processors (byte-identical); an exporter attaches exactly one and receives
    POST-redaction attributes.
  * D44 replay-filter drop correctness through the real `filter_trail`.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime import app as app_module
from data_agent.runtime.app import create_app
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.context.scope_filter import filter_trail
from data_agent.runtime.mcp.client import MCPToolSpec
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.observability import tracing
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import TrailEntry

SESSION_ID = "sess-span-qa"
HEADERS = {"Authorization": "Bearer test-jwt", "X-Session-Id": SESSION_ID}

# A distinctive value planted where PII would ride: a SQL string literal AND a
# result cell. Deliberately NOT a bare digit run — a scope hash / uuid could
# collide with one by chance; this token cannot appear unless redaction leaked.
PII_SENTINEL = "PII_SENTINEL_SSN_QARED_770077"
# The scope this turn runs under; its RAW members must never surface on a span
# (only `hash_scope(...)` may). Made distinctive for the same reason.
SCOPE_MEMBER = "sensitive_db.people.national_id_QARED"


def _route_paths(app: Any) -> set[str]:
    return {getattr(r, "path", None) for r in app.routes}


def _build_app(
    monkeypatch,
    *,
    span_exporter: Any,
    scope: frozenset[str] = frozenset(),
) -> Any:
    """A `create_app` wired to Layer-1 fakes (test_app.py pattern), driving one
    runQuery whose SQL carries `PII_SENTINEL` as a string literal and whose
    scripted result carries it as a cell value. `verify_jwt` is monkeypatched
    to return *scope* (the raw scope this turn runs under)."""
    monkeypatch.setattr(app_module, "verify_jwt", lambda *a, **k: scope)
    mcp_client = FakeMCPClient(
        tools=[
            MCPToolSpec(
                name="runQuery",
                description="",
                input_schema={"type": "object", "properties": {"sql": {"type": "string"}}},
            )
        ],
        scripted={
            "runQuery": [
                {
                    "columns": ["national_id"],
                    "rows": [[PII_SENTINEL]],
                    "row_count": 1,
                    "truncated": False,
                }
            ]
        },
    )
    settings = RuntimeSettings(
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        # Hermeticity: this suite asserts the redacted/hidden posture (SQL literals /
        # result cells / raw scope never reach a span). Since 2026-07-15 that is the
        # NON-default (opt-out) posture — `otlp_hide_llm_content` now defaults False
        # (reveal) — so both flags are pinned EXPLICITLY here to the hidden posture,
        # so an operator's ambient OTLP_DISABLE_REDACTION=1 debug flag (e.g. in .env)
        # can't flip the posture out from under the invariant.
        otlp_disable_redaction=False,
        otlp_hide_llm_content=True,
    )
    return create_app(
        settings=settings,
        session_store=InMemorySessionStore(),
        mcp_client=mcp_client,
        model_client=ScriptedModelClient(
            [
                ModelTurnResult(
                    tool_calls=[
                        ToolCallRequest(
                            id="q1",
                            name="runQuery",
                            arguments={
                                "sql": f"SELECT national_id FROM t WHERE ssn = '{PII_SENTINEL}'"
                            },
                        )
                    ]
                ),
                ModelTurnResult(assistant_text="Looked it up."),
            ]
        ),
        catalog=CatalogHandle({}),
        span_exporter=span_exporter,
    )


def _scan_dump_for(dump: dict[str, Any], needle: str) -> list[str]:
    """Return a list of human-readable locations where *needle* occurs in a
    `/_test/spans` dump — span name, kind, or ANY attribute key/value. The
    SINGLE scan used by both the leak assertion and the non-vacuity meta-test,
    so the two can never disagree on what 'contains PII' means."""
    hits: list[str] = []
    for span in dump["spans"]:
        if needle in str(span.get("name", "")):
            hits.append(f"name={span['name']!r}")
        if needle in str(span.get("kind", "")):
            hits.append(f"kind={span['kind']!r}")
        for key, value in span.get("attributes", {}).items():
            if needle in str(key):
                hits.append(f"attr-key={key!r}")
            if needle in str(value):
                hits.append(f"attr-value {key}={value!r}")
    return hits


def _dump_shape(spans: list[Any]) -> dict[str, Any]:
    """Replicate `app.py::_test_spans`'s exact dump shape from raw finished
    spans — so the non-vacuity meta-test scans the identical structure the
    real endpoint produces, not a hand-rolled stand-in."""
    dumped = [
        {
            "name": s.name,
            "kind": s.attributes.get("openinference.span.kind"),
            "attributes": {k: str(v) for k, v in s.attributes.items()},
        }
        for s in spans
    ]
    return {"spans": dumped, "count": len(dumped)}


# ==========================================================================
# 1. Default-OFF structural proof — the "can't reach in prod" boundary
# ==========================================================================


class TestTestSpansRouteGating:
    def test_no_span_exporter_means_route_is_structurally_absent(self, monkeypatch) -> None:
        # THE load-bearing prod-safety proof: with span_exporter=None (production),
        # the /_test/spans path must not exist AT ALL on the app — not merely
        # error. Asserted over the actual route table, so a future refactor that
        # registers-but-guards the route (a weaker posture) would fail here.
        app = _build_app(monkeypatch, span_exporter=None)
        assert "/_test/spans" not in _route_paths(app)

    def test_no_span_exporter_endpoint_404s(self, monkeypatch) -> None:
        # Belt-and-braces behavioral confirmation of the structural proof above.
        app = _build_app(monkeypatch, span_exporter=None)
        resp = TestClient(app).get("/_test/spans")
        assert resp.status_code == 404

    def test_injected_span_exporter_registers_the_route(self, monkeypatch) -> None:
        # The affordance is reachable ONLY on the injected-exporter path (the demo
        # launcher's DEMO_TEST_SPANS=1). Confirms the gate is a real switch, not
        # dead in both directions.
        app = _build_app(monkeypatch, span_exporter=InMemorySpanExporter())
        assert "/_test/spans" in _route_paths(app)
        resp = TestClient(app).get("/_test/spans")
        assert resp.status_code == 200
        assert resp.json() == {"spans": [], "count": 0}  # no turn yet → empty


# ==========================================================================
# 2. The PII boundary — D25 redaction holds over the REAL shipped spans
# ==========================================================================


class TestSpanDumpIsPIIClean:
    def _drive_turn_and_dump(self, monkeypatch, scope: frozenset[str]) -> dict[str, Any]:
        exporter = InMemorySpanExporter()
        app = _build_app(monkeypatch, span_exporter=exporter, scope=scope)
        client = TestClient(app)
        resp = client.post(
            "/turn",
            json={"message": "look up the national id"},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        # A TOOL span for runQuery must have been emitted (else the assertion is
        # vacuous by construction — nothing to leak).
        dump = client.get("/_test/spans").json()
        assert dump["count"] > 0, "no spans captured — cannot prove the invariant"
        assert any(
            s["name"] == "tool.runQuery" for s in dump["spans"]
        ), "the runQuery TOOL span is missing — nothing carried the SQL literal"
        return dump

    def test_sql_literal_and_result_cell_never_reach_a_span_attribute(
        self, monkeypatch
    ) -> None:
        # The turn ran a runQuery whose SQL embeds PII_SENTINEL as a string
        # literal AND whose result cell equals PII_SENTINEL. D25 requires: masked
        # SQL literals in the telemetry copy, and result values never on a span.
        # Assert the sentinel appears NOWHERE across every span's name/kind/attrs.
        dump = self._drive_turn_and_dump(monkeypatch, scope=frozenset())
        hits = _scan_dump_for(dump, PII_SENTINEL)
        assert hits == [], f"D25 LEAK: PII sentinel reached a span → {hits}"

    def test_raw_scope_never_reaches_a_span_only_the_hash(self, monkeypatch) -> None:
        # The AGENT span labels the turn with `hash_scope(column_scope)` — the RAW
        # scope member must never appear; its stable hash may.
        scope = frozenset({SCOPE_MEMBER})
        dump = self._drive_turn_and_dump(monkeypatch, scope=scope)
        raw_hits = _scan_dump_for(dump, SCOPE_MEMBER)
        assert raw_hits == [], f"D25 LEAK: raw scope member reached a span → {raw_hits}"
        # ...and the hash IS present (proving the span carried the scope label at
        # all — otherwise the negative above would be trivially satisfied).
        from data_agent.runtime.observability.redaction import hash_scope

        assert _scan_dump_for(dump, hash_scope(scope)), (
            "expected the scope HASH on the AGENT span; found neither raw nor hash"
        )


# ==========================================================================
# 3. Non-vacuity meta-test — the scan CAN catch a planted leak
# ==========================================================================


class TestPIIScanIsNonVacuous:
    def test_scan_catches_a_sentinel_planted_in_a_real_span_attribute(self) -> None:
        # If the scan could never fail, the clean-dump assertions above would be
        # worthless. Emit a REAL span whose attribute deliberately carries the
        # sentinel (bypassing redaction by handing raw args to tool_span), dump it
        # in the endpoint's exact shape, and assert the SAME scan flags it.
        exporter = InMemorySpanExporter()
        provider = TracerProvider(resource=Resource.create({"service.name": "qa"}))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = tracing.get_tracer(provider)
        with tracing.tool_span(
            tracer,
            tool_name="runQuery",
            args={"sql": f"SELECT * FROM t WHERE ssn = '{PII_SENTINEL}'"},  # UN-redacted
            status="ok",
            error_code=None,
        ):
            pass
        dump = _dump_shape(exporter.get_finished_spans())
        hits = _scan_dump_for(dump, PII_SENTINEL)
        assert hits, "scan is VACUOUS — it failed to catch a planted PII leak"

    def test_scan_is_clean_on_a_span_with_no_sentinel(self) -> None:
        # The other half: a benign span is NOT flagged (no false positive that
        # would make the clean-dump assertions pass for the wrong reason).
        exporter = InMemorySpanExporter()
        provider = TracerProvider(resource=Resource.create({"service.name": "qa"}))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = tracing.get_tracer(provider)
        with tracing.agent_span(tracer, scope_hash="deadbeef", turn_index=0):
            pass
        dump = _dump_shape(exporter.get_finished_spans())
        assert _scan_dump_for(dump, PII_SENTINEL) == []


# ==========================================================================
# 4. configure_tracing default path — byte-identical when exporter is None
# ==========================================================================


def _processor_count(provider: TracerProvider) -> int:
    # OTel SDK internal: the SynchronousMultiSpanProcessor holding attached
    # processors. Introspected (not a public API) purely to assert the seam adds
    # NOTHING on the default path.
    return len(provider._active_span_processor._span_processors)


class TestConfigureTracingDefaultPath:
    def test_no_endpoint_and_no_exporter_attaches_zero_processors(self) -> None:
        provider = tracing.configure_tracing(
            otlp_endpoint="", service_name="data-agent-runtime", span_exporter=None
        )
        assert _processor_count(provider) == 0

    def test_omitting_span_exporter_kwarg_matches_explicit_none(self) -> None:
        # The production call path passes span_exporter=None; the byte-identical
        # claim also covers callers that omit the kwarg entirely (its default).
        provider = tracing.configure_tracing(
            otlp_endpoint="", service_name="data-agent-runtime"
        )
        assert _processor_count(provider) == 0

    def test_injected_exporter_attaches_exactly_one_processor(self) -> None:
        provider = tracing.configure_tracing(
            otlp_endpoint="",
            service_name="data-agent-runtime",
            span_exporter=InMemorySpanExporter(),
        )
        assert _processor_count(provider) == 1

    def test_injected_exporter_receives_post_redaction_attributes(self) -> None:
        # What actually ships: spans reach the exporter carrying the attributes as
        # the CALLER set them (redaction is the caller's job). Here we confirm the
        # exporter sees a masked-literal SQL, not the raw one — i.e. the exported
        # copy is the post-redaction copy.
        from data_agent.runtime.observability.redaction import mask_sql

        exporter = InMemorySpanExporter()
        provider = tracing.configure_tracing(
            otlp_endpoint="", service_name="data-agent-runtime", span_exporter=exporter
        )
        tracer = tracing.get_tracer(provider)
        raw = f"SELECT * FROM t WHERE ssn = '{PII_SENTINEL}'"
        with tracing.tool_span(
            tracer,
            tool_name="runQuery",
            args={"sql": mask_sql(raw)},  # caller redacts before handing to the span
            status="ok",
            error_code=None,
        ):
            pass
        (span,) = exporter.get_finished_spans()
        exported_sql = span.attributes["tool.args.sql"]
        assert PII_SENTINEL not in exported_sql
        assert exported_sql == mask_sql(raw)


# ==========================================================================
# 5. D44 replay-filter drop correctness (non-e2e, real filter_trail path)
# ==========================================================================


def _entry(turn_index: int, provenance: frozenset[tuple[str, str]] | None) -> TrailEntry:
    return TrailEntry(
        turn_index=turn_index,
        tool_call_id=f"c{turn_index}",
        tool_name="runQuery",
        args={},
        status="ok",
        error_code=None,
        provenance=provenance,
        result_preview=None,
        result_full_ref=None,
        ts="2026-07-02T00:00:00Z",
    )


class TestD44ReplayDropCorrectness:
    def test_narrowed_scope_drops_the_out_of_scope_entry_keeps_the_in_scope_one(
        self,
    ) -> None:
        # The exact D44 mechanic the Slice-2 scope-narrow scenario relies on:
        # after narrowing to ONLY `demo.payroll.department`, a prior entry whose
        # provenance is `demo.payroll.salary` (now out of scope) drops from replay,
        # while an in-scope `demo.payroll.department` entry survives. Driven through
        # the REAL filter_trail (the same function ContextAssembler applies).
        salary_entry = _entry(0, frozenset({("demo.payroll", "salary")}))
        dept_entry = _entry(0, frozenset({("demo.payroll", "department")}))
        narrowed = frozenset({"demo.payroll.department"})

        kept = filter_trail([salary_entry, dept_entry], narrowed)

        assert dept_entry in kept
        assert salary_entry not in kept

    def test_allow_all_baseline_keeps_the_determined_entry(self) -> None:
        # Turn-1 baseline (allow-all): the determined salary entry is replayable —
        # the positive control the scenario narrows AWAY from.
        salary_entry = _entry(0, frozenset({("demo.payroll", "salary")}))
        assert filter_trail([salary_entry], frozenset()) == [salary_entry]

    def test_undetermined_provenance_dropped_even_under_allow_all(self) -> None:
        # Fail-closed floor: an entry with provenance=None is dropped even under
        # allow-all — "never assume in-scope" (D44). Guards against a scenario
        # accidentally leaning on an undetermined entry surviving.
        undetermined = _entry(0, None)
        assert filter_trail([undetermined], frozenset()) == []
