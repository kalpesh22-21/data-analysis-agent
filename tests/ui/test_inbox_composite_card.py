"""What a COMPOSITE candidate's review card renders, driven in a real DOM.

`tests/ui/test_inbox_card_render.py` covers the single-template card. A composite is the shape
that card could not show at all: its `generalization.sql_template` is `None` BY CONSTRUCTION,
so the template section rendered nothing and — worse — the trial block was gated on that same
field and was DELETED from the card entirely. Not a disabled button and not a sentence: a
reviewer looking at a multi-step blueprint saw no SQL and no way to run it.

So the facts pinned here are the ones a reviewer's decision rests on:

  * every step is on the card, in EXECUTION order, with its own SQL — a composite that shows
    four steps and runs five is unreviewable, and `order` is the only thing that says which is
    which;
  * the trial block EXISTS for a candidate with no top-level template, because "is there
    anything to run" and "is there a `sql_template`" are different questions;
  * the binding boxes come from the server's `template_parts` and nothing else, so the page
    never re-spells the `{slot}` grammar and never offers a box for a value the DAG computes
    for itself (the server drops those parts; the page must not re-add them);
  * `table_intermediate_unsupported` reads as a LIMIT OF THE TRIAL, not as a broken blueprint,
    because those need opposite reactions from the reviewer;
  * a damaged `node_templates` entry costs that entry, never the card. This is a rehydrated
    store doc rendered by a page with no build step and no error boundary — one thrown
    exception is a blank card.

The harness is `test_inbox_card_render.py`'s: a loopback `http.server` serving the page and
answering its bootstrap GETs, plus the one POST the trial button makes. No stack, no
subprocess — the seam under test is the renderer.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

_INBOX = pathlib.Path(__file__).resolve().parents[2] / "ui" / "static" / "inbox.html"

# What the stub is currently serving, and what the last POST carried. Module-level because
# `HTTPServer` constructs a fresh handler per request.
_SERVED: dict[str, Any] = {"items": [], "trial": None, "last_post": ""}

_RENDER_TIMEOUT_MS = 5_000

PAYROLL = "payroll.payroll_fact"

# The 3-step shape: two producers and the consumer that bands between them. Deliberately more
# than two steps — a 2-node card cannot tell "sorted by order" apart from "reversed".
NODE_0 = (
    f"SELECT sum(gross_pay) AS total FROM {PAYROLL} "
    "WHERE department_code = {department_code}"
)
NODE_1 = (
    f"SELECT max(gross_pay) AS cap FROM {PAYROLL} "
    "WHERE department_code = {department_code}"
)
NODE_2 = (
    f"SELECT employee_id FROM {PAYROLL} WHERE department_code = {{department_code}} "
    "AND gross_pay > {total} AND gross_pay < {cap}"
)

# `template_parts` EXACTLY as `inbox/models.py::_composite_parts` sends them: the node
# templates concatenated in order, one chip per human-supplied slot (de-duplicated across
# steps), and every consume placeholder demoted to TEXT. The page is only allowed to read
# these — it must not tokenize the SQL itself.
_PARTS: list[dict[str, str]] = [
    {"text": "SELECT sum(gross_pay) AS total FROM payroll.payroll_fact WHERE department_code = "},
    {"slot": "department_code"},
    {"text": "\n\n"},
    {"text": "SELECT max(gross_pay) AS cap FROM payroll.payroll_fact WHERE department_code = "},
    {"text": "{department_code}"},
    {"text": "\n\n"},
    {"text": "SELECT employee_id FROM payroll.payroll_fact WHERE department_code = "},
    {"text": "{department_code}"},
    {"text": " AND gross_pay > "},
    {"text": "{total}"},
    {"text": " AND gross_pay < "},
    {"text": "{cap}"},
]


class _StubHandler(BaseHTTPRequestHandler):
    """The page, its two bootstrap GETs, and the trial POST."""

    def log_message(self, *args: Any) -> None:  # keep pytest output clean
        return

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length") or 0)
        _SERVED["last_post"] = self.rfile.read(length).decode() if length else ""
        status, payload = _SERVED.get("trial") or (200, {})
        self._send(json.dumps(payload).encode(), "application/json", status=status)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
        if self.path.startswith("/api/inbox/health"):
            self._send(b'{"write_plane":"full"}', "application/json")
        elif self.path.startswith("/api/inbox"):
            items = _SERVED["items"]
            body = json.dumps({"count": len(items), "items": items}).encode()
            self._send(body, "application/json")
        else:
            self._send(_INBOX.read_bytes(), "text/html; charset=utf-8")

    def _send(self, body: bytes, ctype: str, *, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    httpd = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/inbox"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    """A chromium instance, or a clean SKIP where one is not installed.

    Same posture as the sibling module: a missing browser binary is an environment fact, and a
    suite that goes red on it teaches people to ignore red.
    """
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        with playwright.sync_playwright() as pw:
            try:
                instance = pw.chromium.launch()
            except Exception as exc:  # pragma: no cover — environment-dependent
                pytest.skip(f"chromium is not launchable here: {exc}")
            try:
                yield instance
            finally:
                instance.close()
    except NotImplementedError as exc:  # pragma: no cover — no browser support at all
        pytest.skip(f"playwright cannot run here: {exc}")


@pytest.fixture
def render(browser: Any, server: str) -> Iterator[Any]:
    """`render(items)` → a loaded page showing exactly those items.

    Any uncaught page error FAILS the test rather than surfacing as a selector timeout: half
    these cases feed the renderer damaged input on purpose, and "element not found" is a much
    worse description of a thrown exception than the exception.
    """
    pages: list[Any] = []

    def _render(items: list[dict[str, Any]]) -> Any:
        _SERVED["items"] = items
        page = browser.new_page()
        pages.append(page)
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(server)
        try:
            page.wait_for_selector(
                '[data-testid="inbox-item"]', state="attached", timeout=_RENDER_TIMEOUT_MS
            )
        except Exception:
            if errors:
                raise AssertionError(f"the page threw while rendering: {errors}") from None
            raise
        assert not errors, f"the page threw while rendering: {errors}"
        return page

    try:
        yield _render
    finally:
        for page in pages:
            page.close()


@pytest.fixture(autouse=True)
def _reset_trial() -> Iterator[None]:
    """No trial response and no recorded POST leak between tests."""
    _SERVED["trial"] = None
    _SERVED["last_post"] = ""
    yield


def _item(**overrides: Any) -> dict[str, Any]:
    """One wire item, defaulted to the shape the service actually sends."""
    base: dict[str, Any] = {
        "candidate_id": "candidate::hash::0",
        "type": "blueprint",
        "status": "in_review",
        "reason": "blueprint_sampled",
        "summary": "employees paid between their department's total and cap",
        "payload_view": {},
        "evidence_refs": ["evidence::s::e1"],
        "entity_scan": {"result": "pass", "hits": [], "scanner": "regex"},
        "dedup": None,
        "created_at": "2026-08-28T00:00:00Z",
        "verified": False,
        "route_reason": None,
        "score": {
            "score": 0.0,
            "novelty": 0.0,
            "groundedness": 0.0,
            "session_quality": 0.0,
            "novelty_measured": False,
            "quality_measured": False,
            "groundedness_measured": False,
        },
        "decline": None,
        "template_parts": [],
        "param_judge": None,
        "leakage_attestation": None,
    }
    base.update(overrides)
    return base


def _composite_payload(nodes: list[Any] | None = None) -> dict[str, Any]:
    """A composite `payload_view` — note `sql_template: null`, which is not optional.

    Stored DELIBERATELY OUT OF ORDER (2, 0, 1) so "renders in execution order" is a claim about
    the sort and not about the document.
    """
    return {
        "intent": "employees paid between their department's total and cap",
        "kind": "composite",
        "generalization": {
            "sql_template": None,
            "uses": [
                f"{PAYROLL}.department_code",
                f"{PAYROLL}.employee_id",
                f"{PAYROLL}.gross_pay",
            ],
            "uses_rules": [],
            "node_templates": nodes
            if nodes is not None
            else [
                {"order": 2, "sql_template": NODE_2},
                {"order": 0, "sql_template": NODE_0},
                {"order": 1, "sql_template": NODE_1},
            ],
            "static_validation": {
                "explain_ok": True,
                "binds_to_subset_uses": True,
                "dag_ok": True,
                "read_only_select": True,
                "outcome": "ok",
                "reason": None,
            },
        },
        "composes": [
            {"order": 0, "node_kind": "query", "feeds_from": [],
             "consumes": {}, "output": {"total": "scalar"}},
            {"order": 1, "node_kind": "query", "feeds_from": [],
             "consumes": {}, "output": {"cap": "scalar"}},
            {"order": 2, "node_kind": "query", "feeds_from": [0, 1],
             "consumes": {"total": "$0.total", "cap": "$1.cap"},
             "output": {"rows": "scalar"}},
        ],
    }


def _composite_item(**overrides: Any) -> dict[str, Any]:
    return _item(payload_view=_composite_payload(), template_parts=_PARTS, **overrides)


def _next_post(timeout: float = 5.0) -> dict[str, Any]:
    """The next POST body the stub receives, decoded. Polled, so the assertion is about the
    body that was sent rather than about how fast the DOM settled."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = _SERVED["last_post"]
        if body:
            return json.loads(body)
        time.sleep(0.02)
    raise AssertionError("no POST arrived within the timeout")


# --- the steps ---------------------------------------------------------------


def test_a_composite_renders_one_block_per_node_in_execution_order(render: Any) -> None:
    """⚠ THE REPRO. A composite has no top-level query, so before this the template section was
    empty and the reviewer was asked to approve a blueprint they could not read.

    ONE BLOCK PER NODE, sorted by `order`, each carrying its own SQL VERBATIM. The order is the
    load-bearing half: step 2 reads `{total}` and `{cap}`, and which step produces which is the
    whole question a reviewer has about a DAG. The document stores them 2, 0, 1.
    """
    page = render([_composite_item()])

    blocks = page.locator('[data-testid="inbox-bp-node"]')
    assert blocks.count() == 3, "one block per node — not one section for the whole DAG"
    templates = page.locator('[data-testid="inbox-bp-node-template"]')
    assert [templates.nth(i).text_content() for i in range(3)] == [NODE_0, NODE_1, NODE_2]

    # Each block says which step it is, in execution order, using the stored `order` rather
    # than its position in the document.
    steps = page.locator(".card-node-step")
    assert [steps.nth(i).text_content() for i in range(3)] == ["step 0", "step 1", "step 2"]

    # The single-template `<pre>` is NOT also rendered: the nodes ARE the template, and a
    # second empty block would read as a fourth query.
    assert page.locator('[data-testid="inbox-bp-template"]').count() == 0


def test_the_card_says_which_step_feeds_which(render: Any) -> None:
    """A DAG a reviewer cannot trace is a list of unrelated queries.

    Step 2 reads `{total}` and `{cap}`; the card has to say that step 0 produces one and step 1
    the other. ⚠ THE FIELD THE CARD READS IS THE WHOLE DEFECT THIS PINS: a node template is
    `{order, sql_template}` and nothing else (`NodeTemplate.to_doc`), so wiring read off the
    NODE found it on a hand-written fixture and never on a real candidate. It lives on
    `payload_view.composes`, joined by `order` — the same join `ReviewInbox._trial_nodes` and
    `inbox/models.py::_composite_parts` already do.

    Asserted on the CONSUMER's block specifically, not on the card as a whole: `composes` is in
    the raw dump at the bottom of every card, so a page-wide search for `$0.total` would pass
    with the typed section still empty.
    """
    page = render([_composite_item()])

    assert "$0.total" in (page.locator('[data-testid="inbox-payload"]').text_content() or ""), (
        "the wiring reached the browser; the typed card must not ignore it"
    )
    consumer = page.locator('[data-testid="inbox-bp-node"]').nth(2).text_content() or ""
    assert "$0.total" in consumer or "step 0" in consumer.replace("step 2", ""), consumer
    # BOTH edges, and the producers' side too — a card that named one input and dropped the
    # other would read as a complete account of the step while hiding half of it.
    # The raw `$N.col` grammar moved off the step block and onto the input pill, which spells
    # the same reference out as `cap ← step 1.cap`: a deliberate change of wording, not a lost
    # edge. What is pinned is the FACT — both value names, both producers.
    assert "total ← step 0" in consumer, consumer
    assert "cap ← step 1" in consumer, consumer
    producers = page.locator('[data-testid="inbox-bp-node"]')
    assert "total" in (producers.nth(0).text_content() or ""), "step 0 says what it produces"
    assert "cap" in (producers.nth(1).text_content() or ""), "step 1 says what it produces"


def test_the_trial_block_renders_with_no_top_level_template(render: Any) -> None:
    """⚠ THE ONE THAT WAS SILENTLY ABSENT. The gate used to be `gen.sql_template`, which is
    `null` for every composite ever built — so the whole trial block was omitted. Not disabled,
    not explained: gone.

    It is gated on "is there anything to run" instead, so node templates are enough. The
    composite caveat is stated BEFORE the round trip, because a reviewer who reads
    "a table intermediate cannot be trialled yet" first will not read the refusal that follows
    as the blueprint being broken.
    """
    page = render([_composite_item()])

    assert page.locator('[data-testid="inbox-trial"]').count() == 1
    assert page.locator('[data-testid="inbox-trial-run"]').count() == 1
    assert page.locator('[data-testid="inbox-trial-token"]').count() == 1
    note = page.locator('[data-testid="inbox-trial-composite-note"]').text_content() or ""
    assert "composite" in note and "TABLE" in note, note


def test_the_binding_boxes_come_from_template_parts_only(render: Any) -> None:
    """⚠ ONE BOX PER SLOT THE TRIAL WILL ACTUALLY USE, and the page is not allowed to work out
    which those are — the server already did, by subtracting each node's `consumes`.

    `department_code` appears in all three steps and gets ONE box (one typed value binds every
    occurrence). `{total}` and `{cap}` appear as text and get NONE: they are computed by steps 0
    and 1, and a box for either would ask a reviewer to hand-type an intermediate — which the
    trial would then silently drop.

    Asserted through to the POST body, because a box the page renders but does not send (or
    sends under another name) fails in exactly the same place.
    """
    _SERVED["trial"] = (200, {"ok": True, "reason": "", "detail": "", "missing": [],
                              "columns": ["employee_id"], "row_count": 3,
                              "row_count_measured": False,
                              "distinct_grain_count": None, "verify_passed": True,
                              "verify_reason": None, "inconclusive": False})
    page = render([_composite_item()])

    inputs = page.locator('[data-testid="inbox-trial-binding"]')
    assert inputs.count() == 1, "one input per human-supplied slot, de-duplicated across steps"
    assert inputs.first.get_attribute("data-slot") == "department_code"

    inputs.first.fill("0420")
    page.locator('[data-testid="inbox-trial-token"]').fill("a-token-the-reviewer-holds")
    page.locator('[data-testid="inbox-trial-run"]').click()
    page.wait_for_selector('[data-testid="inbox-trial-result"]')

    body = _next_post()
    assert body["bindings"] == {"department_code": "0420"}, body["bindings"]
    assert "total" not in body["bindings"] and "cap" not in body["bindings"]
    assert body["token"] == "a-token-the-reviewer-holds"
    # The borrowed credential does not outlive the request that borrowed it.
    assert page.locator('[data-testid="inbox-trial-token"]').input_value() == ""
    assert "row count: not measured" in (
        page.locator('[data-testid="inbox-trial-result"]').text_content() or ""
    )


def test_the_table_intermediate_refusal_reads_as_a_limit_not_a_failure(render: Any) -> None:
    """⚠ THE REASON STRING IS A CONTRACT. The server answers
    `table_intermediate_unsupported` for a step that hands a whole result downstream — the probe
    has no scratch side-channel — and an unmapped reason would surface as that raw token beside
    a red ✗, which reads as "this blueprint is broken".

    It is not. The blueprint may be perfectly good and is still reviewable on its SQL, which the
    card now shows step by step. The sentence has to say so, and has to point at where.
    """
    _SERVED["trial"] = (200, {"ok": False, "reason": "table_intermediate_unsupported",
                              "detail": "step 0 passes a whole table downstream", "missing": [],
                              "columns": [], "row_count": 0, "distinct_grain_count": None,
                              "verify_passed": False, "verify_reason": None,
                              "inconclusive": False})
    page = render([_composite_item()])
    page.locator('[data-testid="inbox-trial-token"]').fill("a-token")
    page.locator('[data-testid="inbox-trial-run"]').click()
    page.wait_for_selector('[data-testid="inbox-trial-result"]')

    text = page.locator('[data-testid="inbox-trial-result"]').text_content() or ""
    assert "table_intermediate_unsupported" not in text, "the raw reason token is not the message"
    assert "table intermediate" in text
    assert "step by step" in text, "it points the reviewer at the per-node SQL above"


def test_a_damaged_node_entry_costs_that_entry_and_not_the_card(render: Any) -> None:
    """The `payload_view` is a rehydrated store doc rendered by a page with no build step and no
    error boundary, so a `null` or a string where a node was must not throw: one exception here
    is a BLANK CARD, and a reviewer cannot tell that apart from an empty queue.

    Non-objects are dropped. A node that IS an object but carries no SQL is kept and SAID —
    "no SQL on this step" — rather than silently omitted, because a step that vanishes makes a
    3-step blueprint look like a 2-step one, and the reviewer would approve the wrong shape.

    ⚠ NOTE THE ASYMMETRY, pinned rather than endorsed: `ReviewInbox._trial_nodes` refuses this
    whole candidate (`malformed_composite`), while the card renders around the damage. The two
    are consistent only in that neither pretends the step ran.
    """
    page = render(
        [
            _item(
                payload_view=_composite_payload(
                    [
                        {"order": 1, "sql_template": NODE_1},
                        None,
                        "not a node at all",
                        {"order": 0, "sql_template": NODE_0},
                        {"order": 2},
                        42,
                    ]
                ),
                template_parts=_PARTS,
            )
        ]
    )

    blocks = page.locator('[data-testid="inbox-bp-node"]')
    assert blocks.count() == 3, "the three object entries survive; the rest are dropped"
    templates = page.locator('[data-testid="inbox-bp-node-template"]')
    assert [templates.nth(i).text_content() for i in range(2)] == [NODE_0, NODE_1]
    assert "no SQL on this step" in (blocks.nth(2).text_content() or "")
    # The rest of the card is intact — the raw dump every card carries is still there, so
    # nothing the renderer did not understand was lost.
    assert page.locator('[data-testid="inbox-payload"]').count() >= 1


def test_a_composite_card_never_builds_markup_out_of_a_server_string(render: Any) -> None:
    """The node SQL is the first place this page prints a server string per step. A `<script>`
    in a node template has to arrive as TEXT — the same `textContent`-only posture the rest of
    the card keeps, restated here because a new sink is exactly where it gets broken."""
    hostile = "SELECT 1 AS x -- <img src=x onerror='alert(1)'><script>alert(2)</script>"
    page = render(
        [
            _item(
                payload_view=_composite_payload([{"order": 0, "sql_template": hostile}]),
                template_parts=[{"text": hostile}],
            )
        ]
    )

    node = page.locator('[data-testid="inbox-bp-node-template"]')
    assert node.text_content() == hostile
    assert node.locator("script, img").count() == 0
