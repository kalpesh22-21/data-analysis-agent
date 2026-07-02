"""Layer-3 spec-conformance suite — Playwright driving the REAL minimal UI
(`ui/static/index.html`) through the REAL BFF (`ui/server.py`) through the
REAL agent runtime (`data_agent.runtime.app.create_app`), deterministic via
the scripted `DemoModelClient`/`DemoMCPClient` doubles in
`scripts/run_ui_runtime.py` (NO OpenAI key, NO live ClickHouse). Only the
model provider and the ClickHouse MCP are faked — JWT verification against
the real `l2-token` container's JWKS endpoint is NOT bypassed (see
`scripts/run_ui_runtime.py`'s module docstring).

Module skip-guard: the whole module is skipped unless `RUN_E2E` is set in
the environment, so `uv run pytest` (no env var) stays green with zero
browser/subprocess infra touched — see the top-level `pytestmark` below.

Each test asserts STRUCTURAL, observable behavior via `data-testid` locators
(progress items appearing, ask-user chips rendering, the answer becoming
non-empty, no error banner) — never exact LLM prose, since the scripted
doubles' wording is demo copy, not a contract.

Three Phase-0 conformance scenarios are intentionally NOT covered here — see
`README.md` in this directory for what and why.
"""

from __future__ import annotations

import json
import os
import re

import httpx
import pytest

# Skip the whole module cleanly at COLLECTION if playwright isn't installed
# (Layer-3 runs nightly/release, not per-PR — so per-PR CI needn't install the
# playwright wheel; importorskip avoids an ImportError before the RUN_E2E guard).
_playwright = pytest.importorskip("playwright.sync_api")
Page = _playwright.Page
expect = _playwright.expect

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_E2E"),
    reason="Layer-3 E2E suite requires RUN_E2E=1 (spins up subprocesses + a browser; "
    "needs the l2-token container up for JWT minting). Skipped by default.",
)

_BFF_URL = "http://localhost:3000"
_RUNTIME_URL = "http://localhost:8000"
_ASSERT_TIMEOUT_MS = 15_000

# The result-cell sentinel the demo's payroll query returns (scripts/
# run_ui_runtime.py::_D44_PAYROLL_SENTINEL). Used by BOTH the D44 scope-narrowing
# scenario (asserts it drops from replay) and the D25 span scenario (asserts it
# never reaches a span attribute).
_PAYROLL_SENTINEL = "PAYROLL_FIGURE_XYZZY_770077"


@pytest.fixture(autouse=True)
def _stack(running_stack: None) -> None:
    """Every test in this module depends on the running two-process stack
    (session-scoped — started once for the whole module, see conftest.py)."""
    return None


def _goto_and_wait_for_session(page: Page) -> None:
    page.goto(_BFF_URL)
    # Bootstrap (`index.html`'s `fetch("/api/session", ...)`) must complete —
    # sending a message before it does surfaces a NO_SESSION error instead.
    expect(page.locator("#session-line")).not_to_have_text(
        "connecting…", timeout=_ASSERT_TIMEOUT_MS
    )


def _send_message(page: Page, message: str) -> None:
    page.get_by_test_id("message-input").fill(message)
    page.get_by_test_id("send-button").click()


class TestProgressStreaming:
    """Scenario 1 — normal/progress (D61): a "columns" message drives a
    getTableSchema tool call; progress events stream, then the answer
    renders."""

    def test_progress_items_appear_and_answer_renders(self, page: Page) -> None:
        _goto_and_wait_for_session(page)
        _send_message(page, "show me the columns")

        expect(page.get_by_test_id("progress-item").first).to_be_visible(
            timeout=_ASSERT_TIMEOUT_MS
        )
        progress_items = page.get_by_test_id("progress-item")
        expect(progress_items).not_to_have_count(0, timeout=_ASSERT_TIMEOUT_MS)

        answer = page.get_by_test_id("answer")
        expect(answer).not_to_have_text("", timeout=_ASSERT_TIMEOUT_MS)

        status = page.get_by_test_id("status")
        expect(status).to_contain_text("done", timeout=_ASSERT_TIMEOUT_MS)


class TestClarifyResume:
    """Scenario 2 — clarify -> resume: an "ask me" message drives askUser,
    the UI renders chip options, clicking one resumes the turn to a final
    answer."""

    def test_clarify_chips_then_resume_to_answer(self, page: Page) -> None:
        _goto_and_wait_for_session(page)
        _send_message(page, "ask me a question")

        expect(page.get_by_test_id("ask-user")).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("ask-user-question")).not_to_have_text(
            "", timeout=_ASSERT_TIMEOUT_MS
        )
        options = page.get_by_test_id("ask-user-option")
        expect(options).not_to_have_count(0, timeout=_ASSERT_TIMEOUT_MS)

        options.first.click()

        expect(page.get_by_test_id("ask-user")).to_be_hidden(timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("answer")).not_to_have_text("", timeout=_ASSERT_TIMEOUT_MS)


class TestScopeDenial:
    """Scenario 3 — scope denial (D57): a "salaries" message drives a
    runQuery -> COLUMN_SCOPE_VIOLATION denial; the UI shows a graceful
    denial, never raw data."""

    def test_denial_is_graceful_and_leaks_no_raw_data(self, page: Page) -> None:
        _goto_and_wait_for_session(page)
        _send_message(page, "show me salaries please")

        answer = page.get_by_test_id("answer")
        expect(answer).not_to_have_text("", timeout=_ASSERT_TIMEOUT_MS)
        expect(answer).to_contain_text(
            _any_of(["access", "scope", "can't", "cannot"]), timeout=_ASSERT_TIMEOUT_MS
        )

        # No crash surfaced as an error banner.
        expect(page.get_by_test_id("error-banner")).to_be_hidden()

        # No raw dollar-figure/row-shaped data anywhere in the rendered page.
        body_text = page.locator("body").inner_text()
        assert not _re_search(r"\$\s?\d", body_text), (
            f"Raw currency-shaped data leaked into the DOM: {body_text!r}"
        )
        assert "AnnualSalary" not in body_text


class TestParserFailClosed:
    """Scenario 4 — parser fail-closed (D63): a "raw sql" message drives a
    runQuery -> PARSE_FAILED_CLOSED denial; the UI shows a graceful
    rejection, never a crash."""

    def test_rejected_gracefully_not_crashed(self, page: Page) -> None:
        _goto_and_wait_for_session(page)
        _send_message(page, "run raw sql for me")

        answer = page.get_by_test_id("answer")
        expect(answer).not_to_have_text("", timeout=_ASSERT_TIMEOUT_MS)
        expect(answer).to_contain_text(
            _any_of(["couldn't validate", "could not validate", "rephrase", "reject"]),
            timeout=_ASSERT_TIMEOUT_MS,
        )
        expect(page.get_by_test_id("error-banner")).to_be_hidden()


class TestBudgetCapPause:
    """Scenario 5 — budget-cap pause (D47): a "keep going forever" message
    never lets the model finish; the loop's own BudgetGuard pauses with
    continue/refine/stop options; clicking "continue" grants a fresh window
    that ends with an answer."""

    def test_budget_cap_pause_then_continue(self, page: Page) -> None:
        _goto_and_wait_for_session(page)
        _send_message(page, "keep going forever")

        expect(page.get_by_test_id("ask-user")).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)
        continue_chip = page.locator('[data-testid="ask-user-option"][data-option="continue"]')
        refine_chip = page.locator('[data-testid="ask-user-option"][data-option="refine"]')
        stop_chip = page.locator('[data-testid="ask-user-option"][data-option="stop"]')
        expect(continue_chip).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)
        expect(refine_chip).to_be_visible()
        expect(stop_chip).to_be_visible()

        continue_chip.click()

        expect(page.get_by_test_id("ask-user")).to_be_hidden(timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("answer")).not_to_have_text("", timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("status")).to_contain_text("done", timeout=_ASSERT_TIMEOUT_MS)


class TestRunBlueprintFastPath:
    """Scenario 6 — runBlueprint fast path (D89): a "headcount by department"
    message drives a `runBlueprint` tool call; the executor runs the node query,
    the D56 grain probe passes, and a verified answer renders."""

    def test_fast_path_runs_and_renders_verified_answer(self, page: Page) -> None:
        _goto_and_wait_for_session(page)
        _send_message(page, "run the headcount by department blueprint")

        # runBlueprint emits tool_dispatch_start/ok → progress items.
        expect(page.get_by_test_id("progress-item").first).to_be_visible(
            timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("progress-item")).not_to_have_count(
            0, timeout=_ASSERT_TIMEOUT_MS
        )

        # Prove VERIFIED-ness, not just a non-empty answer: the scripted model
        # returns prose on ANY runBlueprint tool result (ok or VERIFY_FAILED), so
        # assert the progress stream shows the SUCCESSFUL dispatch — the
        # `tool_dispatch_ok` step ("step complete: runBlueprint") is emitted ONLY on
        # a completed runBlueprint; a VERIFY_FAILED path emits `tool_dispatch_error`
        # (no user-facing label) and so never renders this step.
        progress_list = page.get_by_test_id("progress-list")
        expect(progress_list).to_contain_text(
            "step complete: runBlueprint", timeout=_ASSERT_TIMEOUT_MS
        )

        expect(page.get_by_test_id("answer")).not_to_have_text("", timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("status")).to_contain_text("done", timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("error-banner")).to_be_hidden()


class TestRunBlueprintNoSilentVerification:
    """Scenario 7 — no-silent-verification (D56): a "bad headcount" message drives
    a `runBlueprint` whose result fails the grain-integrity gate. The fan-out
    result is WITHHELD; the model answers from the raw loop without ever
    presenting the unverified numbers, and no error banner appears."""

    def test_unverified_result_is_withheld_and_never_leaks(self, page: Page) -> None:
        _goto_and_wait_for_session(page)
        _send_message(page, "run the bad headcount blueprint")

        answer = page.get_by_test_id("answer")
        expect(answer).not_to_have_text("", timeout=_ASSERT_TIMEOUT_MS)

        # A verify-failed blueprint is a graceful raw-loop fallback, never a crash.
        expect(page.get_by_test_id("error-banner")).to_be_hidden()

        # The withheld fan-out row + figure (the sentinels the demo double emits
        # for the D56 negative fixture) must NEVER reach the DOM — the executor
        # discards them on VERIFY_FAILED.
        body_text = page.locator("body").inner_text()
        assert "FANOUT_LEAK_ROW" not in body_text, (
            f"Withheld fan-out row leaked into the DOM: {body_text!r}"
        )
        assert "987654" not in body_text, (
            f"Withheld fan-out figure leaked into the DOM: {body_text!r}"
        )


class TestRunBlueprintAskClarifyResume:
    """Scenario 8 — slot ask→clarify→resume (D49): an "average tenure" message
    drives a `runBlueprint` with an unfilled required slot; the executor pauses
    for the slot value. Answering it (via the free-text resume affordance)
    resumes the blueprint to a verified answer."""

    def test_slot_pause_then_resume_to_answer(self, page: Page) -> None:
        _goto_and_wait_for_session(page)
        _send_message(page, "show average tenure by department")

        expect(page.get_by_test_id("ask-user")).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("ask-user-question")).not_to_have_text(
            "", timeout=_ASSERT_TIMEOUT_MS
        )

        # A missing required slot pauses with no options → answer via the existing
        # free-text resume affordance (ask-user-input + ask-user-submit).
        page.get_by_test_id("ask-user-input").fill("Sales")
        page.get_by_test_id("ask-user-submit").click()

        expect(page.get_by_test_id("ask-user")).to_be_hidden(timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("answer")).not_to_have_text("", timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("error-banner")).to_be_hidden()


class TestRunBlueprintApprovalPauseResume:
    """Scenario 9 — approval pause/resume (D45/D59b): an "approve headcount"
    message drives a multi-node `runBlueprint` DAG whose approval gate pauses the
    turn showing approve/deny chips. Clicking approve re-enters the executor at
    the awaiting node and completes to a verified answer."""

    def test_approval_pause_then_approve_to_answer(self, page: Page) -> None:
        _goto_and_wait_for_session(page)
        _send_message(page, "approve headcount for sales")

        expect(page.get_by_test_id("ask-user")).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)
        approve_chip = page.locator('[data-testid="ask-user-option"][data-option="approve"]')
        deny_chip = page.locator('[data-testid="ask-user-option"][data-option="deny"]')
        expect(approve_chip).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)
        expect(deny_chip).to_be_visible()

        approve_chip.click()

        expect(page.get_by_test_id("ask-user")).to_be_hidden(timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("answer")).not_to_have_text("", timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("status")).to_contain_text("done", timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("error-banner")).to_be_hidden()


class TestScopeNarrowingDropsReplay:
    """Scenario 10 — mid-session scope narrowing (D44): turn 1 surfaces a
    payroll figure under an allow-all scope; the harness re-mints the session's
    JWT with a NARROWER scope (excluding the payroll column) via the BFF's
    `POST /api/session/scope`; a follow-up turn can no longer recall the figure
    because `ContextAssembler`/`scope_filter` fail-closed drops the now-out-of-
    scope prior entry from replay. D82/D5 intact — the browser never holds the
    JWT; only the BFF re-mints it server-side.

    Two methods, two SEPARATE browser sessions (function-scoped `page`), so the
    baseline's sentinel-bearing assistant message never contaminates the
    narrowed case's context:
      - baseline (wide scope throughout) — proves the figure IS recallable, i.e.
        the trail entry is determined, kept and rendered (guards against a
        false-pass where an undetermined/None provenance dropped it anyway).
      - narrowed — proves that narrowing the scope is what drops it from replay.
    """

    def test_baseline_recall_surfaces_figure_while_in_scope(self, page: Page) -> None:
        _goto_and_wait_for_session(page)

        # Turn 1 (wide scope): run the payroll lookup — provenance resolves to
        # demo.payroll.salary; the sentinel lands in the replayable trail.
        _send_message(page, "payroll lookup for sales")
        expect(page.get_by_test_id("answer")).not_to_have_text("", timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("status")).to_contain_text("done", timeout=_ASSERT_TIMEOUT_MS)

        # Turn 2 (still wide): the figure IS recallable while the payroll column is
        # in scope — the entry was determined, kept, and rendered into context.
        _send_message(page, "recall payroll figure")
        expect(page.get_by_test_id("answer")).to_contain_text(
            _PAYROLL_SENTINEL, timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("error-banner")).to_be_hidden()

    def test_narrowed_scope_drops_prior_result_from_replay(self, page: Page) -> None:
        _goto_and_wait_for_session(page)
        session_id = page.locator("#session-line").inner_text().strip()

        # Turn 1 (wide scope): the payroll lookup lands its sentinel in the trail.
        _send_message(page, "payroll lookup for sales")
        expect(page.get_by_test_id("answer")).not_to_have_text("", timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("status")).to_contain_text("done", timeout=_ASSERT_TIMEOUT_MS)

        # Narrow the session's scope to EXCLUDE the payroll salary column (the
        # BFF re-mints the JWT server-side; the browser never sees it).
        response = httpx.post(
            f"{_BFF_URL}/api/session/scope",
            json={"session_id": session_id, "column_scope": ["demo.payroll.department"]},
            timeout=10.0,
        )
        assert response.status_code == 200, response.text

        # Turn 2 (narrowed): the follow-up can no longer surface the figure — the
        # D44 replay filter dropped the out-of-scope prior entry from context.
        _send_message(page, "recall payroll figure")
        expect(page.get_by_test_id("answer")).to_contain_text(
            "no longer", timeout=_ASSERT_TIMEOUT_MS
        )
        expect(page.get_by_test_id("error-banner")).to_be_hidden()

        # The sentinel value must be ABSENT from the whole rendered page — proof
        # the narrowed scope dropped it from replay (D44 end-to-end).
        body_text = page.locator("body").inner_text()
        assert _PAYROLL_SENTINEL not in body_text, (
            f"Out-of-scope payroll figure leaked into the DOM after narrowing: {body_text!r}"
        )


class TestSpansEmittedPiiClean:
    """Scenario 11 — observability + PII (D25): drive one ordinary turn, then
    dump the emitted OTel spans via the runtime's test-only `GET /_test/spans`
    (an in-process InMemorySpanExporter, NOT a Phoenix container). Assert at
    least one AGENT and one TOOL span exist, and that NO span attribute value
    carries the JWT, a raw SQL literal, or a result cell value (D25)."""

    def test_spans_emitted_and_carry_no_pii(self, page: Page) -> None:
        _goto_and_wait_for_session(page)

        # Drive the payroll turn: it emits an AGENT span (the turn) + a runQuery
        # TOOL span whose args are SQL-literal-masked, and returns a distinctive
        # result cell sentinel — the strongest D25 probe (a result value that must
        # NEVER reach a span attribute).
        _send_message(page, "payroll lookup for sales")
        expect(page.get_by_test_id("answer")).not_to_have_text("", timeout=_ASSERT_TIMEOUT_MS)
        expect(page.get_by_test_id("status")).to_contain_text("done", timeout=_ASSERT_TIMEOUT_MS)

        response = httpx.get(f"{_RUNTIME_URL}/_test/spans", timeout=10.0)
        assert response.status_code == 200, response.text
        spans = response.json()["spans"]
        assert spans, "no spans were captured"

        kinds = {s["kind"] for s in spans}
        assert "AGENT" in kinds, f"no AGENT span emitted; kinds={kinds}"
        assert "TOOL" in kinds, f"no TOOL span emitted; kinds={kinds}"

        # POSITIVE control (keeps the mask assertion below non-vacuous): a runQuery
        # TOOL span DOES carry the SQL as a `tool.args.sql` attribute — but masked.
        # The in-memory exporter accumulates spans across the whole session, so
        # search for the payroll query specifically (not span[0]); if its SQL ever
        # stopped reaching spans, this fails instead of the "no raw literal" check
        # silently passing on an absent attribute.
        sql_values = [
            s["attributes"]["tool.args.sql"] for s in spans if "tool.args.sql" in s["attributes"]
        ]
        assert sql_values, "no span carried a tool.args.sql attribute (mask check would be vacuous)"
        payroll_sql = [v for v in sql_values if "demo.payroll" in v]
        assert payroll_sql, "the payroll runQuery SQL never reached a span"
        # Every SQL that reaches a span is literal-masked — no raw WHERE literal.
        for value in sql_values:
            assert "'Sales'" not in value, f"SQL literal not masked in span: {value!r}"

        blob = json.dumps(spans)
        # (b) PII-clean: no result cell value, no JWT, no raw SQL string literal.
        assert _PAYROLL_SENTINEL not in blob, (
            "a result cell value leaked into a span attribute (D25 violation)"
        )
        assert "eyJ" not in blob, "a JWT leaked into a span attribute (D5/D25 violation)"
        assert "Bearer " not in blob, "an Authorization header leaked into a span attribute"
        # The runQuery SQL is masked (redact_tool_args): the raw WHERE literal
        # 'Sales' must not survive into any span as a bound SQL value.
        assert "= 'Sales'" not in blob, "an unmasked SQL literal leaked into a span attribute"


def _any_of(substrings: list[str]) -> re.Pattern[str]:
    pattern = "|".join(re.escape(s) for s in substrings)
    return re.compile(pattern, re.IGNORECASE)


def _re_search(pattern: str, text: str) -> bool:
    return re.search(pattern, text) is not None
