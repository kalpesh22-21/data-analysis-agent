# Judge timeout recovery

A timed-out judge review gets one retry on the same brief and verdict schema,
with thinking disabled at the vLLM request level. Normal requests retain their
existing reasoning configuration. This is a new inference request, not a continuation
of the timed-out generation. It cannot guarantee a response if provider queueing
or prompt processing is the bottleneck.

Configuration:

- `ANSWER_JUDGE_TIMEOUT_RETRY_ENABLED=true` (default).
- `ANSWER_JUDGE_TIMEOUT_RETRY_SECONDS=30` (default, positive seconds).
- `ANSWER_JUDGE_TIMEOUT_RETRY_TEMPLATE_KWARGS={"enable_thinking":false,"thinking":false}`
  (default; override to match the deployed model's chat template).

The retry uses Chat Completions with `extra_body.chat_template_kwargs` and omits
`thinking_token_budget`. It does not mutate the normal model client. The retry
client disables the application's transport retry loop. Non-timeout provider
errors, malformed verdicts, substantive rejections, and request cancellation do
not trigger this timeout recovery mechanism.

With a 60-second primary timeout and the default retry timeout, one logical review
can take up to about 90 seconds, subject to remaining shared review time. Both
attempts debit the existing review budget. The retry may use unused repair reserve;
non-terminal reviews preserve the terminal reserve. Terminal reviews can use any
remaining review time. There is no additional budget allocation and no retry once
that budget is exhausted. The usual exhausted/previous-rejection handling applies
if neither attempt yields a valid verdict; an unavailable retry is never a reviewed
approval. A valid retry rejection follows the existing repair/withholding rules.

Telemetry emits `loop_answer_judge_timeout_retry` with `site`, alongside the
individual judge spans and their normal outcomes. The timeout verdict carries an
internal `failure_reason` so an internally caught provider timeout is distinguishable
from malformed output. The outer deadline is also handled by the same retry path.

vLLM's [reasoning documentation](https://docs.vllm.ai/en/stable/features/reasoning_outputs/)
documents per-request overrides: Qwen3 uses `enable_thinking`; some templates,
including DeepSeek-V3.1, use `thinking`. These controls require a model/template
that supports non-thinking mode. Hiding reasoning output is not the same as
disabling its generation. Validate the configured keys against the deployed model;
for non-vLLM providers, disable this mechanism unless they support these extensions.
