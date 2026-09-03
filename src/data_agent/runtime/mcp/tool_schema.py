"""Model-visible tool schemas — the MCP tools (live-fetched) + `askUser`.

The MCP is the single source of truth for parameter shape: `list_tools()` is fetched
and each `input_schema` translated VERBATIM into an OpenAI `type: "function"`
declaration, rather than hand-authored copies that can drift from it (D75). `askUser`
is the one locally-authored tool (D6/D45). No schema this module can produce may
declare `session_id`, `jwt` or `scope` as a parameter (D5).
"""

from __future__ import annotations

from typing import Any

from .client import MCPClient, MCPToolSpec

ASK_USER_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "askUser",
    "description": (
        "Pause and ask the user one clarifying question, then resume with their answer. "
        "Offer no more than five choices. Put every choice in `options`; never embed a "
        "choice list in `question`. Consolidate choices when more than five exist. Never "
        "offer a bare code or ID: pair every code with its human-readable label, such as "
        "employee name plus employee code or department name plus department code."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The question only. Do not enumerate choices here; use `options` for them."
                ),
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 5,
                "nullable": True,
                "description": (
                    "At most five concise, mutually exclusive choices. Each coded value must "
                    "include its human-readable label; never provide a bare code or ID."
                ),
            },
        },
        "required": ["question"],
    },
}

# `resolveValues` (D77) is the second locally-authored tool — a runtime
# composite (not an MCP tool), intercepted in the agent loop like `askUser` but
# returning an inline tool result. It declares NO session_id/jwt/scope (D5) —
# the client/tenant is applied automatically by the backing runQuery's D5 RLS +
# D57 column-scope. See docs/decisions/resolvevalues-design.md §10.
RESOLVE_VALUES_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "resolveValues",
    "description": (
        "Resolve a fuzzy business CONCEPT to the concrete, client-specific values of a "
        "code/category column, ranked by how well each value matches the concept and how "
        "frequently it occurs for THIS client. Use this for client-defined or time-varying "
        "code spaces (e.g. earn_code, type_code, department codes) where the exact codes differ "
        "per client and drift over time — never hardcode such codes. Prefer this over sampleRows "
        "when you need the values that mean a concept (e.g. 'PTO earn codes'), not a raw sample. "
        "Each result has a `score` (0-1); if the top scores are low or clustered (no clear "
        "winner), ask the user to confirm with askUser before filtering on a guessed value. "
        "The result also carries a `degraded` flag and a `ranking` mode: when `ranking` is "
        "'freq_only' (semantic matching was unavailable), the scores reflect how COMMON each "
        "value is for this client, NOT how well it matches your concept — do not treat a high "
        "score as a concept match; prefer askUser to confirm. When `ranking` is 'semantic+freq' "
        "the scores blend concept similarity with frequency as normal. "
        "The client/tenant is applied automatically — do not pass any client identifier."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "table": {
                "type": "string",
                "description": "Table holding the column, e.g. 'accrual_events' or "
                "'dbpcm_warehouse.accrual_events'.",
            },
            "column": {
                "type": "string",
                "description": "The code/category column to resolve values for, e.g. 'earn_code'.",
            },
            "concept": {
                "type": "string",
                "description": "The business concept to match, in the user's own words, e.g. "
                "'paid time off' or 'overtime'. Free text — never a code.",
            },
            "period": {
                "type": ["object", "null"],
                "description": "Optional. Restrict to a concrete date window (helps when codes "
                "drift over time). Omit if not needed.",
                "properties": {
                    "column": {
                        "type": "string",
                        "description": "A date/time column on the table to filter on.",
                    },
                    "start": {"type": "string", "description": "Inclusive start (ISO date)."},
                    "end": {"type": "string", "description": "Inclusive end (ISO date)."},
                },
            },
        },
        "required": ["table", "column", "concept"],
    },
}


# The three model-facing knowledge-plane READ tools (read-tools-design §1) —
# runtime-implemented (not MCP tools), intercepted in the agent loop like
# `resolveValues`/`askUser` and returning an inline tool result. None declares
# session_id/jwt/scope (D5) — this user's scope is applied automatically
# (searchBlueprints pre-filters, getBlueprint denies-as-not-found).
SEARCH_BLUEPRINTS_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "searchBlueprints",
    "description": (
        "Search the blueprint library for reusable, validated analyses that match an "
        "intent. Call it for EVERY analytical deliverable the request contains, in your "
        "own words — one search per deliverable, not one for the whole question — "
        "whether or not one of the blueprint cards already offered to you fits. Those "
        "cards were recalled from the whole question as ONE string, so on a multi-part "
        "request they under-serve every part of it. This is normal practice, not a "
        "fallback for when they miss. It re-searches for THIS user's scope and returns "
        "more candidate cards. Each card carries id, intent, slots summary, score, each slot's "
        "name/type/required, any pinned term-to-column resolutions, and the result grain — "
        "enough to CHOOSE between candidates and to fill runBlueprint, so you do NOT need a "
        "getBlueprint on every candidate to decide. A card carries no SQL, though, and its "
        "`intent` is authored prose: once you have picked one you MUST call getBlueprint(id) "
        "on it and read what it actually does before runBlueprint — the runtime refuses a "
        "runBlueprint for an id you have not expanded in this turn. Call getBlueprint too "
        "when a card carries `slots_omitted`, which means it lists only the first few slots "
        "and the rest are on getBlueprint. A `degraded` flag of true means semantic ranking "
        "was unavailable and the order is weaker — treat scores with less confidence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The intent to search for, in your own words. Free text.",
            },
            "k": {
                "type": "integer",
                "description": "Optional. How many cards to return (clamped to a sane maximum). "
                "Omit for a small default.",
            },
        },
        "required": ["query"],
    },
}

GET_BLUEPRINT_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "getBlueprint",
    "description": (
        "Expand one blueprint by id (from a thin card or a searchBlueprints result). "
        "CALL THIS BEFORE EVERY runBlueprint, in the SAME turn: a card carries no SQL, so "
        "until you expand a blueprint you are trusting its authored `intent` line to "
        "describe the query it will run, and the runtime refuses a runBlueprint for an id "
        "you have not expanded this turn (an expansion from an earlier turn does not "
        "count). Returns the blueprint's intent, the tables/columns it reads (its `uses` "
        "footprint), its `result_grain`, its status (validated/drift), its `slots` — each "
        "slot's name, type, plain-English meaning, and whether it is required or optional — "
        "and the SQL it runs: a `sql_template` for a single-step blueprint, or a "
        "`composition` summary (a step count and a note) for a composed one, which is still "
        "ONE atomic runBlueprint call because the runtime chains its internal steps. READ "
        "what it returns and check the blueprint measures what THIS deliverable asked for "
        "before you run it; if it does not, pick another or query it yourself. You may "
        "batch: getBlueprint for several blueprints in one response, then runBlueprint for "
        "them in the next. If the blueprint does not exist or is not available to you, this "
        "returns `found: false` — re-search with searchBlueprints."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "The blueprint id, e.g. 'bp-overtime-by-department'.",
            },
        },
        "required": ["id"],
    },
}

SEARCH_KNOWLEDGE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "searchKnowledge",
    "description": (
        "RAG over global, entity-agnostic institutional knowledge and lessons — "
        "definitions, conventions, gotchas (e.g. how overtime is defined, what a pay "
        "period is). This is NOT client data: do not use it to look up a specific "
        "tenant's values (use resolveValues for that). Returns the most relevant "
        "knowledge chunks; a `degraded` flag of true means ranking was weaker."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look up, in your own words. Free text.",
            },
        },
        "required": ["query"],
    },
}


# --- call-time intent tagging (Release 1, composite/analysis_state.py) -------
#
# ONE optional string property, added to EXACTLY THREE tools: `runQuery`,
# `getTableSchema` (both live-fetched from the MCP and augmented below) and
# `runBlueprint` (locally authored, so it carries it inline). Those three are
# `INTENT_TAGGABLE_TOOLS` — the same three whose results 04 §A accepts as
# completion evidence and whose failures 04 §B.1 accepts for blocking, so a tag
# anywhere else could never resolve to anything and would be a path to nowhere.
#
# It is a RUNTIME concept and is STRIPPED before dispatch
# (`analysis_state.split_serves_intent`): the MCP server never sees it. The
# description tells the model when it is meaningful, because an optional parameter
# with no stated purpose is one a model fills with a placeholder.
SERVES_INTENT_PARAM: dict[str, Any] = {
    "type": "string",
    "description": (
        "Optional. The id of the tracked intent this call is for, e.g. 'i2' — the "
        "runtime uses it to close that intent when you later mark it completed or "
        "blocked, which is why it needs nothing else from you then. Only meaningful "
        "when you have declared intents with updateAnalysisState; leave it empty "
        "otherwise."
    ),
}
# The two MCP-advertised tools the tag is injected into. `runBlueprint` is not
# here: it is locally authored and declares the property directly.
_INTENT_TAGGABLE_MCP_TOOLS: frozenset[str] = frozenset({"runQuery", "getTableSchema"})


# `runBlueprint` (runblueprint-design §5.1) — the deterministic fast-path
# execution tool: run one stored, validated blueprint by id with the model's
# raw slot values. The runtime resolves/binds each slot (never the model),
# executes through the runQuery choke point (D57/D64/D5 free), and runs the
# D56 verify gate before returning — a missing/ambiguous slot pauses to ask the
# user; a verify failure falls back to the raw loop. Declares NO
# session_id/jwt/scope (D5) — scope is applied automatically by the inner
# runQuery's RLS + column-scope enforcement.
RUN_BLUEPRINT_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "runBlueprint",
    "description": (
        "Execute a stored, validated blueprint (from a getBlueprint result) by id to "
        "answer the user's question the fast, deterministic way. CALL getBlueprint ON "
        "THAT ID FIRST, in this same turn, and read what the blueprint actually does: a "
        "runBlueprint for an id you have not expanded this turn is REFUSED (retryable — "
        "expand it, then run it), and an expansion from an earlier turn does not count. "
        "Fill `slot_bindings` "
        "with the raw values from the conversation and the user's own words (use the "
        "blueprint's `slots` and `resolves` to know what each slot means) — the runtime "
        "validates and binds them safely; you never write SQL or codes. An OPTIONAL slot "
        "may be omitted; omitting it means no filter on that dimension (all values) — "
        "only fill it when the user actually constrained it. If a required "
        "slot is missing or a value is ambiguous, the run pauses to ask the user. Every "
        "result is verified before you see it; if verification fails, answer from the raw "
        "tools instead. The client/tenant is applied automatically — do not pass any "
        "client identifier."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "The blueprint id to run, e.g. 'bp-average-salary-by-department'.",
            },
            "slot_bindings": {
                "type": "object",
                "description": "A flat map of {slot_name: raw_value} — the model's best "
                "natural-language read of each slot (a string, number, boolean, or a list "
                "for IN/enum-list slots). The runtime resolves each to a concrete, "
                "scope-safe value. Omit a slot to leave it unfilled (a required one will "
                "pause to ask).",
            },
            # Injected below on the two MCP-backed taggable tools; written out here
            # because `runBlueprint` is locally authored. Same object either way —
            # see `SERVES_INTENT_PARAM`.
            "serves_intent": SERVES_INTENT_PARAM,
        },
        "required": ["id", "slot_bindings"],
    },
}


# `recordAssumptions` (docs/decisions/ui-assumptions-contract.md) — a runtime
# tool the model calls ONCE, just before its final answer, to surface the
# plain-English assumptions behind that answer as a first-class response field
# (alongside `sql`/`result_table`). MODEL-DECLARED, PLAIN ENGLISH ONLY — never
# SQL, codes, or column names. Declares NO session_id/jwt/scope (D5); it carries
# no warehouse data and has no backing stack, so it is always available.
RECORD_ASSUMPTIONS_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "recordAssumptions",
    "description": (
        "Record the plain-English assumptions behind your answer so the user can see "
        "them. Use this when you interpreted an ambiguous term a particular way, applied "
        "a default filter or time period, or resolved a fuzzy concept to specific values. "
        "Call this exactly ONCE, just before you give your final answer, listing every "
        "assumption you made. Each item must be a short, plain-English sentence stated in "
        "the user's own terms — describe WHAT you assumed, never HOW you implemented it. "
        "NEVER include SQL, code, column names, or raw codes. "
        "Good: \"'Active employees' was taken to mean currently-employed staff.\" "
        "Bad: \"EmployeeStatus = 'A'\". "
        "If you did not make any assumptions, do not call this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "assumptions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The plain-English assumptions behind your answer, one short "
                "sentence each, in the user's own terms. No SQL, codes, or column names.",
            },
        },
        "required": ["assumptions"],
    },
}


# `answerWithTable` (composite/answer_with_table.py) — the TERMINAL runtime tool
# the model calls INSTEAD of a plain final message when its answer is a table. It
# carries the final prose AND designates the queries whose rows the user should
# see; the runtime ends the turn on it, saving the round-trip a non-terminal
# designation tool cost. A `blueprint_id` is resolved server-side to that
# blueprint's terminal SQL, so the UI only ever sees SQL and one route
# (`POST /query/page`). Declares NO session_id/jwt/scope (D5).
#
# TABLES-ONLY (08 §O, 2026-08-13). The top-level `sql`/`blueprint_id` pair is GONE
# from the model-facing schema: `tables` is the single, required carrier and a
# single-table answer is a one-entry list. The reason is 03 §C.3.1's measured
# failure — the model CANNOT OMIT DECLARED KEYS. R7 q1's live call was
# `{"answer": …, "sql": "", "blueprint_id": "bp-…", "tables": []}`: four declared
# properties carrying ONE field's worth of information, two of them placeholders
# and a third an empty array, because every declared key must be filled with
# something. Three ways to say one thing is three ways to get it wrong; deleting
# two of them is the same fix the `updateAnalysisState` slim-down made (01a §14).
# The READ path still understands the old shape forever — see
# `composite/answer_with_table.py::resolve_designations`.
ANSWER_WITH_TABLE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "answerWithTable",
    "description": (
        "Give your FINAL answer when that answer is a table. This ENDS the turn — "
        "pass your complete written answer in 'answer', and the user sees it together "
        "with the full table, which their interface renders itself as a scrollable, "
        "paginated grid. Do not send a separate message afterwards. "
        "Use this WHENEVER the answer is more than one row: a breakdown by group, a "
        "month-by-month series, a ranking, a list. "
        "EVERY TABLE GOES IN 'tables', AND THERE IS NOWHERE ELSE TO PUT ONE. A "
        "single-table answer is ONE entry: 'tables': [{sql: \"…\"}]. "
        "ONE TABLE PER PART. If you answered three parts, send three entries in "
        "'tables': [{blueprint_id: \"…\"}, {blueprint_id: \"…\"}, {sql: \"…\"}], in the "
        "order you answered them, each with a short 'caption' naming its part. Send the "
        "result you ALREADY produced for each part — a blueprint result goes in as its "
        "blueprint_id, unchanged. If one query you ran already covered two parts, that is "
        "one table, not two. A part whose answer is a single number still belongs in your "
        "prose, not in a grid of its own. "
        "Identify EACH entry in ONE of two ways. Either give it 'sql' — the query whose "
        "rows ARE that part's answer, written WITHOUT a LIMIT clause, since the interface "
        "adds its own paging and a LIMIT would cap what the user can scroll through. Or "
        "give it 'blueprint_id' when a blueprint you ran THIS TURN produced that part's "
        "answer; the runtime then reuses that blueprint's own final query, so you need "
        "not copy its SQL. If one entry carries both, its 'sql' is used. "
        "CLOSING YOUR LAST INTENT IS NOT THE END OF THE TURN: send updateAnalysisState "
        "closing what remains and this call in the SAME response — state calls run "
        "first, so one response does both. "
        "Do NOT copy the rows into 'answer' — the user can already see them. "
        "Describe what each table shows and call out what matters: the shape, the "
        "outliers, the trend, the total. Quoting two or three individual figures is fine. "
        "If your answer is a single number or a single row, do NOT use this tool — just "
        "reply with your answer as an ordinary message."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "answer": {
                "type": "string",
                "description": "Your complete final answer to the user, in plain prose. "
                "Describes the tables rather than reproducing their rows.",
            },
            # 08. THREE STRINGS, NO ENUM — deliberately, and 03 §C.3.1 is why: a
            # model that cannot omit keys fills the unused ones with placeholders
            # (`""` for a string, the FIRST MEMBER for an enum). For three strings
            # the placeholder serialisation is `{"sql": "", "blueprint_id": "bp-x",
            # "caption": ""}`, which `clean_answer_sql`/`clean_blueprint_id` already
            # normalise — that exact shape is the one the live model was observed
            # emitting when these three were top-level properties, so the normalisers
            # are already load-bearing on it. An enum here would silently resolve to
            # its first member instead. It is also why the ITEM keeps two designation
            # fields while the CALL no longer does: inside one item they name the same
            # table two ways and `sql` wins, which is a local precedence; at the top
            # level they were a second, parallel carrier for the whole call.
            "tables": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": "The read-only SELECT whose rows are this "
                            "part's answer, without a LIMIT clause. Omit if you are "
                            "passing blueprint_id.",
                        },
                        "blueprint_id": {
                            "type": "string",
                            "description": "The id of a blueprint you ran successfully "
                            "this turn whose result is this part's answer. Omit if you "
                            "are passing sql.",
                        },
                        "caption": {
                            "type": "string",
                            "description": "A short label naming the part this table "
                            "answers, in the user's own terms. No SQL, no column names.",
                        },
                    },
                },
                # `minItems: 1` is ADVISORY, not enforcement: these schemas are
                # non-strict (see `_ANALYSIS_STATE_*` below), so nothing rejects
                # `tables: []` at the API boundary. It is declared because it is
                # the honest contract and because the model reads it. The RUNTIME's
                # answer to an empty array is not a rejection either — it is the
                # legacy fold in `resolve_designations`.
                "minItems": 1,
                "description": "One entry per part of your answer, in the order you "
                "answered them. This is the ONLY place a table can be named — a "
                "single-table answer is one entry, not a shortcut somewhere else.",
            },
        },
        # `tables` is REQUIRED (08 §O): this tool exists to deliver a table, and an
        # `answerWithTable` with no table is a terminal call that shows the user
        # nothing. Requiring it is the same argument that made `answer` required.
        "required": ["answer", "tables"],
    },
}


# `updateAnalysisState` (Release 1, composite/analysis_state.py) — the intent
# ledger for a question that asks for more than one thing. Runtime-implemented,
# intercepted in the loop, and EXEMPT from `max_tool_calls_per_iteration` (it is
# bookkeeping, not work) though bounded at two calls per message. Declares NO
# session_id/jwt/scope (D5); it carries no warehouse data and has no backing
# stack, so it is always available.
#
# The description carries four things the runtime cannot enforce by shape and
# the model cannot infer: that ids are assigned by the runtime, that descriptions
# are frozen, that the late-init boundary is closed by ALL FOUR of
# `SUBSTANTIVE_TOOLS` (naming only three here would send a blueprint-first model
# straight into the non-retryable `ANALYSIS_STATE_LATE_INIT`, after which the turn
# runs untracked — the asymmetric silent failure 03 §E warns about; the wording
# tracks `prompts.py`'s tracking section, and a test asserts all four are named),
# and — the one that would otherwise cost a wasted call every turn — that the TAG
# must belong to a call from a PRIOR message, because state calls are dispatched
# before everything else in the batch.
#
# ITEM SHAPE (2026-08-12): THREE PROPERTIES, TWO OF THEM PER MODE.
# `evidence_tool_call_id` and `reason_code` were REMOVED — the runtime derives
# both. Citation had 9 live attempts and 9 hallucinated ids across every session
# ever measured, and a model that will not copy a 24-character opaque id will not
# start; the one shape it was kept for (ONE call closing TWO deliverables) is now
# handled by the auto-bind backstop, which is why this description tells the model
# to just mark the second one completed. `reason_code` went for a different
# reason: the validator ALREADY refused any block whose evidence did not prove the
# declared code, so the model's value was a second copy of a fact the trail
# carried — capable of disagreeing, incapable of adding anything.
#
# The two removed names are still TOLERATED on the wire and silently dropped
# (`analysis_state._LEGACY_ITEM_KEYS`), because the model's own earlier calls are
# replayed to it verbatim and a mid-conversation deploy would otherwise start
# refusing correct updates.
#
# ONE flat object for both modes, deliberately — see the field descriptions. A
# model that cannot omit keys fills the unused ones with placeholders (`""`, or
# the first enum member), which is what a `oneOf` over two item variants would be
# trying to prevent; but nothing ENFORCES a schema without `strict`, which the
# runtime does not set and cannot set for one tool while the MCP-derived schemas
# are non-strict. So the runtime NORMALISES that payload
# (`composite/analysis_state.py`, "a key carrying no information is absent") and
# this description states the same convention, rather than a stricter shape the
# provider would treat as a hint anyway.
UPDATE_ANALYSIS_STATE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "updateAnalysisState",
    "description": (
        "Track the separate deliverables a question asks for, so none is silently "
        "dropped. Use it whenever the user asks for more than one thing ('headcount "
        "and average salary by department, and who left last month'). "
        "FIRST call — BEFORE any substantive tool call, that is before any runQuery, "
        "runBlueprint, sampleRows or resolveValues in this turn — list each "
        "deliverable as an object with just a 'description': one short sentence in "
        "the user's own terms, and no other field. Do not invent ids: the runtime "
        "assigns them (i1, i2, …) and the result of that call tells you what they "
        "are. Once one of those four has run, a first declaration is refused and the "
        "turn goes untracked. "
        "DOING THE WORK — pass 'serves_intent' with an intent's id (e.g. 'i2') on the "
        "runQuery, runBlueprint or getTableSchema you run for it. That tag is how the "
        "intent is closed later, so tag the call when you make it. "
        "LATER calls — update the intents you already declared, several at a time in "
        "ONE call. Send 'intent_id' and the new 'status', and NOTHING ELSE: a "
        "description cannot be changed and an intent cannot be added or removed. "
        "Mark an intent 'completed' once a call you tagged with its id has succeeded. "
        "You never name the call: the runtime looks up the work you tagged for that "
        "intent, and if you tagged nothing it uses the one call that could have served "
        "it. Marking it completed records that you bound that work to this "
        "deliverable, not that the figure is right; check the work yourself. Mark it "
        "'blocked' when a call for it was refused for permissions or came back with no "
        "rows — the runtime reads which of those happened off that call, so there is "
        "no reason to state; tag a DIFFERENT call for each blocked intent. If a query "
        "legitimately returns nothing, that is a completed intent whose answer is "
        "'none found', not a blocked one. "
        "IF ONE CALL ANSWERS TWO DELIVERABLES: tag it with one of them and simply mark "
        "the other completed too — a tag names a single intent, so the runtime binds "
        "that same call to the second one. "
        "IMPORTANT: tag in the round you do the work and close the intent in a LATER "
        "message. This tool runs before the other calls in the same message, so a call "
        "you are making right now has not run yet and cannot close anything until your "
        "next message. "
        "CLOSING YOUR LAST INTENT IS NOT THE END OF THE TURN: the answer still has to "
        "be sent. When the answer is a table, send both in the SAME response — this "
        "call closing what remains, and answerWithTable beside it; this tool runs "
        "first, so one response does both. An ordinary written answer cannot share a "
        "response with a tool call, so there close the intents first and send it next. "
        "Every item uses one object shape, so if you must send a field that does not "
        "apply, leave it EMPTY ('') — an empty field is read as absent. On the first "
        "call 'status' is ignored (every intent starts pending), so leave it as "
        "'pending' there."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "intents": {
                "type": "array",
                "description": "The intents to declare (first call) or update (later "
                "calls). At most 8.",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "First call only. One short sentence naming "
                            "the deliverable, in the user's own terms. Max 500 "
                            "characters. Leave it empty on later calls — a "
                            "description cannot be changed once declared.",
                        },
                        "intent_id": {
                            "type": "string",
                            "description": "Later calls only. The id the runtime "
                            "assigned, e.g. 'i2'. Leave it EMPTY on the first call: "
                            "the runtime assigns the ids and refuses one you invent.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "completed", "blocked"],
                            "description": "Later calls only. The intent's new state. "
                            "On the first call leave it as 'pending' — every intent "
                            "starts pending and any other value there is refused.",
                        },
                    },
                },
            },
        },
        "required": ["intents"],
    },
}


# The locally-authored (runtime-implemented) tool schemas, appended after the
# live-fetched MCP tools. This tuple is the SINGLE source of truth for "these
# names are ours" — the name-collision guard (§6.1) asserts the MCP never
# advertises one of them.
_LOCAL_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    ASK_USER_TOOL_SCHEMA,
    RESOLVE_VALUES_TOOL_SCHEMA,
    SEARCH_BLUEPRINTS_TOOL_SCHEMA,
    GET_BLUEPRINT_TOOL_SCHEMA,
    SEARCH_KNOWLEDGE_TOOL_SCHEMA,
    RUN_BLUEPRINT_TOOL_SCHEMA,
    RECORD_ASSUMPTIONS_TOOL_SCHEMA,
    ANSWER_WITH_TABLE_TOOL_SCHEMA,
    UPDATE_ANALYSIS_STATE_TOOL_SCHEMA,
)
_LOCAL_TOOL_NAMES: frozenset[str] = frozenset(s["name"] for s in _LOCAL_TOOL_SCHEMAS)


class ToolNameCollisionError(RuntimeError):
    """A locally-authored tool name collides with an MCP-advertised one.

        Raised at schema fetch (startup), fail-closed and LOUD: two schemas with the same
        name would make the loop's `runtime_tools` interception silently SHADOW the MCP
        tool, or vice versa, depending on dispatch order.
    """


def translate_tool_spec(tool: MCPToolSpec) -> dict[str, Any]:
    """Translate one `MCPToolSpec` into an OpenAI `type: "function"` declaration."""
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_schema,
    }


def augment_with_serves_intent(schema: dict[str, Any]) -> dict[str, Any]:
    """Add the optional `serves_intent` property to a TRANSLATED MCP schema.

        NON-MUTATING: `MCPToolSpec.input_schema` is the client's own object and the
        translated schemas are cached by `ToolSchemaCache`, so this rebuilds
        `parameters`/`properties` rather than writing into either. A schema whose
        `parameters`/`properties` are not dicts is returned UNCHANGED — the tag is an
        optimisation for the model, never a precondition for calling the tool.
    """
    if schema.get("name") not in _INTENT_TAGGABLE_MCP_TOOLS:
        return schema
    parameters = schema.get("parameters")
    if not isinstance(parameters, dict):
        return schema
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return schema
    return {
        **schema,
        "parameters": {
            **parameters,
            "properties": {**properties, "serves_intent": SERVES_INTENT_PARAM},
        },
    }


async def fetch_function_schemas(
    mcp_client: MCPClient, *, jwt: str, session_id: str
) -> list[dict[str, Any]]:
    """Fetch `list_tools()` from *mcp_client*, translate, and append `askUser`.

        *jwt*/*session_id* are required because the live MCP authenticates every request,
        including `tools/list`; no credential appears in the returned schemas (D5). No
        caching here — see `ToolSchemaCache` for the cached variant used at runtime.
    """
    tools = await mcp_client.list_tools(jwt=jwt, session_id=session_id)
    # §6.1 name-collision guard: the locally-authored tool names MUST be disjoint
    # from the MCP-advertised names. A collision fails LOUD at startup (fail-
    # closed) rather than becoming a silent shadow in the loop's `runtime_tools`
    # interception — the runtime-tool registry is the source of truth for our names.
    mcp_names = {tool.name for tool in tools}
    collisions = mcp_names & _LOCAL_TOOL_NAMES
    if collisions:
        raise ToolNameCollisionError(
            "MCP advertises tool name(s) that collide with locally-authored runtime "
            f"tools: {sorted(collisions)}. Rename the local tool or the MCP tool — a "
            "name collision would silently shadow one of them in the loop."
        )
    # Translate verbatim, then add the ONE runtime-owned parameter
    # (`serves_intent`) to the two taggable MCP tools. The augmentation is applied
    # here, at the single fetch seam, so `ToolSchemaCache` caches the augmented
    # shape and no caller can accidentally advertise the un-augmented one.
    schemas = [augment_with_serves_intent(translate_tool_spec(tool)) for tool in tools]
    # The locally-authored runtime tools (askUser + resolveValues + the three read
    # tools + runBlueprint + recordAssumptions + answerWithTable +
    # updateAnalysisState), always advertised, appended after the MCP tools
    # (count 6 → 15).
    schemas.extend(_LOCAL_TOOL_SCHEMAS)
    return schemas


class ToolSchemaCache:
    """Caches the translated tool-schema list, refreshed on explicit reload.

        `get_schemas` takes the CURRENT turn's `jwt`/`session_id` because the live MCP
        authenticates `tools/list` too, but the catalogue never varies by scope — so the
        FIRST successful fetch populates the cache for every later call, turn, session and
        column scope until `force_reload=True`. Those credentials authenticate that one
        fetch and are never retained or reflected in the cached schemas (D5). The first
        call requires the MCP to be reachable.
    """

    def __init__(self, mcp_client: MCPClient) -> None:
        self._mcp_client = mcp_client
        self._cache: list[dict[str, Any]] | None = None

    async def get_schemas(
        self, *, jwt: str, session_id: str, force_reload: bool = False
    ) -> list[dict[str, Any]]:
        if self._cache is None or force_reload:
            self._cache = await fetch_function_schemas(
                self._mcp_client, jwt=jwt, session_id=session_id
            )
        return self._cache
