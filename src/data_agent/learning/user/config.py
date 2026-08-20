"""UserKnowledgeStoreConfig — the per-user store's dedicated settings surface (S8).

Mirrors the `learning_audit`/`learning_candidates` settings block (D95/D101): its OWN
connection string, KEYSPACE (bucket + scope + collection), and an RBAC user scoped to THAT
keyspace only. Read by `CouchbaseUserKnowledgeStore` at construction; the composition root
builds it from env.

The grant is a keyspace rather than a bucket because all five stores may share one bucket,
separated by named scopes — see the comment block on `user_knowledge_scope` below, and
`learning/user/store.py` for why comparing bucket names there guards nothing.
"""

from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from data_agent.runtime.config import check_settings_keyspaces


class UserKnowledgeStoreConfig(BaseSettings):
    """Env-var config for the per-user knowledge store (S8)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    user_knowledge_connection_string: str = Field(
        "couchbase://localhost",
        description="Couchbase connection string for the user_knowledge keyspace's cluster.",
    )
    user_knowledge_bucket: str = Field(
        "user_knowledge",
        description=(
            "Bucket holding the per-user knowledge keyspace — entity-bearing, "
            "access-controlled (D17). Dedicated to this store in the bucket-per-store "
            "layout; shared, with the grant drawn at the scope below, in the other."
        ),
    )
    # --- The D17 boundary is a KEYSPACE, not a bucket. Mirrors the `*_scope`/`*_collection`
    # pair on every learning store (see `learning/config.py`): the same store reaches either
    # a dedicated bucket (`user_knowledge`.`_default`.`_default`, these defaults) or one
    # named scope inside a shared bucket (`pcm_iwant`.`user`.`knowledge`).
    # `bucket.scope("_default").collection("_default")` is the SAME handle
    # `bucket.default_collection()` returns, so the defaults change nothing.
    #
    # This store matters MOST of the four. It is the only one holding entity-bearing
    # per-user facts, and in a shared bucket a one-part `FROM `pcm_iwant`` in
    # `list_for_user` would read the audit, candidate and corpus scopes too — the exact
    # cross-store surface the scoped RBAC role exists to remove. Both the N1QL keyspace
    # and the `open_keyspace` guard are built from this pair.
    user_knowledge_scope: str = Field(
        "_default",
        description=(
            "Scope holding the knowledge collection. `_default` = the bucket's default scope "
            "(bucket-per-store layout); set it (e.g. `user`) for a shared bucket."
        ),
    )
    user_knowledge_collection: str = Field(
        "_default",
        description=(
            "Collection inside `user_knowledge_scope`. `_default` = the bucket's default "
            "collection; set it (e.g. `knowledge`) for a shared bucket."
        ),
    )
    user_knowledge_username: str = Field(
        "",
        description=(
            "RBAC user scoped to the user_knowledge KEYSPACE ONLY (user_knowledge_writer) — "
            "granted `bucket:scope:collection`, so sharing a bucket with the audit/candidate/"
            "corpus stores does not share their reach into this one."
        ),
    )
    user_knowledge_password: str = Field(
        "", description="Password for the user_knowledge_writer RBAC user (secret)."
    )
    user_knowledge_ttl_seconds: int = Field(
        0,
        ge=0,
        description="Optional TTL (0 = no expiry — a durable per-user fact outlives sessions).",
    )

    @model_validator(mode="after")
    def _reject_unprovisionable_keyspace(self) -> UserKnowledgeStoreConfig:
        """The configured keyspace must be one Couchbase can actually hold.

        The SAME check every other Couchbase-backed store runs — imported from
        `runtime/config.py`, where the shared settings plumbing lives, rather than
        restated, so no two surfaces can drift on what a valid keyspace is. Imported from
        there DIRECTLY rather than re-exported through the sibling learning settings
        module: this surface is deliberately separate (S8), and routing a runtime import
        through a module it otherwise has no reason to touch would invent a dependency the
        separation exists to avoid.

        `mode="after"` because the defect is the RELATIONSHIP between the two fields, not a
        bad value in either.
        """
        check_settings_keyspaces(self)
        return self
