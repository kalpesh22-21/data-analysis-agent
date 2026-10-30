"""Opt-in live-model answer-contract battery through the real HTTP/SSE app.

Uses the local ClickHouse MCP/token services, live retrieval, real model/judge
calls and Phoenix export. Faults are injected only at test seams and labelled
in traces. This is not an Ember browser test. Run with PYTHONPATH=src.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import time
import uuid
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
import openai
import uvicorn
from _e2e_harness import tenant_claims, token_issuer_api_key, token_service_url
from openinference.instrumentation.openai import OpenAIInstrumentor
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime import app as app_module
from data_agent.runtime import composer as composer_module
from data_agent.runtime.app import create_app
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.loop.answer_judge import AnswerJudge, JudgeVerdict
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.real_client import RealMCPClient
from data_agent.runtime.model.client import begin_turn_client
from data_agent.runtime.model.openai_client import build_openai_model_client
from data_agent.runtime.session.memory_store import InMemorySessionStore

CASES = {
    "scalar": ("How many active employees do we have?", ""),
    "comparison": ("How many active employees do we have by department?", ""),
    "records": (
        "Show the top three employees by annual salary, with their names and departments.",
        "",
    ),
    "trend": (
        "Show total payroll amount by month from January through June 2026 as a line chart.",
        "",
    ),
    "separate_tables": (
        "Show active employee counts for Sales and Engineering as separate tables.",
        "",
    ),
    "presentation_repair": (
        "Show active employee counts by department as a bar chart.",
        "judge_reject",
    ),
    "invalid_source_repair": (
        "Show active employee counts by department as a bar chart.",
        "invalid_source",
    ),
    "judge_timeout_salvage": (
        "How many active employees do we have by department?",
        "judge_timeout",
    ),
    "judge_and_salvage_unavailable": (
        "How many active employees do we have by department?",
        "all_reviews_fail",
    ),
    "agent_failure_salvage": (
        "How many active employees do we have by department?",
        "agent_failure",
    ),
    "partial_answer": (
        "Show active employee counts by department and total payroll by month for January through June 2026. If one is unavailable, still show the other.",
        "payroll_denied",
    ),
    "clarification": (
        "Before showing active employee count for one department, ask me to choose Sales or Engineering.",
        "",
    ),
    "clarification_repair": (
        "Before showing active employee count for one department, ask me to choose Sales or Engineering.",
        "question_reject",
    ),
    "empty_result": ("List employees whose hire date is January 1, 2099.", ""),
    "conversation": ("Hello!", ""),
}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2, default=str)


class Warehouse(RealMCPClient):
    def __init__(self, state, url):
        super().__init__(url)
        self.state = state

    async def call_tool(self, tool_name, args, **kwargs):
        sql = args.get("sql", "")
        if (
            tool_name == "runQuery"
            and self.state["fault"] == "payroll_denied"
            and "payroll" in sql.lower()
        ):
            self.state["denials"] += 1
            trace.get_current_span().add_event(
                "battery.injected_failure", {"fault": "payroll_denied"}
            )
            raise MCPToolError(
                "COLUMN_SCOPE_VIOLATION", "Payroll records are unavailable to this session."
            )
        try:
            result = await super().call_tool(tool_name, args, **kwargs)
        except Exception as exc:
            self.state.setdefault("query_errors", []).append(
                {"tool": tool_name, "sql": sql, "message": str(exc)}
            )
            raise
        if tool_name == "runQuery":
            self.state["queries"].append({"sql": sql, "result": result})
        return result


class RecordingModel:
    def __init__(self, inner, state):
        self.inner, self.state = inner, state
        self._model = getattr(inner, "_model", "")

    def begin_turn(self):
        return RecordingModel(begin_turn_client(self.inner), self.state)

    def without_thinking(self, kwargs):
        return RecordingModel(self.inner.without_thinking(kwargs), self.state)

    async def send_turn(self, messages, tools):
        names = {t["name"] for t in tools}
        agent = "finalizeAnswer" in names
        if agent and self.state["fault"] == "agent_failure" and self.state["queries"]:
            self.state["injections"].append("agent_failure")
            trace.get_current_span().add_event(
                "battery.injected_failure", {"fault": "agent_failure"}
            )
            raise openai.APIConnectionError(
                request=httpx.Request("POST", "https://model.invalid/evaluation")
            )
        result = await self.inner.send_turn(messages, tools)
        self.state["model_calls"].append(
            {
                "role": "agent" if agent else "review",
                "calls": [{"name": c.name, "arguments": c.arguments} for c in result.tool_calls],
                "text": result.assistant_text,
                "usage": result.usage,
            }
        )
        if (
            agent
            and self.state["fault"] == "invalid_source"
            and "invalid_source" not in self.state["injections"]
        ):
            calls = []
            for call in result.tool_calls:
                if call.name == "finalizeAnswer" and call.arguments.get("sections"):
                    args = deepcopy(call.arguments)
                    section = next(
                        (s for s in args["sections"] if s.get("presentation", {}).get("kind") in {"figure", "facts", "table", "bars", "line"}), None
                    )
                    if section:
                        section["presentation"]["result_id"] = "missing-evaluation-result"
                        self.state["injections"].append("invalid_source")
                        self.state["queries_at_repair"] = len(self.state["queries"])
                        trace.get_current_span().add_event(
                            "battery.injected_failure", {"fault": "invalid_source"}
                        )
                        call = replace(call, arguments=args)
                calls.append(call)
            result = replace(result, tool_calls=calls)
        return result


def judge_factory(state):
    class FaultJudge(AnswerJudge):
        async def _review(self, brief, span):
            state["reviews"].append({"site": brief.site, "document": brief.answer_document})
            fault = state["fault"]
            inject = (
                fault in {"judge_timeout", "all_reviews_fail", "salvage_chart_type_unsupported"}
                or fault == "judge_reject"
                and "judge_reject" not in state["injections"]
                and brief.site != "ask_user"
                or fault == "question_reject"
                and "question_reject" not in state["injections"]
                and brief.site == "ask_user"
            )
            if inject:
                state["injections"].append(fault)
                trace.get_current_span().add_event("battery.injected_failure", {"fault": fault})
                if span:
                    span.set_attribute("battery.injected_failure", fault)
                state["queries_at_repair"] = len(state["queries"])
                if fault in {"judge_timeout", "all_reviews_fail", "salvage_chart_type_unsupported"}:
                    return JudgeVerdict(False, reviewed=False, failure_reason="timeout")
                return JudgeVerdict(
                    False,
                    violation="answer_not_grounded"
                    if brief.site != "ask_user"
                    else "unnecessary_clarification",
                    feedback="Keep the same results. Include the measure and accessible population clearly in the section title; do not run another query."
                    if brief.site != "ask_user"
                    else "Ask one concise department-selection question with exactly Sales and Engineering as options, as the user requested.",
                    reviewed=True,
                    repair_type="presentation" if brief.site != "ask_user" else "prose",
                )
            return await super()._review(brief, span)

        async def approve_presentation_fallback(self, brief):
            state.setdefault("fallback_reviews", []).append(brief.answer_document)
            return await super().approve_presentation_fallback(brief)

        async def salvage(self, brief):
            state["salvage_calls"] += 1
            if state["fault"] == "all_reviews_fail":
                trace.get_current_span().add_event(
                    "battery.injected_failure", {"fault": "salvage_unavailable"}
                )
                raise TimeoutError("Injected salvage timeout")
            response = await super().salvage(brief)
            state["salvage_results"].append(response)
            return response

    return FaultJudge


def events(body):
    frames = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        kind = next(
            (line[6:].strip() for line in block.splitlines() if line.startswith("event:")), None
        )
        data = "\n".join(
            line[5:].strip() for line in block.splitlines() if line.startswith("data:")
        )
        if data:
            frames.append({"event": kind, "data": json.loads(data)})
    return frames


async def run_case(name, question, fault, settings, *, resume_answer=None):
    # Each sequential case owns a fresh app/provider. Do not leave the global SDK
    # instrumentor attached to the preceding case's shut-down exporter.
    instrumentor = OpenAIInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    state = {
        "fault": fault,
        "queries": [],
        "model_calls": [],
        "reviews": [],
        "injections": [],
        "salvage_calls": 0,
        "salvage_results": [],
        "denials": 0,
        "events": [],
    }
    store = InMemorySessionStore()
    exporter = InMemorySpanExporter()
    model = RecordingModel(
        build_openai_model_client(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
            api_mode=settings.model_api,
            use_reasoning_metadata=settings.use_reasoning_metadata,
        ),
        state,
    )

    def observe(event, payload):
        state["events"].append({"name": event, "payload": deepcopy(payload)})
        span = trace.get_current_span()
        span.set_attribute("battery.case", name)
        span.set_attribute("battery.fault", fault or "none")

    sid = "answer-battery-" + name + "-" + uuid.uuid4().hex[:8]
    async with httpx.AsyncClient(timeout=10) as auth_client:
        auth_response = await auth_client.post(
            token_service_url(),
            headers={"Authorization": "Bearer " + token_issuer_api_key()},
            json={"user_name": "alice", "session_id": sid, "claims": tenant_claims().as_claims()},
        )
        auth_response.raise_for_status()
        token = auth_response.json()["access_token"]
    original_visual = composer_module._visual

    def visual_with_fault(*args, **kwargs):
        if fault == "chart_type_unsupported" or (
            fault == "salvage_chart_type_unsupported" and state["salvage_calls"] > 0
        ):
            state["injections"].append("unsupported_chart_datatype")
            trace.get_current_span().add_event("battery.injected_failure", {"fault": "unsupported_chart_datatype"})
            raise composer_module.PresentationError("The chart measure has an unsupported datatype; use its table.")
        return original_visual(*args, **kwargs)

    with patch.object(app_module, "AnswerJudge", judge_factory(state)), patch.object(
        composer_module, "_visual", visual_with_fault,
    ):
        warehouse = Warehouse(state, settings.mcp_url)
        app = create_app(
            settings=settings,
            session_store=store,
            mcp_client=warehouse,
            model_client=model,
            span_exporter=exporter,
            extra_observers=(observe,),
        )
        exchanges = []
        started = time.monotonic()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        while not server.started:
            if serving.done():
                await serving
                raise RuntimeError("HTTP listener did not start")
            await asyncio.sleep(0.05)
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=240
            ) as client:
                headers = {"Authorization": "Bearer " + token, "X-Session-Id": sid}

                async def exchange(path, payload):
                    response = await client.post(path, json=payload, headers=headers)
                    frames = events(response.text)
                    exchanges.append(
                        {
                            "path": path,
                            "http_status": response.status_code,
                            "http_error": response.text if response.status_code != 200 else None,
                            "events": frames,
                            "heartbeats": response.text.count(": heartbeat"),
                        }
                    )
                    return next((f["data"] for f in frames if f["event"] == "result"), {})

                outcome = await exchange("/turn", {"message": question})
                if outcome.get("status") == "paused_ask_user" and resume_answer is not None:
                    outcome = await exchange("/turn/resume", {"answer": resume_answer})
                if name == "scalar" and outcome.get("status") == "done":
                    outcome = await exchange(
                        "/turn", {"message": "And how many of those are in Sales?"}
                    )
                if name in {"comparison", "presentation_repair", "invalid_source_repair", "judge_timeout_salvage", "agent_failure_salvage", "partial_answer", "partial_current_counts", "chart_timeout_salvage"}:
                    oracle = RealMCPClient(settings.mcp_url)
                    oracle.scope_token_provider = warehouse.scope_token_provider
                    state["truth"] = await oracle.call_tool(
                        "runQuery",
                        {"sql": "SELECT department_name, count() AS employee_count FROM dbpcm_warehouse.employee WHERE employee_status = 'Active' GROUP BY department_name ORDER BY department_name"},
                        jwt=token, session_id=sid,
                    )
        finally:
            server.should_exit = True
            await serving
            listener.close()
        doc = await store.get_or_create_session(sid)
    spans = exporter.get_finished_spans()
    roots = [s for s in spans if s.name == "agent.turn"]
    ids = {s.context.span_id for s in spans}
    root_ids = {s.context.trace_id for s in roots}
    emitted = [s for s in spans if s.name == "loop_response_emitted"]
    result_frames = [f for x in exchanges for f in x["events"] if f["event"] == "result"]
    checks = {
        "one_terminal_result_per_request": len(result_frames) == len(exchanges)
        and all(sum(f["event"] == "result" for f in x["events"]) == 1 for x in exchanges),
        "no_sse_errors": not any(f["event"] == "error" for x in exchanges for f in x["events"]),
        "no_parallel_ui_fields": all(
            not {"assistant_text", "answer_tables", "blocks", "capability_cards"} & f["data"].keys()
            for f in result_frames
        ),
        "terminal_payload": all(
            bool(f["data"].get("pending_question"))
            if f["data"].get("status", "").startswith("paused")
            else bool(f["data"].get("answer_document", {}).get("takeaway"))
            for f in result_frames
        ),
        "persisted_document": bool(outcome.get("answer_document"))
        and any(m.answer_document == outcome["answer_document"] for m in doc.messages),
        "no_speculative_progress": not any(
            "finaliz" in f["data"].get("message", "").lower()
            for x in exchanges
            for f in x["events"]
            if f["event"] == "progress"
        ),
        "traced_turns": len(roots) == len(exchanges),
        "connected_trace_parents": all(not s.parent or s.parent.span_id in ids for s in spans),
        "delivery_in_turn_trace": len(emitted) == len(exchanges)
        and all(s.context.trace_id in root_ids for s in emitted),
        "model_spans_present": any(s.name in {"Response", "ChatCompletion"} for s in spans),
    }
    if fault in {"judge_reject", "invalid_source", "question_reject"}:
        checks["fault_exercised"] = fault in state["injections"]
        checks["approved_after_repair"] = outcome.get("review", {}).get("status") == "approved"
    if fault in {"judge_reject", "invalid_source"}:
        checks["repair_reuses_queries"] = len(state["queries"]) == state.get(
            "queries_at_repair", -1
        )
    if fault in {"judge_timeout", "all_reviews_fail", "agent_failure"}:
        checks["salvage_exercised"] = state["salvage_calls"] > 0
    if fault == "all_reviews_fail":
        checks["safe_limitation"] = outcome.get("review", {}).get(
            "status"
        ) == "exhausted" and not outcome.get("answer_document", {}).get("sections")
    if name.startswith("clarification"):
        checks["question_then_resume"] = (
            len(exchanges) == 2
            and result_frames[0]["data"].get("status") == "paused_ask_user"
            and outcome.get("status") == "done"
        )
    if fault == "payroll_denied":
        checks["missing_part_exercised"] = state["denials"] > 0
        checks["retained_supported_answer"] = bool(
            outcome.get("answer_document", {}).get("sections")
        ) and bool(state["queries"])
    document = outcome.get("answer_document", {})
    kinds = [section["presentation"]["kind"] for section in document.get("sections", [])]
    if name in {
        "scalar",
        "comparison",
        "records",
        "trend",
        "separate_tables",
        "presentation_repair",
        "invalid_source_repair",
    }:
        checks["approved_answer"] = outcome.get("review", {}).get("status") == "approved"
        checks["data_presentation_delivered"] = any(kind != "text" for kind in kinds)
    if name == "trend":
        checks["requested_line_delivered"] = "line" in kinds
    if name in {"presentation_repair", "invalid_source_repair"}:
        checks["requested_bars_delivered"] = "bars" in kinds
    if name == "separate_tables":
        checks["two_requested_tables"] = kinds.count("table") == 2
    if fault in {"judge_timeout", "agent_failure"}:
        checks["salvage_succeeded"] = (
            bool(state["salvage_results"]) and outcome.get("review", {}).get("status") == "approved"
        )
    if fault in {"chart_type_unsupported", "salvage_chart_type_unsupported"}:
        checks["datatype_fallback_exercised"] = "unsupported_chart_datatype" in state["injections"]
        checks["fallback_delivered_with_reason"] = "table" in kinds and any(
            "unsupported datatype" in caveat
            for section in document.get("sections", []) for caveat in section["caveats"]
        )
        reviewed_documents = state.get("fallback_reviews", []) if fault.startswith("salvage") else [r["document"] for r in state["reviews"]]
        checks["exact_fallback_reviewed"] = any(
            reviewed and reviewed["sections"] == document.get("sections") for reviewed in reviewed_documents
        )
    if "truth" in state:
        truth = state["truth"]
        truth_rows = truth.get("rows", truth.get("data", []))
        state["truth_rows"] = truth_rows
        delivered_ids = set(document.get("sources", {}))
        delivered = [e for e in doc.tool_trail if e.tool_call_id in delivered_ids]
        expected = sorted(tuple(str(v) for v in row) for row in truth_rows)
        observed = []
        for entry in delivered:
            preview = entry.to_doc().get("result_preview") or {}
            columns = preview.get("columns", [])
            if "department" in columns and "headcount" in columns:
                observed.extend((str(row[columns.index("department")]), str(row[columns.index("headcount")])) for row in preview.get("preview_rows", []))
        checks["independent_scoped_counts_match"] = bool(expected) and sorted(observed) == expected
    return {
        "case": name,
        "question": question,
        "session_id": sid,
        "seconds": round(time.monotonic() - started, 2),
        "checks": checks,
        "exchanges": exchanges,
        "state": state,
        "traces": [format(s.context.trace_id, "032x") for s in roots],
        "trail": [entry.to_doc() for entry in doc.tool_trail],
    }


async def main(args):
    base = RuntimeSettings()
    config = dict(
        _env_file=None,
        openai_api_key=base.openai_api_key,
        openai_model=args.model or base.openai_model,
        openai_base_url=base.openai_base_url,
        model_api=base.model_api,
        answer_judge_enabled=True,
        answer_judge_timeout_seconds=30,
        progress_summary_enabled=False,
        max_loop_iterations=18,
        max_wall_clock_seconds=180,
        max_budget_windows=2,
        otlp_endpoint=args.phoenix + "/v1/traces",
        otlp_project_name="answer-document-battery",
        otlp_drop_span_names=[],
        otlp_disable_redaction=True,
        mcp_url="http://localhost:18090/mcp",
        mcp_service_key="test-key-abc123",
        jwks_url="http://localhost:19000/.well-known/jwks.json",
        jwt_issuer="http://token:8000/",
        jwt_audience="clickhouse-api",
        catalog_source="mcp",
        neo4j_url="bolt://localhost:7687",
        neo4j_username="neo4j",
        neo4j_password="testpassword",
        embedding_api_url="http://localhost:18003/embed",
        embedding_model="all-mpnet-base-v2",
        embedding_dimension=768,
        reranker_api_url="http://localhost:18004/rerank",
    )
    if args.env_file:
        from dotenv import dotenv_values
        values = dotenv_values(args.env_file)
        key = values.get("FIREWORKS_API_KEY") or values.get("OPENAI_API_KEY")
        if not key or not args.base_url or not args.model:
            raise ValueError("--env-file requires an API key, --base-url and --model")
        config.update(openai_api_key=key, openai_base_url=args.base_url,
                      model_api="chat", use_reasoning_metadata=True)
    if args.key_file:
        key, endpoint, model = [v.strip() for v in args.key_file.read_text().splitlines()[:3]]
        config.update(
            openai_api_key=key,
            openai_base_url=endpoint,
            openai_model=args.model or model,
            model_api="chat",
            use_reasoning_metadata=True,
        )
    settings = RuntimeSettings(**config)
    if args.hydrate:
        from data_agent.runtime.retrieval.hydrator import build_hydrator
        hydrator = build_hydrator(settings)
        if hydrator is None:
            raise RuntimeError("Hydrator unavailable")
        try:
            await hydrator.run_once()
        finally:
            await hydrator.close()
    report = {
        "model": settings.openai_model,
        "phoenix": args.phoenix,
        "project": "answer-document-battery",
        "scope": "Live model/judge; real runtime HTTP/SSE, local ClickHouse MCP/token, retrieval and catalog; fault injection at dependency seams; no Ember/browser.",
        "results": [],
    }
    cases = {**CASES}
    question_specs = {}
    if args.questions:
        for item in json.loads(args.questions.read_text()):
            question_specs[item["name"]] = item
            cases[item["name"]] = (item["question"], item.get("fault", ""))
    for name in args.cases or cases:
        question, fault = cases[name]
        print("START", name, flush=True)
        try:
            result = await run_case(name, question, fault, settings, resume_answer=question_specs.get(name, {}).get("resume_answer", "Sales" if name.startswith("clarification") else None))
        except Exception as exc:
            result = {
                "case": name,
                "checks": {"completed": False},
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        spec = question_specs.get(name, {})
        if spec.get("source"):
            result["question_source"] = spec["source"]
        if spec.get("expect_data"):
            final_frames = [f["data"] for x in result.get("exchanges", []) for f in x["events"] if f["event"] == "result"]
            final = final_frames[-1] if final_frames else {}
            result["checks"]["requested_data_delivered"] = any(
                s.get("presentation", {}).get("kind") != "text"
                for s in final.get("answer_document", {}).get("sections", [])
            )
            result["checks"]["approved_answer"] = final.get("review", {}).get("status") == "approved"
        if spec.get("expected_kind"):
            frames = [f["data"] for x in result.get("exchanges", []) for f in x["events"] if f["event"] == "result"]
            document = frames[-1].get("answer_document", {}) if frames else {}
            result["checks"]["requested_presentation_preserved"] = any(
                section.get("presentation", {}).get("kind") == spec["expected_kind"]
                for section in document.get("sections", [])
            )
        report["results"].append(result)
        save(args.output, report)
        failed = [k for k, v in result["checks"].items() if not v]
        print(
            name,
            "PASS" if not failed else "FAIL " + ",".join(failed),
            result.get("seconds"),
            flush=True,
        )
    return int(any(not all(r["checks"].values()) for r in report["results"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hydrate", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--questions", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--phoenix", default="http://localhost:6006")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
