#!/usr/bin/env python
"""Replay the Kimi conversation against a running real runtime; retain private SSE artifacts."""

import argparse
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import httpx
from _e2e_harness import mint_bound_token

QUESTIONS = (
    "How many employees do we have?",
    "Who has been here longest?",
    "Who has taken most leave this month?",
    "What about May?",
    "What different categories do we have?",
)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--continue-existing", action="store_true")
    parser.add_argument(
        "--question",
        action="append",
        help="Override the default questions; repeat for multiple turns.",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = args.output / "conversation.json"
    if path.exists() and not args.continue_existing:
        raise SystemExit("Use a new output directory or --continue-existing.")
    report = (
        json.loads(path.read_text())
        if path.exists()
        else {"session_id": "kimi-quality-" + uuid.uuid4().hex[:12], "exchanges": []}
    )
    questions = args.question or list(QUESTIONS)
    completed = [e for e in report["exchanges"] if e["mode"] == "turn"]
    if [e["input"] for e in completed] != questions[: len(completed)]:
        raise SystemExit("Existing questions do not match this probe.")
    if report["exchanges"] and report["exchanges"][-1].get("result", {}).get("status") not in {
        "done",
        "stopped_no_progress",
    }:
        raise SystemExit("Existing probe has an unresolved request; inspect it before continuing.")
    catalog = json.loads(Path("tests/fixtures/catalog_export.json").read_text())["catalog"]
    scope = [f"{table}.{column}" for table, data in catalog.items() for column in data["columns"]]

    def save():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream, indent=2)

    async with httpx.AsyncClient(timeout=httpx.Timeout(1200, connect=10)) as client:
        report["readiness"] = (await client.get(args.base_url + "/ready")).json()

        async def exchange(mode, message):
            token = await mint_bound_token(scope, report["session_id"])
            headers = {"Authorization": "Bearer " + token, "X-Session-Id": report["session_id"]}
            entry = {"mode": mode, "input": message, "events": []}
            report["exchanges"].append(entry)
            save()
            started = time.monotonic()
            print("Starting", len(report["exchanges"]), mode, message, flush=True)
            route = "/turn/resume" if mode == "resume" else "/turn"
            payload = {"answer" if mode == "resume" else "message": message}
            async with client.stream(
                "POST", args.base_url + route, headers=headers, json=payload
            ) as response:
                entry["http_status"] = response.status_code
                response.raise_for_status()
                event, data = "", []
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].strip())
                    elif not line and data:
                        value = json.loads("\n".join(data))
                        entry["events"].append(
                            {
                                "event": event,
                                "seconds": round(time.monotonic() - started, 2),
                                "data": value,
                            }
                        )
                        if event in {"result", "error"}:
                            entry[event] = value
                        event, data = "", []
                        save()
            entry["seconds"] = round(time.monotonic() - started, 2)
            history = await client.get(args.base_url + "/session/history", headers=headers)
            if history.status_code == 200:
                report["history"] = history.json()
            save()
            result = entry.get("result", {})
            print(
                "Finished",
                len(report["exchanges"]),
                entry["seconds"],
                result.get("status"),
                result.get("review"),
                flush=True,
            )
            if "error" in entry or not result:
                raise RuntimeError("Probe failed; see private artifact.")
            return result

        for question in questions[len(completed) :]:
            result = await exchange("turn", question)
            if result.get("status") == "paused_ask_user" and question == QUESTIONS[2]:
                result = await exchange(
                    "resume", "All types of time off, ranked by total approved hours."
                )
            if result.get("status") not in {"done", "stopped_no_progress"}:
                raise RuntimeError("Unexpected pause; inspect the artifact before continuing.")
    print("Saved", path, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
