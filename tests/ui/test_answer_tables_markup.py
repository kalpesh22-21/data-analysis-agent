"""Document table markup and history adapter invariants."""

from __future__ import annotations

import pathlib
import re

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


def test_history_forwards_the_exact_document_without_legacy_presentation():
    match = re.search(r"function renderHistoryTurn\(turn\) \{(.*?)\n  \}", _HTML, re.S)
    assert match is not None
    body = match.group(1)
    assert "answer_document: turn.answer_document" in body
    assert "answer_tables" not in body
    assert "answer_sql" not in body


def test_live_and_history_use_the_document_renderer():
    assert "window.renderAnswerDocument(container, doc," in _HTML
    assert "data.assistant_text" not in _HTML
    assert "data.answer_tables" not in _HTML
