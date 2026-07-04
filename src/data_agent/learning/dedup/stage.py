"""DedupStage — the D48 two-layer blueprint dedup `CandidateStage` (Slice 6).

Runs third in the write-router pipeline (generalize → leakage → **dedup** → writer)
and writes `envelope.dedup` (Contract C, `DedupVerdict`).

Two layers (D48):

  1. **Hard key** (`canonical_key`, §3). Race-safe by construction (identical
     semantics ⇒ identical key). A hard-key HIT means an equivalent artifact is
     already landed → `action="increment"`: bump the EXISTING artifact's `hit_count`
     and DROP this duplicate (`control="drop"` — nothing new is persisted; the
     count lives on the artifact, not the envelope, §11.1). A MISS falls through to
     the soft layer.

  2. **Soft layer** — embedding similarity on `intent` (the injected embedder seam;
     scripted/fake in tests, no network). A near-match with a DIFFERENT hard key is
     never auto-appended (§3): it is stamped `merge` (a mergeable blueprint variant)
     or `conflict` (partial overlap) and left for the writer to route to the inbox.
     Below the near-miss band ⇒ `insert` (genuinely new).

**Fail-soft (D52).** If `canonical_ast_norm` is absent/empty (S4 could not produce
it), the hard key is SKIPPED and the candidate falls straight to the soft layer — a
missing template never mints a spurious hard key, and a degraded/failing embedder
degrades to `insert`, never to a wrong `merge`.

The stage only adjudicates BLUEPRINTS (the hard key is AST-derived). Non-blueprint
targets pass through untouched (`dedup` stays `None`); the writer stage routes them.

Thresholds and the corpus/embedder are injected (composition-root config knobs), so
nothing here reads global settings and tests stay hermetic.
"""

from __future__ import annotations

from dataclasses import replace

from ..candidate.models import CandidateEnvelope
from ..candidate.verdicts import DedupVerdict
from ..stage import StageContext, StageResult
from .canonical_key import compute_canonical_key
from .corpus import BlueprintCorpus, CorpusArtifact

# Provisional bands (RuntimeSettings-style tunables, wired as knobs — §11).
_DEFAULT_MERGE_THRESHOLD = 0.95
_DEFAULT_CONFLICT_THRESHOLD = 0.83


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


class DedupStage:
    """The S6 dedup stage. `stage_id == "dedup"` (the frozen pipeline slot)."""

    stage_id = "dedup"

    def __init__(
        self,
        corpus: BlueprintCorpus,
        embedder: _EmbeddingClient,
        *,
        merge_threshold: float = _DEFAULT_MERGE_THRESHOLD,
        conflict_threshold: float = _DEFAULT_CONFLICT_THRESHOLD,
    ) -> None:
        self._corpus = corpus
        self._embedder = embedder
        self._merge_threshold = merge_threshold
        self._conflict_threshold = conflict_threshold

    async def process(self, env: CandidateEnvelope, ctx: StageContext) -> StageResult:
        if env.type != "blueprint":
            # Dedup is AST-keyed; only blueprints carry a hard key. Others pass
            # through to the writer with dedup=None.
            return StageResult(env, "continue")

        gen = env.payload.get("generalization")
        if not isinstance(gen, dict):
            # Pre-S4 / non-generalized blueprint — nothing to hash. Pass through.
            return StageResult(env, "continue")

        norm = (gen.get("canonical_ast_norm") or "").strip()
        resolves = env.payload.get("resolves") or {}
        uses_rules = gen.get("uses_rules") or []
        result_grain = gen.get("result_grain") or {}

        if norm:
            hard_key = compute_canonical_key(resolves, uses_rules, result_grain, norm)
            artifact = await self._corpus.get_by_canonical_key(hard_key)
            if artifact is not None:
                # Hard-key HIT: bump the existing artifact, drop this duplicate.
                await self._corpus.increment_hit_count(hard_key)
                verdict = DedupVerdict(
                    canonical_key=hard_key,
                    matched_id=artifact.id,
                    similarity=1.0,
                    action="increment",
                    layer="hard",
                )
                return StageResult(replace(env, dedup=verdict), "drop")
            verdict = await self._soft_layer(env, hard_key=hard_key)
        else:
            # Fail-soft (D52): no canonical_ast_norm ⇒ skip the hard key entirely.
            verdict = await self._soft_layer(env, hard_key="")

        await self._seed_on_insert(env, verdict)
        return StageResult(replace(env, dedup=verdict), "continue")

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
                intent=(env.payload.get("intent") or "").strip(),
                hit_count=1,
                uses_rules=tuple(gen.get("uses_rules") or []),
            )
        )

    async def _soft_layer(self, env: CandidateEnvelope, *, hard_key: str) -> DedupVerdict:
        """Embedding near-miss adjudication on `intent`. Degrades to `insert` on an
        empty intent, an empty corpus, or ANY embedder failure — never a wrong merge.

        The verdict's `layer` reflects which layer actually ADJUDICATED it: an
        `insert`/`merge`/`conflict` is produced by THIS soft layer, so `layer="soft"`
        (a hard-key `increment` is stamped `layer="hard"` by the caller). The prior
        code mislabelled a soft-adjudicated insert as `hard` (review nit)."""
        intent = (env.payload.get("intent") or "").strip()
        artifacts = [a for a in await self._corpus.list_artifacts() if a.canonical_key != hard_key]
        if not intent or not artifacts:
            return DedupVerdict(hard_key, None, 0.0, "insert", "soft")

        try:
            vectors = await self._embedder.embed([intent, *(a.intent for a in artifacts)])
        except Exception:  # noqa: BLE001 — any embedder failure degrades to insert (D52)
            return DedupVerdict(hard_key, None, 0.0, "insert", "soft")

        query = vectors[0]
        best: CorpusArtifact | None = None
        best_sim = 0.0
        for art, vec in zip(artifacts, vectors[1:], strict=False):
            sim = _cosine(query, vec)
            if sim > best_sim:
                best_sim, best = sim, art

        if best is not None and best_sim >= self._merge_threshold:
            return DedupVerdict(hard_key, best.id, best_sim, "merge", "soft")
        if best is not None and best_sim >= self._conflict_threshold:
            return DedupVerdict(hard_key, best.id, best_sim, "conflict", "soft")
        return DedupVerdict(hard_key, None, best_sim, "insert", "soft")
