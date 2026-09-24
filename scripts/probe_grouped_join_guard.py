"""Exercise the aggregate guard against the scoped local integration warehouse."""

from __future__ import annotations

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
from data_agent.runtime.loop.measurement import validate_join_cardinality
from data_agent.runtime.mcp.real_client import RealMCPClient

SOURCE = """SELECT employee_code, department_code, annual_salary
FROM dbpcm_warehouse.employee
WHERE employee_status='A' AND annual_salary IS NOT NULL AND department_code IS NOT NULL"""
LOOKUP = """SELECT department_code, avg(annual_salary) AS dept_avg
FROM dbpcm_warehouse.employee
WHERE employee_status='A' AND annual_salary IS NOT NULL AND department_code IS NOT NULL
GROUP BY department_code"""
MEASURES = "count(*) AS active_dept_pairs, countIf(e.annual_salary > d.dept_avg) AS above_dept_avg"
SAFE_SQL = (
    f"SELECT {MEASURES} FROM ({SOURCE}) e JOIN ({LOOKUP}) d ON e.department_code=d.department_code"
)


async def main(args):
    catalog = json.loads(Path("tests/fixtures/catalog_export.json").read_text())["catalog"]
    scope = [f"{table}.{column}" for table, info in catalog.items() for column in info["columns"]]
    sid = "grouped-join-probe-" + uuid.uuid4().hex[:12]
    credentials = RuntimeCredentials(sid, await mint_bound_token(scope, sid), frozenset(scope))
    dispatcher = ToolDispatcher(RealMCPClient("http://localhost:18090/mcp"), catalog_handle())
    cases = [
        ("inline_grouped_lookup", SAFE_SQL, True),
        (
            "cte_grouped_lookup",
            f"WITH averages AS ({LOOKUP}) SELECT {MEASURES} FROM ({SOURCE}) e JOIN averages d ON e.department_code=d.department_code",
            True,
        ),
        ("repeated_lookup_measure", SAFE_SQL.replace(MEASURES, "count(*), sum(d.dept_avg)"), False),
        (
            "payroll_leave_fanout",
            "SELECT sum(p.amount) FROM dbpcm_warehouse.payroll p JOIN dbpcm_warehouse.accrual_events a ON p.employee_code=a.employee_code",
            False,
        ),
    ]
    results = []
    for name, sql, allowed in cases:
        detail = await validate_join_cardinality(sql, dispatcher, credentials)
        item = {
            "case": name,
            "sql": sql,
            "guard_error": detail,
            "passed": (detail is None) == allowed,
        }
        if allowed and detail is None:
            result = await dispatcher.dispatch("runQuery", {"sql": sql}, credentials)
            item["result"] = result.result_full
            # Exact expectations for the existing seven-employee integration fixture.
            item["passed"] = result.status == "ok" and (result.result_full or {}).get("rows") == [
                [7, 4]
            ]
        results.append(item)
        print(f"{name}: {'PASS' if item['passed'] else 'FAIL'}", flush=True)
    write_results(args.output, {"session_id": sid, "results": results})
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
