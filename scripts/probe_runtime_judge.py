"""Evaluate synthetic v2 counterexamples against the real runtime judge prompt.

Run with PYTHONPATH=src. Requires the existing local model key; never saves it.
A skipped, malformed or failed-open review is a failed evaluation, not an approval.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from _e2e_harness import load_openai_key
from probe_runtime_acceptance import write_results

from data_agent.runtime.loop.answer_judge import AnswerJudge, JudgeBrief
from data_agent.runtime.model.openai_client import build_openai_model_client


async def main(args):
    client = build_openai_model_client(api_key=load_openai_key(), model=args.model, base_url='')
    judge = AnswerJudge(model_client=client, token_budget=32000)
    cases = json.loads(args.cases.read_text())
    results = []
    semaphore = asyncio.Semaphore(2)
    async def review(case):
        async with semaphore:
            verdict = await judge.review(JudgeBrief(**case['brief']))
            passed = (verdict.reviewed and verdict.approved == case['expected_approved']
                      and (verdict.approved or verdict.violation in case['acceptable_violations']))
            results.append({'name': case['name'], 'passed': passed, 'verdict': asdict(verdict)})
            print(f"{case['name']}: {'PASS' if passed else 'FAIL'}; reviewed={verdict.reviewed}, violation={verdict.violation}", flush=True)
    await asyncio.gather(*(review(case) for case in cases))
    write_results(args.output, {'model': args.model, 'results': results})
    return 0 if all(r['passed'] for r in results) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='gpt-5.5')
    parser.add_argument('--cases', type=Path, default=Path(__file__).resolve().parents[1] / 'tests/fixtures/runtime/remote_v2_judge_cases.json')
    parser.add_argument('--output', type=Path, required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
