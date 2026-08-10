"""structural_key — the LOOSE, cross-authoring-path blueprint identity (PriorArtIndex).

The D48 hard key (`learning/dedup/canonical_key.py::compute_canonical_key`) hashes
`[resolves, uses_rules, result_grain, canonical_ast_norm]` and its inputs/order are
FROZEN (Contract C §3). That key is correct for the learning loop's own S4→S6 leg and
is NOT touched here — but it cannot match ACROSS authoring paths, because two of its
four members are effectively learning-only:

  * `resolves`   — absent from 9 of the 10 MCP-canon blueprint YAMLs; the S3 extractor
                   always populates it.
  * `uses_rules` — absent from 7 of 10 (canon's `uses` is a column-scope list, a
                   DIFFERENT thing).

So a hand-authored canon blueprint and a learning-extracted candidate that describe the
SAME query hash to two different canonical keys, and the learning loop re-proposes an
artifact the canon already carries. The `structural_key` is the SECOND, looser key that
closes that gap: it hashes ONLY the two inputs both authoring paths genuinely have.

    structural_key = sha256( canonical_json( [ grain_columns, structural_ast_norm ] ) )

Two normalizations are load-bearing:

  * **Grain shape AND casing.** Canon writes a BARE LIST (`result_grain: [Department]`);
    the learning side writes the D56 `{columns, verifiable}` stamp. Both collapse to the
    same sorted, deduplicated, LOWERCASED column list, and `verifiable` is DROPPED. The
    case fold is not cosmetic: 6 of the 10 canon blueprints capitalize a grain label whose
    SQL selects the lowercase alias, and since the AST half already case-folds identifiers,
    an unfolded grain would split the key on precisely the department-grouped blueprints
    this index exists to match. See `normalize_structural_grain`.

  * **`structural_ast_norm`, NOT `canonical_ast_norm`.** The key hashes its own render of
    the template, which is the frozen §11.2 recipe PLUS a standard-function-name fold and
    a comment strip — two differences the FROZEN key is not allowed to take, because its
    digests are already persisted in the corpus bucket. Canon writes `SUM`/`COUNT`
    uppercase and the S4 fixture writes `sum(...)` lowercase, so without that fold the two
    tiers miss on essentially every aggregate blueprint. Use `structural_key_from_templates`
    — it renders the right string for you; the raw `structural_key` will happily hash a
    `canonical_ast_norm` into a digest that matches nothing.

Like the frozen key's, the rendered string is a hash INPUT and is never re-parsed, and
its byte-stability rests on the pinned sqlglot version.

`canonical_json` + the `sha256:` prefix mirror `compute_canonical_key` exactly (D96 §5:
sorted keys, no insignificant whitespace, UTF-8, `ensure_ascii=False`), so the digest is
deterministic across processes and Python runs.

**Why this module lives under `runtime/blueprint/` and not next to the frozen key.**
The two writers that must agree byte-for-byte are `runtime/retrieval/corpus_loader.py`
(the MCP-canon seeder, imported by the online hydrator) and `learning/generalize/
mapping.py` (the learning landing seed). The D58c no-import invariant forbids any module
under `runtime/` from importing the learning package (a structural test enforces it), so
the single definition has to sit on the runtime side and be imported DOWNWARD by the
learning plane — which already depends on `runtime.blueprint`. A duplicated
implementation is the one failure mode that would defeat the whole point of the key, so
there is exactly one here.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize import normalize
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from .template import SLOT_TOKEN

# The pinned dialect for the §11.2 normalization recipe (D62). Changing it re-renders
# every AST and invalidates every stored key.
_DIALECT = "clickhouse"

# BUMP BY HAND whenever the structural normalization changes in a way that moves digests
# — a new fold, a changed grain rule, a different dialect. The sqlglot version is picked
# up automatically; this covers the half of the recipe that lives in this file.
_RECIPE_REVISION = 1


def structural_key_recipe() -> str:
    """The identity of the recipe that produced a `structural_key`, e.g. `"r1+sqlglot30.12.1"`.

    Stored on the node NEXT TO the key. Nothing reads it yet, and that is fine — the point
    is to make an asymmetry DETECTABLE that is otherwise silent:

      * MCP-canon keys are RE-DERIVED on every reseed, so they always track the current
        render.
      * Learning keys are stamped ONCE at landing and then persist on the node forever.

    So a sqlglot bump (or a hand edit to the fold) splits the two tiers: freshly reseeded
    canon nodes carry new digests while previously-landed learning nodes keep old ones.
    Every cross-tier lookup then misses — which is precisely the failure this key exists
    to prevent, arriving invisibly, and diagnosable only by noticing that prior-art hits
    stopped firing. With the recipe stamped, a reader can compare it against the running
    value and see the split immediately.

    Written now rather than when a reader needs it, because adding it later would mean
    backfilling every already-landed node — the keys are only re-derivable from a seed
    that the learning tier does not keep."""
    return f"r{_RECIPE_REVISION}+sqlglot{sqlglot.__version__}"


def canonical_json(obj: Any) -> str:
    """Canonical JSON (D96 §5): sorted keys, UTF-8, no insignificant whitespace.

    Byte-identical to `learning/dedup/canonical_key.py::_canonical_json` — the two keys
    must share one serialization convention or their digests are incomparable."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_ast_norm_one(sql_template: str) -> str:
    """Normalize a single template by the exact §11.2 recipe (D48). Assumes a parseable
    template; raises whatever `sqlglot` raises otherwise (callers decide fail-soft).

    The stored template is BRACE authoring form (`{slot}`); each `{slot}` is rewritten to
    `:slot` FIRST using the runtime binder's `SLOT_TOKEN` — the SAME rewrite the executor
    applies — so the hash input is computed from the identical `:slot` intermediate the
    runtime parses, regardless of the stored placeholder surface. A `:slot` parses to a
    sqlglot `Placeholder` and renders in the ClickHouse dialect as the stable token
    `{slot: }`, surviving the round-trip unchanged.

    The recipe is schema-free and deterministic on purpose — do NOT run the full
    optimizer or `qualify`, which need a schema and choke on slot placeholders:

        SLOT_TOKEN.sub `{slot}` -> `:slot`
          -> parse_one(dialect="clickhouse")
          -> normalize_identifiers   (case-fold)
          -> normalize               (canonical boolean form)
          -> .sql(dialect="clickhouse", normalize=True, pretty=False)

    **HASH-INPUT ONLY.** The returned string is never re-parsed. It MUST be byte-stable
    across builds/CI, which is why the sqlglot version is pinned (`~=30.12`): a minor
    bump can change the render, mint a different key, and degrade a D48 `increment` into
    a spurious `insert` (a duplicate blueprint).

    **DO NOT add normalization here.** This function's output is an input to the FROZEN
    D48 `canonical_key` (S4 computes it, S6 hashes it, the digest is persisted in the
    Couchbase corpus bucket). Any change re-keys every stored artifact: every landed
    blueprint becomes unreachable and the loop re-proposes the entire corpus. The looser
    `structural_ast_norm` below is where cross-tier normalization belongs."""
    return _normalized_ast(sql_template).sql(dialect=_DIALECT, normalize=True, pretty=False)


def _normalized_ast(sql_template: str) -> exp.Expression:
    """The shared §11.2 parse+normalize pipeline both renderers start from."""
    colon_template = SLOT_TOKEN.sub(lambda m: f":{m.group(1)}", sql_template)
    ast = sqlglot.parse_one(colon_template, dialect=_DIALECT)
    ast = normalize_identifiers(ast, dialect=_DIALECT)
    return normalize(ast)


# Function nodes whose NAME IS DATA — sqlglot stores the user's spelling in the node's
# `this` arg because it did not recognize the function, and the generator echoes it back
# verbatim. These are exactly the nodes the casing fold must NOT touch: an unrecognized
# name is (in ClickHouse) case-SENSITIVE and canonical as authored.
#
# The list is explicit rather than `exp.Anonymous` alone because sqlglot's "unrecognized"
# hierarchy is NOT rooted at `Anonymous` (QA finding). In sqlglot 30.12 there are three
# roots — an unrecognized scalar is `Anonymous`, an unrecognized AGGREGATE is
# `AnonymousAggFunc` (`uniqExact`, `anyLast`, `groupArray`), and a parameterized one is
# `ParameterizedAgg` — and NEITHER agg root subclasses `Anonymous`. `CombinedAggFunc`
# (`sumIf`, `sumMerge`) and `CombinedParameterizedAgg` inherit from those, so subclass
# checks cover the whole family.
_NAME_CARRYING_FUNCS: tuple[type[exp.Expression], ...] = (
    exp.Anonymous,
    exp.AnonymousAggFunc,  # covers CombinedAggFunc
    exp.ParameterizedAgg,  # covers CombinedParameterizedAgg
)


def structural_ast_norm_one(sql_template: str) -> str:
    """The §11.2 recipe PLUS the two cross-tier folds the FROZEN key cannot take.

    Identical to `canonical_ast_norm_one` except for two post-passes on the normalized
    AST. Both exist because the canon tier is HAND-authored and the learning tier is
    LLM-authored, so they differ systematically on things that carry no meaning:

      * **Recognized function names are folded to sqlglot's canonical spelling.** Every
        one of the 10 MCP-canon blueprints writes `SUM`/`COUNT`/`AVG` uppercase; the
        frozen S4 fixture writes `sum(...)` lowercase. `normalize_identifiers` folds
        IDENTIFIERS and never touches function names, so the two tiers sat on opposite
        sides of a fold that never happened — a miss on all 10 aggregate blueprints.

        Mechanism: drop the parser's recorded spelling (`meta["name"]`) so the generator
        falls back to the node's own canonical name. It is deliberately NOT a blanket
        `normalize_functions="upper"`, which also uppercases `toFloat64` -> `TOFLOAT64`.

        **What the fold rests on.** The safety argument is `_NAME_CARRYING_FUNCS`: for a
        node sqlglot RECOGNIZED, the name is redundant with the node type, so re-deriving
        it is lossless; for a node it did NOT recognize, the name is the only record of
        which function was called, and ClickHouse treats it case-sensitively. The
        exclusion list enumerates all three unrecognized roots explicitly. Do NOT reduce
        it to `not isinstance(node, exp.Anonymous)` — that reads as if it covered the
        unrecognized set and does not: it lets the fold pop `meta["name"]` on the entire
        `AnonymousAggFunc` family, which in 30.12 is harmless ONLY because sqlglot leaves
        `meta` empty for those nodes. That is an incidental property of the pinned
        version, not a guarantee, and a bump that started recording it would silently
        re-key `uniqExact` -> `UNIQEXACT` across the whole corpus.

        **Coverage is standard SQL functions only.** ClickHouse recognition is
        case-sensitive, so `uniqExact` parses to `AnonymousAggFunc` while `UNIQEXACT`
        parses to `Anonymous` — two node types, two keys, no fold. The
        `uniq*`/`-If`/`-Merge`/`anyLast`/`groupArray` family therefore does NOT
        casing-normalize. Defensible (`UNIQEXACT` is not a valid ClickHouse function) but
        it is a real residual gap, not an oversight. `countIf` is the exception that
        proves the split: it is a first-class `exp.CountIf`, recognized case-insensitively,
        and already collapsed before this slice existed.

        Related residual, NOT addressed by the fold: `toStartOfMonth(d)` is `TimestampTrunc`
        (recognized — the FROZEN recipe already rewrites it to `dateTrunc('MONTH', d)`),
        so a template authored as `dateTrunc('month', d)` still mints a different key
        because the UNIT LITERAL casing differs. Literals are values, not names; folding
        them is out of scope here.

      * **Comments are stripped.** They are preserved through the recipe by default (a
        `--` line comment is even re-rendered as `/* ... */`), so an explanatory comment
        in a hand-authored canon YAML would make that blueprint unmatchable. No canon
        template carries one today; the YAMLs are hand-authored and commented elsewhere
        in the same files, so this is a plausible next-blueprint regression.

    **HASH-INPUT ONLY**, same as the frozen recipe: never re-parsed, byte-stability
    depends on the pinned sqlglot version — which is why `structural_key_recipe()`
    stamps that version onto every node beside the key."""
    ast = _normalized_ast(sql_template)
    for node in ast.walk():
        node.comments = None
        if isinstance(node, exp.Func) and not isinstance(node, _NAME_CARRYING_FUNCS):
            node.meta.pop("name", None)
    return ast.sql(dialect=_DIALECT, normalize=True, pretty=False)


def blueprint_canonical_ast_norm(
    sql_template: str | None,
    node_templates: Sequence[tuple[int, str]] = (),
) -> str:
    """The single- OR composite-blueprint canonical string (§11.2 composite rule).

    Single: *sql_template* is the one top-level template.
    Composite: *sql_template* is None; the per-node `(order, sql_template)` pairs are
    normalized individually and joined by a SINGLE newline in ASCENDING `order`.

    The composite rule is PINNED so the S4 producer and every other hasher cannot
    diverge; a node carrying no template contributes nothing (canon authors an
    output-only DAG node without SQL, and an empty line would shift the join)."""
    return _join_nodes(canonical_ast_norm_one, sql_template, node_templates)


def structural_ast_norm(
    sql_template: str | None,
    node_templates: Sequence[tuple[int, str]] = (),
) -> str:
    """The single- OR composite-blueprint STRUCTURAL string — `structural_ast_norm_one`
    under the same §11.2 composite join. This is the string `structural_key` hashes."""
    return _join_nodes(structural_ast_norm_one, sql_template, node_templates)


def _join_nodes(
    render: Callable[[str], str],
    sql_template: str | None,
    node_templates: Sequence[tuple[int, str]],
) -> str:
    """Apply *render* to one top-level template, or to each composite node in ASCENDING
    `order` joined by a SINGLE newline. Shared so the frozen and structural renderers can
    never disagree about the composite rule itself.

    NOTE — a behavior change to the FROZEN composite path, made when this helper was
    extracted, in a module that otherwise says "do not add normalization here": the
    `if tpl` filter SKIPS a node whose template is empty/blank, where the previous code
    passed it to `parse_one` and raised `ParseError`. So an empty-template composite node
    now yields a digest instead of no digest. It is unreachable from S4 — `NodeTemplate`
    requires a non-empty `sql_template` and `builder.py` only emits nodes it rewrote — and
    nothing persisted is affected (verified: every stored `canonical_key` digest is
    unchanged). It exists so the CANON seeder can carry an output-only DAG node without
    SQL, which canon does author, and because contributing an empty line would shift the
    join for every following node. Recorded because the skip rests on that S4 invariant
    rather than on the frozen contract."""
    if sql_template is not None:
        return render(sql_template)
    ordered = sorted(node_templates, key=lambda pair: pair[0])
    return "\n".join(render(tpl) for _, tpl in ordered if tpl)


def normalize_structural_grain(result_grain: Any) -> list[str] | None:
    """Collapse EITHER grain authoring shape onto one canonical column list, or return
    `None` meaning the grain is UNUSABLE and no key may be minted.

    Accepts the canon BARE LIST (`["month"]`), the learning D56 stamp
    (`{"columns": [...], "verifiable": bool}`), or `None`/absent.

    **Container vs. member — the two failure modes are treated differently on purpose.**
    An unrecognized CONTAINER (`None`, a bare string, a number) normalizes to `[]`, i.e.
    "no grain declared" — which is a legitimate, common shape (canon omits `result_grain`
    entirely, and `bp-hires-projection` writes `[]`), so reading it that way is the
    conservative match. A recognized container holding a NON-STRING MEMBER
    (`result_grain: [~]` from a dangling YAML `-`, or an unquoted `[2024]`) is an
    authoring BUG, and it makes the WHOLE grain unusable: previously `str(col)` coerced a
    null into the literal column `"none"`, which then collided with a real column named
    `none`. A confident wrong key is worse than an absent one, and silently dropping the
    bad member would still mint a confident key from a guess at what the author meant.
    `None` here propagates to no key at all.

    `{"columns": "month"}` — a recognized container whose `columns` is a bare string —
    sits between the two and routes to `[]`. That is a DON'T-CARE, not a considered
    classification: `ResultGrain.parse` rejects the shape upstream, so it cannot reach a
    real seed from either authoring path. If it ever became reachable it belongs in the
    `None` branch by the principle above.

    Normalizations applied to a usable grain, each load-bearing for cross-tier matching:

      * `verifiable` is DROPPED — canon has no equivalent field, so hashing it would
        make every cross-tier comparison miss.

      * Entries are NFC-normalized, whitespace-stripped, and LOWERCASED. The case fold is
        the MAJORITY case, not an edge case: 6 of the 10 MCP-canon blueprints declare
        `result_grain: [Department]` while their SQL selects `AS department`. The AST half
        of the key already folds identifiers (`normalize_identifiers`), so folding here is
        what stops the grain half from disagreeing on exactly the department-grouped
        blueprints the learning loop is most likely to re-derive. Grain entries are
        OUTPUT-COLUMN display labels for a `SELECT ... AS x`, not case-distinguishing
        identifiers, so folding loses no meaning. **Do not "fix" this back to
        case-sensitive** — see
        `test_case_differences_must_not_split_the_key_canon_capitalizes_grain_labels`.

        The fold is `str.lower()`, and that spelling is PINNED with the key exactly like
        the sqlglot version is. `str.casefold()` is the Unicode-aggressive fold (`ß` ->
        `ss`); switching to it as a "correctness" improvement would silently re-key every
        non-ASCII grain and orphan the stored digests. NFC runs LAST so a precomposed
        `Département` and its combining-accent spelling agree.

      * Empty entries are DROPPED (a stray `-` in a YAML block list strips to `""`, sorts
        FIRST, and would otherwise mint a key nothing can match).

      * Entries are DEDUPLICATED and then SORTED — the same `sorted(set(...))` treatment
        `compute_canonical_key` applies to `uses_rules` (QA-Q5). Dedup must follow the
        fold, because the fold is what CREATES the duplicate: `["Department",
        "department"]` are two distinct strings pre-fold and one column post-fold. Sorting
        is required because `canonical_json` preserves list order (QA-Q6).
    """
    if isinstance(result_grain, Mapping):
        columns = result_grain.get("columns")
    elif isinstance(result_grain, (list, tuple)):
        columns = result_grain
    else:
        columns = None
    if not isinstance(columns, (list, tuple)):
        return []
    folded: set[str] = set()
    for col in columns:
        if not isinstance(col, str):
            return None  # malformed member ⇒ the whole grain is unusable ⇒ no key
        entry = unicodedata.normalize("NFC", col.strip().lower())
        if entry:
            folded.add(entry)
    return sorted(folded)


def structural_key(result_grain: Any, structural_ast_norm: str) -> str:
    """The `sha256:`-prefixed LOOSE key over `[grain_columns, structural_ast_norm]`.

    *result_grain* may be the canon bare list or the learning `{columns, verifiable}`
    dict — both collapse through `normalize_structural_grain`.

    *structural_ast_norm* MUST be a `structural_ast_norm`/`structural_ast_norm_one`
    render, NOT a `canonical_ast_norm` one. The two differ by the function-name fold and
    comment strip, so passing the frozen render here mints a key that matches nothing.
    The parameter is named for the required input precisely so the mistake is visible at
    the call site; `structural_key_from_templates` is the safe entry point for any caller
    holding templates. It is OPAQUE and hashed as given — never re-parsed.

    Returns the EMPTY STRING — meaning NO key — in two cases, both fail-soft (D52):

      * a blank *structural_ast_norm*: the digest would degenerate to a hash of the grain
        alone, collapsing every unparseable blueprint of that grain into one bogus prior-
        art match;
      * an UNUSABLE grain (`normalize_structural_grain` returned `None`).

    Both writers map an empty return to an ABSENT node property rather than storing
    `""`, which a naive equality lookup would match against every keyless blueprint."""
    norm = (structural_ast_norm or "").strip()
    if not norm:
        return ""
    grain = normalize_structural_grain(result_grain)
    if grain is None:
        return ""
    parts: list[Any] = [grain, norm]
    digest = hashlib.sha256(canonical_json(parts).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def structural_key_from_templates(
    result_grain: Any,
    sql_template: str | None,
    node_templates: Iterable[tuple[int, str]] = (),
) -> str:
    """The SAFE entry point: `structural_key` for any caller holding TEMPLATES.

    Both production writers use this — the MCP-canon seeder (canon YAMLs carry a
    `sql_template`/`composes` and no precomputed norm at all) and the learning landing
    seed (which holds S4's templates alongside its frozen `canonical_ast_norm`). Routing
    both through one function is what guarantees they render the SAME
    `structural_ast_norm` and therefore the same digest; a caller that reached for the
    raw `structural_key` with a `canonical_ast_norm` string in hand would silently mint
    a non-matching key.

    FAIL-SOFT: an unparseable template (sqlglot chokes on an exotic placeholder or a
    dialect gap) returns `""` rather than raising. This is defensive DEPTH, not the
    active load-path policy — `corpus_loader._validate_blueprint_dag` already rejects
    every template this recipe would reject, in an earlier unconditional pass, and
    aborts the whole load fail-CLOSED. The guard matters only if the recipe ever becomes
    stricter than loader validation."""
    try:
        norm = structural_ast_norm(sql_template, tuple(node_templates))
    except Exception:  # noqa: BLE001 - an unparseable template yields NO key, never a raise
        return ""
    return structural_key(result_grain, norm)


__all__ = [
    "blueprint_canonical_ast_norm",
    "canonical_ast_norm_one",
    "canonical_json",
    "normalize_structural_grain",
    "structural_ast_norm",
    "structural_ast_norm_one",
    "structural_key",
    "structural_key_from_templates",
    "structural_key_recipe",
]
