"""Probe conversational, table, capability and clarification delivery on the local runtime."""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from pathlib import Path

import httpx
from _e2e_harness import mint_bound_token
from _probe_transport import capture_turn
from probe_runtime_acceptance import SCOPE, write_results

CASES = {
    "conversation": "Hello!",
    "table": "How many active employees do we have by department?",
    "capability": "Take me to Position Management.",
    "clarification": "Before showing the active employee count for one department, ask me to choose Sales or Engineering.",
}


async def main(args):
    results = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=10)) as client:
        for name in args.cases:
            sid = "delivery-probe-" + uuid.uuid4().hex[:12]
            token = await mint_bound_token(SCOPE, sid)
            headers = {"Authorization": "Bearer " + token, "X-Session-Id": sid}
            started = time.monotonic()
            status, events, outcome, error = await capture_turn(
                client, args.base_url, headers=headers, message=CASES[name])
            document = (outcome or {}).get("answer_document") or {}
            sections = document.get("sections", [])
            checks = {
                "structured_result": status == 200 and bool(outcome) and not error,
                "review_approved": (outcome or {}).get("review", {}).get("status") == "approved",
                "no_dependency_failure": not (outcome or {}).get("failure"),
            }
            if name == "conversation":
                checks["text_only"] = bool(document.get("takeaway")) and not sections
            elif name == "table":
                checks["table_delivered"] = any(s.get("presentation", {}).get("kind") == "table" for s in sections)
            elif name == "capability":
                checks["card_delivered"] = any(s.get("actions") for s in sections)
            else:
                checks["question_delivered"] = (outcome or {}).get("status") == "paused_ask_user"
            history = (await client.get(args.base_url.rstrip("/") + "/session/history", headers=headers)).json()
            if outcome and outcome.get("status") == "done":
                turns = history.get("turns", [])
                checks["history_parity"] = (
                    bool(turns)
                    and turns[-1].get("answer_document") == outcome.get("answer_document")
                    and turns[-1].get("review") == outcome.get("review")
                )
            results.append(
                {
                    "case": name,
                    "session_id": sid,
                    "seconds": round(time.monotonic() - started, 2),
                    "checks": checks,
                    "result": outcome,
                    "http_status": status,
                    "sse_events": events,
                    "error": error,
                    "history": history,
                }
            )
            write_results(args.output, {"results": results})
            print(f"{name}: {'PASS' if all(checks.values()) else 'FAIL'} {checks}", flush=True)
    return 0 if all(all(r["checks"].values()) for r in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    raise SystemExit(asyncio.run(main(parser.parse_args())))
