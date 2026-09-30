# Blueprint diagnostic script

Run from the data-analysis-agent repository with its dependencies installed. This uses the **real runtime blueprint compiler, executor, slot resolver, dispatcher, and API provenance checks**, with a local blueprint file and live MCP. It needs neither the agent backend nor an LLM.

```bash
export MCP_TOKEN='your-token'
uv run python scripts/probe_blueprint.py \
  --blueprint /path/to/bp-employee-check-detail-for-period.yaml \
  --mcp http://localhost:18090/mcp \
  --bindings '{"employee":"A4I8","period":"2026-08-08"}' \
  --output /tmp/blueprint-report.json
```

Alternatively use `--token-file /path/to/token.txt` or `--token 'your-token'`. For a session-bound token, supply its matching `--session-id`. The API remains the authorization boundary. The supplied token is removed from the printed/saved JSON.

Additional options:

- `--bindings-file slots.json`: supply slot bindings from a JSON object file. Missing bindings may legitimately produce a pause; they are never invented.
- `--corpus-dir /path/to/blueprints`: load dependencies for a blueprint with `composes.ref`, using the production reference compiler.
- `--catalog-url https://host/catalog`: override the catalog base URL (the script appends `/export`). By default this uses the MCP URL with its trailing `/mcp` replaced by `/catalog`.
- `--timeout 180`: total deadline across catalog loading, MCP connection, and execution.
- `--enable-scratch`: enable the API's temporary scratch tables for blueprints with table intermediates. Disabled by default.

## Reading the report

The JSON includes the last runtime stage, every internal SQL call with its runtime phase, duration, status, actual API error/denial detail, and result column names/counts. Successful row payloads are omitted. SQL literals, bindings, and clarification options remain visible for diagnosis.

| Result | Exit code | Meaning |
|---|---|---|
| `completed` | 0 | Template execution and runtime verification completed; an empty result is still a completed execution. |
| `failed` | 1 | Inspect `stage`, `error_code`, `message`, and `calls` for the failure. |
| `paused` | 2 | The executor needs clarification or approval; inspect `reason` and `question`. This is not reported as successful completion. |

A failure in `blueprint_validation` points to local authoring, reference resolution, or compilation. `catalog`/`mcp_connection` failures indicate connection, authentication, or service setup. The executor emits `resolving_slots`, `resolving_rule`, `executing_node`, `verifying`, and `materializing` phases. These identify **where** execution failed; the error and SQL identify the cause. A failed query is not automatically proof the blueprint is wrong.

For the employee/period regression, expect targeted slot-verification queries followed by the bound template query with its WHERE clause and GROUP BY. A report containing only slot probes plus `paused` means the template never ran.

Limitations: this exercises the file you supplied, not the deployed retrieval-index copy. It does not test the LLM, answer judge, or clarification resume flow. Dynamic value resolution uses the runtime's lexical fallback without embeddings and reports degradation normally. Completion does not establish business correctness of the answer.
