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


def load_known_rule_ids(schema_dir: str | None = None) -> frozenset[str]:
    """Return the set of existing catalog rule ids (semantic-catalog `rules[*].id`).

    Empty if the catalog declares no rules or cannot be loaded — in which case the
    `rule` role is inert (every `rule`-role plan declines `missing_rule`), which is
    the SAFE direction (fail-to-review, never a silent unresolved rule)."""
    from data_agent.catalog.loader import load_semantic_catalog

    catalog = load_semantic_catalog(schema_dir)
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
