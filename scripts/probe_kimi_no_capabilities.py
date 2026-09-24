"""Ten-question Kimi evaluation against the local synthetic warehouse."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx
from _e2e_harness import mint_bound_token, parse_sse
from probe_runtime_acceptance import traces, write_results

from data_agent.runtime.mcp.real_client import RealMCPClient

CASES = [
    (
        "cross_table_fanout",
        "For every current department, show active employee headcount, total EARN payroll paid in January 2024 for those active employees, and their approved Time Off Request hours during April–June 2026. Include departments with zero qualifying activity. Use current employee department, not the event's recorded department, and avoid double-counting when employees have multiple payroll lines and time-off events.",
    ),
    (
        "application_fanout",
        "By application source, how many distinct applications have at least two education records and at least one employment-history record? Include each source even when its qualifying count is zero. Count applications, not joined child rows.",
    ),
    (
        "salary_antijoin",
        "List active employee codes whose current annual salary is above the average for active employees in their own department and who have no approved Time Off Request events during January–June 2026. Show department, annual salary, and department average. Do not treat payout requests as time off.",
    ),
    (
        "distinct_workflows",
        "By current employee department, count distinct performance discussion IDs in Final Approval and distinct personnel-action transaction IDs in Final Approval, considering records dated before July 1, 2026. Include zero counts, and avoid inflating either count from repeated discussion fields or changed fields on a transaction.",
    ),
    ("vague_struggling", "Which teams are struggling?"),
    ("vague_spend", "How much did we spend on people recently?"),
    (
        "partial_prediction",
        "Show current active employee headcount by department, then tell me the exact probability that each employee will voluntarily resign in the next 90 days and the reason they will leave. Complete any part supported by the data and clearly identify what you cannot determine.",
    ),
    (
        "partial_balance",
        "By current employee department, show approved PTO hours from Time Off Request events during April–June 2026. Also show everyone's remaining PTO balance today. Complete the supported part even if balances are not available; do not infer balances from event totals.",
    ),
    (
        "partial_market",
        "Show the current average annual salary of active employees by department and compare each with today's local-market median for equivalent roles. Quantify how far above or below market we are. Complete the internal-data part even if external benchmarks are unavailable.",
    ),
    (
        "write_and_scenario",
        "Increase every active Sales employee's annual salary by 10%, and show the current total annual salary, hypothetical new total, and annual increase for that group. If you cannot make the change, still calculate the scenario and clearly say whether anything was actually changed.",
    ),
]

ORACLES = {
    1: """SELECT d.department_name, countIf(e.employee_status='A') AS active_headcount,
 sum(if(e.employee_status='A',coalesce(p.earnings,0),0)) AS january_earnings,
 sum(if(e.employee_status='A',coalesce(a.hours,0),0)) AS approved_time_off_hours
 FROM dbpcm_warehouse.department d
 LEFT JOIN dbpcm_warehouse.employee e ON d.department_code=e.department_code
 LEFT JOIN (SELECT employee_code,sum(amount) AS earnings FROM dbpcm_warehouse.payroll WHERE register_type='EARN' AND pay_date >= '2024-01-01' AND pay_date < '2024-02-01' GROUP BY employee_code) p ON e.employee_code=p.employee_code
 LEFT JOIN (SELECT employee_code,sum(hours) AS hours FROM dbpcm_warehouse.accrual_events WHERE status='Approved' AND event_type='Time Off Request' AND request_date >= '2026-04-01' AND request_date < '2026-07-01' GROUP BY employee_code) a ON e.employee_code=a.employee_code
 GROUP BY d.department_name ORDER BY d.department_name""",
    2: """SELECT a.application_source,countIf(coalesce(e.n,0)>=2 AND coalesce(h.n,0)>=1) AS qualifying_applications FROM dbpcm_warehouse.applicant_tracking_application a
 LEFT JOIN (SELECT application_id,count() AS n FROM dbpcm_warehouse.candidate_education GROUP BY application_id) e ON a.application_id=e.application_id
 LEFT JOIN (SELECT application_id,count() AS n FROM dbpcm_warehouse.candidate_employment_history GROUP BY application_id) h ON a.application_id=h.application_id GROUP BY a.application_source ORDER BY a.application_source""",
    3: """SELECT e.employee_code,e.department_name,e.annual_salary,d.average_salary FROM dbpcm_warehouse.employee e
 INNER JOIN (SELECT department_code,avg(annual_salary) AS average_salary FROM dbpcm_warehouse.employee WHERE employee_status='A' GROUP BY department_code) d ON e.department_code=d.department_code
 WHERE e.employee_status='A' AND e.annual_salary>d.average_salary AND e.employee_code NOT IN (SELECT employee_code FROM dbpcm_warehouse.accrual_events WHERE status='Approved' AND event_type='Time Off Request' AND request_date >= '2026-01-01' AND request_date < '2026-07-01') ORDER BY e.employee_code""",
    4: """SELECT d.department_name,uniqExactIf(p.discussion_id,p.discussion_state='Final Approval' AND p.creation_date < '2026-07-01') AS discussions,
 uniqExactIf(f.paf_transaction_id,f.paf_status='Final Approval' AND f.paf_effective_date < '2026-07-01') AS transactions
 FROM dbpcm_warehouse.department d LEFT JOIN dbpcm_warehouse.employee e ON d.department_code=e.department_code
 LEFT JOIN dbpcm_warehouse.performance_discussions p ON e.employee_code=p.employee_code
 LEFT JOIN dbpcm_warehouse.personnel_action_form_changes f ON e.employee_code=f.employee_code GROUP BY d.department_name ORDER BY d.department_name""",
    6: "SELECT pay_period_end_date, uniqExactIf(employee_code, register_type='EARN') AS employees_paid, sumIf(amount, register_type='EARN') AS gross_earnings FROM dbpcm_warehouse.payroll GROUP BY pay_period_end_date ORDER BY pay_period_end_date DESC",
    7: "SELECT department_name, count() AS active_headcount FROM dbpcm_warehouse.employee WHERE employee_status='A' GROUP BY department_name ORDER BY department_name",
    8: """SELECT e.department_name,sum(a.hours) AS approved_pto_hours FROM dbpcm_warehouse.employee e INNER JOIN dbpcm_warehouse.accrual_events a ON e.employee_code=a.employee_code WHERE a.status='Approved' AND a.earn_code='PTO' AND a.event_type='Time Off Request' AND a.request_date >= '2026-04-01' AND a.request_date < '2026-07-01' GROUP BY e.department_name ORDER BY e.department_name""",
    9: "SELECT department_name,avg(annual_salary) AS average_salary FROM dbpcm_warehouse.employee WHERE employee_status='A' GROUP BY department_name ORDER BY department_name",
    10: "SELECT count() AS employees,sum(annual_salary) AS current_total,sum(annual_salary)*1.1 AS hypothetical_total,sum(annual_salary)*0.1 AS annual_increase FROM dbpcm_warehouse.employee WHERE employee_status='A' AND department_name='Sales'",
}


async def main(args):
    catalog = json.loads(Path("tests/fixtures/catalog_export.json").read_text())["catalog"]
    scope = [
        f"{table}.{column}"
        for table, definition in catalog.items()
        for column in definition["columns"]
    ]
    report = {
        "model": "kimi-k2.7-code",
        "capabilities": False,
        "scope": scope,
        "cases": [],
        "baseline": {},
    }
    if args.prior_output:
        previous = json.loads(args.prior_output.read_text())
        report["cases"] = [c for c in previous["cases"] if c["number"] not in args.questions]
        report["prior_attempts_artifact"] = str(args.prior_output)
    mcp = RealMCPClient("http://localhost:18090/mcp")
    oracle_sid = "kimi-oracle-" + uuid.uuid4().hex[:10]
    jwt = await mint_bound_token(scope, oracle_sid)
    for number, sql in ORACLES.items():
        try:
            report["baseline"][str(number)] = {
                "sql": sql,
                "result": await mcp.call_tool(
                    "runQuery", {"sql": sql}, jwt=jwt, session_id=oracle_sid
                ),
            }
        except Exception as exc:
            report["baseline"][str(number)] = {"sql": sql, "error": str(exc)}
    write_results(args.output, report)
    async with httpx.AsyncClient(timeout=httpx.Timeout(960, connect=10)) as client:
        for number, (kind, question) in enumerate(CASES, 1):
            if number not in args.questions:
                continue
            sid = f"kimi-no-caps-q{number}-" + uuid.uuid4().hex[:10]
            token = await mint_bound_token(scope, sid)
            headers = {"Authorization": "Bearer " + token, "X-Session-Id": sid}
            started = time.monotonic()
            item = {"number": number, "kind": kind, "question": question, "session_id": sid}
            try:
                response = await client.post(
                    args.base_url + "/turn", headers=headers, json={"message": question}
                )
                progress, outcome, error = parse_sse(response.text)
                item.update(
                    http_status=response.status_code, result=outcome, error=error, progress=progress
                )
                # Long paced evaluations can outlive the token used to start the turn.
                headers["Authorization"] = "Bearer " + await mint_bound_token(scope, sid)
                item["history"] = (
                    await client.get(args.base_url + "/session/history", headers=headers)
                ).json()
                item["pages"] = []
                for table in (outcome or {}).get("answer_tables") or []:
                    page = await client.post(
                        args.base_url + "/query/page",
                        headers=headers,
                        json={"sql": table["sql"], "limit": 100, "offset": 0},
                    )
                    item["pages"].append(
                        {
                            "caption": table.get("caption"),
                            "http_status": page.status_code,
                            "body": page.json(),
                        }
                    )
            except Exception as exc:
                item["transport_error"] = type(exc).__name__
            item["seconds"] = round(time.monotonic() - started, 2)
            report["cases"].append(item)
            report["cases"].sort(key=lambda case: case["number"])
            write_results(args.output, report)
            result = item.get("result") or {}
            print(
                f"Q{number} {kind}: {result.get('status')} review={result.get('review')} {item['seconds']}s",
                flush=True,
            )
        report["salary_after"] = await mcp.call_tool(
            "runQuery",
            {"sql": ORACLES[10]},
            jwt=await mint_bound_token(scope, oracle_sid),
            session_id=oracle_sid,
        )
        try:
            await asyncio.sleep(6)
            report["traces"] = await traces(
                client, {item["session_id"] for item in report["cases"]}
            )
        except Exception as exc:
            report["trace_error"] = type(exc).__name__
        write_results(args.output, report)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument(
        "--questions", type=int, nargs="+", choices=range(1, 11), default=list(range(1, 11))
    )
    p.add_argument("--prior-output", type=Path)
    asyncio.run(main(p.parse_args()))
