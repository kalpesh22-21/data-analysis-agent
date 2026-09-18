# Using a vLLM model server

The runtime can use vLLM through the existing OpenAI-compatible client. Set:

```dotenv
OPENAI_BASE_URL=http://localhost:8000/v1
OPENAI_MODEL=your-served-model-name
OPENAI_API_KEY=your-server-api-key
MODEL_API=chat
MODEL_TOOL_CHOICE=auto
USE_REASONING_METADATA=false
```

Use the model name exposed by your server. If the server has no authentication,
a nonempty placeholder such as `local` satisfies the SDK's key requirement.
`localhost` must be reachable from the runtime process; use the server's service
address when running in separate containers.

`MODEL_API=chat` sends requests directly to `/chat/completions`, independently
of reasoning replay. The default `auto` preserves the existing Responses-first
behavior and Chat fallback; enabling reasoning replay also selects Chat as before.
The protocol setting applies to the main, judge and progress-summary clients.

For `MODEL_TOOL_CHOICE=auto`, configure vLLM with `--enable-auto-tool-choice`
and the model's supported `--tool-call-parser`. The model must support tool
calling and its chat template must render tool messages. Parser and template
choices depend on the model and installed vLLM version; follow the
[official tool-calling guide](https://docs.vllm.ai/en/stable/features/tool_calling/).
The runtime expects structured `tool_calls`, not tool markup embedded in prose.

Enable `USE_REASONING_METADATA=true` only when the served model expects reasoning
fields to be replayed. Configure the server's reasoning parser as appropriate.
The transport flag does not enable or disable model thinking.

If progress summaries are enabled, set `OPENAI_SUMMARY_MODEL` to a model available
on the configured summary endpoint. An empty `ANSWER_JUDGE_MODEL` reuses the main
client; any explicitly configured judge model must also be served. Match
`MODEL_CONTEXT_WINDOW` and `RESPONSE_TOKEN_RESERVE` to your deployment's limits.

Validation uses the real OpenAI SDK against a mock HTTP endpoint to check direct
Chat routing, tool-call parsing, result replay, optional reasoning replay and
per-turn configuration. No live vLLM server has been tested in this checkout.
