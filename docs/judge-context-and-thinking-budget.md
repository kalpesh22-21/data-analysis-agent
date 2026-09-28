# Judge SQL context and optional thinking budget

The judge payload keeps each execution's SQL in `results[].execution.sql`.
Identical copies in that result's arguments and `queries_run` are omitted.
Selected components reference the execution through `result_id`; displayed tables
use that reference when the SQL identifies a unique execution. Unmatched proposed
SQL remains verbatim. Distinct executions and their previews remain available,
including earlier rejected calculations; query recency does not imply supersession.
Stored tool results, UI table SQL, and API provenance receipts are unchanged.

Catalog review retains table grain, access context, defaults and all authorized
rules, including predicates missing from the SQL. Optional notes, joins, measures
and ambiguities are selected by referenced columns. Rules and selected semantic
metadata can bring in needed column definitions; broad schema prose cannot pull in
unrelated columns. Existing scope filtering and explicit size-omission markers apply.

## Configuration

```sh
ANSWER_JUDGE_THINKING_BUDGET_ENABLED=false
ANSWER_JUDGE_THINKING_TOKEN_BUDGET=4096
```

The flag defaults to false: no provider reasoning parameter is sent. The token
budget must be nonnegative; zero requests no reasoning-token allowance through the
budget mechanism, subject to provider support. The value is ignored while disabled.

Enabling the flag builds a separate judge client, even when it uses the main
agent's model. Judge calls use Chat Completions and send
`extra_body={"thinking_token_budget": 4096}`. Agent and progress-summary clients
are unaffected. No completion-token cap is added; provider generation limits still
apply, and must allow space for the verdict after reasoning.

This requires a compatible vLLM version/runner, configured reasoning boundaries
and a proxy that forwards the field. A local Kimi probe with a 4096-token budget
returned an approved verdict in 9.68 seconds using 739 reasoning tokens. This verifies a successful request, but
not enforcement at the cap because the response stayed below it. An unsupported
parameter or incomplete verdict follows existing review-unavailable handling and cannot clear an explicit rejection. Existing judge
timeouts and aggregate wall-clock budgets remain in force.

Reference: https://github.com/vllm-project/vllm/blob/main/docs/features/reasoning_outputs.md
