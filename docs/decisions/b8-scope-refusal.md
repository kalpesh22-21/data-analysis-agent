# B8: out-of-scope request channel

Base commit: `362d61949ecbe7ecd03cb1a4a5607b30c1fa39a7`.
This is the canonical response to lane5's 2026-09-18 B8 request. The lab's
append-only divergence ledger is not present in this repository.

## Contract (§2)

- `OUT_OF_SCOPE_REQUEST` is registered with `kind=DenialKind.GATE`,
  `retryable=True`, and text naming HR, payroll, and product-usage scope.
- The agent can call `declineOutOfScope` with the out-of-domain part of the
  original request. The runtime validates that the quoted part occurs in that
  original request and returns a data-free refusal receipt. This records an
  agent scope decision; it does not claim a model review occurred.
- The existing deterministic creative-content answer rule uses the same
  `scope_request_refused` builder before answer dispatch. Its existing bounded
  repair and final refusal behavior is preserved.
- Each emitted refusal calls `loop_answer_rule_refused` once, with fixed
  `rule=outside_hr_payroll_product_scope` and `site=agent_scope_declaration` or
  `site=answer_scope_rule`. No request text is included in the event. Both fields
  reach progress events and guardrail spans.
- `retryable=True` permits answer repair and continued supported work. The receipt
  explicitly says not to retry the refused request. A scope refusal is neither
  warehouse evidence nor proof of missing data or execution failure.
- Mixed requests complete their supported parts. A `declineOutOfScope` receipt
  can support only a blocked disposition, using reason `OUT_OF_SCOPE_REQUEST`;
  the model tags/binds it to the matching out-of-scope intent. It cannot support
  completion. A generic answer repair receipt does not justify blocking an intent.
- Scope declarations and refused answer drafts do not replay into later turns.
  A narrowed in-scope follow-up proceeds normally in the same session.
- Disabled features, unavailable services, missing data and access denials remain
  in-scope limitations. Their existing error classifications are unchanged.

## Baseline and validation (§4–§5)

Before changes: `pytest -q tests/runtime` — **3,881 passed, 6 skipped**.
After changes: `pytest -q tests/runtime tests/learning` — **7,585 passed, 9 skipped**.
The final refusal-message cleanup also passed all seven dedicated B8 tests.
Ruff and whitespace checks pass on the changed files.

Regressions cover the weather-refusal receipt and scope-naming answer with the
judge disabled; exactly one event per refusal; a same-session in-scope follow-up;
a mixed request completing one intent and blocking only the other; repair through
the existing deterministic rule; product/data availability remaining distinct;
rejection of an invented request fragment; GATE/enforcement metrics projection;
and shape-only progress/Phoenix fields. Existing schema and reason-code pins were
updated for the new channel; existing behavioral tests pass.

A live Kimi 2.7 Code weather probe was attempted with judge/measurement reviews
disabled, native provider IDs, and the previously authorized unredacted Phoenix
setting. Both attempts failed with `APIConnectionError` before any model response.
Credential inspection subsequently found a probe bug: the key file now contains
key, endpoint, and model on separate lines, but the probe read the whole file as
the key. These failures do not establish a provider outage or invalid credentials.
The probe parser was corrected to read the three fields separately.
Trace IDs: `a132ecde9050c22b7d402e9a5640bb86` and
`01be754fc885c3fb5cfc5131afb06704`. These are not live acceptance passes.
The corrected-credential live weather probe completed with `kimi-k2.7-code`:
`done`, scope-naming answer, one `OUT_OF_SCOPE_REQUEST` receipt, exactly one
scope event, three model rounds, and zero repeated tool calls. All observer
events were exported. Trace: `0189e82543024e0f8c8fd3be02719250`.
Judge/measurement reviews remained disabled; this verifies the judge-independent
scope channel. The lab's full P06/probe battery was not available or rerun here.

A subsequent mixed weather/headcount probe enabled both the answer judge and
measurement reviewer. It completed with a scope-naming decline, a warehouse
table, approved measurement and answer reviews, one `OUT_OF_SCOPE_REQUEST`
receipt, and exactly one scope event. Trace:
`d5056497b5e0f18d053035b86da47ee4`; all ten completion spans exported.
The agent did not declare the requested multipart intent ledger, so this live
probe does not verify intent binding (the deterministic regression covers it).
This used live Kimi and local warehouse MCP with repository mock Help Center
and capability services, fixture catalog, and blueprint retrieval disabled.
Calls were paced 35 seconds apart; test-only review timeouts were 90 seconds.
Other judged route probes exposed answer-quality and review-timeout issues;
this result is not a claim that the broader live matrix passed without issues.

The multi-app probe initially lost completion spans after its first case because
OpenAI instrumentation remained attached to a closed tracing provider. Resetting
instrumentation before each probe app fixed export; affected cases were rerun.
This was a probe lifecycle correction, with no production tracing change.

## Known limitations

The channel is judge-independent; general semantic scope routing remains an
agent decision. It is not a new exhaustive deterministic classifier of arbitrary
natural-language topics. The existing lexical creative-content rule remains a
limited backstop. Runtime validation establishes the request fragment and the
fixed scope policy, not that every agent classification is correct. Disabled or
unavailable features must not be routed here, as stated in both prompt and schema.
No separate judge dependency or broader scope mandate was introduced.
