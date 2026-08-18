"""Extractor grounding — the catalog inputs the extractor needs (S3).

S3 grounds the `rule` role ONLY: it needs the set of EXISTING catalog rule ids so a
`rule`-role plan referencing a non-existent rule fails to review (D97 §4.2 / §7). Two views
of one parse: `known_rule_ids_from_catalog` answers "does this id exist?", and
`rule_index_from_catalog` answers what else the catalog says about the rules it declares —
which table, which columns — for `rule_match.py`. The id set is PROJECTED from the index,
so the two can never disagree about what the catalog contains.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogRule:
    """One `rules[*]` entry of the semantic catalog, with the table it is declared on.

    Carries exactly what `rule_match.py` READS. It deliberately omits
    `applies_when`/`description`: those are prose, and a matcher that scored prose would match
    on shared English rather than on a shared identifier. `predicate` is here because it names
    COLUMNS, not because it is SQL.
    """

    id: str
    table: str  # the "database.table" key the rule is declared under
    predicate: str  # the rule's definition as authored — SQL for most, prose for a few


@dataclass(frozen=True)
class RuleIndex:
    """Every catalog rule, indexed for the deterministic matcher.

    Empty is a legitimate state: every lookup returns nothing, the matcher finds no hint, and a
    `rule`-role plan citing an unknown id declines terminally exactly as it did before the
    matcher existed.
    """

    rules: tuple[CatalogRule, ...] = ()
    # EVERY catalogued table, not just the ones that declare rules. The distinction is
    # load-bearing for the matchers: without it, "a table the catalog knows and which
    # declares no rules" is indistinguishable from "an alias this module cannot
    # resolve", and the two must go opposite ways — the first is authoritative evidence
    # that there is no rule to name, the second is no evidence at all.
    tables: frozenset[str] = frozenset()

    def ids(self) -> frozenset[str]:
        return frozenset(rule.id for rule in self.rules)

    def knows_table(self, table: str) -> bool:
        """Does the catalog declare *table*?

        Matched the same suffix-tolerant way `on_table` matches, so the two always agree about
        what a qualifier resolves to.
        """
        wanted = table.strip().lower()
        if not wanted:
            return False
        return any(_same_table(known.strip().lower(), wanted) for known in self.tables)

    def by_id(self, rule_id: str) -> tuple[CatalogRule, ...]:
        """Every rule declared under *rule_id*.

        Usually one; the plural is not theoretical — ids are unique within a table's YAML, not
        across the catalog.
        """
        return tuple(rule for rule in self.rules if rule.id == rule_id)

    def on_table(self, table: str) -> tuple[CatalogRule, ...]:
        """The rules declared on *table*, matched suffix-tolerantly.

        A `ParamPlan` locator's `table` is model-authored and frequently the BARE name where the
        catalog key is fully qualified, so a bare name matches the LAST SEGMENT only — unlike
        `validation.py::_table_compatible`, whose permissiveness is right for a coverage check and
        would scope the matcher's candidate set to the ENTIRE catalog. An empty or unmatched
        *table* returns `()`, which the matcher reads as "no table evidence".
        """
        wanted = table.strip().lower()
        if not wanted:
            return ()
        return tuple(
            rule
            for rule in self.rules
            if _same_table(rule.table.strip().lower(), wanted)
        )


def _same_table(catalog_table: str, plan_table: str) -> bool:
    return (
        catalog_table == plan_table
        or catalog_table.endswith(f".{plan_table}")
        or plan_table.endswith(f".{catalog_table}")
    )


def rule_index_from_catalog(catalog: dict) -> RuleIndex:
    """Build the `RuleIndex` from a parsed catalog dict.

    Same tolerance and same skips as `known_rule_ids_from_catalog`, which is projected from
    this: a non-list `rules`, a non-dict rule and a rule with no `id` are skipped rather than
    raised on, because a malformed catalog entry must degrade the `rule` role, never stop the
    learning loop draining its queue.
    """
    rules: list[CatalogRule] = []
    tables: set[str] = set()
    for table, entry in catalog.items():
        tables.add(str(table))
        declared = entry.get("rules") or []
        if not isinstance(declared, list):
            continue
        for rule in declared:
            rule_id = rule.get("id") if isinstance(rule, dict) else None
            if not rule_id:
                continue
            rules.append(
                CatalogRule(
                    id=str(rule_id),
                    table=str(table),
                    predicate=str(rule.get("predicate") or ""),
                )
            )
    return RuleIndex(rules=tuple(rules), tables=frozenset(tables))


def known_rule_ids_from_catalog(catalog: dict) -> frozenset[str]:
    """The set of catalog rule ids (`rules[*].id`) from a parsed catalog dict.

    The dict has the shape the MCP `/catalog/export` serves — one `{db.table: <entry>}`
    mapping. Empty keeps the `rule` role inert (fail-to-review, the SAFE direction) rather than
    silently accepting an unresolved rule.
    """
    return rule_index_from_catalog(catalog).ids()
