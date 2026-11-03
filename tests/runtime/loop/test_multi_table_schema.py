"""The advertised schema teaches the same table economy as the runtime validator."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from data_agent.runtime.composer import PROPOSAL_SCHEMA, RECOVERY_SCHEMA
from data_agent.runtime.composer.components import restrict_schema


def section(kind="table", reason=None):
    p = {"kind": kind, "result_id": "q"}
    if kind == "text":
        p = {"kind": "text", "markdown": "Context."}
    elif kind == "figure":
        p.update(column="n", label="Count")
    s = {"id": "s", "presentation": p, "evidence_refs": ["q"], "caveats": [], "actions": []}
    if reason is not None:
        s["table_reason"] = reason
    return s


@pytest.mark.parametrize("schema", [PROPOSAL_SCHEMA, RECOVERY_SCHEMA])
@pytest.mark.parametrize("sections,valid", [
    ([], True),
    ([section()], True),
    ([section(), section("text"), section("figure")], True),
    ([section(), section()], False),
    ([section(reason="Department detail"), section()], False),
    ([section(reason="Department detail"), section(reason="")], False),
    ([section(reason="Department detail"), section(reason=" \t\n")], False),
    ([section(reason="Department detail"), section(reason="Employee detail")], True),
    ([section(reason="Department detail"), section("text"), section(reason="Employee detail")], True),
])
def test_table_reason_is_conditional_in_advertised_schema(schema, sections, valid):
    advertised = restrict_schema(schema)
    Draft202012Validator.check_schema(advertised)
    args = {"takeaway": "Requested details.", "sections": deepcopy(sections), "evidence": ["q"]}
    if schema is RECOVERY_SCHEMA:
        args["unfinished"] = ["Another requested part is unavailable."]
    assert Draft202012Validator(advertised).is_valid(args) is valid
