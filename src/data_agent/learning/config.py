"""LearningSettings — env-var config for the offline learning loop (D96 §11).

Mirrors `runtime/config.py`'s pattern (pydantic-settings, uppercased env vars, `.env`,
`extra="ignore"`) but is a SEPARATE settings surface. The D58c kill-switch is deliberately
NOT a field here: it must not be frozen behind a cached settings singleton (see
`learning_enabled`).
"""

from __future__ import annotations

import logging
import os
import socket

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from data_agent.runtime.config import (
    TRUTHY_ENV_VALUES,
    _read_vault_secret,
    check_settings_keyspaces,
    vault,
)

# Recognized truthy spellings for the kill-switch (case-insensitive). Anything
# else (including unset → default) resolves per the rules in `learning_enabled`.
#
# Shared with the runtime's `HYDRATOR_ENABLED` switch rather than restated — see
# `runtime/config.py::TRUTHY_ENV_VALUES` for why the two must accept the same spellings
# and why the definition lives on that side. This does NOT weaken the separation this
# module's docstring is about: the SETTINGS SURFACES stay separate (own env vars, own
# `BaseSettings` classes, no shared `@lru_cache`d singleton) — only the spelling table
# an operator types against is shared. The marginal import cost is two modules; the
# learning package already loads far more of `runtime/` than this.
#
# The same import carries the Vault plumbing (`vault`, `_read_vault_secret`) and the
# Couchbase keyspace validator (`check_settings_keyspaces`) for the same reason and in the
# same direction: one guarded `paycompy` import, one fail-soft KV reader, and one
# definition of which scope/collection pairs a store may be pointed at — shared rather
# than restated, so both planes read secrets and validate keyspaces with identical
# semantics. The validator in particular has to be ONE function: all five stores bind
# the same kind of handle, and two copies of "which keyspaces are legal" that drifted
# would accept a config on one plane that the other refuses. Only inward-to-outward —
# see the D58c note above; that is why it lives on the runtime side even though this
# plane has three of the four learning keyspaces.
_TRUTHY = TRUTHY_ENV_VALUES


class _KillSwitchSettings(BaseSettings):
    """A one-field settings surface for `LEARNING_ENABLED`, constructed FRESH on every call.

    Reads BOTH `.env` and the process environment, process env taking precedence, so an
    operator flipping the switch in either place is honored.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    learning_enabled: str | None = None


def learning_enabled() -> bool:
    """Read the D58c master kill-switch FRESH, EVERY call, so a flip needs no restart/deploy.

    Default (unset or blank) is enabled. An unrecognized value is treated as DISABLED —
    fail-safe: a typo'd override halts the loop rather than silently running it.
    """
    raw = _KillSwitchSettings().learning_enabled
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() in _TRUTHY


def _default_consumer_name() -> str:
    return f"worker-{socket.gethostname()}-{os.getpid()}"


class LearningSettings(BaseSettings):
    """All learning-loop configuration, read from environment variables (or `.env`).

    `LEARNING_ENABLED` is deliberately ABSENT: it is read uncached, per cycle, via
    `learning_enabled()` so the toggle is never frozen by the settings cache.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Redis Streams transport (D30) ---
    learning_redis_url: str = Field(
        "redis://localhost:6379/0", description="Redis Streams endpoint for the learning queue."
    )
    learning_jobs_stream: str = Field(
        "learning:jobs", description="Work stream — one entry per claimed session."
    )
    learning_dead_letter_stream: str = Field(
        "learning:jobs:dead", description="Terminal parking stream for poison jobs."
    )
    learning_consumer_group: str = Field(
        "learning-workers", description="Redis consumer group name."
    )
    learning_consumer_name: str = Field(
        default_factory=_default_consumer_name,
        description="Per-replica consumer identity for the PEL (default worker-<host>-<pid>).",
    )

    # --- Sweeper / consumer cadence + thresholds ---
    learning_sweep_interval_seconds: float = Field(
        60.0, gt=0, description="Sweep cadence (seconds between idle scans)."
    )
    learning_idle_threshold_seconds: int = Field(
        1800,
        ge=1,
        description=(
            "Idle age past which a session is 'closed' and eligible for learning. "
            "Must keep the §6 TTL invariant: SESSION_TTL > this + P95(dwell+processing)."
        ),
    )
    learning_max_deliveries: int = Field(
        5,
        ge=1,
        description="Dead-letter threshold N: past N deliveries a job → dead-letter stream.",
    )
    learning_reclaim_min_idle_seconds: int = Field(
        300, ge=1, description="XAUTOCLAIM min-idle for reclaiming stuck PEL entries."
    )
    learning_batch_size: int = Field(10, ge=1, description="XREADGROUP COUNT per consume batch.")
    learning_block_ms: int = Field(5000, ge=0, description="XREADGROUP BLOCK milliseconds.")
    learning_consumer_idle_sleep_seconds: float = Field(
        5.0,
        gt=0,
        description=(
            "How long the consumer sleeps before re-checking the kill-switch when "
            "disabled (its OWN knob — not the sweeper's sweep interval)."
        ),
    )
    learning_scan_limit: int = Field(
        200, ge=1, description="Max idle sessions scanned/claimed per sweep cycle."
    )

    # --- Audit / provenance store (D95, §4) — READ in Slice 2. The dedicated
    # `learning_audit` bucket + its own `learning_audit_writer` RBAC user back the
    # CouchbaseAuditStore. S2 provisions + tests the client but writes NO evidence
    # (the first snapshot is S3's, §4.3).
    learning_audit_connection_string: str = Field(
        "couchbase://localhost",
        description="Couchbase connection string for the learning_audit bucket (may be the same cluster).",
    )
    learning_audit_bucket: str = Field(
        "learning_audit", description="Dedicated audit bucket (D95) — separate retention/RBAC clock."
    )
    # --- KEYSPACE, not just a bucket. Every learning store below carries a
    # `*_scope`/`*_collection` pair alongside its bucket, because the D95/D101/D48/D17
    # boundary is a KEYSPACE boundary and Couchbase 7.x can draw it two ways: one bucket
    # per store (what these defaults describe), or ONE bucket whose named scopes separate
    # the planes (`pcm_iwant`.`learning`.`audit`, `.sessions`.`sessions`, `.user`.`knowledge`).
    # The second layout is what a consolidated cluster wants, and it costs nothing here —
    # `bucket.scope("_default").collection("_default")` is the SAME handle
    # `bucket.default_collection()` returns, so a deployment that never sets these fields
    # behaves exactly as it did before they existed.
    #
    # These are NOT decorative: the N1QL statements in the candidate/corpus/user stores
    # interpolate the full three-part keyspace from them. A one-part `FROM `bucket`` in a
    # SHARED bucket scans every scope in it — see those call sites for what that breaks.
    learning_audit_scope: str = Field(
        "_default",
        description=(
            "Scope holding the audit collection. `_default` = the bucket's default scope "
            "(bucket-per-store layout); set it (e.g. `learning`) for a shared bucket."
        ),
    )
    learning_audit_collection: str = Field(
        "_default",
        description=(
            "Collection inside `learning_audit_scope`. `_default` = the bucket's default "
            "collection; set it (e.g. `audit`) for a shared bucket."
        ),
    )
    learning_audit_username: str = Field(
        "", description="RBAC user scoped to learning_audit ONLY (learning_audit_writer)."
    )
    learning_audit_password: str = Field(
        "", description="Password for the learning_audit_writer RBAC user (secret)."
    )
    learning_audit_ttl_seconds: int = Field(
        7_776_000,  # 90 days
        ge=1,
        description="Audit retention floor (D95): audit_TTL ≥ max_candidate_lifetime. Set fresh per write.",
    )

    # --- Candidate holding store (D101, Slice 3) — a dedicated, access-controlled
    # bucket sibling to learning_audit; holds extracted candidate envelopes at
    # status=extracted, queryable by status. Its own RBAC user scoped to it only.
    learning_candidates_connection_string: str = Field(
        "couchbase://localhost",
        description="Couchbase connection string for the learning_candidates bucket.",
    )
    learning_candidates_bucket: str = Field(
        "learning_candidates", description="Dedicated candidate-holding bucket (D101)."
    )
    learning_candidates_scope: str = Field(
        "_default",
        description=(
            "Scope holding the candidates collection (see `learning_audit_scope`). "
            "`_default` = the bucket's default scope; set it (e.g. `learning`) for a shared bucket."
        ),
    )
    learning_candidates_collection: str = Field(
        "_default",
        description=(
            "Collection inside `learning_candidates_scope`. `_default` = the bucket's default "
            "collection; set it (e.g. `candidates`) for a shared bucket. The `list_by_status` / "
            "`supersede` N1QL keyspaces AND the provisioned GSIs are built from this pair."
        ),
    )
    learning_candidates_username: str = Field(
        "", description="RBAC user scoped to learning_candidates ONLY (learning_candidates_writer)."
    )
    learning_candidates_password: str = Field(
        "", description="Password for the learning_candidates_writer RBAC user (secret)."
    )
    learning_candidates_ttl_seconds: int = Field(
        7_776_000,  # 90 days — a candidate must outlive the review-inbox dwell (D101/D95).
        ge=1,
        description="Candidate retention (≥ review-inbox dwell). Set fresh per write.",
    )

    # --- Blueprint corpus (Wave 3b-(i), D48) — the DURABLE landed-artifact store the
    # S6 dedup hard key looks up + the S9 promotion `hit_count` reader. A dedicated,
    # access-controlled bucket sibling to learning_candidates/learning_audit, with its
    # OWN RBAC user scoped to it only. Holds landed blueprint artifacts keyed by
    # `canonical_key`; `hit_count` is bumped ATOMICALLY server-side (sub-document
    # counter), never read-modify-write (the D48 "one create + one increment"
    # invariant). Durable by design (no TTL default — a landed artifact + its
    # cross-session hit_count must outlive any candidate/session).
    learning_corpus_connection_string: str = Field(
        "couchbase://localhost",
        description="Couchbase connection string for the learning_corpus bucket.",
    )
    learning_corpus_bucket: str = Field(
        "learning_corpus", description="Dedicated landed-artifact corpus bucket (D48)."
    )
    learning_corpus_scope: str = Field(
        "_default",
        description=(
            "Scope holding the corpus collection (see `learning_audit_scope`). "
            "`_default` = the bucket's default scope; set it (e.g. `learning`) for a shared bucket."
        ),
    )
    learning_corpus_collection: str = Field(
        "_default",
        description=(
            "Collection inside `learning_corpus_scope`. `_default` = the bucket's default "
            "collection; set it (e.g. `corpus`) for a shared bucket. `list_artifacts`' N1QL "
            "keyspace is built from this pair."
        ),
    )

    @model_validator(mode="after")
    def _reject_unprovisionable_keyspaces(self) -> LearningSettings:
        """Each of the three keyspaces must be one Couchbase can actually hold.

        The three groups are NOT listed here. `check_settings_keyspaces` derives every
        pair from the fields declared above, so a fourth learning keyspace is guarded by
        declaring it — the failure mode this repo keeps hitting is a hand-maintained list
        that the author of the new field forgets to extend.

        `mode="after"` so all six fields are populated: the defect is a RELATIONSHIP
        between a scope and its collection, not a bad value in either one alone — both
        `learning` and `_default` are perfectly good names, and only the pair is wrong.
        See `check_keyspace_pair` for what makes it unprovisionable and why it fails here
        rather than at the first KV op.

        THIS IS NOT THE ONLY GATE. `load_learning_settings_from_vault` re-runs the same
        check after its assignments, because a model validator does not re-fire on
        assignment — see that function.
        """
        check_settings_keyspaces(self)
        return self
    learning_corpus_username: str = Field(
        "", description="RBAC user scoped to learning_corpus ONLY (learning_corpus_writer)."
    )
    learning_corpus_password: str = Field(
        "", description="Password for the learning_corpus_writer RBAC user (secret)."
    )
    learning_corpus_ttl_seconds: int = Field(
        0,
        ge=0,
        description=(
            "Corpus retention (0 = no expiry — a landed artifact + its cross-session "
            "hit_count is durable and must outlive candidates/sessions)."
        ),
    )

    # --- S6 dedup soft layer (D48 §11) — the embedding near-miss bands. Previously
    # hardcoded in `dedup/stage.py`; surfaced here so an operator can tune them without
    # a code change. They are ORDERED bands over cosine similarity on `intent`:
    # `>= merge` ⇒ a mergeable variant, `>= conflict` ⇒ a partial-overlap conflict,
    # below ⇒ `insert`. NEITHER auto-appends: both route to the review inbox (D48 §3),
    # so a mis-tuned band costs review noise, never a bad landing.
    learning_dedup_merge_threshold: float = Field(
        0.95,
        ge=0.0,
        le=1.0,
        description=(
            "Cosine similarity at/above which an intent near-match is stamped `merge` "
            "(a mergeable blueprint variant, routed to review — never auto-appended)."
        ),
    )
    learning_dedup_conflict_threshold: float = Field(
        0.83,
        ge=0.0,
        le=1.0,
        description=(
            "Cosine similarity at/above which an intent near-match is stamped `conflict` "
            "(partial overlap, routed to review). Must be <= the merge threshold; below "
            "it the candidate is a genuinely-new `insert`."
        ),
    )

    # --- S6 soft recurrence counter (plan §4) — the DORMANT paraphrase counter. ---
    learning_recurrence_similarity_threshold: float = Field(
        0.90,
        ge=0.0,
        le=1.0,
        description=(
            "Cosine at/above which a candidate's intent counts as a soft RECURRENCE "
            "sighting of an existing learning_corpus artifact, bumping that artifact's "
            "`recurrence_count`. Set between the conflict band (0.83) and the merge band "
            "(0.95): the counter exists to catch the paraphrase pair that mints two "
            "different canonical keys (measured around 0.96 in practice), and counting "
            "'vaguely related question' as a recurrence would make it meaningless before "
            "anyone reads it. The counter is weighted 0.0 in the promotion gate today "
            "(LEARNING_PROMOTION_RECURRENCE_WEIGHT) and is accrued anyway, so that "
            "turning the weight up later has history behind it."
        ),
    )

    # --- S9 promotion policy (plan §4) — the knobs behind `PromotionPolicy`. ---
    #
    # Every one of these existed as a dataclass default that no entrypoint could reach:
    # all three promotion factories accepted a `policy=` and nobody passed one. They are
    # here so `promotion.models.policy_from_settings` can build the real thing.
    learning_promotion_routing_threshold: int = Field(
        1,
        ge=1,
        description=(
            "Corroboration threshold T for routing a blueprint candidate to the human "
            "review queue (`PromotionPolicy.blueprint_hit_threshold`). Was an unreachable "
            "3: the hard hit count requires byte-identical normalized-AST equality across "
            "sessions, which has never happened, so no candidate ever reached a human. "
            "Safe at 1 ONLY because the edge it gates now ends at `in_review` rather than "
            "`validated` — it rations a reviewer's attention, not corpus trust. Every "
            "correctness guard (leakage, static validation, depends_on, golden replay) is "
            "unchanged. Raise it with LEARNING_PROMOTION_RECURRENCE_WEIGHT when volume "
            "makes a human unable to read the queue."
        ),
    )
    learning_promotion_recurrence_weight: float = Field(
        0.0,
        ge=0.0,
        description=(
            "Weight on the SOFT recurrence count in the corroboration sum "
            "`hit_count + weight * recurrence_count`. 0.0 (dormant) is the shipped "
            "default: at threshold 1 every candidate clears the gate on its own first "
            "sighting, so a second signal changes nothing. It becomes load-bearing "
            "together with a raised routing threshold. KNOWN BEFORE YOU RAISE IT: the "
            "underlying counter has no per-sighting idempotency — a re-processed "
            "candidate (redelivery, re-enqueue, peer race, pipeline re-run) re-bumps "
            "every near artifact, so the stored count is sightings PLUS redelivery "
            "noise, biased upward. See PromotionPolicy.recurrence_weight."
        ),
    )
    learning_promotion_scan_limit: int = Field(
        200,
        ge=1,
        description=(
            "Max candidates the promotion cron examines per status per cycle. The scan "
            "ROTATES on `last_scanned_at`, so this bounds cost per cycle rather than "
            "starving the tail."
        ),
    )
    learning_promotion_interval_seconds: float = Field(
        300.0, gt=0, description="Promotion cron cadence (`run_forever` sleep)."
    )
    learning_drift_freshness_seconds: float = Field(
        86_400.0,
        gt=0,
        description=(
            "How stale a D43 drift verdict may be and still be TRUSTED on the blueprint "
            "silent fast path."
        ),
    )
    learning_replay_recheck_interval_seconds: float = Field(
        43_200.0,
        gt=0,
        description=(
            "How often the scheduler will PAY for a golden replay (a JWT mint + two live "
            "warehouse queries) on one candidate. Must stay <= "
            "LEARNING_DRIFT_FRESHNESS_SECONDS — a verdict must never be reused for longer "
            "than the trust window says it may be believed. The scheduler ENFORCES that "
            "at the point of use (it takes the min), so a misconfiguration can only make "
            "it probe more often, never trust a verdict longer."
        ),
    )
    learning_review_score_cutoff: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum inbox `review_score` an in_review candidate needs to appear in the "
            "default listing. 0.0 = no cutoff (the shipped posture — at <20 sessions/day "
            "a human skims the whole queue). Applied at LIST time, never at routing time: "
            "a routing-time cutoff would be a silent terminal state, whereas a hidden row "
            "is still stored, still queryable, and reappears when the knob moves. "
            "SET THIS FROM MEASURED DATA, NOT INTUITION: the score does not use the top "
            "of its range. Sentence-embedding cosines over English prose have a high "
            "floor (~0.53 between unrelated texts), so novelty lives in roughly "
            "[0.0, 0.47] and a perfect candidate scores about 0.26 against the live "
            "corpus. A 'moderate-sounding' 0.5 would hide the entire queue. See the "
            "measurement table in learning/inbox/ranking.py."
        ),
    )

    # --- Extractor (Slice 3, D31/D34) — the LLM structured-output model + retry. ---
    learning_extractor_model: str = Field(
        "claude-opus-4-8",
        description="Model id for the extractor's structured-output call (via the runtime ModelClient).",
    )
    learning_extractor_max_retries: int = Field(
        2, ge=0, description="Retries on a malformed (non-tool-call) extractor response (D31)."
    )
    learning_extractor_max_shape_corrections: int = Field(
        2,
        ge=0,
        description=(
            "CORRECTIVE turns one extraction may spend when a candidate PARSED but "
            "could not be READ — the model is told which field and what shape, and "
            "re-emits. A separate budget from LEARNING_EXTRACTOR_MAX_RETRIES on "
            "purpose: that one is for 'you did not call the tool', this one is for "
            "'your candidate is the wrong shape', and pooling them would let either "
            "failure starve the other. 0 disables it (a shape decline becomes terminal "
            "again and the model is never told). Watch learning.extract.correction_count: "
            "a rate that climbs is a PROMPT defect, not a model one."
        ),
    )
    learning_extractor_api_key: str = Field(
        "", description="API key for the extractor model client (secret). Empty ⇒ extractor dormant."
    )
    learning_extractor_base_url: str = Field(
        "", description="Optional OpenAI-compatible base URL for the extractor model client."
    )

    # --- Coverage judge (plan §3b) — the model call that cancels an extraction. ---
    #
    # Every threshold here is a knob because the plan requires it ("everything
    # threshold-shaped must be config, not a constant") and because one of them can
    # DISCARD an analyst's session. They live on `LearningSettings` rather than on
    # `PromotionPolicy` deliberately: `PromotionPolicy` is accepted by all three
    # promotion factories and passed by NO entrypoint, so a knob added there today is a
    # knob that does not exist in production. Wiring that is slice 4's job; a knob that
    # silently cancels work must be live from the first deploy.
    learning_judge_enabled: bool = Field(
        True,
        description=(
            "Master switch for the coverage judge. ON: a session whose work the corpus "
            "already carries is DROPPED before extraction (a durable record is written "
            "to learning_audit for every drop). OFF: the judge is never built and the "
            "loop behaves exactly as it did before plan §3b. Requires a prior-art index "
            "— with no index there is nothing to be covered BY, so no judge is built "
            "regardless of this flag."
        ),
    )
    learning_judge_shadow_mode: bool = Field(
        False,
        description=(
            "SHADOW MODE — the safe rollout. The judge runs, is asked, and records "
            "EVERY verdict to learning_audit exactly as it would in anger, but the drop "
            "is forced to False and logged: nothing is ever discarded. The row carries "
            "`would_drop=true` and `shadow=true`, so "
            "`SELECT count(*) ... WHERE would_drop = true` answers 'what would we have "
            "thrown away last week?' before anything is thrown away. This is NOT the "
            "same as a very high confidence bar (a bar cannot exceed 1.0, and at 1.0 a "
            "model asserting perfect certainty still drops) and NOT the same as "
            "LEARNING_JUDGE_ENABLED=false (which records nothing at all). Run this for "
            "a period, read the distribution, then turn it off."
        ),
    )
    learning_judge_record_ttl_seconds: int = Field(
        94_608_000,  # 3 years
        ge=1,
        description=(
            "Retention for judge verdict records — its OWN clock, deliberately much "
            "longer than LEARNING_AUDIT_TTL_SECONDS. An evidence quote is "
            "entity-bearing and should expire on the D95 90-day floor; a verdict row is "
            "scalars plus one capped reason, and the questions it exists to answer "
            "('how many drops last quarter', 'do verdicts skew to existing-plus-delta') "
            "accumulate over months. Inheriting the evidence retention would erase the "
            "dataset about as fast as its signal accrues."
        ),
    )
    learning_judge_model: str = Field(
        "",
        description=(
            "Model id for the judge's structured-output call. EMPTY => reuse the "
            "extractor's model client. The economics favour a smaller/cheaper model "
            "here: the judge sees a bounded brief plus five summary cards, while the "
            "call it cancels carries the whole session transcript."
        ),
    )
    learning_judge_pre_drop_confidence: float = Field(
        0.90,
        ge=0.0,
        le=1.0,
        description=(
            "PRE-extraction drop bar: a `duplicate` verdict at or above this cancels "
            "extraction entirely. Deliberately HIGHER than the post-extraction bar — "
            "the pre-extraction judge sees raw SQL with literals but no generalization, "
            "no parameterization and no result grain, so it is the cheapest place to "
            "drop and the least-informed one."
        ),
    )
    learning_judge_post_drop_confidence: float = Field(
        0.75,
        ge=0.0,
        le=1.0,
        description=(
            "POST-extraction drop bar: a `duplicate` verdict at or above this discards "
            "an extracted candidate. Lower than the pre-extraction bar because the "
            "judge is shown the generalized template, the grain and the rule ids."
        ),
    )
    learning_judge_band_low: float = Field(
        0.70,
        ge=0.0,
        le=1.0,
        description=(
            "Bottom of the ambiguous prior-art band. Below it nothing in the corpus is "
            "close enough to be worth a model call, at either stage."
        ),
    )
    learning_judge_band_high: float = Field(
        0.97,
        ge=0.0,
        le=1.0,
        description=(
            "Top of the ambiguous band, applied to the POST-extraction judge only. "
            "Above it the deterministic dedup layers and the merge routing already have "
            "an opinion. Not applied pre-extraction: there the score compares a "
            "session's raw text against entity-free corpus intents, which is far "
            "noisier than the post-extraction intent-against-intent comparison, so a "
            "high score is not a settled answer. Must be >= the band low."
        ),
    )
    learning_judge_timeout_seconds: float = Field(
        30.0,
        gt=0.0,
        description=(
            "Hard ceiling on one judge model call. On expiry the judge fails OPEN and "
            "extraction proceeds — it is an optimization and must never be the reason a "
            "session takes longer than it used to."
        ),
    )

    # --- Observability (D23/D24) ---
    otlp_endpoint: str = Field(
        "", description="OTLP collector endpoint (Phoenix). Empty => no-op provider."
    )
    learning_service_name: str = Field(
        "learning-loop", description="OTel service.name / Phoenix project for both processes."
    )
    learning_trace_verbose: bool = Field(
        True,
        description=(
            "D25 GATE, amended 2026-07-15 (deliberate operator posture flip): the default "
            "is now TRUE (verbose). The triage/consume/extract/promote spans AND the "
            "learning-sessions projection carry human-readable content (the user question, "
            "accepted SQL, learned intent/slots, resolves, rationale, evidence quotes, "
            "blueprint id) BY DEFAULT — so the `learning-loop` AND `learning-sessions` "
            "Phoenix projects are ENTITY-BEARING BY DEFAULT and MUST be access-controlled "
            "like the audit/session store (D51 in-boundary PII posture). Set FALSE to "
            "restore the D25 shape-only telemetry posture (counters/labels/session.id only — "
            "no transcript, SQL, question, or intent). The gate MECHANISM is unchanged; only "
            "the default posture flipped."
        ),
    )

    # --- HashiCorp Vault (sensitive-value sourcing) ---
    # When VAULT_ENABLED=true, the sensitive values on THIS surface (the learning Redis
    # URL, the three per-bucket Couchbase RBAC credentials, the extractor model API key)
    # are read from Vault at startup and OVERRIDE anything from the environment. Each
    # *_VAULT_PATH points at a KV secret; a path left empty skips that group (its env or
    # default value is kept). See load_learning_settings_from_vault() for the read logic.
    # Defaults keep Vault OFF, so existing env-based deploys are byte-identical.
    #
    # `VAULT_ENABLED` is the SAME env var the runtime surface reads, deliberately — the
    # same rationale as the shared TRUTHY_ENV_VALUES table above: one operator, one
    # deployment config, one switch that flips BOTH planes. A learning-only spelling
    # would let an operator turn Vault on for the request path and leave the offline
    # plane silently on env values (or the reverse), which fails as "the secret is wrong
    # in one process", the hardest shape to diagnose.
    vault_enabled: bool = Field(
        False,
        description="Read sensitive values from HashiCorp Vault at startup (overrides env).",
    )
    learning_redis_vault_path: str = Field(
        "application/datascience/iwant_reporting/redis",
        description=(
            "Vault KV path holding the learning Redis URL (key: LEARNING_REDIS_URL). "
            "Empty → use env/default."
        ),
    )
    learning_audit_vault_path: str = Field(
        "application/datascience/iwant_reporting/learning_audit",
        description=(
            "Vault KV path holding the learning_audit Couchbase credentials "
            "(connection string, bucket, scope, collection, username, password). "
            "Empty → use env/default."
        ),
    )
    learning_candidates_vault_path: str = Field(
        "application/datascience/iwant_reporting/learning_candidates",
        description=(
            "Vault KV path holding the learning_candidates Couchbase credentials "
            "(connection string, bucket, scope, collection, username, password). "
            "Empty → use env/default."
        ),
    )
    learning_corpus_vault_path: str = Field(
        "application/datascience/iwant_reporting/learning_corpus",
        description=(
            "Vault KV path holding the learning_corpus Couchbase credentials "
            "(connection string, bucket, scope, collection, username, password). "
            "Empty → use env/default."
        ),
    )
    learning_extractor_api_key_vault_path: str = Field(
        "application/datascience/iwant_reporting/llm",
        description=(
            "Vault KV path holding the extractor model API key (key: "
            "LEARNING_EXTRACTOR_API_KEY). Same path the runtime reads OPENAI_API_KEY "
            "from, under a different key. Empty → use env/default."
        ),
    )


def load_learning_settings_from_vault() -> LearningSettings:
    """Construct LearningSettings, then override sensitive values from Vault when enabled.

    Priority for each sensitive value:

      1. Vault  — when VAULT_ENABLED and the relevant ``*_VAULT_PATH`` is set
      2. Environment variable / ``.env``
      3. Field default

    Vault runs AFTER construction (it mutates the already-built LearningSettings) because
    pydantic-settings sources env/defaults at construction time; this mirrors
    `runtime/config.py::load_settings_from_vault`.  When Vault is disabled the env-built
    settings object is returned unchanged, so nothing about the existing env-based
    deployment path changes.
    """
    settings = LearningSettings()

    if not settings.vault_enabled:
        return settings

    if vault is None:
        raise RuntimeError(
            "VAULT_ENABLED=true but the internal 'paycompy' package is not "
            "installed. Install paycompy or set VAULT_ENABLED=false."
        )

    vc = vault.get_client_using_os_environ()

    # --- Learning queue Redis URL ---
    if settings.learning_redis_vault_path:
        settings.learning_redis_url = _read_vault_secret(
            vc,
            "LEARNING_REDIS_URL",
            settings.learning_redis_vault_path,
            settings.learning_redis_url,
        )

    # --- learning_audit KEYSPACE + credentials (D95) ---
    # `*_SCOPE`/`*_COLLECTION` are not secret material, and they are read from the same
    # KV path anyway: a credential and the keyspace it is granted on are ONE deployment
    # fact, and splitting them across two sources is how a scoped RBAC user ends up
    # pointed at a collection it has no grant on. `_read_vault_secret` is fail-soft with
    # a default, so an operator whose KV path predates these keys keeps `_default`.
    if settings.learning_audit_vault_path:
        p = settings.learning_audit_vault_path
        settings.learning_audit_connection_string = _read_vault_secret(
            vc, "LEARNING_AUDIT_CONNECTION_STRING", p, settings.learning_audit_connection_string
        )
        settings.learning_audit_bucket = _read_vault_secret(
            vc, "LEARNING_AUDIT_BUCKET", p, settings.learning_audit_bucket
        )
        settings.learning_audit_scope = _read_vault_secret(
            vc, "LEARNING_AUDIT_SCOPE", p, settings.learning_audit_scope
        )
        settings.learning_audit_collection = _read_vault_secret(
            vc, "LEARNING_AUDIT_COLLECTION", p, settings.learning_audit_collection
        )
        settings.learning_audit_username = _read_vault_secret(
            vc, "LEARNING_AUDIT_USERNAME", p, settings.learning_audit_username
        )
        settings.learning_audit_password = _read_vault_secret(
            vc, "LEARNING_AUDIT_PASSWORD", p, settings.learning_audit_password
        )

    # --- learning_candidates bucket credentials (D101) ---
    if settings.learning_candidates_vault_path:
        p = settings.learning_candidates_vault_path
        settings.learning_candidates_connection_string = _read_vault_secret(
            vc,
            "LEARNING_CANDIDATES_CONNECTION_STRING",
            p,
            settings.learning_candidates_connection_string,
        )
        settings.learning_candidates_bucket = _read_vault_secret(
            vc, "LEARNING_CANDIDATES_BUCKET", p, settings.learning_candidates_bucket
        )
        settings.learning_candidates_scope = _read_vault_secret(
            vc, "LEARNING_CANDIDATES_SCOPE", p, settings.learning_candidates_scope
        )
        settings.learning_candidates_collection = _read_vault_secret(
            vc, "LEARNING_CANDIDATES_COLLECTION", p, settings.learning_candidates_collection
        )
        settings.learning_candidates_username = _read_vault_secret(
            vc, "LEARNING_CANDIDATES_USERNAME", p, settings.learning_candidates_username
        )
        settings.learning_candidates_password = _read_vault_secret(
            vc, "LEARNING_CANDIDATES_PASSWORD", p, settings.learning_candidates_password
        )

    # --- learning_corpus bucket credentials (D48) ---
    if settings.learning_corpus_vault_path:
        p = settings.learning_corpus_vault_path
        settings.learning_corpus_connection_string = _read_vault_secret(
            vc, "LEARNING_CORPUS_CONNECTION_STRING", p, settings.learning_corpus_connection_string
        )
        settings.learning_corpus_bucket = _read_vault_secret(
            vc, "LEARNING_CORPUS_BUCKET", p, settings.learning_corpus_bucket
        )
        settings.learning_corpus_scope = _read_vault_secret(
            vc, "LEARNING_CORPUS_SCOPE", p, settings.learning_corpus_scope
        )
        settings.learning_corpus_collection = _read_vault_secret(
            vc, "LEARNING_CORPUS_COLLECTION", p, settings.learning_corpus_collection
        )
        settings.learning_corpus_username = _read_vault_secret(
            vc, "LEARNING_CORPUS_USERNAME", p, settings.learning_corpus_username
        )
        settings.learning_corpus_password = _read_vault_secret(
            vc, "LEARNING_CORPUS_PASSWORD", p, settings.learning_corpus_password
        )

    # --- Extractor model API key ---
    if settings.learning_extractor_api_key_vault_path:
        settings.learning_extractor_api_key = _read_vault_secret(
            vc,
            "LEARNING_EXTRACTOR_API_KEY",
            settings.learning_extractor_api_key_vault_path,
            settings.learning_extractor_api_key,
        )

    # RE-VALIDATE the keyspaces. Every assignment above went straight onto an
    # already-constructed model, and pydantic does NOT re-run a `mode="after"` validator on
    # assignment unless `validate_assignment` is set — which is deliberately off here, since
    # it would re-validate the whole model on each of the ~20 assignments above, most of
    # them against half-updated state. So `LearningSettings.__init__`'s check covered the
    # ENV values and nothing that Vault subsequently wrote over them.
    #
    # That gap was the primary production path, not a corner: every daemon entrypoint
    # reaches this function through `get_learning_settings()`, and `_read_vault_secret` is
    # FAIL-SOFT — an absent or misspelled KV key keeps the current value. So a KV path
    # holding LEARNING_AUDIT_SCOPE=learning with no LEARNING_AUDIT_COLLECTION beside it
    # leaves the collection at its `_default` default and produces exactly the
    # unprovisionable `learning`.`_default` pair this slice exists to refuse — silently,
    # surfacing as a keyspace-not-found at the first write. The env-var spelling of that
    # same typo failed at construction; this makes the Vault spelling fail here.
    #
    # Same derived check the constructor runs, so the two cannot drift.
    check_settings_keyspaces(settings)

    return settings


def get_learning_settings() -> LearningSettings:
    """A fresh, Vault-aware `LearningSettings` — deliberately not `@lru_cache`d.

    The process-static fields are read once at process start by the entrypoints; the only
    runtime-toggled value is `LEARNING_ENABLED`, served fresh by `learning_enabled()`.

    Un-cached costs nothing even with Vault in the path: every call site is a
    once-per-process startup construction (the daemon entrypoints and the inbox service
    build their settings before entering their loops), so a fresh Vault read per call is
    a handful of KV reads at boot, not per cycle.
    """
    return load_learning_settings_from_vault()


def unrecognized_learning_env_vars(environ: dict[str, str] | None = None) -> tuple[str, ...]:
    """Every `LEARNING_*` variable in *environ* that is NOT a settings field.

    `extra="ignore"` is required (the process env is full of unrelated variables) and quietly
    dangerous: a typo'd `LEARNING_MAX_DELIVERES` is accepted, dropped, and the shipped default
    silently applies. Prefix-scoped, so it can only flag a variable that looks meant for this
    surface; `LEARNING_ENABLED` is known-good (the kill-switch, deliberately absent).
    """
    known = {name.upper() for name in LearningSettings.model_fields}
    known.add("LEARNING_ENABLED")
    source = os.environ if environ is None else environ
    return tuple(
        sorted(
            name
            for name in source
            if name.upper().startswith("LEARNING_") and name.upper() not in known
        )
    )


def warn_unrecognized_learning_env_vars(logger: logging.Logger) -> tuple[str, ...]:
    """Log (WARNING) any `LEARNING_*` variable this settings surface will ignore; return them.

    Called once at entrypoint startup — the only moment at which the difference between
    "configured" and "believed to be configured" is still cheap.
    """
    unknown = unrecognized_learning_env_vars()
    if unknown:
        logger.warning(
            "IGNORING %d LEARNING_* environment variable(s) that are not settings "
            "fields — most likely typos, and each one means the SHIPPED DEFAULT is in "
            "effect where an override was intended: %s",
            len(unknown),
            ", ".join(unknown),
        )
    return unknown
