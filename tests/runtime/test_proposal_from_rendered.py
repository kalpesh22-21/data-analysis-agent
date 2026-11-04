"""A model shown a rendered answer can copy its sections back in proposal form.

The judge and salvage review the RENDERED document but must answer in PROPOSAL form.
`proposal_from_rendered` inverts the renderer, so every presentation kind must survive
proposal -> render -> proposal unchanged.
"""

import pytest

from data_agent.runtime.composer import _visual, proposal_from_rendered
from data_agent.runtime.composer.components import ALL_COMPONENTS, CURRENT_COMPONENTS

COLUMNS = ["Department", "Region", "Headcount", "Payroll"]
ROWS = [["Sales", "East", 3, 100], ["Ops", "West", 2, 50]]
FORMATS = {"Department": {"kind": "text"}, "Region": {"kind": "text"},
           "Headcount": {"kind": "integer"}, "Payroll": {"kind": "integer"}}
PROPOSALS = [
    {"kind": "bars", "result_id": "r", "category_column": "Department", "measure_column": "Headcount", "label": "Headcount"},
    {"kind": "bars", "result_id": "r", "category_column": "Department", "measure_column": "Headcount", "label": "Headcount",
     "direction": "descending", "ties": "dense"},
    {"kind": "bars", "result_id": "r", "category_columns": ["Department", "Region"], "measure_column": "Headcount", "label": "Headcount"},
    {"kind": "figure", "result_id": "r", "column": "Headcount", "label": "Headcount"},
    {"kind": "facts", "result_id": "r", "columns": ["Headcount", "Payroll"]},
    {"kind": "donut", "result_id": "r", "category_column": "Department", "measure_column": "Headcount", "label": "Headcount",
     "composition": True},
    {"kind": "heatmap", "result_id": "r", "category_column": "Department", "series_column": "Region",
     "measure_column": "Headcount", "label": "Headcount"},
    {"kind": "grouped_bars", "result_id": "r", "category_column": "Department", "label": "People and pay",
     "series": [{"measure_column": "Headcount", "label": "Headcount"}, {"measure_column": "Payroll", "label": "Payroll"}]},
]


@pytest.mark.parametrize("proposal", PROPOSALS, ids=lambda p: p["kind"] + ("-ranked" if "direction" in p else ""))
def test_every_kind_survives_render_and_back(proposal) -> None:
    token = CURRENT_COMPONENTS.set(ALL_COMPONENTS)
    try:
        rendered = _visual(proposal, COLUMNS, ROWS, column_formats=FORMATS)
    finally:
        CURRENT_COMPONENTS.reset(token)
    document = {"takeaway": "x", "sections": [{"id": "s", "presentation": rendered}], "sources": {}, "answer_id": "a"}
    restored, changed = proposal_from_rendered(document)
    assert restored == {"takeaway": "x", "sections": [{"id": "s", "presentation": proposal}]}
    assert changed >= 2  # the runtime-owned fields, at least


def test_a_proposal_is_returned_untouched() -> None:
    document = {"takeaway": "x", "sections": [{"id": "s", "presentation": PROPOSALS[0]}]}
    assert proposal_from_rendered(document) == (document, 0)
    assert proposal_from_rendered("not a document") == ("not a document", 0)
