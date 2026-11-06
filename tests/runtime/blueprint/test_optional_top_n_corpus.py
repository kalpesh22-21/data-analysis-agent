"""Exercise optional limits from the synchronized, authored MCP corpus."""
from pathlib import Path

import pytest
import yaml

from data_agent.runtime.blueprint.template import bind_template, parse_template, referenced_slots

_BLUEPRINTS = yaml.safe_load(
    (Path(__file__).parents[2] / "fixtures/corpus_snapshot/blueprints.yaml").read_text()
)
_OPTIONAL = [b for b in _BLUEPRINTS if any(
    s["name"] == "top_n" and s.get("optional_pattern") == "TRUE" for s in b.get("slots", [])
)]


@pytest.mark.parametrize("blueprint", _OPTIONAL, ids=lambda b: b["id"])
def test_authored_optional_limit_binds_and_omits_without_changing_query(blueprint):
    template = blueprint["sql_template"]
    # Other slots bind to inert literals here: this test isolates the cap's AST,
    # while the executor tests exercise typed resolution and actual omission.
    names = referenced_slots(template)
    bindings = {name: 1 for name in names - {"top_n"}}
    bindings.update({s["name"]: s["default"] for s in blueprint["slots"]
                     if s["name"] in {"order_by", "order_direction"}})
    full = parse_template(bind_template(template, bindings, optional_patterns={"top_n": "TRUE"}))
    capped = parse_template(bind_template(template, {**bindings, "top_n": 10}))
    assert full.args.get("limit") is None
    assert capped.args["limit"].expression.this == "10"
    capped.set("limit", None)
    assert capped == full
