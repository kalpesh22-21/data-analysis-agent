"""Exercise the runtime over HTTP with the local integration tenant and real model.

Requires the real runtime, local token/MCP services and optional Help/Capability
fixtures. Raw answers are written only to a private output file; credentials are
never logged. Scripted failure/repair scenarios live in test_harness_improvements.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx
from _e2e_harness import mint_bound_token, parse_sse
from probe_runtime_acceptance import SCOPE, write_results

CASES = {
    "headcount": "How many active employees do we have by department?",
    "separate_tables": "Show active employee counts for Sales and Engineering as separate tables.",
    "top_n": "Show the top three employees by annual salary, with their names and departments.",
    "ambiguous_department": "How many active employees are in the department Nonexistent Department 91827?",
    "empty_result": "List employees whose hire date is January 1, 2099.",
    "metadata": "What employee information can you help me analyze? Use business descriptions.",
    "help": "How do I request time off?",
    "missing_help": "How do I configure the Quantum Teleportation feature in Paycom?",
    "navigation": "Take me to Position Management.",
    "mixed": "How many active employees do we have, and how do I submit a time-off request?",
    "scope": "Show me the salary and SSN of every employee named Smith.",
    "off_topic": "What's the weather in Paris?",
    "clarification": "Before showing the active employee count for one department, ask me to choose Sales or Engineering.",
}


async def main(args):
    results = []
    semaphore = asyncio.Semaphore(2)
    async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=10)) as client:

        async def probe(name):
            async with semaphore:
                sid = "harness-" + name + "-" + uuid.uuid4().hex[:10]
                token = await mint_bound_token(SCOPE, sid)
                headers = {"Authorization": "Bearer " + token, "X-Session-Id": sid}
                started = time.monotonic()
                item = {"case": name, "question": CASES[name], "checks": {}}
                try:
                    response = await client.post(
                        args.base_url + "/turn", headers=headers, json={"message": CASES[name]}
                    )
                    progress, outcome, error = parse_sse(response.text)
                    item.update(
                        http_status=response.status_code,
                        result=outcome,
                        error=error,
                        progress=progress,
                    )
                    item["checks"]["valid_response"] = (
                        response.status_code == 200 and outcome is not None and error is None
                    )
                    if (
                        name == "clarification"
                        and outcome
                        and outcome["status"] == "paused_ask_user"
                    ):
                        item["checks"]["clarification_reached"] = True
                        resumed = await client.post(
                            args.base_url + "/turn/resume",
                            headers=headers,
                            json={"answer": "Sales"},
                        )
                        _, outcome, error = parse_sse(resumed.text)
                        item.update(resumed_result=outcome, resumed_error=error)
                        again = await client.post(
                            args.base_url + "/turn/resume",
                            headers=headers,
                            json={"answer": "Sales"},
                        )
                        item["checks"]["resume_exactly_once"] = again.status_code == 409
                    elif name == "clarification":
                        item["checks"]["clarification_reached"] = False
                    if outcome:
                        item["checks"]["completed"] = outcome["status"] in (
                            {"done", "paused_ask_user"}
                            if name in {"ambiguous_department", "scope"}
                            else {"done"}
                        )
                        tables = outcome.get("answer_tables") or []
                        if name == "separate_tables":
                            item["checks"]["distinct_executions"] = (
                                len(tables) == 2 and len({t["sql"] for t in tables}) == 2
                            )
                        if name == "navigation":
                            item["checks"]["prepared_option_selected"] = bool(
                                outcome.get("capability_cards")
                            )
                        pages = []
                        for table in tables:
                            page = await client.post(
                                args.base_url + "/query/page",
                                headers=headers,
                                json={"sql": table["sql"], "limit": 2, "offset": 0},
                            )
                            pages.append(
                                {
                                    "caption": table.get("caption"),
                                    "status": page.status_code,
                                    "body": page.json(),
                                }
                            )
                        item["pages"] = pages
                        if tables:
                            item["checks"]["pagination"] = all(p["status"] == 200 for p in pages)
                        history = await client.get(
                            args.base_url + "/session/history", headers=headers
                        )
                        item["history"] = history.json()
                        if tables:
                            stored = item["history"]["turns"][0].get("answer_tables") or []
                            item["checks"]["history_matches_live"] = [t["sql"] for t in stored] == [
                                t["sql"] for t in tables
                            ]
                        if name == "headcount" and tables:
                            narrow_token = await mint_bound_token(
                                ["dbpcm_warehouse.employee.employee_code"], sid
                            )
                            narrow_headers = {**headers, "Authorization": "Bearer " + narrow_token}
                            denied = await client.post(
                                args.base_url + "/query/page",
                                headers=narrow_headers,
                                json={"sql": tables[0]["sql"], "limit": 2, "offset": 0},
                            )
                            hidden = await client.get(
                                args.base_url + "/session/history", headers=narrow_headers
                            )
                            item["checks"]["page_scope_rechecked"] = denied.status_code == 403
                            item["checks"]["history_scope_rechecked"] = not hidden.json()["turns"][
                                0
                            ].get("answer_tables")
                except Exception as exc:
                    item["error_type"] = type(exc).__name__
                    item["checks"]["no_transport_error"] = False
                item["seconds"] = round(time.monotonic() - started, 2)
                results.append(item)
                write_results(args.output, {"base_url": args.base_url, "results": results})
                failed = [k for k, v in item["checks"].items() if not v]
                print(
                    f"{name}: {'PASS' if not failed else 'FAIL ' + ','.join(failed)} ({item['seconds']}s)",
                    flush=True,
                )

        await asyncio.gather(*(probe(name) for name in (args.cases or CASES)))
    summary = {r["case"]: r["checks"] for r in results}
    print(json.dumps(summary, indent=2))
    return int(any(not v for r in results for v in r["checks"].values()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18104")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cases", nargs="+", choices=list(CASES))
    raise SystemExit(asyncio.run(main(parser.parse_args())))
