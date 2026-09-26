# Runtime follow-up: progress lifecycle contract

Date: 2026-09-26. Follow-up to lane5's 2026-09-25 contract note for Ember MR-1657,
G2/G6. The original shared note is unchanged.

Dispatch progress SSE frames now carry the additive `shape.lifecycle` field:

| Observer event | `shape.lifecycle` |
|---|---|
| `tool_dispatch_start` | `start` |
| `tool_dispatch_ok` | `ok` |
| `tool_dispatch_denied` | `denied` |
| `tool_dispatch_error` | `error` |

Example wire payload:

```json
{"step":"Checking payroll totals","shape":{"tool_name":"runQuery","tool_call_id":"call-42","error_code":"COLUMN_SCOPE_VIOLATION","lifecycle":"denied"}}
```

The translator derives lifecycle from the observer event, not from human-readable
copy or an arbitrary payload value. This covers MCP dispatch, runtime handlers,
local runtime errors, blueprint resume, and reviewed finalization dispatches.
The resume boundary preserves the actual result status. Finalization progress
remains hidden until explicit judge approval.

Dispatch frames retain the task summary in `step` from start through completion.
If no summary is available, they retain the safe start label. Late summaries after
completion are dropped. The UI should render one row per tool call: summary text
with a spinner for `start`, done for `ok`, denied for `denied`, and failure for `error`.
Terminal frames change the indicator, not the label.

Summary and non-dispatch events omit lifecycle. They update copy without starting,
closing, or reopening a tool row. Existing ordering of reserved asynchronous summary
slots is unchanged. Clients should correlate dispatch events by `shape.tool_call_id`,
use lifecycle for state, and treat `step` as presentation copy; no prefix regex is
needed. A denied or error row must retain that state when the result frame arrives.
Older clients may ignore the new field. No Ember code was changed in this repository.

Validation includes actual SSE serialization for MCP and runtime success, denial,
and error paths; late summary text; summary timeout ordering; and resumed blueprint
status. SQL, credentials, and raw error details remain excluded from progress shapes.
