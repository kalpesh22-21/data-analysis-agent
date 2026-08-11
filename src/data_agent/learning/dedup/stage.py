"""DedupStage — the blueprint dedup `CandidateStage` (D48 Slice 6, + PriorArt Slice 2).

Runs third in the write-router pipeline (generalize → leakage → **dedup** → writer)
and writes `envelope.dedup` (Contract C, `DedupVerdict`).

**Three layers now, in strictly decreasing certainty.** The order is the design: a
deterministic answer is always preferred to a probabilistic one, and only the two
deterministic layers are allowed to discard a candidate.

  1. **Hard key** (`canonical_key`, D48 §3) against the `learning_corpus` bucket. The
     SHA-256 over `(resolves, uses_rules, result_grain, canonical_ast_norm)`. Race-safe
     by construction — two workers hashing the same candidate always agree — and that
     property is load-bearing for the cross-session hit count, which is why this stays
     the first layer and stays unchanged. A HIT ⇒ `action="increment"`: bump the
     EXISTING artifact's `hit_count` and DROP the duplicate (the count lives on the
     artifact, not the envelope, §11.1).

  2. **Structural key** (`runtime/blueprint/structural_key.py`) against the
     `PriorArtIndex`. THE NEW LAYER, and the reason this slice exists. The hard key
     cannot match across authoring paths — two of its four inputs (`resolves`,
     `uses_rules`) are effectively learning-only, absent from 9-of-10 and 7-of-10 of
     the MCP-canon YAMLs respectively — so a hand-authored canon blueprint and a
     learning candidate describing the SAME query hash to different canonical keys. The
     structural key hashes only what both paths genuinely have. A hit is an identity
     claim, not a similarity guess, so it is allowed to be terminal:
       * against the **MCP canon** ⇒ `redundant_with_canon`, DROP. `increment` would be
         wrong here: there is no corpus artifact behind a git-versioned blueprint and no
         count the loop owns. See below for why this is counted, not just logged.
       * against the **learning tier** (or an unsourced node) ⇒ `merge`, routed to a
         human. We may well already own it, but we cannot bump a count we cannot key,
         and an unsourced node is not evidence of canon.

  3. **Soft layer** — embedding similarity on `intent`, over the UNION of BOTH stores:
     the `PriorArtIndex` (the graph — canon + landed learning tier) AND the
     `learning_corpus` bucket (candidates that have NOT landed yet, which the graph
     cannot see at all). A near-match with a DIFFERENT key is NEVER auto-appended and
     never dropped (§3): `merge` (a mergeable variant) or `conflict` (partial overlap),
     both routed to the inbox by the writer. Below the band ⇒ `insert`.

     The union is a REGRESSION FIX, not an optimization — see `_soft_layer`. Treating
     the bucket as a mere fallback silently removed the only check that caught two
     concurrent sessions proposing the same blueprint.

**Why `redundant_with_canon` is COUNTED.** A high rate of it is not the loop working
well — it is a RETRIEVAL defect surfacing in the learning loop. The agent had a
blueprint for this question, failed to recall it, the analyst wrote the SQL by hand, and
the loop then re-derived what we already own. That signal is only visible here, so the
verdict is emitted on a `learning.dedup` span (shape-only attributes) as well as logged,
making the rate queryable rather than anecdotal.

**Fail-soft (D52), extended.**
  * No `canonical_ast_norm` (S4 could not produce one) ⇒ the hard key is SKIPPED; a
    missing template never mints a spurious key.
  * No structural key (the templates do not normalize) ⇒ layer 2 is skipped.
  * **The prior-art index is FAIL-OPEN.** An unreachable/unconfigured graph raises
    `PriorArtUnavailableError`, and that half of the soft union simply contributes
    nothing, with a LOUD log; the bucket half still runs. Never stop learning because a
    graph read failed. The residual exposure is stated plainly: while the index is down
    the loop cannot see the canon or the landed tier, so it will mint duplicates it would
    otherwise have dropped. That is strictly better than dropping the session, and the
    log is what makes the window visible. Layer 2 (the canon-redundancy drop) simply does
    not fire during that window.
  * A degraded/failing embedder, or a failed corpus scan, contributes an empty bucket
    half — never a wrong `merge`.

**A FOURTH, optional adjudicator sits behind the soft layer (plan §3b).** When a
`CoverageJudge` is wired, a soft-layer best score inside the ambiguous band
(~0.70-0.97) is put to a model: "does this artifact already do what the candidate
does?" Outside the band nothing is asked, because outside it the answer is obvious and
free — below, nothing is close; above, the deterministic layers and the merge routing
already have an opinion.

It runs HERE rather than as a new pipeline stage for two reasons. The stage order is
frozen (D102 §7.1), and this is not a new decision so much as a better-informed version
of the one the soft layer is already making — it needs exactly the cards the soft layer
just retrieved, and a separate stage would either re-embed and re-search (paying the
soft layer's cost twice) or adjudicate a set the routing never saw.

The judge may only ever DROP. It never softens a verdict, never turns a `merge` into an
`insert`, and never advances a candidate past a gate — so the worst a mis-tuned judge
can do is discard work, which is why it drops only on positive evidence and writes a
durable `learning_audit` record before it does. Note that a `learning.dedup` span is
emitted for a judge-dropped candidate exactly as it would be otherwise: it faithfully
reports what DEDUP decided, and the drop is reported on the `learning.judge` span. So a
dedup span is not a claim that the candidate survived the pipeline.

**Two plan-§4 side outputs ride along, because this is the only stage that has already
paid for the embed and the graph query.**

  * `envelope.novelty` — the inbox-ranking novelty axis, computed from the GRAPH cards
    ALONE (never the bucket half). See `_soft_layer` and `_novelty_from`.
  * `CorpusArtifact.recurrence_count` — a DORMANT (weight 0.0) counter of paraphrase
    sightings, bumped for each `learning_corpus` artifact inside the recurrence band.
    See `_bump_recurrence`.

Neither can change a dedup verdict, and both fail soft.

The stage only adjudicates BLUEPRINTS (both keys are AST-derived). Non-blueprint targets
pass through untouched (`dedup` stays `None`); the writer stage routes them.

Thresholds, the corpus, the embedder and the prior-art index are all injected
(composition-root config knobs), so nothing here reads global settings and tests stay
hermetic.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace

from ..candidate.models import CandidateEnvelope
from ..candidate.signals import NoveltyStamp
from ..candidate.verdicts import DedupVerdict
from ..judge import CoverageJudge
from ..observability import dedup_span
from ..priorart import (
    TIER_LEARNING,
    TIER_MCP,
    PriorArtCard,
    PriorArtIndex,
    PriorArtUnavailableError,
)
from ..stage import StageContext, StageResult
from .canonical_key import compute_canonical_key
from .corpus import BlueprintCorpus, CorpusArtifact

_logger = logging.getLogger(__name__)

# The near-miss bands. These are the CONSTRUCTOR defaults only — production reads them
# off `LearningSettings.learning_dedup_{merge,conflict}_threshold` and the composition
# root passes them in, so an operator can retune without a code change. They stay here
# (rather than becoming a settings import) because this stage must not read global
# settings: injection is what keeps the unit suite hermetic (§11).
_DEFAULT_MERGE_THRESHOLD = 0.95
_DEFAULT_CONFLICT_THRESHOLD = 0.83

# How many prior-art cards the soft layer asks for. Small on purpose: the soft layer only
# ever uses the single best card, and every extra card is a row neo4j sorts and a row a
# future judge would have to be shown. Enough to survive a couple of near-ties.
_DEFAULT_PRIOR_ART_LIMIT = 5

# Cosine at/above which a candidate's intent counts as a SOFT recurrence sighting of an
# existing artifact (plan §4). A constructor default, like the bands above; the
# composition root passes `LearningSettings.learning_recurrence_similarity_threshold`.
#
# Set BELOW the merge band (0.95) and ABOVE the conflict band (0.83) on purpose. The soft
# counter exists precisely because the hard key never fires, so it must catch the
# paraphrase pair that mints two different canonical keys — those measure around 0.96 in
# practice (QA measured a real one at 0.9645). Setting it at the conflict band would
# count "vaguely related question" as a recurrence and make the counter meaningless
# before anyone ever looks at it.
_DEFAULT_RECURRENCE_THRESHOLD = 0.90


class _EmbeddingClient:  # structural doc only — the injected embedder duck-types this
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _text(raw: object) -> str:
    """A rehydrated JSON value as a stripped `str`, or `""`.

    NOT `(raw or "").strip()`, which is what this replaced: that raises AttributeError on
    a non-string truthy value (`5`, `["SELECT 1"]`) — and both `canonical_ast_norm` and
    `intent` come straight out of a model-authored, store-rehydrated payload."""
    return raw.strip() if isinstance(raw, str) else ""


def _hard_key_inputs_ok(resolves: object, uses_rules: object, result_grain: object) -> bool:
    """Are the FROZEN hard key's three structured inputs shaped the way it needs?

    DERIVED FROM `compute_canonical_key`, not from a remembered field list. Its inputs
    are rehydrated JSON from `learning_candidates`, and the key function is FROZEN
    (Contract C §3 — its digests are persisted), so it cannot be made defensive itself.
    The operation each input is subjected to is what forces the requirement:

      input           operation in compute_canonical_key       ⇒ requirement
      resolves        `dict(resolves)` — ValueError on a str
                      or a list of scalars, TypeError on an int ⇒ Mapping
      uses_rules      `set(uses_rules)` — TypeError on an
                      unhashable member (list/dict) and on a
                      non-iterable; a BARE STRING iterates
                      char-wise into fictitious rule ids (no
                      crash, a silently wrong key).
                      Then `sorted(...)` — TypeError comparing
                      mixed types                              ⇒ list/tuple of str
      result_grain    `dict(result_grain)` — as resolves.
                      Then `sorted(grain["columns"])` when it
                      is a list/tuple — TypeError on mixed
                      types                                    ⇒ Mapping, str columns

    A failure SKIPS the hard key, which is the SAME fail-soft path an absent
    `canonical_ast_norm` already takes (D52): a malformed input never mints a spurious
    key, and the candidate falls through to the structural and soft layers rather than
    dead-lettering the session. Before this guard a bare-string `result_grain` raised
    ValueError out of `process` and killed the whole extraction.

    Note the deliberate asymmetry with `_normalized_grain`: it tolerates a `columns` that
    is not a list at all (it simply does not sort it), so this guard only constrains the
    MEMBERS of a list/tuple `columns` — matching what the function actually does rather
    than what its type hints suggest.

    A NEW input to the frozen key belongs in this table before it belongs in the code."""
    if not isinstance(resolves, Mapping):
        return False
    if not isinstance(uses_rules, (list, tuple)):
        return False
    if not all(isinstance(rule, str) for rule in uses_rules):
        return False
    if not isinstance(result_grain, Mapping):
        return False
    columns = result_grain.get("columns")
    return not (
        isinstance(columns, (list, tuple))
        and not all(isinstance(col, str) for col in columns)
    )


# The `:Blueprint` id prefix the landing writer MERGEs a learning artifact under
# (`promotion/landing.py::_LANDING_PREFIX["blueprint"]`). Duplicated rather than
# imported: `promotion.landing` pulls in `runtime.retrieval.corpus_loader` and its whole
# sqlglot seeding machinery, which this stage must not carry. The duplication is bounded
# by `test_the_landing_prefix_matches_the_landing_writer` in the unit suite — the house
# pattern for a mirror constant (one definition plus an identity test beats a copy).
_BLUEPRINT_LANDING_PREFIX = "bp::"


def _bucket_landing_id(canonical_key: str) -> str:
    """The neo4j node id a `learning_corpus` artifact WILL have (or already has) once it
    lands. The join key between the two soft sources — see `DedupStage._corpus_cards`."""
    return f"{_BLUEPRINT_LANDING_PREFIX}{canonical_key}"


def _card_from_artifact(artifact: CorpusArtifact, *, similarity: float) -> PriorArtCard:
    """Project a `learning_corpus` artifact onto a `PriorArtCard` so both soft sources
    adjudicate through ONE code path.

    `id` stays the artifact's own id (the originating candidate id), NOT the derived
    landing id: it is what the verdict's `matched_id` has always carried for a bucket
    match, and it is the id a human chasing the duplicate can actually look up.

    `model_matched=True` is a statement of fact here, not an assumption: the query
    vector and this artifact's vector were produced by the SAME `embed` call on the SAME
    client moments ago, so they are in the same space by construction. There is no
    stored `embedding_model` to compare — and inventing a mismatch would apply the
    cross-space discount to a comparison that has no cross-space risk.

    Fields the bucket genuinely does not have are EMPTY rather than guessed:
    `structural_key` (never stamped on an artifact), `drift_status`, `result_grain`, and
    `verified` (`None` — "the artifact does not say", the same tri-state a node without
    the property gets)."""
    return PriorArtCard(
        id=artifact.id,
        kind="blueprint",
        tier=TIER_MCP if artifact.source == TIER_MCP else TIER_LEARNING,
        status=artifact.status,
        verified=None,
        drift_status="",
        intent=artifact.intent,
        result_grain=(),
        uses_rules=tuple(r for r in artifact.uses_rules if isinstance(r, str)),
        structural_key="",
        embedding_model="",
        similarity=similarity,
        model_matched=True,
        score_basis="vector",
        origin="corpus",
    )


def _novelty_from(graph_cards: list[PriorArtCard], *, measured: bool) -> NoveltyStamp:
    """The plan-§4 novelty stamp from the LANDED (graph) prior-art cards.

    Scored on `confidence`, not raw `similarity`, for exactly the reason
    `_adjudicate_cards` is: a cosine taken across two embedding spaces carries no
    information, and letting a meaningless 0.97 declare a genuinely-new blueprint
    unoriginal would push the most valuable candidate to the bottom of the review queue.

    *measured* is threaded through from whether the index was actually CONSULTED, not
    inferred from the card list being empty. `[]` from a healthy index is the strongest
    possible novelty claim ("nothing like this has landed"); `[]` from an index that
    raised is no claim at all, and the two must never render as the same number."""
    if not measured:
        return NoveltyStamp()
    best = max((card.confidence for card in graph_cards), default=0.0)
    return NoveltyStamp.from_best_similarity(best, compared_against=len(graph_cards))


class ThresholdConfigError(ValueError):
    """Raised when the soft-layer bands are ordered such that one is unreachable."""


class DedupStage:
    """The S6 dedup stage. `stage_id == "dedup"` (the frozen pipeline slot)."""

    stage_id = "dedup"

    def __init__(
        self,
        corpus: BlueprintCorpus,
        embedder: _EmbeddingClient,
        *,
        prior_art: PriorArtIndex | None = None,
        merge_threshold: float = _DEFAULT_MERGE_THRESHOLD,
        conflict_threshold: float = _DEFAULT_CONFLICT_THRESHOLD,
        prior_art_limit: int = _DEFAULT_PRIOR_ART_LIMIT,
        recurrence_threshold: float = _DEFAULT_RECURRENCE_THRESHOLD,
        judge: CoverageJudge | None = None,
        tracer: object | None = None,
    ) -> None:
        if conflict_threshold > merge_threshold:
            # `_adjudicate` tests `>= merge` FIRST, so an inverted pair makes the
            # `conflict` branch UNREACHABLE: with merge=0.80/conflict=0.90 a 0.85
            # similarity is stamped `merge` — a mergeable variant — when the operator
            # asked for it to be a `conflict`. Both verdicts route to the review inbox,
            # so nothing lands wrongly, but every near-miss is silently MISLABELLED with
            # no log to explain it. Fail at construction (composition root, process
            # start) rather than mis-adjudicate for the life of the deployment.
            raise ThresholdConfigError(
                f"dedup conflict_threshold ({conflict_threshold}) must be <= "
                f"merge_threshold ({merge_threshold}); the soft layer tests the merge "
                "band first, so an inverted pair makes the conflict band unreachable "
                "and silently relabels every near-miss as `merge`."
            )
        self._corpus = corpus
        self._embedder = embedder
        self._prior_art = prior_art
        self._merge_threshold = merge_threshold
        self._conflict_threshold = conflict_threshold
        self._prior_art_limit = max(1, prior_art_limit)
        self._recurrence_threshold = recurrence_threshold
        # Plan §3b. Optional and default-absent: with none wired this stage behaves
        # exactly as it did before the slice, and no model is ever called from here.
        self._judge = judge
        self._tracer = tracer

    async def process(self, env: CandidateEnvelope, ctx: StageContext) -> StageResult:
        if env.type != "blueprint":
            # Dedup is AST-keyed; only blueprints carry a key. Others pass through to
            # the writer with dedup=None.
            return StageResult(env, "continue")

        gen = env.payload.get("generalization")
        if not isinstance(gen, dict):
            # Pre-S4 / non-generalized blueprint — nothing to hash. Pass through.
            return StageResult(env, "continue")

        norm = _text(gen.get("canonical_ast_norm"))
        resolves = env.payload.get("resolves") or {}
        uses_rules = gen.get("uses_rules") or []
        result_grain = gen.get("result_grain") or {}

        hard_key = ""
        if norm and _hard_key_inputs_ok(resolves, uses_rules, result_grain):
            hard_key = compute_canonical_key(resolves, uses_rules, result_grain, norm)
            artifact = await self._corpus.get_by_canonical_key(hard_key)
            if artifact is not None:
                # Layer 1 HIT: bump the existing artifact, drop this duplicate.
                #
                # DELIBERATELY unfiltered by status, unlike every other prior-art read
                # here. A byte-identical re-derivation of an idea a human REJECTED must
                # still be dropped — the human said no to exactly this thing, and letting
                # it back through because the artifact is dead would resurrect a settled
                # decision. (The soft layer DOES skip terminal artifacts, because a
                # near-match to a rejected idea is not the same idea.)
                #
                # But the two increments mean very different things, so they must not be
                # indistinguishable in telemetry: `matched_status` on the span separates
                # "this is the third sighting of a live artifact" (which feeds the
                # promotion count) from "somebody re-derived a declined idea" (which is a
                # datapoint about how good our rejections are). Hence the status tag
                # rather than a bare `increment`.
                if artifact.is_terminal:
                    _logger.info(
                        "dedup: candidate %s is a byte-identical re-derivation of the "
                        "%s artifact at %s — dropped. Counted as "
                        "action=increment/matched_status=%s.",
                        env.candidate_id,
                        artifact.status,
                        hard_key[:23],
                        artifact.status,
                    )
                await self._corpus.increment_hit_count(hard_key)
                verdict = DedupVerdict(
                    canonical_key=hard_key,
                    matched_id=artifact.id,
                    similarity=1.0,
                    action="increment",
                    layer="hard",
                )
                self._observe(
                    env, verdict, tier=None, matched_status=artifact.status,
                    matched_origin="corpus",  # the hard key only ever reads the bucket
                )
                return StageResult(replace(env, dedup=verdict), "drop")

        # Layer 2 — the cross-tier structural identity. Deterministic, so it runs BEFORE
        # any cosine and can be terminal.
        structural = await self._structural_layer(env, hard_key=hard_key)
        if structural is not None:
            verdict, card = structural
            # A structural hit is an IDENTITY claim against a GRAPH node — i.e. against
            # something that has landed — so novelty is 0.0 and it is MEASURED. Stamping
            # it here rather than leaving it `None` matters: layer 2 short-circuits the
            # soft layer, so this is the only place a structurally-redundant candidate
            # can be told apart from one nobody could measure (plan §4).
            env = replace(
                env,
                dedup=verdict,
                novelty=NoveltyStamp(novelty=0.0, measured=True, compared_against=1),
            )
            if verdict.action == "redundant_with_canon":
                # DROP without touching the corpus: there is no artifact to increment,
                # and seeding one would record a canon blueprint as a learning artifact.
                self._observe(
                    env, verdict, tier=card.tier, matched_status=card.status,
                    matched_origin=card.origin,
                )
                return StageResult(env, "drop")
            self._observe(
                env, verdict, tier=card.tier, matched_status=card.status,
                matched_origin=card.origin,
            )
            return StageResult(env, "continue")

        # Layer 3 — the soft near-miss band.
        verdict, card, cards, novelty = await self._soft_layer(env, hard_key=hard_key)
        env = replace(env, dedup=verdict, novelty=novelty)

        # Layer 3b (plan §3b) — the optional model adjudication, ambiguous band only.
        # BEFORE `_seed_on_insert`, deliberately: a candidate the judge discards must not
        # first register a `learning_corpus` artifact that would then accrue hit counts
        # for work nobody kept.
        env, dropped = await self._adjudicate(env, ctx, cards)

        if not dropped:
            await self._seed_on_insert(env, verdict)
        self._observe(
            env,
            verdict,
            tier=card.tier if card is not None else None,
            matched_status=card.status if card is not None else None,
            matched_origin=card.origin if card is not None else None,
        )
        return StageResult(env, "drop" if dropped else "continue")

    # -- layer 3b: the optional coverage judge ---------------------------------

    async def _adjudicate(
        self, env: CandidateEnvelope, ctx: StageContext, cards: list[PriorArtCard]
    ) -> tuple[CandidateEnvelope, bool]:
        """Put a band-straddling near-match to the coverage judge. Returns the (possibly
        verdict-stamped) envelope and whether the candidate was DROPPED.

        The band test itself lives in the judge, not here: it is the judge's own knob and
        both of its stages consult it, so duplicating the comparison would be a second
        place for the band to drift. This method's job is to decide whether the judge is
        ASKED at all (blueprint, judge wired) and to carry the result back onto the
        envelope.

        Never raises. The judge is fail-open internally, but a Protocol violation by an
        injected collaborator must not escape into `_run_stages` and abort the whole
        extraction — the same posture every other read in this stage takes (D52)."""
        if self._judge is None:
            return env, False
        try:
            outcome = await self._judge.adjudicate_candidate(env, ctx.summary, cards)
        except Exception:  # noqa: BLE001 - a broken judge may never cost the candidate
            _logger.warning(
                "dedup: the coverage judge raised for candidate %s — keeping the "
                "candidate (fail-open). This is a judge bug: every internal failure "
                "path is supposed to be handled inside it.",
                env.candidate_id,
                exc_info=True,
            )
            return env, False
        if outcome.assessment is not None:
            # Stamped even when the verdict did NOT drop: `new` and
            # `existing-plus-delta` on a surviving candidate are the two most useful
            # rows in the dataset, and a human reading the review inbox wants to see
            # that the machine already had an opinion.
            env = replace(env, judge=outcome.assessment)
        return env, outcome.drop

    # -- layer 2: the cross-tier structural key --------------------------------

    async def _structural_layer(
        self, env: CandidateEnvelope, *, hard_key: str
    ) -> tuple[DedupVerdict, PriorArtCard] | None:
        """Look this candidate's LOOSE structural key up across every corpus tier.

        Returns `None` — meaning "fall through to the soft layer" — for every
        non-answer: no index wired, no derivable key, a genuine miss, or an index that
        could not be consulted. Only a real hit produces a verdict, so this layer can
        never invent one out of a degraded read.

        The candidate's key is derived here rather than read off the envelope because
        nothing stamps it on a candidate: `generalize/mapping.py` mints it at LANDING
        time for the seed. Deriving it costs one sqlglot render of a template S4 has
        already parsed, and — critically — it goes through
        `structural_key_from_templates`, the SAME entry point the canon seeder and the
        landing writer use. Calling the raw `structural_key` with `canonical_ast_norm`
        in hand would mint a digest that matches nothing, silently."""
        if self._prior_art is None:
            return None
        key = self._candidate_structural_key(env)
        if not key:
            return None
        try:
            card = await self._prior_art.get_by_structural_key(key)
        except PriorArtUnavailableError:
            _logger.warning(
                "dedup: prior-art structural lookup UNAVAILABLE (graph unreachable or "
                "unconfigured); falling back to the learning_corpus bucket for this "
                "candidate. The MCP canon is INVISIBLE while this persists, so "
                "already-owned blueprints will be re-proposed as new.",
                exc_info=True,
            )
            return None
        if card is None:
            return None

        if card.is_canon:
            # The canon already carries a structurally identical blueprint. Drop, and
            # count it — see the module docstring on why this is a retrieval signal.
            _logger.warning(
                "dedup: candidate %s is REDUNDANT WITH THE MCP CANON (blueprint %s, "
                "structural_key %s) — dropped. A high rate here is a RETRIEVAL defect: "
                "the agent is not recalling a blueprint it already has, so the analyst "
                "hand-wrote SQL the corpus could have answered.",
                env.candidate_id,
                card.id,
                key[:23],
            )
            return (
                DedupVerdict(
                    canonical_key=hard_key,
                    matched_id=card.id,
                    similarity=card.confidence,
                    action="redundant_with_canon",
                    layer="structural",
                ),
                card,
            )

        # A learning-tier (or unsourced) structural twin. Structurally the same query,
        # but we cannot bump a hit count we cannot key — the corpus bucket is keyed by
        # the FROZEN canonical key and this match was found by the loose one, so there
        # is no artifact to increment. Route it to a human as a mergeable variant, which
        # is what `merge` already means to the writer (reason `dedup_conflict`).
        return (
            DedupVerdict(
                canonical_key=hard_key,
                matched_id=card.id,
                similarity=card.confidence,
                action="merge",
                layer="structural",
            ),
            card,
        )

    def _candidate_structural_key(self, env: CandidateEnvelope) -> str:
        """This candidate's loose structural key, or `""` when one cannot be minted.

        Imported lazily: `runtime.blueprint.structural_key` pulls in sqlglot's optimizer
        passes, and the stage must stay cheap to import for a consumer that never wires
        a prior-art index. Never raises — a malformed generalization is a `""` key
        (fall through to the soft layer), never a dead-lettered session."""
        from ...runtime.blueprint.structural_key import structural_key_from_templates

        gen = env.payload.get("generalization")
        if not isinstance(gen, dict):
            return ""
        raw_nodes = gen.get("node_templates")
        nodes: list[tuple[int, str]] = []
        if isinstance(raw_nodes, list):
            for node in raw_nodes:
                # Untrusted rehydrated JSON: `structural_key_from_templates` sorts these
                # by `order` (mixed types break `<`) and renders `sql_template` as text.
                if not isinstance(node, dict):
                    continue
                order = node.get("order")
                template = node.get("sql_template")
                if isinstance(order, bool) or not isinstance(order, int):
                    continue
                if not isinstance(template, str):
                    continue
                nodes.append((order, template))
        sql_template = gen.get("sql_template")
        try:
            return structural_key_from_templates(
                gen.get("result_grain"),
                sql_template if isinstance(sql_template, str) else None,
                nodes,
            )
        except Exception:  # noqa: BLE001 - an unparseable template is a miss, not a crash
            _logger.warning(
                "dedup: could not derive a structural_key for candidate %s; the "
                "cross-tier prior-art layer is skipped for it",
                env.candidate_id,
                exc_info=True,
            )
            return ""

    # -- layer 3: the soft near-miss band --------------------------------------

    async def _soft_layer(
        self, env: CandidateEnvelope, *, hard_key: str
    ) -> tuple[DedupVerdict, PriorArtCard | None, list[PriorArtCard], NoveltyStamp]:
        """Embedding near-miss adjudication on `intent` over the UNION of both soft
        sources — the neo4j prior-art index AND the `learning_corpus` bucket.

        **The bucket is a SECOND SOURCE, not a fallback, and that distinction is a
        regression fix.** The first cut of this slice returned the index's answer and
        reached the bucket only when the index RAISED. That silently removed working
        behaviour: `_seed_on_insert` registers a candidate's artifact in the bucket the
        moment it is adjudicated `insert`, months before it LANDS in the graph, so the
        bucket is the only place an IN-FLIGHT sibling candidate is visible. With the
        index wired, the graph answered `[]` honestly (it holds no unlanded candidate)
        and two analysts asking the same question five minutes apart both got `insert` —
        a pair QA measured at a live 0.9645 cosine, i.e. above the merge threshold, which
        the PRE-slice loop routed to a human.

        The two sources are complementary, not redundant:
          * the INDEX sees the MCP canon and the landed learning tier, cheaply (one ANN);
          * the BUCKET sees candidates that have not landed yet, expensively (N+1
            embeddings — see `_corpus_cards`).

        Returns the verdict, the matched CARD (or `None` on an `insert`) so the caller
        can record its tier, status and origin — a `merge` against the canon, a `merge`
        against a landed node, and a `merge` against an unlanded sibling are the same
        verdict but three very different facts — and the WHOLE union.

        The third element exists for the coverage judge (plan §3b) and is deliberately
        NOT the matched card: an `insert` returns no matched card by design (nothing was
        near enough to route on), but a 0.75 near-match is exactly the ambiguous case
        the judge is for, so the judge must see the candidates the banding rejected.
        Handing over the assembled list rather than a search handle is what keeps the
        judge adjudicating EXACTLY what dedup banded, and pays the embed once.

        The FOURTH element is the inbox-ranking novelty stamp (plan §4), and it is
        computed from the GRAPH half ALONE — never from the union. That asymmetry is the
        point: novelty must be measured against what has LANDED, because measuring it
        against sibling candidates would score the first sighting of an idea as novel and
        each of its corroborations as redundant, an ordering-dependent answer that gets
        the sign of the evidence backwards. It is computed here because this is the one
        place in the pipeline that has already paid for the embed and the ANN query.

        Degrades to `insert` on an empty intent, both sources empty, or ANY failure —
        never a wrong merge. An index that RAISES is logged loudly and simply contributes
        nothing to the union; the bucket half still runs, which is why the fail-open path
        is now strictly a subset of the healthy path rather than a different one. In that
        window the novelty stamp reports `measured=False` rather than "maximally novel":
        a candidate we could not compare must not be ranked as a discovery."""
        intent = _text(env.payload.get("intent"))
        if not intent:
            # Nothing to embed, so nothing to be novel WITH RESPECT TO — an unmeasured
            # stamp, not a zero. (An intent-less blueprint is separately un-rankable.)
            return (
                DedupVerdict(hard_key, None, 0.0, "insert", "soft"),
                None,
                [],
                NoveltyStamp(),
            )

        graph_cards: list[PriorArtCard] = []
        graph_consulted = False
        if self._prior_art is not None:
            try:
                graph_cards = list(
                    await self._prior_art.search(
                        intent, kinds=("blueprint",), limit=self._prior_art_limit
                    )
                )
            except PriorArtUnavailableError:
                _logger.warning(
                    "dedup: prior-art search UNAVAILABLE (graph unreachable or "
                    "unconfigured); adjudicating on the learning_corpus bucket alone. "
                    "The loop keeps running, but it cannot see the MCP canon or the "
                    "landed learning tier while this persists.",
                    exc_info=True,
                )
            else:
                graph_consulted = True

        bucket_cards = await self._corpus_cards(
            intent=intent,
            hard_key=hard_key,
            # A landed artifact is in BOTH sources. Skip the bucket copy of anything the
            # graph already returned — see `_corpus_cards` for the join.
            already_seen=frozenset(card.id for card in graph_cards),
        )
        cards = graph_cards + bucket_cards
        verdict, matched = self._adjudicate_cards(cards, hard_key=hard_key)
        return verdict, matched, cards, _novelty_from(graph_cards, measured=graph_consulted)

    def _adjudicate_cards(
        self, cards: list[PriorArtCard], *, hard_key: str
    ) -> tuple[DedupVerdict, PriorArtCard | None]:
        """Band the best card of the UNION. Scored on `confidence`, NOT on the raw
        cosine: a card whose stored `embedding_model` differs from the query's was
        compared across two vector spaces, and its cosine carries no information — the
        discount is what keeps it out of the merge/conflict bands instead of letting a
        meaningless 0.97 route a genuinely-new blueprint into the inbox as a duplicate.

        **Sorts here rather than trusting the port's ordering contract.** `search` does
        promise best-first, but this list is now a UNION of two independently-ordered
        sources, so "the port is ordered" cannot make the merged list ordered. Sorting at
        the point of use also removes a silent coupling: an implementation that returned
        an unsorted list would previously have mis-banded with no test able to see it.
        The `id` tiebreak keeps the choice deterministic across equal confidences.

        Never emits `redundant_with_canon`, even for a canon card at 0.99. A cosine over
        intent PROSE is not an identity claim, and dropping a candidate on one would make
        the loop's most consequential decision on its weakest evidence. Layer 2 is where
        canon redundancy is settled; a canon card here is a `merge` a human reads."""
        if not cards:
            return DedupVerdict(hard_key, None, 0.0, "insert", "soft"), None
        best = min(cards, key=lambda c: (-c.confidence, c.id))
        score = best.confidence
        if score >= self._merge_threshold:
            return DedupVerdict(hard_key, best.id, score, "merge", "soft"), best
        if score >= self._conflict_threshold:
            return DedupVerdict(hard_key, best.id, score, "conflict", "soft"), best
        return DedupVerdict(hard_key, None, score, "insert", "soft"), None

    async def _corpus_cards(
        self, *, intent: str, hard_key: str, already_seen: frozenset[str]
    ) -> list[PriorArtCard]:
        """The `learning_corpus` half of the soft union, projected onto `PriorArtCard`.

        **Cost, stated plainly.** `list_artifacts()` is `SELECT c.*` with no WHERE and no
        LIMIT, and this embeds EVERY surviving artifact's intent for EVERY candidate —
        N+1 embeddings per candidate. The original slice removed this path for exactly
        that reason and thereby dropped the in-flight duplicate check with it; the cost
        is being paid back deliberately, because a correct answer that is expensive beats
        a cheap answer that is wrong. The bound is `len(learning_corpus)`, which is the
        set of artifacts the loop itself has minted.

        The principled way to shrink it is to scan only the NOT-YET-LANDED subset — the
        graph covers everything else — but nothing distinguishes those today: `status`
        is written only by the terminal transitions, so every live artifact reads
        `extracted` whether or not it has landed. Recording a landed stamp is the
        follow-up; it is not guesswork this function should be doing.

        Two exclusions, each for a different reason:
          * `canonical_key == hard_key` — this candidate's OWN artifact from an earlier
            processing attempt of the same session. Comparing it to itself would band a
            perfect 1.0 and route every redelivery to a human.
          * `is_terminal` — a human declined it; a NEAR-match to a rejected idea is not
            the same idea, and resurfacing it would turn a settled decision into
            recurring review noise. (The HARD layer deliberately does not skip terminal
            artifacts — see `CorpusArtifact.is_terminal`.)

        **The join with the graph half.** Once an artifact LANDS, the same blueprint
        exists in both sources under different ids: the bucket keys it by
        `canonical_key`, and the landed node's id is the deterministic landing id
        `bp::<canonical_key>` (`promotion/landing.py::landing_id`). Deriving that id here
        is what lets *already_seen* drop the duplicate, and the graph copy is the one
        kept — it is the richer projection (a real tier, status, `verified`, structural
        key) where the bucket card has only a name and a cosine.

        **It has ONE side effect**, and it is here rather than in the caller because this
        is where the (artifact, cosine) pairs exist: each near-matched artifact gets its
        dormant soft recurrence counter bumped (`_bump_recurrence`, plan §4).

        Never raises: a corpus-scan or embedder failure contributes `[]` (D52), so the
        graph half of the union still adjudicates."""
        try:
            all_artifacts = await self._corpus.list_artifacts()
        except Exception:  # noqa: BLE001 — a corpus-listing failure contributes nothing (D52)
            # A missing primary index, an unreachable query service, or a malformed doc
            # must NOT escape and dead-letter the session.
            _logger.warning(
                "dedup soft layer: corpus.list_artifacts() failed; the learning_corpus "
                "half of the prior-art union is EMPTY for this candidate (in-flight "
                "duplicates will not be caught). Check the learning_corpus primary "
                "index / query service.",
            )
            return []
        artifacts = [
            a
            for a in all_artifacts
            if a.canonical_key != hard_key
            and not a.is_terminal
            and _bucket_landing_id(a.canonical_key) not in already_seen
        ]
        if not artifacts:
            return []

        try:
            vectors = await self._embedder.embed([intent, *(a.intent for a in artifacts)])
        except Exception:  # noqa: BLE001 — any embedder failure contributes nothing (D52)
            _logger.warning(
                "dedup soft layer: the embedder failed; the learning_corpus half of the "
                "prior-art union is EMPTY for this candidate.",
                exc_info=True,
            )
            return []

        query = vectors[0]
        cards: list[PriorArtCard] = []
        recurred: list[CorpusArtifact] = []
        for art, vec in zip(artifacts, vectors[1:], strict=False):
            similarity = _cosine(query, vec)
            cards.append(_card_from_artifact(art, similarity=similarity))
            if similarity >= self._recurrence_threshold:
                recurred.append(art)
        await self._bump_recurrence(recurred)
        return cards

    async def _bump_recurrence(self, artifacts: list[CorpusArtifact]) -> None:
        """Record a SOFT recurrence sighting against each near-matched artifact (plan §4).

        **Why here.** This is the only loop in the system that already knows a candidate's
        intent cosine against a KEYED artifact. The graph half of the union cannot be
        counted — a `PriorArtCard` from neo4j carries no `canonical_key`, and "we cannot
        bump a count we cannot key" is the same constraint that makes layer 2's
        learning-tier hit a `merge` rather than an `increment`.

        **What is deliberately NOT filtered.** The artifacts reaching here have already
        been stripped of this candidate's own key and of terminal (rejected/retired)
        artifacts by the caller, which is exactly right: a paraphrase of a declined idea
        must not accrue evidence for re-proposing it.

        **A judge-dropped candidate still counts.** The drop happens after this, and that
        ordering is intentional — the judge drops a candidate because an artifact already
        COVERS it, and "somebody asked this again" is true whether or not we kept the
        candidate. Contrast `_seed_on_insert`, which is deliberately skipped for a
        dropped candidate: that would create a NEW artifact for work nobody kept, which
        is a different thing entirely.

        **NO PER-SIGHTING IDEMPOTENCY, and this is the thing to know before the weight is
        raised.** `increment_hit_count` fires once per hard-key hit and the candidate is
        then DROPPED, so a redelivery collapses to one increment. Nothing here does that:
        a candidate that survives dedup can be re-processed (a queue redelivery, a
        re-enqueue, a peer race, a pipeline re-run) and will re-bump every near artifact
        again. The stored count is therefore "sightings PLUS redelivery noise", biased
        upward and not bounded by the number of distinct sessions. Harmless while the
        weight is 0.0; at a non-zero weight a flapping session can corroborate itself.
        Closing it needs a per-(artifact, content_hash) marker in the corpus — a store
        change, not a knob change.

        Fail-soft per artifact and in aggregate (D52): this counter is weighted 0.0
        today, so it must never be the reason a candidate fails to be adjudicated."""
        if not artifacts:
            return
        for art in artifacts:
            try:
                await self._corpus.increment_recurrence_count(art.canonical_key)
            except Exception:  # noqa: BLE001 — a dormant counter may never cost a candidate
                _logger.warning(
                    "dedup: soft recurrence increment failed for artifact %s; the "
                    "counter under-reports for it (it is weighted 0.0 today, so this "
                    "changes no decision).",
                    art.canonical_key[:23],
                    exc_info=True,
                )

    # -- bookkeeping -----------------------------------------------------------

    async def _seed_on_insert(self, env: CandidateEnvelope, verdict: DedupVerdict) -> None:
        """On a genuinely-new `insert` with a real hard key, register the corpus
        artifact at `hit_count=1` from THIS first candidate (D48 §11.1) so the
        count-based promotion threshold can accrue before the artifact lands. A
        fail-soft insert (no hard key) has nothing to key on — skip it."""
        if verdict.action != "insert" or not verdict.canonical_key:
            return
        gen = env.payload.get("generalization") or {}
        await self._corpus.seed_artifact(
            CorpusArtifact(
                id=env.candidate_id,
                canonical_key=verdict.canonical_key,
                intent=_text(env.payload.get("intent")),
                hit_count=1,
                uses_rules=tuple(gen.get("uses_rules") or []),
            )
        )

    def _observe(
        self,
        env: CandidateEnvelope,
        verdict: DedupVerdict,
        *,
        tier: str | None,
        matched_status: str | None = None,
        matched_origin: str | None = None,
    ) -> None:
        """Emit the shape-only `learning.dedup` span for this verdict.

        Three separate rates come out of the same span, which is why the two dimensions
        are carried on EVERY verdict rather than only on the one that motivated them:

          * `action=redundant_with_canon` — a deterministic canon rediscovery;
          * `action=merge AND prior_art_tier=mcp` — a probable one, on softer evidence;
          * `action=increment AND matched_status IN (rejected, retired)` — somebody
            re-derived, byte for byte, an idea a human already DECLINED. Without
            `matched_status` that is indistinguishable from an ordinary hit-count bump
            against a live artifact, and the two say opposite things about the loop.
          * `matched_origin=corpus` — the soft match came from an IN-FLIGHT sibling in
            the `learning_corpus` bucket rather than from anything landed. That is the
            concurrency signal, and it is the only way to see how often the bucket half
            of the union is the one doing the work.

        No tracer wired ⇒ nothing emitted."""
        if self._tracer is None:
            return
        with dedup_span(
            self._tracer,
            session_id=env.source_session,
            candidate_id=env.candidate_id,
            action=verdict.action,
            layer=verdict.layer,
            similarity=verdict.similarity,
            prior_art_tier=tier,
            matched_status=matched_status,
            matched_origin=matched_origin,
        ):
            pass


__all__ = ["DedupStage", "ThresholdConfigError"]
