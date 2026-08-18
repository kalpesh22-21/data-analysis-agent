"""UserKnowledgeStoreConfig — the per-user store's dedicated settings surface (S8).

Mirrors the `learning_audit`/`learning_candidates` settings block (D95/D101): its OWN
connection string, bucket, and an RBAC user scoped to THAT bucket only. Read by
`CouchbaseUserKnowledgeStore` at construction; the composition root builds it from env.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
        description="Couchbase connection string for the user_knowledge bucket.",
    )
    user_knowledge_bucket: str = Field(
        "user_knowledge",
        description="Dedicated per-user knowledge bucket — entity-bearing, access-controlled (D17).",
    )
    user_knowledge_username: str = Field(
        "",
        description="RBAC user scoped to the user_knowledge bucket ONLY (user_knowledge_writer).",
    )
    user_knowledge_password: str = Field(
        "", description="Password for the user_knowledge_writer RBAC user (secret)."
    )
    user_knowledge_ttl_seconds: int = Field(
        0,
        ge=0,
        description="Optional TTL (0 = no expiry — a durable per-user fact outlives sessions).",
    )
