"""Multi-table `answerWithTable` — the static invariants of `ui/static/index.html`
(08 §E).

`ui/static/index.html` is hand-written with no build step, so there is no unit
harness for its JS; the browser-level behaviour (independent paging closures,
lazy fetch, per-table badges) is exercised with Playwright in `tests/e2e`, which
is gated on `RUN_E2E=1` and needs a live runtime. What this module pins is the
narrow set of *structural* facts whose breakage is SILENT — the page still loads,
renders, and simply does less than it claims:

* the per-table `<template>` exists and the turn template no longer carries a
  singleton `.result-table` (a re-added one would be cloned AND rendered inline);
* the history adapter forwards `answer_tables`. This exact class of drift has
  already happened once — `sql` -> `sql_executed` and `result_table` ->
  `answer_sql` were renamed on the live path and the adapter kept passing the old
  names, so a RELOADED transcript silently lost its table while the live turn
  rendered fine (see the comment on `renderHistoryTurn`). Reload is the only place
  that regression shows, and nothing else here would catch it;
* the browser's table cap still equals the runtime's `MAX_INTENTS`. 08 §F ties
  them deliberately: a browser cap below the runtime's would drop tables the
  runtime designated, with no error anywhere.
"""

from __future__ import annotations

import pathlib
import re

from data_agent.runtime.composite.analysis_state import MAX_INTENTS
from data_agent.runtime.composite.answer_with_table import VERIFICATION_EMPTY_STATUS

_INDEX = pathlib.Path(__file__).resolve().parents[2] / "ui" / "static" / "index.html"
_HTML = _INDEX.read_text(encoding="utf-8")


def _template(template_id: str) -> str:
    """The markup inside `<template id="...">`, which is where the cloned
    subtrees live (templates do not nest, so a non-greedy match is exact)."""
    match = re.search(rf'<template id="{re.escape(template_id)}">(.*?)</template>', _HTML, re.S)
    assert match is not None, f"#{template_id} is missing from index.html"
    return match.group(1)


class TestAnswerTableTemplate:
    def test_the_answer_table_template_exists_and_is_a_result_table_panel(self) -> None:
        """`cloneTablePanel` clones this template and immediately queries
        `.result-table` out of the fragment — a renamed root class throws on the
        first answer of every session."""
        template = _template("answer-table-template")
        assert 'class="result-table result-panel"' in template
        assert 'data-testid="result-table"' in template

    def test_the_panel_carries_every_node_the_renderers_query(self) -> None:
        template = _template("answer-table-template")
        for selector in (
            "result-table-title",  # summary label: caption, or "Results" at N=1
            "table-blueprint-chip",  # per-table chip, that table's own blueprint only
            "table-verified-badge",  # per-table badge, that table's own block only
            "result-table-head",
            "result-table-body",
            "result-grid",
            "result-table-caption",  # error / truncation line, per table
            "table-pager",
            "pager-prev",
            "pager-status",
            "pager-next",
        ):
            assert selector in template, f".{selector} is missing from the panel template"

    def test_the_turn_template_holds_a_container_not_a_table(self) -> None:
        """The panel used to be a singleton inside the turn block, found by
        `root.querySelector('.result-table')` — which is why one turn could only
        ever page ONE grid. If it comes back, the turn renders that stale empty
        panel BESIDE the cloned ones."""
        turn = _template("turn-template")
        assert 'class="answer-tables"' in turn
        assert "result-table" not in turn


class TestHistoryReloadForwardsTheList:
    def test_render_history_turn_passes_answer_tables_and_answer_sql(self) -> None:
        """A reloaded multi-table turn must rebuild all of its grids. Dropping
        `answer_tables` here degrades it to the derived first table with no error
        — the live turn keeps rendering fine, so only a reload shows it."""
        match = re.search(r"function renderHistoryTurn\(turn\) \{(.*?)\n  \}", _HTML, re.S)
        assert match is not None, "renderHistoryTurn is missing or was renamed"
        body = match.group(1)
        assert "answer_sql: turn.answer_sql," in body
        assert "answer_tables: turn.answer_tables," in body


class TestTheEmptyUnverifiableBadge:
    """J6 — a blueprint that returned ZERO rows carries `passed: false` +
    `empty_result: true`, and BOTH badge renderers hid on `passed !== true`.

    This is the silent-breakage class this module exists for: the wire carries a
    new, honest state and the page renders nothing for it — the grid then sits
    under exactly the silence a hand-written query gets, which is the opposite of
    what the change was for. Structural, because there is no JS unit harness.
    """

    def test_both_renderers_handle_empty_result_before_the_passed_check(self) -> None:
        for name in ("renderVerification", "renderTableVerification"):
            match = re.search(rf"function {name}\((.*?)\n  \}}", _HTML, re.S)
            assert match is not None, f"{name} is missing or was renamed"
            body = match.group(1)
            empty_at = body.find("empty_result === true")
            passed_at = body.find("passed !== true")
            assert empty_at != -1, f"{name} does not handle the empty_result state"
            assert passed_at != -1, f"{name} lost its passed check"
            assert empty_at < passed_at, (
                f"{name} checks passed!==true first, so an empty_result block "
                "hides the badge instead of rendering the chip"
            )

    def test_the_chip_text_is_the_status_string_the_runtime_emits(self) -> None:
        """The wire carries a display string; the browser renders one. Two places
        that must say the same thing, and only one of them can import the
        constant — the same tie `MAX_ANSWER_TABLES` has below."""
        match = re.search(r"function renderEmptyVerification\((.*?)\n  \}", _HTML, re.S)
        assert match is not None, "renderEmptyVerification is missing or was renamed"
        assert f'badge.textContent = "{VERIFICATION_EMPTY_STATUS}";' in match.group(1)

    def test_the_empty_chip_reuses_the_neutral_badge_styling(self) -> None:
        """It is not a failure state and must not be coloured as one. Both badge
        nodes carry the shared, deliberately neutral `.badge` pill; a dedicated
        error class here would render the retraction as an alarm."""
        assert 'class="verified-badge badge"' in _HTML
        assert 'class="table-verified-badge badge"' in _HTML


class TestTheCapMatchesTheRuntime:
    def test_max_answer_tables_equals_max_intents(self) -> None:
        """08 §F: the cap is `MAX_INTENTS`, so the ceiling can never be the thing
        that forces the model to merge two deliverables into one grid. The
        runtime imports the constant; the browser cannot, so it is asserted."""
        match = re.search(r"var MAX_ANSWER_TABLES = (\d+);", _HTML)
        assert match is not None, "MAX_ANSWER_TABLES is missing from index.html"
        assert int(match.group(1)) == MAX_INTENTS
