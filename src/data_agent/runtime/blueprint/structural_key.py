"""structural_key — the LOOSE, cross-authoring-path blueprint identity (PriorArtIndex).

The D48 hard key (`learning/dedup/canonical_key.py::compute_canonical_key`) hashes
`[resolves, uses_rules, result_grain, canonical_ast_norm]`, and its inputs and order are
FROZEN. That key is correct for the learning loop's own leg and is NOT touched here, but it
cannot match ACROSS authoring paths, because two of its four members are effectively
learning-only: `resolves` and `uses_rules` are absent from most MCP-canon YAMLs. So a
hand-authored canon blueprint and a learning-extracted candidate that describe the SAME
query hash to two different keys, and the loop re-proposes what canon already carries.

This is the SECOND, looser key that closes that gap, hashing ONLY the two inputs both
authoring paths genuinely have:

    structural_key = sha256( canonical_json( [ grain_columns, structural_ast_norm ] ) )

Two normalizations are load-bearing:

  * GRAIN SHAPE AND CASING. Canon writes a BARE LIST (`result_grain: [Department]`); the
    learning side writes the D56 `{columns, verifiable}` stamp. Both collapse to the same
    sorted, deduplicated, LOWERCASED column list, and `verifiable` is DROPPED. The case fold
    is not cosmetic: the AST half already case-folds identifiers, so an unfolded grain would
    split the key on precisely the department-grouped blueprints this index exists to match.
  * `structural_ast_norm`, NOT `canonical_ast_norm`. The key hashes its own render — the
    frozen recipe PLUS a standard-function-name fold and a comment strip, two differences
    the FROZEN key is not allowed to take because its digests are already persisted. Use
    `structural_key_from_templates`; the raw `structural_key` will happily hash a
    `canonical_ast_norm` into a digest that matches nothing.

The rendered string is a hash INPUT, never re-parsed, and its byte-stability rests on the
pinned sqlglot version. `canonical_json` and the `sha256:` prefix match
`compute_canonical_key` exactly, so the digest is deterministic across processes.

It lives under `runtime/blueprint/` because the two writers that must agree byte-for-byte
are `runtime/blueprint/compiler.py` and `learning/generalize/mapping.py`, and the D58c
no-import invariant forbids any module under `runtime/` from importing the learning package.
So the single definition sits on the runtime side and is imported DOWNWARD — a duplicated
implementation is the one failure mode that would defeat the whole point of the key.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize import normalize
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from data_agent.canonical import canonical_json

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

        Stored on the node NEXT TO the key. Nothing reads it yet, and that is fine — the point is
        to make an asymmetry DETECTABLE that is otherwise silent: MCP-canon keys are RE-DERIVED
        on every reseed and so always track the current render, while learning keys are stamped
        ONCE at landing and persist forever. A sqlglot bump therefore splits the two tiers and
        every cross-tier lookup misses — the exact failure this key exists to prevent, arriving
        invisibly and diagnosable only by noticing that prior-art hits stopped firing.

        Written now rather than when a reader needs it, because adding it later would mean
        backfilling every landed node from a seed the learning tier does not keep.
    """
    return f"r{_RECIPE_REVISION}+sqlglot{sqlglot.__version__}"


def canonical_ast_norm_one(sql_template: str) -> str:
    """Normalize a single template by the exact frozen D48 recipe. Assumes a parseable
        template; raises whatever `sqlglot` raises otherwise, so callers decide fail-soft.

        The stored template is BRACE authoring form (`{slot}`); each `{slot}` is rewritten to
        `:slot` FIRST using the runtime binder's `SLOT_TOKEN` — the SAME rewrite the executor
        applies — so the hash input is computed from the identical intermediate the runtime
        parses, regardless of the stored placeholder surface.

        The recipe is schema-free and deterministic on purpose; do NOT run the full optimizer or
        `qualify`, which need a schema and choke on slot placeholders:

            SLOT_TOKEN.sub `{slot}` -> `:slot`
              -> parse_one(dialect="clickhouse")
              -> normalize_identifiers   (case-fold)
              -> normalize               (canonical boolean form)
              -> .sql(dialect="clickhouse", normalize=True, pretty=False)

        HASH-INPUT ONLY: the returned string is never re-parsed, and it MUST be byte-stable
        across builds, which is why the sqlglot version is pinned — a minor bump can change the
        render, mint a different key, and degrade a D48 `increment` into a spurious `insert`.

        DO NOT add normalization here. This output is an input to the FROZEN `canonical_key`,
        whose digests are persisted in the corpus bucket: any change re-keys every stored
        artifact, making every landed blueprint unreachable and re-proposing the entire corpus.
        The looser `structural_ast_norm` below is where cross-tier normalization belongs.
    """
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
    """The frozen recipe PLUS the two cross-tier folds the FROZEN key cannot take.

        Both exist because the canon tier is HAND-authored and the learning tier is LLM-authored,
        so they differ systematically on things that carry no meaning.

        RECOGNIZED FUNCTION NAMES ARE FOLDED to sqlglot's canonical spelling. Every MCP-canon
        blueprint writes `SUM`/`COUNT`/`AVG` uppercase while the frozen fixture writes them
        lowercase, and `normalize_identifiers` folds IDENTIFIERS and never touches function
        names — a miss on every aggregate blueprint. The mechanism drops the parser's recorded
        spelling (`meta["name"]`) so the generator falls back to the node's own canonical name;
        it is deliberately NOT a blanket `normalize_functions="upper"`, which would also
        uppercase `toFloat64`.

        What the fold rests on is `_NAME_CARRYING_FUNCS`: for a node sqlglot RECOGNIZED, the name
        is redundant with the node type, so re-deriving it is lossless; for a node it did NOT
        recognize, the name is the only record of which function was called, and ClickHouse
        treats it case-sensitively. Do NOT reduce that list to `not isinstance(node,
        exp.Anonymous)` — it reads as if it covered the unrecognized set and does not, letting
        the fold pop `meta["name"]` on the entire `AnonymousAggFunc` family, which is harmless
        ONLY because the pinned sqlglot happens to leave `meta` empty for those nodes.

        Coverage is standard SQL functions only. ClickHouse recognition is case-sensitive, so
        `uniqExact` and `UNIQEXACT` parse to different node types and never fold — a real
        residual gap, not an oversight. Related residual: a template authored as
        `dateTrunc('month', d)` still mints a different key from `toStartOfMonth(d)`, because the
        unit LITERAL casing differs and literals are values, not names.

        COMMENTS ARE STRIPPED. They are preserved through the recipe by default, so an
        explanatory comment in a hand-authored canon YAML would make that blueprint unmatchable.

        HASH-INPUT ONLY, same as the frozen recipe: never re-parsed, byte-stability depends on
        the pinned sqlglot version — which is why `structural_key_recipe()` stamps that version
        onto every node beside the key.
    """
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
    """The single- OR composite-blueprint canonical string.

        Single: *sql_template* is the one top-level template. Composite: *sql_template* is None,
        and the per-node `(order, sql_template)` pairs are normalized individually and joined by
        a SINGLE newline in ASCENDING `order`.

        The composite rule is PINNED so the producer and every other hasher cannot diverge; a
        node carrying no template contributes nothing, since an empty line would shift the join.
    """
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

        NOTE — a behaviour change to the FROZEN composite path, made when this helper was
        extracted: the `if tpl` filter SKIPS a node whose template is empty or blank, where the
        previous code passed it to `parse_one` and raised. It is unreachable from the learning
        producer (`NodeTemplate` requires a non-empty template) and nothing persisted is
        affected; it exists so the CANON seeder can carry an output-only DAG node without SQL,
        and because contributing an empty line would shift the join. Recorded because the skip
        rests on that producer invariant rather than on the frozen contract.
    """
    if sql_template is not None:
        return render(sql_template)
    ordered = sorted(node_templates, key=lambda pair: pair[0])
    return "\n".join(render(tpl) for _, tpl in ordered if tpl)


def normalize_structural_grain(result_grain: Any) -> list[str] | None:
    """Collapse EITHER grain authoring shape onto one canonical column list, or return `None`
        meaning the grain is UNUSABLE and no key may be minted.

        Accepts the canon BARE LIST (`["month"]`), the learning D56 stamp
        (`{"columns": [...], "verifiable": bool}`), or `None`/absent.

        CONTAINER VS MEMBER — the two failure modes are treated differently on purpose. An
        unrecognized CONTAINER (`None`, a bare string, a number) normalizes to `[]`, i.e. "no
        grain declared", which is a legitimate and common shape. A recognized container holding a
        NON-STRING MEMBER (a dangling YAML `-`, an unquoted `[2024]`) is an authoring BUG that
        makes the WHOLE grain unusable: `str(col)` previously coerced a null into the literal
        column `"none"`, which then collided with a real column named `none`. A confident wrong
        key is worse than an absent one, and silently dropping the bad member would still mint a
        confident key from a guess at what the author meant.

        Normalizations applied to a usable grain, each load-bearing for cross-tier matching:

          * `verifiable` is DROPPED — canon has no equivalent field, so hashing it would make
            every cross-tier comparison miss.
          * Entries are NFC-normalized, whitespace-stripped and LOWERCASED. The case fold is the
            MAJORITY case, not an edge case: most canon blueprints declare a capitalized grain
            label whose SQL selects the lowercase alias, and the AST half of the key already
            folds identifiers. Grain entries are OUTPUT-COLUMN display labels, not
            case-distinguishing identifiers, so folding loses no meaning. DO NOT "fix" this back
            to case-sensitive.

            The fold is `str.lower()`, and that spelling is PINNED with the key exactly like the
            sqlglot version. `str.casefold()` is the Unicode-aggressive fold (`ß` -> `ss`), and
            switching to it as a correctness improvement would silently re-key every non-ASCII
            grain and orphan the stored digests. NFC runs LAST so a precomposed accent and its
            combining spelling agree.
          * Empty entries are DROPPED (a stray `-` strips to `""`, sorts FIRST, and would
            otherwise mint a key nothing can match).
          * Entries are DEDUPLICATED and then SORTED. Dedup must FOLLOW the fold, because the
            fold is what CREATES the duplicate; sorting is required because `canonical_json`
            preserves list order.
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

        *result_grain* may be the canon bare list or the learning `{columns, verifiable}` dict —
        both collapse through `normalize_structural_grain`.

        *structural_ast_norm* MUST be a `structural_ast_norm`/`structural_ast_norm_one` render,
        NOT a `canonical_ast_norm` one: the two differ by the function-name fold and the comment
        strip, so passing the frozen render here mints a key that matches nothing. The parameter
        is named for the required input precisely so the mistake is visible at the call site, and
        `structural_key_from_templates` is the safe entry point. The value is OPAQUE and hashed
        as given, never re-parsed.

        Returns the EMPTY STRING — meaning NO key — in two fail-soft cases (D52): a blank
        *structural_ast_norm*, whose digest would degenerate to a hash of the grain alone and
        collapse every unparseable blueprint of that grain into one bogus prior-art match; and an
        UNUSABLE grain. Both writers map an empty return to an ABSENT node property rather than
        storing `""`, which a naive equality lookup would match against every keyless blueprint.
    """
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

        Both production writers use this — the MCP-canon seeder and the learning landing seed —
        which is what guarantees they render the SAME `structural_ast_norm` and therefore the
        same digest. A caller that reached for the raw `structural_key` with a
        `canonical_ast_norm` string in hand would silently mint a non-matching key.

        FAIL-SOFT: an unparseable template returns `""` rather than raising. This is defensive
        DEPTH, not the active load-path policy — `compiler.validate_blueprint_dag` already
        rejects every template this recipe would reject, in an earlier unconditional pass. The
        guard matters only if the recipe ever becomes stricter than loader validation.
    """
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
