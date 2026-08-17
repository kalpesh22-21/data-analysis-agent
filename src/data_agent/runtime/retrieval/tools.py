"""retrieval/tools.py — the three model-facing knowledge-plane read tools (D8/D77).

`searchBlueprints`, `getBlueprint`, `searchKnowledge` — runtime-implemented tools
(read-tools-design), intercepted in the agent loop through the `RuntimeTool`
registry exactly like `resolveValues`: each returns an inline `ToolResult`, counts
as exactly one `tool_calls_made`, and never reaches `ToolDispatcher.dispatch`
under its own name. They are the read-path siblings of the data-less tools
(`listDatabases`/`listTables`/`explainQuery`): every result is corpus METADATA
(blueprint intents, a blueprint's `uses` column IDENTIFIERS, knowledge prose),
never out-of-scope warehouse row data. `ToolResult.provenance` is the safe-empty
`frozenset()` (determined, zero warehouse columns → always kept in D44 replay)
for `searchKnowledge` and the `getBlueprint` not-found path, and a real column
footprint for the two results that NAME columns: `getBlueprint`'s FOUND path
(its scoped `uses`) and — since card enrichment, release-1 §02 —
`searchBlueprints` (the UNION of the returned cards' `uses`). Both drop from
replay under a later scope narrowing. It is `None` (undetermined → dropped
fail-closed, always) only where the footprint cannot be established: a malformed
stored `uses` key, or a card printing a column identifier the union does not
cover — see `_cards_to_provenance` and the printed-column guard above it.

Shape/behaviour (read-tools §1/§3/§6):
  - `searchBlueprints(query, k)` → reranked ThinCards, scope PRE-FILTERED (a card
    the user cannot run is never returned); `degraded=true` on the recall-order
    degrade so the model knows ranking is weaker.
  - `getBlueprint(id)` → the stored D87 projection; out-of-scope AND absent both
    return the identical `{found: false}` (the §3 non-oracle — no scope-probing).
  - `searchKnowledge(query)` → reranked chunks; BYPASSES scope (entity-agnostic,
    leakage-gated at write, D58(a)).
All degrade-not-fail: a wired-but-degraded stack returns `status="ok"` + empty +
`degraded=true`; malformed args fail-closed to `RETRIEVAL_TOOL_INVALID_ARGS`
before any work (`k` is clamped to `[1, max_k]`, never rejected for over-ask).
The unwired case (no pipeline/store) is handled one level up in the loop registry
(`RETRIEVAL_TOOL_UNAVAILABLE`), so these tools always hold live dependencies.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from data_agent.runtime.blueprint.models import slot_type_gloss
from data_agent.runtime.dispatch.tool_dispatcher import (
    _DEFAULT_MAX_TOOL_RESULT_TOKENS,
    ToolObserver,
    ToolResult,
    _build_preview,
    _default_observer,
)
from data_agent.runtime.observability import tracing
from data_agent.runtime.observability.redaction import tool_span_args

from . import scope_filter
from .models import Candidate, ThinCard

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opentelemetry.trace import Tracer

    from data_agent.runtime.auth.credentials import RuntimeCredentials
    from data_agent.runtime.loop.agent_loop import TurnContext

    from .pipeline import RetrievalPipeline
    from .vector_index import VectorIndex

INVALID_ARGS_CODE = "RETRIEVAL_TOOL_INVALID_ARGS"
UNAVAILABLE_CODE = "RETRIEVAL_TOOL_UNAVAILABLE"

# B4-parity: a last-resort code for an UNEXPECTED crash that slipped every guard
# (the pipeline/store degrade-not-fail, but a programming error must never abort
# the turn or leak `str(exc)`). Reuses the shared invalid-args code family? No —
# an internal crash is distinct and non-retryable.
INTERNAL_ERROR_CODE = "RETRIEVAL_TOOL_INTERNAL_ERROR"
_INTERNAL_ERROR_MESSAGE = "Blueprint/knowledge search hit an internal error. Please try again."

_logger = logging.getLogger(__name__)


class _ReadTool:
    """Shared plumbing for the three read tools: one TOOL span (with `query`
    redacted, §5), symmetric `tool_dispatch_start`/`ok`/`error` progress, a
    B4-parity crash guard, and the `provenance = frozenset()` guarantee baked
    into every `ToolResult` these tools ever return."""

    tool_name: str = ""

    def __init__(
        self,
        *,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
        disable_redaction: bool = False,
        max_result_tokens: int = _DEFAULT_MAX_TOOL_RESULT_TOKENS,
    ) -> None:
        self._observer = observer
        self._tracer = tracer
        # Per-result preview SIZE cap (tokens), the SAME operator lever
        # `ToolDispatcher` takes (`RuntimeSettings.max_tool_result_tokens`). It is
        # threaded here because `_build_preview` defaults it: without this the read
        # tools were pinned to 4,000 tokens no matter what the operator configured,
        # so raising the setting silently did nothing for the one result shape that
        # actually needs it — an enriched `searchBlueprints` card list at a large
        # `k` (release-1 §02).
        self._max_result_tokens = max_result_tokens
        # Access-controlled TELEMETRY DEBUG switch (RuntimeSettings.
        # otlp_disable_redaction). Default False keeps the D25 span (`query`
        # redacted, §5). When True the span carries the REAL `query` free text —
        # telemetry-only; `_guarded` below always gets the raw model_args, so the
        # search/scope-filter path is unaffected.
        self._disable_redaction = disable_redaction

    async def run(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None = None,
    ) -> ToolResult:
        # *turn* (03 §C.1): the loop threads its own `TurnContext` to every
        # runtime tool. This one does not need it — accepted and ignored so the
        # `RuntimeTool` protocol has ONE signature rather than two shapes the
        # dispatch site has to tell apart.
        self._observer("tool_dispatch_start", {"tool_name": self.tool_name})
        if self._tracer is None:
            result = await self._guarded(model_args, credentials)
        else:
            with tracing.tool_span(
                self._tracer,
                tool_name=self.tool_name,
                args=tool_span_args(
                    self.tool_name, model_args, disable_redaction=self._disable_redaction
                ),
                status="ok",
                error_code=None,
                reveal_complex_args=self._disable_redaction,
            ) as span:
                result = await self._guarded(model_args, credentials)
                span.set_attribute("tool.status", result.status)
                if result.error_code is not None:
                    span.set_attribute("tool.error_code", result.error_code)
        if result.status == "ok":
            self._observer("tool_dispatch_ok", {"tool_name": self.tool_name})
        else:
            self._observer(
                "tool_dispatch_error",
                {"tool_name": self.tool_name, "error_code": result.error_code},
            )
        return result

    async def _guarded(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ToolResult:
        try:
            return await self._execute(model_args, credentials)
        except Exception:  # noqa: BLE001 - B4-parity: never abort the turn / leak str(exc)
            _logger.exception(
                "%s internal error (session=%s)", self.tool_name, credentials.session_id
            )
            return self._error(INTERNAL_ERROR_CODE, _INTERNAL_ERROR_MESSAGE, retryable=False)

    async def _execute(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ToolResult:  # pragma: no cover - overridden
        raise NotImplementedError

    # -- ToolResult builders --------------------------------------------------

    def _ok(
        self,
        result_full: dict[str, Any],
        *,
        provenance: frozenset[tuple[str, str]] | None = frozenset(),
        preview_row_count: int = 20,
    ) -> ToolResult:
        """*provenance* defaults to the safe-empty `frozenset()` (determined,
        zero warehouse columns → always kept in D44 replay) — correct for
        `searchKnowledge` and the `getBlueprint` not-found path, both of which
        name no column. The two results that DO name columns override it so the
        entry drops under a later scope narrowing (§3): `getBlueprint`'s FOUND
        path with the blueprint's scoped `uses`, and `searchBlueprints` with the
        union of its enriched cards' `uses` (release-1 §02) — or `None` when that
        union does not cover what the cards print (`_cards_to_provenance`)."""
        return ToolResult(
            status="ok",
            tool_name=self.tool_name,
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=provenance,
            result_preview=_build_preview(
                result_full,
                preview_row_count,
                self._max_result_tokens,
                observer=self._observer,
                tool_name=self.tool_name,
            ),
            result_full=result_full,
        )

    def _error(self, code: str, message: str, *, retryable: bool) -> ToolResult:
        return ToolResult(
            status="error",
            tool_name=self.tool_name,
            error_code=code,
            retryable=retryable,
            user_message=message,
            provenance=frozenset(),
            result_preview=None,
            result_full=None,
        )


def _clamp_k(
    raw: Any, *, default_k: int, max_k: int
) -> tuple[int | None, str | None]:
    """Resolve the model-supplied `k`: absent → *default_k*; a non-integer or a
    value < 1 is malformed (§6, fail-closed); a valid `k` over *max_k* is CLAMPED
    (a lenient over-ask, not a rejection, §9). Returns `(k, None)` or
    `(None, error_message)`."""
    if raw is None:
        # N3: a mis-set config (default_k > max_k) must never leak an
        # over-max default through the tool — clamp the default too.
        return min(default_k, max_k), None
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None, "'k' must be an integer number of cards."
    if raw < 1:
        return None, "'k' must be a positive integer."
    return min(raw, max_k), None


def _uses_to_provenance(uses: frozenset[str]) -> frozenset[tuple[str, str]] | None:
    """Split a blueprint's scoped `uses` ("database.table.column" keys) into the
    `(db_table, column)` provenance tuples the D44 replay filter consumes — it
    rebuilds each key as `f"{db_table}.{column}"` and checks scope membership
    (`context/scope_filter.is_provenance_in_scope`). This is what makes a
    getBlueprint trail entry DROP under a later scope narrowing (S1/§3): its
    footprint is no longer a subset of the narrowed scope.

    Fail-closed: any malformed key (no dot, empty part) → `None` for the WHOLE
    set (undetermined → dropped from replay), never a partial set that could
    fail-open. An empty `uses` → `frozenset()` (determined, zero columns → kept)."""
    tuples: set[tuple[str, str]] = set()
    for use in uses:
        db_table, sep, column = use.rpartition(".")
        if not sep or not db_table or not column:
            return None
        tuples.add((db_table, column))
    return frozenset(tuples)


# Per-slot notes the model reads to understand the required/optional contract:
# a REQUIRED slot omitted PAUSES the run to ask; an OPTIONAL slot omitted runs
# UNFILTERED on that dimension ("all values") — but ONLY when it carries an
# `optional_pattern` (the executor substitutes it on omission). A pattern-less
# optional slot omitted fails closed to the raw loop, so we do NOT promise "all
# values" for it (the load gate forbids a referenced pattern-less optional, but
# this note stays honest even if that gate is later relaxed). See _enrich_slot.
_REQUIRED_SLOT_NOTE = "Must be provided; if omitted the run pauses to ask the user."
_OPTIONAL_ALL_VALUES_NOTE = (
    "May be omitted; omitting means no filter on this dimension (all values)."
)
_OPTIONAL_PLAIN_NOTE = "May be omitted."


def _enrich_slot(raw: Any) -> dict[str, Any]:
    """Shape one raw slot dict (name/type/required/binds_to/...) into the ENRICHED
    model-facing view: keep name/type, ADD a plain-English `type_meaning` gloss, a
    `requirement` ("required"/"optional"), and a `note` explaining the contract.
    Preserves `binds_to`/`enum_values`/`min_value`/`max_value` when present (useful,
    harmless). A raw slot missing `type` → generic gloss; missing/invalid `required`
    → defaults to required=True (the SlotSpec default) — the slot is never dropped.

    The optional note only promises "no filter (all values)" when the slot carries
    an `optional_pattern` (round-tripped via slots_json) — the executor substitutes
    that pattern on omission. Without one, omission fails closed to the raw loop, so
    the note softens to a neutral "May be omitted." rather than overclaim."""
    slot = raw if isinstance(raw, dict) else {}
    type_ = slot.get("type")
    required = slot.get("required", True)
    if not isinstance(required, bool):
        required = True
    if required:
        note = _REQUIRED_SLOT_NOTE
    elif slot.get("optional_pattern"):
        note = _OPTIONAL_ALL_VALUES_NOTE
    else:
        note = _OPTIONAL_PLAIN_NOTE
    enriched: dict[str, Any] = {
        "name": slot.get("name"),
        "type": type_,
        "type_meaning": slot_type_gloss(type_),
        "requirement": "required" if required else "optional",
        "note": note,
    }
    for key in ("binds_to", "enum_values", "min_value", "max_value"):
        if slot.get(key) is not None:
            enriched[key] = slot[key]
    return enriched


def _composition_annotation(composes: list[Any]) -> dict[str, Any]:
    """Compact, model-facing stand-in for a composed blueprint's raw `composes`
    DAG. The raw DAG (per-node `sql_template`/`feeds_from`/`consumes`/`$0.x` refs)
    is the confusion vector — it invites the model to hand-run steps or reason
    about their order. Replace it with a step count + a "one atomic call" note;
    the runtime executes the DAG from the store, never from this serialization."""
    steps = len(composes)
    return {
        "steps": steps,
        "note": (
            f"This blueprint runs {steps} internal steps that the runtime executes "
            "and chains for you. Call runBlueprint once with the slots above — do "
            "NOT run these steps yourself or reason about their order."
        ),
    }


def _put_if_present(target: dict[str, Any], key: str, value: Any) -> None:
    """Add *key*→*value* only when the blueprint actually stored the DAG field
    (non-None) — keeps the `getBlueprint` FOUND shape strictly additive so a
    DAG-less D87/D88 blueprint renders byte-identically to before (§1.3)."""
    if value is not None:
        target[key] = value


def _require_text(raw: Any, name: str) -> tuple[str | None, str | None]:
    """A required non-blank free-text arg (`query`/`id`). Fail-closed on
    missing/blank/non-string, naming ONLY the bad arg (no enumeration, §6)."""
    if not isinstance(raw, str) or not raw.strip():
        return None, f"A non-empty '{name}' is required."
    return raw, None


def _search_card(card: ThinCard) -> dict[str, Any]:
    """Serialize one `ThinCard` into a `searchBlueprints` result entry.

    ENRICHED (release-1 §02) so the model can pick between candidates without a
    `getBlueprint` round-trip each. The enrichment keys are OMITTED when the
    blueprint stored no DAG (`None`), so a DAG-less blueprint serialises
    byte-identically to before this change.

    `status` is DELIBERATELY absent. Recall filters
    `coalesce(node.status,'validated') = 'validated'`
    (`vector_index._BLUEPRINT_RECALL_QUERY`), so every card here is validated by
    construction — the field would be a constant carrying no information, and
    printing it would imply a distinction that cannot occur in a search result.
    It stays on `getBlueprint`, where a keyed fetch by id genuinely can return a
    non-validated blueprint.

    `slots` are the `{name, type, required}` SUMMARY only (the projection is
    enforced upstream in `pipeline._project_slots`); `binds_to`, `enum_values`,
    `optional_pattern` and the numeric bounds are `getBlueprint`'s job.

    ⚠ Every key emitted here is read back by the printed-column guard below
    (`_card_printed_columns`), which decides whether the trail entry's D44
    provenance can be claimed as determined. A NEW key is guarded by default —
    its strings must be covered by the cards' `uses` union or the whole entry
    fails closed out of replay — so classify it there when you add it.
    """
    entry: dict[str, Any] = {
        "id": card.id,
        "intent": card.intent,
        "slots_summary": card.slots_summary,
        "score": card.score,
    }
    _put_if_present(entry, "resolves", card.resolves)
    if card.slots is not None:
        entry["slots"] = [
            {"name": slot.name, "type": slot.type, "required": slot.required}
            for slot in card.slots
        ]
        # Only when the per-card cap actually dropped slots — a `0` on every
        # enriched card would break the "omit what is absent" shape, and its
        # ABSENCE here is what tells the model the list is complete.
        if card.slots_omitted > 0:
            entry["slots_omitted"] = card.slots_omitted
    if card.result_grain is not None:
        entry["result_grain"] = list(card.result_grain)
    return entry


# --- the printed-column guard (release-1 §02 ⚠Provenance — the QA residue) ----
#
# `uses` is the TRANSITIVE SET OF COLUMNS THE BLUEPRINT'S DAG READS. The enriched
# card also prints column identifiers that are NOT derived from it: `resolves` is
# AUTHORED disambiguation metadata (term → column NAME). The two are related but
# not identical, and claiming the `uses` union as the entry's provenance while the
# card prints a column outside it is a DETERMINED-AND-NARROW claim about a
# footprint the card has already exceeded — under a scope narrowed to exactly
# `uses`, `is_entry_in_scope` returns True and the card replays the uncovered
# column name after the caller lost access to it. §02 closed this leak class only
# for the columns `uses` happens to cover; this is the rest of it.
#
# The guard is therefore derived from what the card ACTUALLY PRINTS — the
# serialised entry — and not from a list of field names enumerated here: every
# field is walked and every string it carries is treated as a column identifier
# the claimed provenance must cover, UNLESS the field is classified below.
# Deny-by-default is the point: a future enrichment field carrying a column name
# is caught by construction instead of leaking until someone notices — the same
# posture `pipeline._project_slots` takes for the slot projection, and the repo's
# own recorded lesson that spot-patching named fields misses the class.

# Card fields whose strings are PROSE or AUTHORED LABELS, never column
# identifiers. Adding a field here is a security decision, so each carries its
# reason: `id` is an authored blueprint slug; `intent` is the one-line intent
# sentence; `slots_summary` is the authored comma-joined slot LABELS (see
# `_SLOT_LABEL_FIELDS`). `score`/`slots_omitted` need no exemption — they are
# numbers, and the walk collects strings only.
_CARD_PROSE_FIELDS = frozenset({"id", "intent", "slots_summary"})

# Sub-keys of a projected `slots` entry that are labels rather than columns. A
# slot's `name` is the authored bind-site label ("department"), deliberately NOT
# the column it binds to — `binds_to`, the fully-qualified column identifier, is
# excluded from the card upstream (`models.SlotSummary`). Every OTHER sub-key is
# walked as a column identifier, so if `binds_to` (or any new authored field)
# ever reaches a card it is covered-or-fail-closed rather than silently printed.
_SLOT_LABEL_FIELDS = frozenset({"name", "type"})


def _printed_strings(value: Any) -> set[str]:
    """Every non-blank string reachable from *value* — dict KEYS included, lists
    and nested containers walked. The deny-by-default collector: an unclassified
    card field is walked whole, so nothing it carries escapes the guard."""
    found: set[str] = set()
    if isinstance(value, str):
        if value.strip():
            found.add(value)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            found |= _printed_strings(key)
            found |= _printed_strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            found |= _printed_strings(item)
    return found


def _slot_printed_columns(value: Any) -> set[str]:
    """Column identifiers printed by a card's `slots` list: everything except
    the two label sub-keys (see `_SLOT_LABEL_FIELDS`). A non-list or a non-dict
    entry is walked whole — a malformed shape must not open a hole."""
    if not isinstance(value, (list, tuple)):
        return _printed_strings(value)
    printed: set[str] = set()
    for slot in value:
        if not isinstance(slot, Mapping):
            printed |= _printed_strings(slot)
            continue
        for key, item in slot.items():
            if key in _SLOT_LABEL_FIELDS:
                continue
            printed |= _printed_strings(item)
    return printed


def _grain_printed_columns(value: Any) -> set[str]:
    """Column identifiers printed by a card's `result_grain`.

    A grain entry is an OUTPUT-COLUMN DISPLAY LABEL for a `SELECT … AS x`, not a
    warehouse column identifier. That is this repo's own recorded reading —
    `blueprint/structural_key.py::normalize_structural_grain` says so and pins a
    case fold on the strength of it — and the seed corpus bears it out: 6 of the
    11 seeds declare `result_grain: [Department]` over a footprint whose column
    is `employee.department_name`, and the two hires blueprints declare `[month]`
    for `toStartOfMonth(hire_date)`.

    So requiring a BARE grain entry to be covered would make 8 of the 11 real
    blueprints undetermined — and undetermined is dropped from replay ALWAYS
    (`context/scope_filter`: `None` is dropped even under allow-all, and even
    inside its own turn once the entry is `ok`), i.e. the model would stop seeing
    its own search results one round-trip after asking for them. That is a
    strictly worse outcome than the label it would be withholding, which the
    card's `intent`/`slots_summary` prose names in any case.

    A bare entry is therefore read as the display label it is. A QUALIFIED entry
    ("db.table.column") is no display label under any reading — it is a column
    identifier and must be covered. (Residual, recorded deliberately: a bare
    grain entry that IS a real column name of some other table cannot be told
    apart from a label at this layer — retrieval holds no catalog — so it is not
    caught here. A `resolves` value naming that column IS caught, and the corpus
    test pins the seeds.)
    """
    return {entry for entry in _printed_strings(value) if "." in entry}


def _card_printed_columns(entry: Mapping[str, Any]) -> set[str]:
    """The column identifiers ONE serialised search card prints."""
    printed: set[str] = set()
    for key, value in entry.items():
        if key in _CARD_PROSE_FIELDS:
            continue
        if key == "resolves":
            # Keys are the ambiguous TERM the user might say ("salary") — free
            # text; VALUES are the column names the term pins to.
            printed |= _printed_strings(
                list(value.values()) if isinstance(value, Mapping) else value
            )
        elif key == "slots":
            printed |= _slot_printed_columns(value)
        elif key == "result_grain":
            printed |= _grain_printed_columns(value)
        else:
            printed |= _printed_strings(value)
    return printed


def _is_covered(printed: str, *, qualified: set[str], bare: set[str]) -> bool:
    """Is one printed identifier covered by the footprint being claimed?

    THE qualification mismatch: `uses` keys are fully qualified
    ("dbpcm_warehouse.employee.annual_salary") while a card prints BARE column
    names ("annual_salary") — a card never prints a qualification, which is
    exactly why `binds_to` is kept off it. Comparing the two as whole strings
    would match nothing and make EVERY enriched card undetermined, silently
    disabling replay for the entire release (see `_grain_printed_columns` for
    what an undetermined entry costs). So:

      - a BARE printed name is compared against the LAST SEGMENT of each `uses`
        key — the only comparison that can succeed at all;
      - a DOTTED printed name is compared against the whole key, suffix-matched
        on a dot boundary so a `table.column` spelling still matches its
        `db.table.column` key.

    Both sides are case-folded, matching `normalize_structural_grain`'s pinned
    `str.lower()`. The real corpus hazard is authoring case skew (`Amount` vs
    `amount`), not two columns of one table differing only by case.

    A bare name matching no footprint column cannot be resolved to a qualified
    column; that is itself undetermined, and the caller fails closed on it.
    """
    token = printed.strip().lower()
    if "." not in token:
        return token in bare
    return any(key == token or key.endswith(f".{token}") for key in qualified)


def _cards_to_provenance(
    cards: Sequence[ThinCard], serialised: Sequence[Mapping[str, Any]]
) -> frozenset[tuple[str, str]] | None:
    """The `searchBlueprints` entry's D44 provenance: the UNION of the returned
    cards' `uses` footprints (release-1 §02 ⚠Provenance) — but only when that
    union covers every column identifier the cards actually PRINT.

    `_ok` defaults to the safe-empty `frozenset()`, and that WAS correct here for
    a precise reason: a thin card carried no column identifier, so there was
    nothing a later scope narrowing could forbid. Enrichment breaks that premise
    — `resolves` maps a term to a COLUMN NAME and `result_grain` is a
    column/alias list — so a `frozenset()` entry would be kept in replay FOREVER,
    including after the caller's scope narrows past the columns it names. That is
    exactly the leak `getBlueprint`'s FOUND-path override closes, reintroduced on
    the tool that returns `k` cards at once.

    Every returned card is already in scope (recall pre-filters `uses ⊆ scope`),
    so the union is in scope at write time; when scope later narrows past ANY of
    it, the WHOLE entry drops. Whole-entry granularity is coarse but fail-closed,
    and it is the granularity every other multi-column entry already has.

    Fail-closed three times over — an UNDETERMINED card footprint (`uses is
    None`; it should never reach here, the scope pre-filter drops those), a
    malformed `uses` key (via `_uses_to_provenance`), and a PRINTED COLUMN the
    union does not cover — each yields `None` for the whole set (undetermined →
    dropped from replay), never a partial or empty set that would fail open. NO
    cards → `frozenset()` (determined, zero columns → kept), which keeps the
    empty result byte-identical to its pre-enrichment behaviour.

    The coverage check runs against the UNION, not per card, because the entry's
    provenance IS the union: a column card A prints that lives in card B's `uses`
    is still in the claimed footprint, so the entry drops the moment that column
    leaves scope. Per-card checking would fail closed on that case for no gain.

    *serialised* must be the very dicts placed in `result_full` — the guard is
    derived from what is printed, so handing it anything else (a re-render, a
    subset) would measure the wrong thing.
    """
    union: set[str] = set()
    for card in cards:
        if card.uses is None:
            return None
        union |= card.uses
    provenance = _uses_to_provenance(frozenset(union))
    if provenance is None:
        return None
    qualified = {use.strip().lower() for use in union}
    bare = {use.rpartition(".")[2].strip().lower() for use in union}
    printed: set[str] = set()
    for entry in serialised:
        printed |= _card_printed_columns(entry)
    if any(not _is_covered(name, qualified=qualified, bare=bare) for name in printed):
        return None
    return provenance


class SearchBlueprintsTool(_ReadTool):
    """`searchBlueprints(query, k)` — reranked, scope-pre-filtered thin cards."""

    tool_name = "searchBlueprints"

    def __init__(
        self,
        *,
        pipeline: RetrievalPipeline,
        default_k: int,
        max_k: int,
        preview_row_count: int = 20,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
        disable_redaction: bool = False,
        max_result_tokens: int = _DEFAULT_MAX_TOOL_RESULT_TOKENS,
    ) -> None:
        super().__init__(
            observer=observer,
            tracer=tracer,
            disable_redaction=disable_redaction,
            max_result_tokens=max_result_tokens,
        )
        self._pipeline = pipeline
        self._default_k = default_k
        self._max_k = max_k
        self._preview_row_count = preview_row_count

    async def _execute(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ToolResult:
        query, err = _require_text(model_args.get("query"), "query")
        if err is not None:
            return self._error(INVALID_ARGS_CODE, err, retryable=True)
        k, k_err = _clamp_k(model_args.get("k"), default_k=self._default_k, max_k=self._max_k)
        if k_err is not None:
            return self._error(INVALID_ARGS_CODE, k_err, retryable=True)

        cards, reranked = await self._pipeline.search_blueprints(
            question=query,  # type: ignore[arg-type]
            column_scope=credentials.column_scope,
            k=k,  # type: ignore[arg-type]
        )
        entries = [_search_card(card) for card in cards]
        result_full: dict[str, Any] = {
            "count": len(cards),
            "degraded": not reranked,
            "blueprints": entries,
        }
        # NOT the inherited safe-empty frozenset(): an enriched card names
        # columns, so the entry carries the union of the cards' `uses` footprints
        # — and only when that union covers every column the SERIALISED cards
        # print, else `None` (undetermined, dropped fail-closed). Either way it
        # drops from D44 replay under a later scope narrowing; see
        # `_cards_to_provenance`.
        provenance = _cards_to_provenance(cards, entries)
        if provenance is None:
            # Degrade-not-fail, never SILENTLY: an undetermined footprint costs
            # the model this result on its next round-trip (D44 drops `None`
            # unconditionally), so it is logged server-side and emitted as
            # shape-only telemetry. Counts and the tool name only (D25) — never a
            # card id, a column name or the query.
            _logger.warning(
                "%s provenance undetermined (cards=%d): a returned card prints a "
                "column identifier its `uses` footprint does not cover, or a "
                "stored `uses` key is malformed; the trail entry will be dropped "
                "from replay",
                self.tool_name,
                len(cards),
            )
            self._observer(
                "retrieval_provenance_undetermined",
                {"tool_name": self.tool_name, "blueprints": len(cards)},
            )
        return self._ok(
            result_full,
            provenance=provenance,
            preview_row_count=self._preview_row_count,
        )


class SearchKnowledgeTool(_ReadTool):
    """`searchKnowledge(query)` — reranked knowledge chunks, scope-bypassed."""

    tool_name = "searchKnowledge"

    def __init__(
        self,
        *,
        pipeline: RetrievalPipeline,
        knowledge_k: int,
        preview_row_count: int = 20,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
        disable_redaction: bool = False,
        max_result_tokens: int = _DEFAULT_MAX_TOOL_RESULT_TOKENS,
    ) -> None:
        super().__init__(
            observer=observer,
            tracer=tracer,
            disable_redaction=disable_redaction,
            max_result_tokens=max_result_tokens,
        )
        self._pipeline = pipeline
        self._knowledge_k = knowledge_k
        self._preview_row_count = preview_row_count

    async def _execute(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ToolResult:
        query, err = _require_text(model_args.get("query"), "query")
        if err is not None:
            return self._error(INVALID_ARGS_CODE, err, retryable=True)

        hits, reranked = await self._pipeline.search_knowledge(
            question=query,  # type: ignore[arg-type]
            k=self._knowledge_k,
        )
        result_full: dict[str, Any] = {
            "count": len(hits),
            "degraded": not reranked,
            "knowledge": [
                {"id": hit.id, "text": hit.text, "score": hit.score, "title": hit.title}
                for hit in hits
            ],
        }
        return self._ok(result_full, preview_row_count=self._preview_row_count)


class GetBlueprintTool(_ReadTool):
    """`getBlueprint(id)` — keyed fetch of the stored projection; out-of-scope OR
    absent → the identical `{found: false}` (the §3 non-oracle)."""

    tool_name = "getBlueprint"

    def __init__(
        self,
        *,
        vector_index: VectorIndex,
        preview_row_count: int = 20,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
        disable_redaction: bool = False,
        max_result_tokens: int = _DEFAULT_MAX_TOOL_RESULT_TOKENS,
    ) -> None:
        super().__init__(
            observer=observer,
            tracer=tracer,
            disable_redaction=disable_redaction,
            max_result_tokens=max_result_tokens,
        )
        self._vector_index = vector_index
        self._preview_row_count = preview_row_count

    async def _execute(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ToolResult:
        blueprint_id, err = _require_text(model_args.get("id"), "id")
        if err is not None:
            return self._error(INVALID_ARGS_CODE, err, retryable=True)

        detail = await self._vector_index.get_blueprint(blueprint_id)  # type: ignore[arg-type]
        # A store failure returns None too (degrade), rendered identically to a
        # real miss. The scope check reuses the CANONICAL blueprint predicate:
        # `uses is None` (undetermined) → fail-closed → not-found, never fail-open.
        if detail is None or not scope_filter.is_blueprint_in_scope(
            Candidate(id=detail.id, kind="blueprint", text=detail.intent, uses=detail.uses),
            credentials.column_scope,
        ):
            return self._ok({"found": False}, preview_row_count=self._preview_row_count)

        # Determined at the scope check above: `detail.uses` is not None.
        uses = detail.uses if detail.uses is not None else frozenset()
        result_full: dict[str, Any] = {
            "found": True,
            "id": detail.id,
            "intent": detail.intent,
            "slots_summary": detail.slots_summary,
            # A sorted list is deterministic; `uses` is a subset of scope here
            # (the check above passed), and is column IDENTIFIERS, never data.
            "uses": sorted(uses),
            "status": detail.status,
            "drift_status": detail.drift_status,
            "hit_count": detail.hit_count,
            "catalog_sha": detail.catalog_sha,
        }
        # Additive full-DAG expansion (runblueprint-design §1.3) — the D8
        # progressive-disclosure "expand" step. Rendered ONLY when the blueprint
        # stored them (a DAG-less D87/D88 blueprint carries `None` → the FOUND shape
        # is byte-identical to before). The non-oracle {found:false} posture
        # (D88(b)) above is unchanged.
        _put_if_present(result_full, "resolves", detail.resolves)
        # ENRICH the model-facing `slots`: each slot carries a plain-English type
        # gloss, a required/optional `requirement`, and a `note` on the contract, so
        # the model grasps the nomenclature (period ≠ calendar date; optional omit =
        # no filter) instead of the raw JSON dicts. The raw typed `slots` the runtime
        # BINDS from are re-fetched by the executor from the store, untouched here.
        if detail.slots is not None:
            result_full["slots"] = [_enrich_slot(s) for s in detail.slots]
        _put_if_present(result_full, "uses_rules", detail.uses_rules)
        # Single-node `sql_template` is INTENTIONALLY kept exposed (progressive
        # disclosure) — do NOT "fix" the asymmetry with the composed-path DAG
        # hiding below. A single-node blueprint is one query with no internal step
        # order to confuse the model; the prompt's "NEVER hand-run" guidance is the
        # mitigation. The composed DAG is hidden because its per-node SQL / step
        # order / $0.x refs are the actual confusion vector, not leaf SQL itself.
        _put_if_present(result_full, "sql_template", detail.sql_template)
        # COMPOSED blueprints: hide the raw `composes` DAG (per-node SQL / feeds_from
        # / consumes / $0.x refs — the confusion vector) and show a compact "one
        # atomic call" note instead. This is PURELY model-facing: the executor
        # re-fetches the BlueprintDetail from the store and reads `detail.composes`
        # itself (blueprint/executor.py::execute), so it never depends on this
        # serialization. A DAG-less blueprint has no `composes` → no `composition`.
        if detail.composes:
            result_full["composition"] = _composition_annotation(detail.composes)
        _put_if_present(result_full, "result_grain", detail.result_grain)
        # S1: provenance is the blueprint's SCOPED uses footprint (NOT the
        # safe-empty frozenset()) — this is the same class of info getTableSchema
        # exposes (column identifiers) and, like it, must drop from D44 replay
        # under a later scope narrowing so a now-forbidden blueprint's existence +
        # footprint is not re-surfaced (06-security §scope table, §3).
        return self._ok(
            result_full,
            provenance=_uses_to_provenance(uses),
            preview_row_count=self._preview_row_count,
        )


__all__ = [
    "GetBlueprintTool",
    "SearchBlueprintsTool",
    "SearchKnowledgeTool",
]
