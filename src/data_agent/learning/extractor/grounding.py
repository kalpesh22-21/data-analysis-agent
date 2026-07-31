"""Extractor grounding — the catalog inputs the extractor needs (S3).

Slice 3 grounds the `rule` role ONLY: it needs the set of EXISTING catalog rule
ids so a `rule`-role plan referencing a non-existent rule fails to review
(D97 §4.2 / §7 missing-rule pairing). The rule ids live in the semantic catalog's
per-table `rules[*].id` (D67); `load_known_rule_ids` enumerates them.

DEFERRED (a documented later-slice deferral, NOT built here): full RAG-over-corpus
grounding — recall over the neo4j blueprint corpus + knowledge index so the
extractor proposes only what is NOT already represented (D27 dedup-awareness).
That is a Slice-6-adjacent concern; S3 emits candidates without corpus-dedup and
the Slice-6 dedup stage catches near-duplicates.
"""

from __future__ import annotations


def known_rule_ids_from_catalog(catalog: dict) -> frozenset[str]:
    """Return the set of catalog rule ids (`rules[*].id`) from a parsed catalog dict.

    The catalog dict has the shape `load_semantic_catalog()` returns and the MCP
    `/catalog/export` serves — one `{db.table: <entry>}` mapping. Empty if no rules
    are declared, which keeps the `rule` role inert (fail-to-review, the SAFE
    direction) rather than silently accepting an unresolved rule."""
    ids: set[str] = set()
    for entry in catalog.values():
        rules = entry.get("rules") or []
        if not isinstance(rules, list):
            continue
        for rule in rules:
            rule_id = rule.get("id") if isinstance(rule, dict) else None
            if rule_id:
                ids.add(str(rule_id))
    return frozenset(ids)


def load_known_rule_ids(schema_dir: str | None = None) -> frozenset[str]:
    """Return the set of existing catalog rule ids (semantic-catalog `rules[*].id`).

    Reads the dir-based semantic catalog (retained for the offline/dir path) and
    projects it via `known_rule_ids_from_catalog`. Empty if the catalog declares no
    rules or cannot be loaded — in which case the `rule` role is inert (every
    `rule`-role plan declines `missing_rule`), the SAFE direction."""
    from data_agent.catalog.loader import load_semantic_catalog

    return known_rule_ids_from_catalog(load_semantic_catalog(schema_dir))
