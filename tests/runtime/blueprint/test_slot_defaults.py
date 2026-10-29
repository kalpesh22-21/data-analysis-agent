"""LANE5-SPEC-2026-001: authored defaults survive validation and typed binding."""

import pytest

from data_agent.runtime.blueprint.compiler import validate_blueprint_dag
from data_agent.runtime.blueprint.models import BlueprintParseError, SlotSpec
from data_agent.runtime.blueprint.slots import SlotBinding, resolve_slot
from data_agent.runtime.retrieval.corpus_loader import CorpusLoadError
from tests.runtime.retrieval.test_corpus_loader_dag_qa4 import _seed


def declaration(**overrides):
    return dict(name="direction", type="enum", required=False,
                enum_values=["DESC", "ASC"], default="DESC", **overrides)


def test_default_and_description_preserved():
    spec = SlotSpec.parse(declaration(description=" Sort direction. "))
    assert spec.default == "DESC"
    assert spec.description == " Sort direction. "
    assert resolve_slot(None, spec) == SlotBinding("direction", "DESC", "default")
    assert resolve_slot("ASC", spec).value == "ASC"
    assert resolve_slot("ASC", spec).resolved_from == "enum"


@pytest.mark.parametrize("updates", [
    {"default": "desc"}, {"default": "OTHER"}, {"default": ["DESC"]},
    {"default": {"value": "DESC"}}, {"default": ("DESC",)},
    {"required": True}, {"type": "period_range"}, {"type": "list"},
    {"optional_pattern": "TRUE"}, {"description": ""}, {"description": 123},
])
def test_invalid_declarations_fail_at_parse(updates):
    raw = declaration()
    raw.update(updates)
    with pytest.raises(BlueprintParseError, match="direction"):
        SlotSpec.parse(raw)


@pytest.mark.parametrize("value", [0, False, 1.5, "", "O'Reilly"])
def test_scalar_defaults_are_bound_verbatim(value):
    raw = declaration()
    raw.update(type="string", default=value)
    binding = resolve_slot(None, SlotSpec.parse(raw))
    assert binding.value == value
    assert type(binding.value) is type(value)
    assert binding.resolved_from == "default"


@pytest.mark.parametrize("default", ["DESC", None])
def test_compiler_bindability(default):
    raw = declaration()
    raw["default"] = default
    seed = _seed(slots=[raw], sql_template=(
        "SELECT Department FROM dbpcm_warehouse.employee "
        "ORDER BY CASE WHEN {direction} = 'DESC' THEN Department END DESC"
    ))
    if default is None:
        with pytest.raises(CorpusLoadError, match="NO optional_pattern and NO default"):
            validate_blueprint_dag(seed)
    else:
        validate_blueprint_dag(seed)
