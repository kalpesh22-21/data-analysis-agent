"""Live regression matrix for SQL guard edge cases against the scoped fixture."""

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from _catalog import catalog_handle
from _e2e_harness import mint_bound_token
from probe_runtime_acceptance import write_results

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.measurement import cardinality_probes, validate_join_cardinality
from data_agent.runtime.mcp.real_client import RealMCPClient

E = "dbpcm_warehouse.employee"
D = "dbpcm_warehouse.department"
P = "dbpcm_warehouse.payroll"
join = f"FROM {E} e JOIN {D} d ON e.department_code=d.department_code"
cases = {
    "baseline_salary": f"SELECT sum(e.annual_salary) FROM {E} e",
    "safe_sum_lookup": f"SELECT sum(e.annual_salary) {join}",
    "plain_lookup_count": f"SELECT count(*) {join}",
    "distinct_lookup_count": f"SELECT count(*) FROM {E} e JOIN (SELECT DISTINCT department_code FROM {E}) d ON e.department_code=d.department_code",
    "grouped_lookup_count": f"SELECT count(*) FROM {E} e JOIN (SELECT department_code FROM {E} GROUP BY department_code) d ON e.department_code=d.department_code",
    "positional_group_count": f"SELECT count(*) FROM {E} e JOIN (SELECT department_code FROM {E} GROUP BY 1) d ON e.department_code=d.department_code",
    "conditional_lookup_sum": f"SELECT sumIf(e.annual_salary,d.department_code='D01') {join}",
    "conditional_baseline": f"SELECT sum(e.annual_salary) FROM {E} e WHERE e.department_code='D01'",
    "negated_equality_sum": f"SELECT sum(e.annual_salary) FROM {E} e JOIN {D} d ON NOT(e.department_code=d.department_code)",
    "having_only_sum": f"SELECT e.department_code FROM {E} e JOIN {P} p ON e.employee_code=p.employee_code GROUP BY e.department_code HAVING sum(e.annual_salary)>500000 ORDER BY e.department_code",
    "having_baseline": f"SELECT e.department_code FROM {E} e GROUP BY e.department_code HAVING sum(e.annual_salary)>500000 ORDER BY e.department_code",
    "filtered_payroll_join": f"SELECT sum(e.annual_salary) FROM {E} e JOIN {P} p ON e.employee_code=p.employee_code WHERE p.register_type='EARN' AND p.pay_period_end_date='2024-01-15'",
    "prefiltered_payroll_join": f"SELECT sum(e.annual_salary) FROM {E} e JOIN (SELECT employee_code FROM {P} WHERE register_type='EARN' AND pay_period_end_date='2024-01-15') p ON e.employee_code=p.employee_code",
    "filtered_outer_count": f"SELECT count(d.department_code) FROM {E} e LEFT JOIN {D} d ON e.department_code=d.department_code WHERE d.department_code='D01'",
    "filtered_inner_count": f"SELECT count(d.department_code) {join} WHERE d.department_code='D01'",
    "qualified_lookup_count": f"SELECT count(e.employee_code) {join}",
    "nested_cte_sum": f"SELECT * FROM (WITH dept AS (SELECT department_code FROM {D}) SELECT sum(e.annual_salary) FROM {E} e JOIN dept d ON e.department_code=d.department_code)",
}
cases.update(
    {
        "order_only_sum": f"SELECT e.department_code FROM {E} e JOIN {P} p ON e.employee_code=p.employee_code GROUP BY e.department_code ORDER BY sum(e.annual_salary) DESC",
        "order_baseline": f"SELECT e.department_code FROM {E} e GROUP BY e.department_code ORDER BY sum(e.annual_salary) DESC",
        "mandatory_key_with_or": f"SELECT sum(e.annual_salary) {join} AND (d.department_code='D01' OR d.department_code='D02')",
        "unqualified_measure": f"SELECT sum(annual_salary) {join}",
        "computed_join_key": f"SELECT sum(e.annual_salary) FROM {E} e JOIN {D} d ON lower(e.department_code)=lower(d.department_code)",
        "any_payroll_join": f"SELECT sum(e.annual_salary) FROM {E} e ANY INNER JOIN {P} p ON e.employee_code=p.employee_code",
        "negated_eq_with_valid_key": f"SELECT sum(e.annual_salary) FROM {E} e JOIN {P} p ON e.department_code=p.department_code AND NOT(e.employee_code=p.employee_code)",
    }
)


cases["shadowed_cte"] = (
    f"WITH original AS (SELECT department_code FROM {D}), dept AS (SELECT * FROM original) SELECT * FROM (WITH original AS (SELECT department_code FROM {E}) SELECT sum(e.annual_salary) FROM {E} e JOIN dept d ON e.department_code=d.department_code)"
)


async def main(output):
    catalog = json.loads(Path("tests/fixtures/catalog_export.json").read_text())["catalog"]
    scope = [f"{t}.{c}" for t, info in catalog.items() for c in info["columns"]]
    sid = "guard-audit-" + uuid.uuid4().hex[:12]
    creds = RuntimeCredentials(sid, await mint_bound_token(scope, sid), frozenset(scope))
    catalog_snapshot = catalog_handle()
    dispatcher = ToolDispatcher(RealMCPClient("http://localhost:18090/mcp"), catalog_snapshot)
    rows = []
    for name, sql in cases.items():
        error = await validate_join_cardinality(sql, dispatcher, creds)
        r = await dispatcher.dispatch("runQuery", {"sql": sql}, creds)
        try:
            probes = cardinality_probes(sql, schema=catalog_snapshot.schema)
        except Exception as exc:
            probes = [str(exc)]
        item = dict(
            case=name,
            sql=sql,
            guard_error=error,
            status=r.status,
            error_code=r.error_code,
            result=r.result_full,
            detail=r.denial_detail,
            probes=probes,
        )
        blocked = name in {
            "shadowed_cte",
            "negated_equality_sum",
            "having_only_sum",
            "order_only_sum",
            "negated_eq_with_valid_key",
        }
        expected = {
            "plain_lookup_count": [[7]],
            "distinct_lookup_count": [[7]],
            "grouped_lookup_count": [[7]],
            "positional_group_count": [[7]],
            "qualified_lookup_count": [[7]],
            "filtered_outer_count": [[2]],
            "filtered_inner_count": [[2]],
            "conditional_lookup_sum": [["250000.000000"]],
            "conditional_baseline": [["250000.000000"]],
            "mandatory_key_with_or": [["575000.000000"]],
            "having_baseline": [],
            "order_baseline": [["D02"], ["D01"], ["D03"]],
        }.get(name, [["700000.000000"]])
        item["passed"] = (
            bool(error)
            if blocked
            else error is None
            and r.status == "ok"
            and (r.result_full or {}).get("rows") == expected
        )
        rows.append(item)
        print(f"{name}: {'PASS' if item['passed'] else 'FAIL'}", flush=True)
    write_results(output, {"results": rows})
    return 0 if all(item["passed"] for item in rows) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args().output)))
