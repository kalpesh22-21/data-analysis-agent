"""Real-model handoff probe with the unresolved payroll capability from the trace.

Uses live local IdP/MCP and current runtime code. The capability HTTP service is an
isolated trace fixture, not the remote provider; no employee data is returned.
Run: DEMO_MODEL=gpt-5.5 .venv/bin/python scripts/probe_capability_handoff.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
import uuid
from collections import Counter
from dataclasses import asdict

import uvicorn
from _e2e_harness import load_openai_key, mint_bound_token, parse_sse, pick_openai_model
from fastapi import FastAPI
from fastapi.testclient import TestClient

from data_agent.runtime.app import create_app
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.session.memory_store import InMemorySessionStore

NAME = "get_employee_payroll_totals"
DESCRIPTION = (
    "This tool provides details on an employee's paystub, year to date totals, and "
    "W-2/1099 totals. The paystub has their net amount, taxes paid and deductions "
    "from their pay checks."
)
PARAMETERS = [
    {
        "name": "employees",
        "description": "The employee to fetch the payroll totals information for.",
        "type": "employee",
        "collection": True,
        "default": "all",
        "resolution": {"strategy": "torch", "entity_type": "employee"},
    },
    {
        "name": "payroll_totals_date",
        "description": "Filters to display payroll total information for a particular date. The date should be in YYYY-MM-DD. Select 'null' if no date is provided.",
        "type": "date",
        "collection": False,
        "default": "null",
        "resolution": {"strategy": "direct"},
    },
]


def main() -> int:
    fixture = FastAPI()
    requests = []

    @fixture.post("/v1/capabilities/search")
    async def search(body: dict):
        requests.append({"operation": "search"})
        return {
            "cards": [
                {
                    "id": NAME,
                    "tool_name": NAME,
                    "kind": "data_widget",
                    "summary": DESCRIPTION,
                    "matched_questions": [
                        "View $employee's paystub.",
                        "Show me $employee's pay stubs.",
                    ],
                    "matched_actions": ["Print paystub"],
                    "matched_data_points": ["Paystub", "Employee paystub"],
                }
            ]
        }

    @fixture.get("/v1/capabilities/tools/" + NAME)
    async def definition():
        requests.append({"operation": "definition"})
        return {
            "name": NAME,
            "version": "1",
            "kind": "data_widget",
            "description": DESCRIPTION,
            "parameters": PARAMETERS,
            "metadata": {"preamble_url": "ember:PayrollTotalsCl"},
        }

    @fixture.post("/v1/capabilities/tools/" + NAME + "/hydrate")
    async def hydrate(body: dict):
        raw = body["raw_arguments"]
        requests.append({"operation": "hydrate", "arguments": raw})
        filters = {"employees": []}
        if "payroll_totals_date" in raw:
            filters["payroll_totals_date"] = {"identifier": raw["payroll_totals_date"]}
        return {
            "name": NAME,
            "arguments": {
                "filters": filters,
                "unresolved_entities": {"employees": raw.get("employees", ["Venkat"])},
                "has_unresolved_entities": True,
            },
            "metadata": {"preamble_url": "ember:PayrollTotalsCl", "ui_parameters": PARAMETERS},
            "parameters": PARAMETERS,
            "resolved_entities": {},
            "additional_arguments": {},
            "next_best_tools": [],
            "are_best_tools_suggestion": False,
        }

    key = load_openai_key()
    model = pick_openai_model(key)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(fixture, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("Fixture startup timed out")
            time.sleep(0.01)
        settings = RuntimeSettings(
            _env_file=None,
            openai_api_key=key,
            openai_model=model,
            mcp_url="http://localhost:18090/mcp",
            jwks_url="http://localhost:19000/.well-known/jwks.json",
            jwt_issuer="http://token:8000/",
            jwt_audience="clickhouse-api",
            catalog_source="fixture",
            retrieval_enabled=False,
            scratch_enabled=False,
            capability_tools_enabled=True,
            capability_prefetch_enabled=True,
            capability_api_url=f"http://127.0.0.1:{port}/v1/capabilities",
            answer_judge_enabled=True,
            progress_summary_enabled=False,
        )
        store = InMemorySessionStore()
        app = create_app(settings=settings, session_store=store)
        sid = "paystub-handoff-" + uuid.uuid4().hex[:12]
        token = asyncio.run(mint_bound_token([], sid, allow_unscoped=True))
        with TestClient(app) as client:
            response = client.post(
                "/turn",
                json={"message": "Venkat's last paystub"},
                headers={"Authorization": "Bearer " + token, "X-Session-Id": sid},
            )
            progress, outcome, error = parse_sse(response.text)
            trail = client.portal.call(store.load_trail, sid)
            session = client.portal.call(store.get_or_create_session, sid)
        counts = Counter(entry.tool_name for entry in trail)
        hydrations = [r for r in requests if r["operation"] == "hydrate"]
        checks = {
            "completed": response.status_code == 200
            and bool(outcome)
            and outcome["status"] == "done"
            and not error,
            "prepared_once": counts[NAME] == 1 and len(hydrations) == 1,
            "loaded_once": counts["getCapabilityTool"] == 1,
            "no_identity_query_or_clarification": not counts["runQuery"] and not counts["askUser"],
            "no_invented_date": bool(hydrations)
            and all(
                r["arguments"].get("payroll_totals_date") in (None, "null") for r in hydrations
            ),
            "option_shipped": bool(
                outcome
                and any(c.get("name") == NAME for c in outcome.get("capability_cards") or [])
            ),
            "unique_receipts": len({e.tool_call_id for e in trail}) == len(trail),
        }
        report = {
            "model": model,
            "session_id": sid,
            "fixture": "trace-based capability; live model, IdP and MCP",
            "checks": checks,
            "tool_counts": dict(counts),
            "capability_requests": requests,
            "outcome": outcome,
            "error": error,
            "progress": progress,
            "trail": [asdict(e) for e in trail],
            "review_states": session.review_states,
        }
        path = os.environ.get("PROBE_OUTPUT", "/tmp/capability-handoff-live.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream, indent=2, default=str)
        print(
            json.dumps(
                {
                    "checks": checks,
                    "tool_counts": dict(counts),
                    "answer": (outcome or {}).get("assistant_text"),
                    "report": path,
                },
                indent=2,
            )
        )
        return 0 if all(checks.values()) else 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
