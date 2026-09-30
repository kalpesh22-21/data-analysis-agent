"""Diagnose a local blueprint through the real runtime executor and live MCP."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import yaml

from data_agent.corpus.seeds import BlueprintSeed
from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.compiler import (
    resolve_blueprint_references,
    validate_blueprint_dag,
    validate_blueprint_uses,
)
from data_agent.runtime.blueprint.executor import BlueprintExecutor, ExecCompleted, ExecPaused
from data_agent.runtime.catalog.export_client import HttpCatalogClient
from data_agent.runtime.composite.resolve_values import ResolveValuesComposite
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.real_client import RealMCPClient
from data_agent.runtime.mcp.scratch_client import ScratchClient
from data_agent.runtime.provenance.catalog_handle import load_catalog_handles_from_export
from data_agent.runtime.retrieval.models import BlueprintDetail


def load_blueprint(path: Path, corpus_dir: Path | None = None) -> BlueprintDetail:
    def read(file):
        raw = yaml.safe_load(file.read_text())
        if not isinstance(raw, dict):
            raise ValueError(f"Expected one blueprint object in {file}")
        names = {f.name for f in fields(BlueprintSeed)}
        return BlueprintSeed(**{k: v for k, v in raw.items() if k in names})

    target = read(path)
    seeds = [target]
    if corpus_dir:
        seeds.extend(
            read(p)
            for p in sorted(corpus_dir.rglob("*"))
            if p.suffix in {".yaml", ".yml", ".json"} and p.resolve() != path.resolve()
        )
    resolved = resolve_blueprint_references(seeds)
    seed = next(b for b in resolved if b.id == target.id)
    validate_blueprint_dag(seed)
    validate_blueprint_uses(seed)
    values = {
        f.name: getattr(seed, f.name) for f in fields(BlueprintDetail) if hasattr(seed, f.name)
    }
    values.update(uses=frozenset(seed.uses), hit_count=0)
    return BlueprintDetail(**values)


class RecordingDispatcher(ToolDispatcher):
    def __init__(self, *args, calls, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = calls
        self.phase = "execution"

    async def dispatch(self, tool_name, model_args, credentials, **kwargs):
        call = {
            "number": len(self.calls) + 1,
            "tool": tool_name,
            "phase": self.phase,
            "arguments": model_args,
            "status": "running",
        }
        self.calls.append(call)
        started = time.monotonic()
        try:
            result = await super().dispatch(tool_name, model_args, credentials, **kwargs)
            call.update(
                status=result.status,
                error_code=result.error_code,
                message=result.user_message,
                denial_detail=result.denial_detail,
            )
            if result.result_preview:
                preview = result.result_preview
                call.update(
                    columns=preview.columns,
                    row_count=preview.row_count,
                    truncated=preview.truncated,
                )
            return result
        except BaseException as exc:
            call.update(
                status="interrupted" if isinstance(exc, asyncio.CancelledError) else "error",
                exception=type(exc).__name__,
            )
            raise
        finally:
            call["seconds"] = round(time.monotonic() - started, 3)


async def diagnose(args, token, report):
    report["stage"] = "blueprint_validation"
    detail = load_blueprint(args.blueprint, args.corpus_dir)
    report["blueprint_id"] = detail.id
    bindings = json.loads(args.bindings_file.read_text() if args.bindings_file else args.bindings)
    if not isinstance(bindings, dict):
        raise ValueError("Bindings must be a JSON object")
    report["bindings"] = bindings
    report["required_slots"] = [s["name"] for s in (detail.slots or []) if s.get("required")]
    report["stage"] = "catalog"
    base = args.mcp.rstrip("/").removesuffix("/mcp")
    creds = RuntimeCredentials(
        args.session_id or "blueprint-probe-" + uuid.uuid4().hex, token, frozenset()
    )
    # The API enforces authorization. No unverified JWT claims grant local access.
    export = await HttpCatalogClient(args.catalog_url or base + "/catalog").fetch_export(
        jwt=token, session_id=creds.session_id
    )
    catalog, semantic = load_catalog_handles_from_export(export)
    report["catalog_sha"] = export.get("catalog_sha")
    report["stage"] = "mcp_connection"
    client = RealMCPClient(args.mcp)
    tools = await client.list_tools(jwt=token, session_id=creds.session_id)
    report["available_tools"] = [t.name for t in tools]
    dispatcher = RecordingDispatcher(client, catalog, calls=report["calls"])
    resolver = ResolveValuesComposite(tool_dispatcher=dispatcher, catalog=catalog)

    async def get_blueprint(bid):
        return detail if bid == detail.id else None

    def observe(event, shape):
        if event == "blueprint_step":
            dispatcher.phase = shape["step"]
            report["stage"] = shape["step"]
        report.setdefault("events", []).append({"event": event, **shape})

    executor = BlueprintExecutor(
        tool_dispatcher=dispatcher,
        vector_index=SimpleNamespace(get_blueprint=get_blueprint),
        semantic_catalog=semantic,
        resolve_values=resolver,
        observer=observe,
        scratch_client=ScratchClient(base + "/scratch/v1") if args.enable_scratch else None,
    )
    report["stage"] = "execution"
    report["limitations"] = [
        "No LLM, answer judge, retrieval index, or clarification-resume flow is exercised.",
        "resolveValues uses runtime lexical fallback without an embedding service.",
        "Scratch materialization is enabled."
        if args.enable_scratch
        else "Scratch materialization is disabled; table-intermediate DAGs may report unsupported.",
        "A completed execution verifies runtime checks, not business correctness of the answer.",
    ]
    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings=bindings, credentials=creds
    )
    if isinstance(outcome, ExecCompleted):
        report.update(
            status="completed",
            row_count=outcome.preview.row_count,
            columns=outcome.preview.columns,
            truncated=outcome.preview.truncated,
        )
        return 0
    if isinstance(outcome, ExecPaused):
        report.update(
            status="paused",
            reason=outcome.reason,
            question=outcome.pending_question,
            awaiting_node=outcome.awaiting_node,
        )
        return 2
    report.update(
        status="failed",
        error_code=outcome.error_code,
        message=outcome.user_message,
        retryable=outcome.retryable,
    )
    return 1


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--blueprint", type=Path, required=True, help="One blueprint YAML/JSON file")
    p.add_argument(
        "--corpus-dir", type=Path, help="Blueprint directory for composes.ref dependencies"
    )
    p.add_argument(
        "--mcp", required=True, help="Streamable HTTP endpoint, e.g. http://localhost:18090/mcp"
    )
    p.add_argument("--catalog-url", help="Catalog base URL (defaults to MCP base + /catalog)")
    auth = p.add_mutually_exclusive_group()
    auth.add_argument("--token", help="Bearer token; alternatively set MCP_TOKEN")
    auth.add_argument("--token-file", type=Path)
    p.add_argument("--session-id", help="Use the session associated with a session-bound token")
    slots = p.add_mutually_exclusive_group()
    slots.add_argument("--bindings", default="{}", help="JSON slot bindings")
    slots.add_argument("--bindings-file", type=Path, help="JSON file of slot bindings")
    p.add_argument("--timeout", type=float, default=180, help="Total deadline in seconds")
    p.add_argument(
        "--enable-scratch",
        action="store_true",
        help="Allow runtime temporary table materialization",
    )
    p.add_argument("--output", type=Path, help="Also write the diagnostic JSON to this file")
    return p


def main():
    p = parser()
    args = p.parse_args()
    token = (
        args.token_file.read_text()
        if args.token_file
        else args.token or os.environ.get("MCP_TOKEN", "")
    ).strip()
    if token.startswith("Bearer "):
        token = token[7:].strip()
    if not token:
        p.error("Provide --token, --token-file, or MCP_TOKEN")
    if args.timeout <= 0:
        p.error("--timeout must be positive")
    report = {"status": "failed", "stage": "setup", "calls": []}

    async def run():
        async with asyncio.timeout(args.timeout):
            return await diagnose(args, token, report)

    try:
        code = asyncio.run(run())
    except Exception as exc:
        report.update(status="failed", exception=type(exc).__name__, message=str(exc))
        code = 1
    # Keep actual SQL/errors for diagnosis, but never emit the supplied credential.
    rendered = json.dumps(report, indent=2, default=str).replace(token, "[TOKEN REMOVED]")
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
