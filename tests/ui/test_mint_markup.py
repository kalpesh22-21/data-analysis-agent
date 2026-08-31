"""Static guarantees about the blueprint-authoring page.

The sink test is the load-bearing one and it is the same argument `test_inbox_markup.py` makes:
this page renders the DRAFTED SQL and the validator's decline detail, both of which carry
literals lifted from a real query and are shown verbatim by design. An HTML sink anywhere in the
file turns an authoring surface into an injection surface.

The rest pin the two claims the page makes to the person using it — that nothing is published,
and that a query they say RAN is used verbatim — since a page that quietly stopped being true
about either would mislead an expert into approving something they did not author.
"""

from __future__ import annotations

from pathlib import Path

_HTML = (Path(__file__).parents[2] / "ui" / "static" / "mint.html").read_text(
    encoding="utf-8"
)


def test_the_page_has_no_html_sink() -> None:
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        occurrences = [
            line
            for line in _HTML.splitlines()
            if sink in line and not line.strip().startswith("//")
        ]
        assert occurrences == [], f"{sink} appears in mint.html: {occurrences}"


def test_the_page_posts_to_the_mint_route_as_a_json_body() -> None:
    """The BFF forwards the body VERBATIM, so the shape this page sends is the shape the
    validator sees. A page that sent a query string would have its submission rejected for
    fields it never delivered."""
    assert '"/api/inbox/mint"' in _HTML
    assert "JSON.stringify(payload)" in _HTML
    for field in ("question", "tables", "steps", "assumptions", "sql", "sql_mode"):
        assert f"{field}:" in _HTML, f"the payload omits {field}"


def test_the_page_reads_its_table_list_from_the_schema_route() -> None:
    """Grounding is not optional: the tables offered must be the ones the minter can actually
    supply columns for, which is why the page asks rather than hard-coding a list."""
    assert '"/api/inbox/mint/schema"' in _HTML


def test_the_page_says_that_nothing_is_published() -> None:
    """The single most important thing an expert must not misunderstand. Minting FILES a
    candidate; a human still approves it. A page that read as "publish" would collect
    approvals nobody meant to give."""
    assert "does <strong>not</strong> publish anything" in _HTML
    assert "Nothing is published yet" in _HTML


def test_the_three_sql_modes_are_offered_and_named_by_what_they_promise() -> None:
    """`exact` is a PROMISE that the query ran, and it buys a real guarantee — the assistant is
    given no field to rewrite it in. The option's label has to say that, because an expert who
    picks it casually gets a blueprint built on a query nobody verified."""
    for mode in ('value="none"', 'value="pseudo"', 'value="exact"'):
        assert mode in _HTML
    assert "A query that ran — use it verbatim" in _HTML


def test_a_deployment_without_a_minting_plane_disables_the_submit() -> None:
    """The schema route answers `available: false` rather than erroring, so the page must
    actually act on it — otherwise the form renders normally and every submission 503s."""
    assert "if (!schema.available)" in _HTML
    assert 'document.getElementById("submit").disabled = true' in _HTML


def test_the_page_lets_the_expert_declare_the_dag_rather_than_infer_it() -> None:
    """The structure is the expert's. A model asked to decompose prose into a DAG invents the
    dependency edges, and `check_dag` proves a graph is well-formed — never that it is the one
    that was meant. So the page collects steps, output names and backward edges explicitly."""
    assert 'id="shape"' in _HTML
    assert 'value="composite"' in _HTML
    for field in ("step_intent:", "output_name:", "feeds_from:"):
        assert field in _HTML, f"the node payload omits {field}"


def test_a_step_can_only_depend_on_earlier_steps() -> None:
    """Backward edges only, so the graph is acyclic by construction. The page renders a
    checkbox per PRIOR step and none for the first."""
    assert "the first step cannot depend on anything" in _HTML
    assert '"node-dep"' in _HTML


def test_both_output_kinds_are_offered_and_neither_is_disabled() -> None:
    """`NODE_OUTPUT_KINDS` has two members and both are live. `table` was disabled while S4
    could not validate one; it is enabled now that a node's declared table consumes are exempt
    from the session-ownership check. A table is how MANY values flow to the next step."""
    assert "passes on one value" in _HTML
    assert "passes on a table" in _HTML
    assert "not available yet" not in _HTML
    assert "opt.disabled" not in _HTML
    assert "output_kind:" in _HTML


def test_the_whole_query_box_and_the_step_boxes_are_never_both_offered() -> None:
    """Server-side they are mutually exclusive — sending both is a 400. Showing both invited
    that error only AFTER every step had been filled in."""
    assert 'id="sql-whole"' in _HTML
    assert 'getElementById("sql-whole").hidden = composite' in _HTML


def test_the_step_sql_placeholder_tells_the_truth_in_exact_mode() -> None:
    """"leave blank to have it written for you" is false when the expert has said these queries
    RAN — the server refuses a blank step. The two controls sit in different sections, so
    without this their combination was only adjudicated by a 400."""
    assert "required — you said these queries ran" in _HTML
    assert "leave blank to have it written for you" in _HTML


def test_an_emptied_step_editor_cannot_silently_mint_a_single_blueprint() -> None:
    """An empty `nodes` array is indistinguishable from "not a composite", so removing every
    step row minted a ONE-QUERY blueprint while the selector still said "several steps"."""
    assert "You chose several steps but there are none" in _HTML


def test_the_page_warns_about_prior_art_without_blocking() -> None:
    """A near-duplicate intent with genuinely different SQL is real — the same question at
    another grain. The page shows what exists and still lets the expert draft."""
    assert '"/api/inbox/mint/prior_art"' in _HTML
    assert "A warning, not a block" in _HTML
    assert "already in canon" in _HTML


def test_the_duplicate_check_runs_before_the_draft_is_paid_for() -> None:
    """Bound to the question field losing focus, so the expert learns a blueprint exists
    without first buying a drafting turn."""
    assert 'getElementById("question").addEventListener("blur", checkPriorArt)' in _HTML


def test_the_bff_actually_registers_every_minting_route() -> None:
    """⚠ REGRESSION GUARD, and it earned its place: these four routes were silently DELETED by
    an unrelated edit that removed a block of `ui/server.py` by slicing between two anchors —
    the minting routes happened to sit between them. Nothing failed. The markup tests only read
    `mint.html`, the service tests exercise the inbox app directly, and no test asserted the
    BFF's own routing table, so a page that 404s shipped green.

    Asserted on the live app object rather than on the source text: what matters is what
    FastAPI resolves, not whether a string appears in a file.
    """
    import os

    os.environ.setdefault("REVIEW_INBOX_ENABLED", "1")
    os.environ.setdefault("REVIEWER_TOKEN", "t")
    from ui import server

    routes = {(r.path, tuple(sorted(getattr(r, "methods", None) or ()))) for r in server.app.routes}
    assert ("/mint", ("GET",)) in routes
    assert ("/api/inbox/mint/schema", ("GET",)) in routes
    assert ("/api/inbox/mint", ("POST",)) in routes
    assert ("/api/inbox/mint/prior_art", ("POST",)) in routes


def test_the_minting_hop_gets_the_model_timeout_not_the_crud_one() -> None:
    """A drafting turn writes a whole query. With the 10s CRUD budget the BFF abandons the hop
    mid-draft and reports the service unreachable — the exact failure `revise` already hit."""
    from ui import server

    assert server._hop_timeout("/inbox/mint").read == server._INBOX_MODEL_HOP_TIMEOUT_SECONDS
    assert server._hop_timeout("/inbox/mint/schema").read == server._INBOX_HOP_TIMEOUT_SECONDS
