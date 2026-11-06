"""Categorical lists retain exact population semantics and bind as literal IN sets."""

import sqlite3
from pathlib import Path

import pytest
import yaml
from sqlglot import exp

from data_agent.runtime.blueprint.models import BlueprintParseError, SlotSpec
from data_agent.runtime.blueprint.slots import AskUser, SlotBinding, resolve_slot
from data_agent.runtime.blueprint.template import TemplateBindError, bind_template, parse_template
from data_agent.runtime.retrieval.tools import _enrich_slot

CORPUS = yaml.safe_load(
    (Path(__file__).parents[2] / "fixtures/corpus_snapshot/blueprints.yaml").read_text()
)
MULTI = [(b, s) for b in CORPUS for s in b["slots"] if s.get("multiple")]
STATUS = next(
    s for b, s in MULTI if b["id"] == "bp-compensation-ranking" and s["name"] == "employee_status"
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, ["Active", "On Leave"]),
        ("Active", ["Active"]),
        (["active", "on leave", "Active"], ["Active", "On Leave"]),
        (["All"], ["All"]),
        (["Active", "Retired"], ["Active", "Retired"]),
    ],
)
def test_status_default_and_explicit_lists(raw, expected):
    spec = SlotSpec.parse(STATUS)
    result = resolve_slot(raw, spec)
    assert isinstance(result, SlotBinding) and result.value == expected
    result.value.append("sentinel")
    assert spec.default == ["Active", "On Leave"]


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {},
        [["Active"]],
        ["Active", None],
        [True],
        [""],
        ["All", "Active"],
        ["not-a-status"],
        ["Active"] * 101,
    ],
)
def test_invalid_multi_values_fail_closed(raw):
    assert isinstance(resolve_slot(raw, SlotSpec.parse(STATUS)), AskUser)


@pytest.mark.parametrize(
    "default", [[], ["UnknownValue"], ["All", "Active"], ["Active", "Active"], "Active", [True]]
)
def test_invalid_list_defaults_rejected(default):
    with pytest.raises(BlueprintParseError):
        SlotSpec.parse({**STATUS, "default": default})


def test_scalar_controls_do_not_accept_lists():
    spec = SlotSpec.parse(
        {"name": "group_by", "type": "enum", "enum_values": ["month", "department"]}
    )
    assert isinstance(resolve_slot(["month", "department"], spec), AskUser)
    with pytest.raises(BlueprintParseError):
        SlotSpec.parse({"name": "n", "type": "positive_integer", "multiple": True})


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 3), ("Active", 2), ("On Leave", 1), ("All", 4), (["Active", "Terminated"], 3)],
)
def test_real_headcount_predicate_selects_population(raw, expected):
    bp = next(b for b in CORPUS if b["id"] == "bp-active-headcount-by-department")
    spec = SlotSpec.parse(next(s for s in bp["slots"] if s["name"] == "employee_status"))
    predicate = next(
        n
        for n in parse_template(bp["sql_template"]).find_all(exp.Or)
        if any(p.name == "employee_status" for p in n.find_all(exp.Placeholder))
    )
    # Exercise the authored predicate with the real slot resolver and binder.
    template = predicate.sql().replace(":employee_status", "{employee_status}")
    sql = bind_template(
        "SELECT count(*) FROM employee WHERE " + template,
        {"employee_status": resolve_slot(raw, spec).value},
    )
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE employee(employee_status TEXT)")
        db.executemany(
            "INSERT INTO employee VALUES (?)",
            [("Active",), ("Active",), ("On Leave",), ("Terminated",)],
        )
        assert db.execute(parse_template(sql).sql(dialect="sqlite")).fetchone()[0] == expected


def test_compensation_default_excludes_terminated_high_earner():
    bp = next(b for b in CORPUS if b["id"] == "bp-compensation-ranking")
    sql = bind_template(
        bp["sql_template"],
        {"top_n": 1, "employee_status": resolve_slot(None, SlotSpec.parse(STATUS)).value},
        optional_patterns={"department": "TRUE"},
    )
    tree = parse_template(sql)
    for table in tree.find_all(exp.Table):
        table.set("db", None)
    with sqlite3.connect(":memory:") as db:
        db.execute(
            "CREATE TABLE employee(employee_code TEXT,employee_name TEXT,department_name TEXT,pay_type TEXT,hourly_salary REAL,annual_salary REAL,employee_status TEXT)"
        )
        db.executemany(
            "INSERT INTO employee VALUES (?,?,?,?,?,?,?)",
            [
                ("a", "A", "Dept", "Salary", None, 100, "Active"),
                ("b", "B", "Dept", "Salary", None, 200, "On Leave"),
                ("c", "C", "Dept", "Salary", None, 999, "Terminated"),
                ("d", "D", "Dept", "Hourly", 10, None, "Active"),
            ],
        )
        assert [(r[0], r[2]) for r in db.execute(tree.sql(dialect="sqlite"))] == [
            ("Hourly", "d"),
            ("Salary", "b"),
        ]


def test_injection_is_one_literal_not_sql():
    malicious = "Sales') OR 1=1 --"
    sql = bind_template(
        "SELECT count(*) FROM employee WHERE department IN ({department})",
        {"department": ["Sales", malicious]},
    )
    tree = parse_template(sql)
    assert tree.find(exp.Or) is None
    assert [v.this for v in tree.find(exp.In).expressions] == ["Sales", malicious]


@pytest.mark.parametrize("bp,slot", MULTI, ids=[b["id"] + ":" + s["name"] for b, s in MULTI])
def test_all_authored_multi_slots_are_explicit_in_filters_and_advertised(bp, slot):
    spec = SlotSpec.parse(slot)
    view = _enrich_slot(slot)
    assert spec.multiple and view["multiple"]
    assert view["value_schema"]["type"] == "array"
    assert "JSON list" in view["note"]
    for node in [bp, *bp.get("composes", [])]:
        if not node.get("sql_template"):
            continue
        for p in parse_template(node["sql_template"]).find_all(exp.Placeholder):
            if p.name == slot["name"]:
                assert isinstance(p.parent, exp.In)
                assert p.find_ancestor(exp.Where, exp.Having)


async def test_multi_entity_probe_is_targeted_and_receipt_keeps_list():
    from data_agent.runtime.blueprint.executor import ExecCompleted
    from data_agent.runtime.mcp.fake_client import FakeMCPClient
    from tests.runtime.blueprint.test_executor_optional_pattern_slice_c import (
        _creds,
        _detail,
        _executor,
        _rq,
    )

    slot = {
        "name": "department",
        "type": "entity",
        "multiple": True,
        "required": False,
        "binds_to": "dbpcm_warehouse.employee.Department",
        "optional_pattern": "TRUE",
    }
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Sales"], ["Eng"]]),
                _rq(["department", "avg_salary"], [["Sales", 1.0], ["Eng", 2.0]]),
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),
            ]
        }
    )
    outcome = await _executor(
        mcp,
        _detail(
            slots=[slot],
            sql_template="SELECT Department AS department, AVG(AnnualSalary) AS avg_salary "
            "FROM dbpcm_warehouse.employee WHERE Department IN ({department}) GROUP BY Department",
        ),
    ).execute(
        blueprint_id="bp-c", slot_bindings={"department": ["sales", "Eng"]}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    assert "IN ('sales', 'eng')" in mcp.calls[0].args["sql"]
    assert "Department IN ('Sales', 'Eng')" in outcome.result_full["terminal_sql"]
    assert outcome.result_full["bound_slots"]["department"] == ["Sales", "Eng"]


async def test_invalid_list_returns_retryable_error_without_pause_or_query():
    from data_agent.runtime.blueprint.executor import ExecFailed
    from data_agent.runtime.mcp.fake_client import FakeMCPClient
    from tests.runtime.blueprint.test_executor_optional_pattern_slice_c import (
        _creds,
        _detail,
        _executor,
    )

    slot = {
        "name": "department",
        "type": "entity",
        "multiple": True,
        "required": False,
        "binds_to": "dbpcm_warehouse.employee.Department",
        "optional_pattern": "TRUE",
    }
    mcp = FakeMCPClient()
    outcome = await _executor(
        mcp,
        _detail(
            slots=[slot],
            sql_template="SELECT Department AS department, AVG(AnnualSalary) AS avg_salary "
            "FROM dbpcm_warehouse.employee WHERE Department IN ({department}) GROUP BY Department",
        ),
    ).execute(blueprint_id="bp-c", slot_bindings={"department": [["Sales"]]}, credentials=_creds())
    assert (
        isinstance(outcome, ExecFailed)
        and outcome.retryable
        and outcome.error_code == "RUN_BLUEPRINT_SLOT_INVALID"
    )
    assert mcp.calls == []


def test_equality_does_not_implicitly_become_membership():
    with pytest.raises(TemplateBindError, match="authored IN"):
        bind_template(
            "SELECT x FROM employee WHERE department={department}",
            {"department": ["Sales", "Engineering"]},
        )


@pytest.mark.parametrize("values", ["Sales", ["Sales"], ["Sales", "Engineering"]])
def test_authored_in_supports_scalar_and_list_without_nested_tuples(values):
    sql = bind_template(
        "SELECT department FROM employee WHERE department IN ({department})", {"department": values}
    )
    predicate = parse_template(sql).find(exp.In)
    assert all(isinstance(v, exp.Literal) for v in predicate.expressions)
    assert [v.this for v in predicate.expressions] == (
        [values] if isinstance(values, str) else values
    )


@pytest.mark.parametrize("kind", ["string", "entity"])
async def test_mismatched_list_value_returns_discovery_error_not_partial_options(kind):
    from data_agent.runtime.blueprint.executor import ExecFailed
    from data_agent.runtime.mcp.fake_client import FakeMCPClient
    from tests.runtime.blueprint.test_executor_optional_pattern_slice_c import (
        _creds,
        _detail,
        _executor,
        _rq,
    )
    slot = {"name": "department", "type": kind, "multiple": True,
            "binds_to": "dbpcm_warehouse.employee.Department", "optional_pattern": "TRUE"}
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["Department"], [["Sales"]])]})
    outcome = await _executor(mcp, _detail(slots=[slot], sql_template=
        "SELECT Department AS department, AVG(AnnualSalary) AS avg_salary "
        "FROM dbpcm_warehouse.employee WHERE Department IN ({department}) GROUP BY Department"
    )).execute(blueprint_id="bp-c", slot_bindings={"department": ["Sales", "Engineerng"]}, credentials=_creds())
    assert isinstance(outcome, ExecFailed) and outcome.retryable
    assert outcome.error_code == "BLUEPRINT_ENTITY_NOT_FOUND"
    assert len(mcp.calls) == 1  # lookup only; no partial execution


@pytest.mark.parametrize("multiple", [False, True])
def test_shared_list_resolver_bounds_dedup_and_rejects_nested_values(multiple):
    spec = SlotSpec.parse({"name": "x", "type": "string" if multiple else "list", "multiple": multiple})
    assert resolve_slot(["Sales", "sales"], spec, domain=["Sales"]).value == ["Sales"]
    for value in (["Sales"] * 101, [], [["Sales"]], [None], [""]):
        assert isinstance(resolve_slot(value, spec), AskUser)


def test_legacy_list_preserves_numeric_scalar_coercion():
    spec = SlotSpec.parse({"name": "x", "type": "list"})
    assert resolve_slot([1, 2, 1], spec).value == ["1", "2"]


@pytest.mark.parametrize("predicate,valid", [
    ("department IN ({department})", True),
    ("department = {department}", False),
    ("department IN ({department}, 'Other')", False),
    ("{department} IN ('Sales')", False),
])
def test_load_checks_multi_value_binding_sites(predicate, valid):
    from data_agent.corpus.seeds import BlueprintSeed
    from data_agent.runtime.blueprint.compiler import CorpusLoadError, validate_blueprint_dag
    seed = BlueprintSeed(id="multi", intent="headcount", slots_summary="", uses=["db.employee.department"],
        slots=[{"name": "department", "type": "string", "multiple": True, "required": True}],
        sql_template=f"SELECT department FROM db.employee WHERE {predicate}")
    if valid:
        validate_blueprint_dag(seed)
    else:
        with pytest.raises(CorpusLoadError, match="requires IN"):
            validate_blueprint_dag(seed)


async def test_missing_required_multi_enum_still_asks_business_question():
    from data_agent.runtime.blueprint.executor import ExecPaused
    from data_agent.runtime.mcp.fake_client import FakeMCPClient
    from tests.runtime.blueprint.test_executor_optional_pattern_slice_c import (
        _creds,
        _detail,
        _executor,
    )
    slot = {"name": "department", "type": "enum", "multiple": True,
            "required": True, "enum_values": ["Sales", "Eng"]}
    mcp = FakeMCPClient()
    outcome = await _executor(mcp, _detail(slots=[slot], sql_template=
        "SELECT Department AS department, AVG(AnnualSalary) AS avg_salary "
        "FROM dbpcm_warehouse.employee WHERE Department IN ({department}) GROUP BY Department"
    )).execute(blueprint_id="bp-c", slot_bindings={}, credentials=_creds())
    assert isinstance(outcome, ExecPaused)
    assert "department" in outcome.pending_question["question"]
    assert mcp.calls == []


def test_list_expansion_also_binds_left_hand_scalar():
    sql = bind_template("SELECT 1 WHERE {value} IN ({choices})",
                        {"value": "Sales", "choices": ["Sales", "Eng"]})
    assert parse_template(sql).find(exp.Placeholder) is None
    with sqlite3.connect(":memory:") as db:
        assert db.execute(sql).fetchall() == [(1,)]


@pytest.mark.parametrize("template", [
    "SELECT 1 WHERE {choices} IN ('Sales')",
    "SELECT 1 WHERE department IN ({choices}, 'Other')",
])
def test_list_rejects_unsupported_in_positions_at_runtime(template):
    with pytest.raises(TemplateBindError, match="authored IN"):
        bind_template(template, {"choices": ["Sales", "Eng"]})


def test_legacy_bare_in_list_remains_supported():
    sql = bind_template("SELECT 1 WHERE 'Sales' IN {choices}", {"choices": ["Sales", "Eng"]})
    with sqlite3.connect(":memory:") as db:
        assert db.execute(sql).fetchall() == [(1,)]
