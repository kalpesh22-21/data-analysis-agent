"""Opt-in semantic accuracy evaluation using synthetic evidence, never warehouse data.

PYTHONPATH=src python scripts/evaluate_post_execution_judge.py --key-file kimi_api_key.txt \
    --output /tmp/judge-evaluation.json
Key file: key, endpoint, model on separate lines. Otherwise use OPENAI_API_KEY,
OPENAI_BASE_URL and OPENAI_MODEL. Pacing is evaluation-only; default is zero.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from data_agent.runtime.loop.answer_judge import AnswerJudge, JudgeBrief
from data_agent.runtime.model.openai_client import build_openai_model_client
from data_agent.runtime.observability import tracing


def classify(expected, verdict):
    if not verdict.reviewed:
        return "unavailable"
    if verdict.approved == expected:
        return "correct"
    return "false_approval" if verdict.approved else "false_rejection"


async def main(args):
    key = os.environ.get("OPENAI_API_KEY", "")
    endpoint = os.environ.get("OPENAI_BASE_URL", "")
    model = os.environ.get("OPENAI_MODEL", "")
    if args.key_file:
        lines = args.key_file.read_text().splitlines()
        key, endpoint, model = (line.strip() for line in lines[:3])
    if not key or not model:
        raise SystemExit(
            "Configure a model and credentials; credentials are never written to the report."
        )
    client = build_openai_model_client(
        api_key=key,
        model=model,
        base_url=endpoint,
        tool_choice="auto",
        api_mode="chat",
        use_reasoning_metadata=True,
    )
    provider = tracing.configure_tracing(
        otlp_endpoint=args.otlp_endpoint,
        service_name="judge-evaluation",
        project_name="data-agent-runtime",
        hide_llm_content=False,
        drop_span_names=(),
    )
    tracing.instrument_openai(provider, hide_content=False)
    tracer = tracing.get_tracer(provider)
    judge = AnswerJudge(client, token_budget=32000, timeout_seconds=30, tracer=tracer)
    results = []
    for index, case in enumerate(json.loads(args.cases.read_text())):
        if index and args.pace_seconds:
            await asyncio.sleep(args.pace_seconds)
        with tracer.start_as_current_span("judge.evaluation") as span:
            span.set_attribute("case", case["name"])
            verdict = await judge.review(JudgeBrief(**case["brief"]))
            trace_id = format(span.get_span_context().trace_id, "032x")
        provider.force_flush()
        outcome = classify(case["expected_approved"], verdict)
        results.append(
            {
                "name": case["name"],
                "outcome": outcome,
                "trace_id": trace_id,
                "verdict": asdict(verdict),
            }
        )
        print(f"{case['name']}: {outcome}", flush=True)
        # Preserve partial results if an operator cancels an expensive evaluation.
        counts = dict(Counter(r["outcome"] for r in results))
        args.output.write_text(
            json.dumps({"model": model, "counts": counts, "results": results}, indent=2) + "\n"
        )
    provider.shutdown()
    return int(any(r["outcome"] != "correct" for r in results))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--otlp-endpoint", default="")
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "tests/fixtures/runtime/post_execution_judge_cases.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pace-seconds", type=float, default=0)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
