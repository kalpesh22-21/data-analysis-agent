"""DedupStage — the blueprint dedup `CandidateStage` (D48 Slice 6, + PriorArt Slice 2).

Runs third in the frozen pipeline and writes `envelope.dedup` (Contract C). THREE LAYERS in
strictly decreasing certainty, and only the two DETERMINISTIC ones may discard a candidate:

  1. HARD KEY (`canonical_key`) against the `learning_corpus` bucket — race-safe by
     construction, which is what makes the cross-session hit count sound. A hit ⇒ `increment`
     the EXISTING artifact and DROP the duplicate.
  2. STRUCTURAL KEY against the `PriorArtIndex`, hashing only what BOTH authoring paths have,
     because two of the hard key's four inputs are effectively learning-only. Against the MCP
     canon ⇒ `redundant_with_canon`, DROP (a git-versioned blueprint has no corpus artifact
     and no count the loop owns); against the learning tier or an unsourced node ⇒ `merge`.
  3. SOFT LAYER — embedding similarity on `intent` over the UNION of the graph AND the bucket,
     the latter being the only place an IN-FLIGHT sibling is visible. Never drops:
     `merge`/`conflict` route to the inbox, below the band is `insert`.

`redundant_with_canon` is COUNTED because a high rate is a RETRIEVAL defect surfacing here —
the agent owned a blueprint, failed to recall it, and the loop re-derived it. FAIL-SOFT (D52):
a missing key skips its own layer, and an unreachable prior-art index contributes nothing to
the union with a LOUD log, so the loop mints duplicates it would have dropped rather than
dropping the session. An optional COVERAGE JUDGE adjudicates the ambiguous band and may only
ever DROP. Two plan-§4 side outputs ride along because this stage has already paid for the
embed: `envelope.novelty` (from the GRAPH cards ALONE) and the dormant `recurrence_count`;
neither can change a verdict. Only BLUEPRINTS are adjudicated.
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

    NOT `(raw or "").strip()`, which raises AttributeError on a non-string truthy value — and
    both `canonical_ast_norm` and `intent` come out of a model-authored, rehydrated payload.
    """
    return raw.strip() if isinstance(raw, str) else ""


def _hard_key_inputs_ok(resolves: object, uses_rules: object, result_grain: object) -> bool:
    """Are the FROZEN hard key's three structured inputs shaped the way it needs?

    DERIVED FROM `compute_canonical_key`, not from a remembered field list: its inputs are
    rehydrated JSON and the key function is FROZEN (its digests are persisted), so it cannot be
    made defensive itself. `resolves` and `result_grain` are `dict(...)`-ed ⇒ Mapping;
    `uses_rules` is `set(...)`-then-`sorted(...)` ⇒ list/tuple of str, because a BARE STRING
    iterates char-wise into fictitious rule ids with no crash and a silently wrong key; a
    list/tuple `columns` must hold str members, since `sorted` raises across mixed types.

    A failure SKIPS the hard key — the same fail-soft path an absent `canonical_ast_norm` takes
    (D52) — so a malformed input never mints a spurious key and never dead-letters the session.
    Note the deliberate asymmetry with `_normalized_grain`, which tolerates a `columns` that is
    not a list at all. A NEW input to the frozen key belongs in this docstring before it belongs
    in the code.
    """
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
    """The neo4j node id a `learning_corpus` artifact will have once it lands.

    The join key between the two soft sources — see `DedupStage._corpus_cards`.
    """
    return f"{_BLUEPRINT_LANDING_PREFIX}{canonical_key}"


def _card_from_artifact(artifact: CorpusArtifact, *, similarity: float) -> PriorArtCard:
    """Project a `learning_corpus` artifact onto a `PriorArtCard`.

    So both soft sources adjudicate through ONE code path. `id` stays the artifact's own id (the
    originating candidate id), NOT the derived landing id: it is what `matched_id` has always
    carried for a bucket match, and what a human chasing the duplicate can look up.
    `model_matched=True` is a statement of fact, not an assumption — the query vector and this
    artifact's were produced by the SAME `embed` call moments ago. Fields the bucket genuinely
    does not have are EMPTY rather than guessed.
    """
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

    Scored on `confidence`, not raw `similarity`, for the same reason `_adjudicate_cards` is: a
    cosine taken across two embedding spaces carries no information, and letting a meaningless
    0.97 declare a genuinely-new blueprint unoriginal would push the most valuable candidate to
    the bottom of the review queue. *measured* is threaded from whether the index was actually
    CONSULTED, not inferred from an empty card list — `[]` from a healthy index is the strongest
    possible novelty claim, `[]` from an index that raised is no claim at all.
    """
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
                # Layer 1 HIT: bump the existing artifact. What happens to THIS candidate
                # then depends on whether the artifact it matched is still alive.
                #
                # The lookup is DELIBERATELY unfiltered by status, unlike every other
                # prior-art read here, and the two outcomes are the reason why:
                #
                #   TERMINAL match (rejected/retired) ⇒ DROP. The human said no to exactly
                #     this thing; letting it back through because the artifact is dead
                #     would resurrect a settled decision, and a rejection has to work as
                #     negative memory or the loop re-proposes what it was told to stop
                #     proposing. It is also a corpus-integrity hole, not just noise:
                #     `promotion/landing.py::landing_id` derives the graph node id from
                #     `dedup.canonical_key`, which for a hard-key hit IS the matched
                #     artifact's key — so approving the duplicate MERGEs the RETIRED
                #     blueprint's own node and stamps it `validated` again. (The soft layer
                #     SKIPS terminal artifacts entirely, because a near-match to a rejected
                #     idea is not the same idea.)
                #   LIVE match ⇒ CONTINUE. Nothing is settled about a live artifact, so the
                #     duplicate stays as an editable review item; the writer recognizes the
                #     non-insert verdict and routes it to the inbox as a suppressed
                #     duplicate, where the matched artifact remains visible.
                #
                # The increment happens on BOTH paths and is pre-existing behaviour: the
                # two increments mean very different things, so they must not be
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
                return StageResult(
                    replace(env, dedup=verdict),
                    "drop" if artifact.is_terminal else "continue",
                )

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
                # The candidate is structurally IDENTICAL to a blueprint the agent already
                # recalls from git-versioned MCP canon, so a reviewer has nothing to decide
                # — and approving it would land a SECOND, learning-tier node for a query
                # the canon already answers, which is the duplication the governed-corpus
                # split exists to prevent. The `warning` in `_structural_layer` is the
                # signal that matters here: this is a RETRIEVAL defect upstream.
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
        """Put a band-straddling near-match to the coverage judge.

        Returns the (possibly verdict-stamped) envelope and whether the candidate was DROPPED. The
        band test itself lives in the judge — it is the judge's own knob and both of its stages
        consult it — so this method only decides whether the judge is ASKED at all. Never raises: a
        Protocol violation by an injected collaborator must not escape into `_run_stages` and abort
        the whole extraction (D52).
        """
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

        Returns `None` — "fall through to the soft layer" — for every non-answer: no index wired, no
        derivable key, a genuine miss, or an index that could not be consulted, so this layer can
        never invent a verdict out of a degraded read. The key is derived here because nothing
        stamps it on a candidate, and it goes through `structural_key_from_templates`, the SAME
        entry point the canon seeder and the landing writer use — calling the raw `structural_key`
        with `canonical_ast_norm` in hand would mint a digest that matches nothing, silently.
        """
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

        Imported lazily: `runtime.blueprint.structural_key` pulls in sqlglot's optimizer passes, and
        the stage must stay cheap to import for a consumer that never wires a prior-art index. Never
        raises — a malformed generalization is a `""` key, never a dead-lettered session.
        """
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
        """Embedding near-miss adjudication on `intent` over the UNION of both soft sources.

        The bucket is a SECOND SOURCE, not a fallback: `_seed_on_insert` registers a candidate's
        artifact the moment it is adjudicated `insert`, months before it LANDS in the graph, so the
        bucket is the only place an IN-FLIGHT sibling is visible. Treating it as a fallback silently
        removed the check that catches two analysts asking the same question minutes apart. The two
        are complementary: the INDEX sees the MCP canon and the landed tier cheaply (one ANN), the
        BUCKET sees unlanded candidates expensively (N+1 embeddings, see `_corpus_cards`).

        Returns the verdict; the matched CARD (or `None` on an `insert`) so the caller can record
        its tier, status and origin — a merge against the canon, against a landed node, and against
        an unlanded sibling are three very different facts; the WHOLE union, because an `insert`
        returns no matched card yet a 0.75 near-match is exactly the ambiguous case the coverage
        judge is for; and the novelty stamp, computed from the GRAPH half ALONE — measuring novelty
        against sibling candidates would score the first sighting of an idea as novel and each of
        its corroborations as redundant, getting the sign of the evidence backwards.

        Degrades to `insert` on an empty intent, both sources empty, or ANY failure — never a wrong
        merge. An index that raises contributes nothing and the bucket half still runs, so the
        fail-open path is a subset of the healthy one; in that window the novelty stamp reports
        `measured=False` rather than "maximally novel".
        """
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
        """Band the best card of the UNION, scored on `confidence` and NOT on the raw cosine.

        A card whose stored `embedding_model` differs from the query's was compared across two
        vector spaces and its cosine carries no information; the discount is what keeps it out of
        the merge/conflict bands. SORTS here rather than trusting the port's ordering contract,
        because a UNION of two independently-ordered sources cannot inherit either one's order; the
        `id` tiebreak keeps the choice deterministic. NEVER emits `redundant_with_canon`, even for a
        canon card at 0.99 — a cosine over intent PROSE is not an identity claim, and layer 2 is
        where canon redundancy is settled.
        """
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

        COST, stated plainly: `list_artifacts()` is a scan with no WHERE and no LIMIT, and this
        embeds EVERY surviving artifact's intent for EVERY candidate — N+1 embeddings, bounded by
        the artifacts the loop itself has minted. It is paid deliberately, because removing it
        dropped the in-flight duplicate check with it. The principled way to shrink it is to scan
        only the NOT-YET-LANDED subset, and nothing distinguishes those today.

        Two exclusions: this candidate's OWN artifact from an earlier processing attempt (comparing
        it to itself would band a perfect 1.0 and route every redelivery to a human), and
        `is_terminal` artifacts (a near-match to a rejected idea is not the same idea; the HARD
        layer deliberately does NOT skip them). The join with the graph half derives the landed id
        `bp::<canonical_key>` so *already_seen* drops the duplicate, keeping the graph copy — the
        richer projection. ONE side effect: each near-matched artifact gets its dormant recurrence
        counter bumped. Never raises — a scan or embedder failure contributes `[]` (D52).
        """
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

        Here because this is the only loop that already knows a candidate's intent cosine against a
        KEYED artifact: a graph card carries no `canonical_key`, and "we cannot bump a count we
        cannot key" is the same constraint that makes layer 2's learning-tier hit a `merge`. The
        caller has already stripped this candidate's own key and every terminal artifact, which is
        exactly right — a paraphrase of a declined idea must not accrue evidence for re-proposing
        it. A judge-dropped candidate STILL counts, since the drop happens after this and "somebody
        asked this again" is true either way; contrast `_seed_on_insert`, deliberately skipped for a
        drop because it would create a NEW artifact for work nobody kept.

        NO PER-SIGHTING IDEMPOTENCY, and this is the thing to know before the weight is raised: a
        candidate that survives dedup can be re-processed (a redelivery, a peer race, a pipeline
        re-run) and will re-bump every near artifact, so the stored count is "sightings PLUS
        redelivery noise". Harmless at the shipped weight of 0.0; at a non-zero weight a flapping
        session can corroborate itself. Closing it needs a per-(artifact, content_hash) marker in
        the corpus — a store change, not a knob change. Fail-soft per artifact and in aggregate.
        """
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
        """On a genuinely-new `insert` with a real hard key, register the artifact at `hit_count=1`.

        From THIS first candidate (D48 §11.1), so the count-based promotion threshold can accrue
        before the artifact lands. A fail-soft insert (no hard key) has nothing to key on — skip.
        """
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

        Both extra dimensions are carried on EVERY verdict, not only the one that motivated them, so
        several rates come out of one span: `redundant_with_canon` (a deterministic canon
        rediscovery), `merge AND prior_art_tier=mcp` (a probable one on softer evidence), `increment
        AND matched_status IN (rejected, retired)` (a byte-identical re-derivation of an idea a human
        already DECLINED, otherwise indistinguishable from an ordinary bump), and
        `matched_origin=corpus` (the concurrency signal — how often the bucket half is doing the
        work). No tracer wired ⇒ nothing emitted.
        """
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
