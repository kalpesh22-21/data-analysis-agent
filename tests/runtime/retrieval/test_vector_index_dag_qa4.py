"""QA4 Layer-1: getBlueprint full-DAG record mapping — malformed/degrade (§1.3).

`map_blueprint_detail_record` JSON-decodes the six additive `*_json` properties and
must degrade a null/malformed/wrong-typed stored value to `None` (fail-soft: the
tool then simply omits the field), NEVER crash the keyed fetch. This file drives
each additive field through malformed inputs and pins the type-coercion guards
(a decoded value of the WRONG json type — e.g. `resolves` decoding to a list — is
coerced to `None`, not surfaced as a broken shape).

ADD-only.
"""

from __future__ import annotations

from data_agent.runtime.retrieval.vector_index import map_blueprint_detail_record

_BASE = {
    "id": "b",
    "intent": "i",
    "slots_summary": "",
    "uses": [],
    "status": "",
    "drift_status": "",
    "hit_count": 0,
    "catalog_sha": "",
}


def _rec(**dag: object) -> dict:
    return {**_BASE, **dag}


def test_malformed_json_in_every_field_degrades_to_none() -> None:
    d = map_blueprint_detail_record(
        _rec(
            resolves_json="{not valid",
            slots_json="[oops",
            uses_rules_json="}{",
            composes_json="nope",
            result_grain_json="<<<",
            sql_template=None,
        )
    )
    assert d.resolves is None
    assert d.slots is None
    assert d.uses_rules is None
    assert d.composes is None
    assert d.result_grain is None
    assert d.sql_template is None


def test_json_null_string_decodes_to_none() -> None:
    d = map_blueprint_detail_record(_rec(resolves_json="null", slots_json="null"))
    assert d.resolves is None and d.slots is None


def test_wrong_json_type_is_coerced_to_none() -> None:
    # `resolves` must be an object; a JSON list is the wrong shape → coerced None.
    # `slots` must be a list; a JSON object is wrong → None. This pins the
    # isinstance guards in the mapper (fail-soft, never a mis-typed surface).
    d = map_blueprint_detail_record(
        _rec(resolves_json='["a","b"]', slots_json='{"x":1}')
    )
    assert d.resolves is None
    assert d.slots is None


def test_valid_dag_fields_round_trip_decoded() -> None:
    d = map_blueprint_detail_record(
        _rec(
            resolves_json='{"salary":"AnnualSalary"}',
            slots_json='[{"name":"department","type":"string"}]',
            uses_rules_json='["active_employee"]',
            composes_json="[]",
            result_grain_json='["Department"]',
            sql_template="SELECT 1",
        )
    )
    assert d.resolves == {"salary": "AnnualSalary"}
    assert d.slots == [{"name": "department", "type": "string"}]
    assert d.uses_rules == ["active_employee"]
    assert d.composes == []  # an empty list is a valid list, kept (not None)
    assert d.result_grain == ["Department"]
    assert d.sql_template == "SELECT 1"


def test_result_grain_dict_form_decodes() -> None:
    d = map_blueprint_detail_record(
        _rec(result_grain_json='{"columns":["EmployeeCode"],"verifiable":false}')
    )
    assert d.result_grain == {"columns": ["EmployeeCode"], "verifiable": False}


def test_absent_dag_fields_map_to_none_like_a_legacy_blueprint() -> None:
    d = map_blueprint_detail_record(dict(_BASE))
    assert d.resolves is None
    assert d.slots is None
    assert d.uses_rules is None
    assert d.composes is None
    assert d.result_grain is None
    assert d.sql_template is None


def test_non_string_sql_template_coerced_to_none() -> None:
    d = map_blueprint_detail_record(_rec(sql_template=123))
    assert d.sql_template is None
