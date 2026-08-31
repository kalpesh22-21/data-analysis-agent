"""QA (delta pass): a REALISTIC cross-tier match rate, measured through the real S4 path.

The builder's headline "10/10 match, up from 0/10" was measured against a SIMULATED
learning twin — the canon templates with their aggregates lowercased by hand. That
measurement is circular: the twin was constructed by applying exactly the transformation
the fold is designed to undo, so 10/10 was guaranteed by construction. It establishes
that the fold inverts casing divergence; it establishes NOTHING about whether real
extractor output matches canon.

This file measures something the simulation cannot: take each real MCP-canon template,
substitute literals back into its `{slot}` sites (recovering an "accepted SQL" of the
shape S3 hands S4), and push it through the ACTUAL shipping learning-side renderer,
`learning/generalize/rewrite.py::rewrite_sql_to_template` — a sqlglot parse + Placeholder
substitution + `.sql(dialect="clickhouse")` round-trip. THAT is what determines the
learning tier's template text, not an LLM's spelling, because D35 forbids re-emitting SQL
from the model.

It is still not the real thing: the model's accepted SQL may differ from canon's phrasing
in ways no round-trip can simulate (that is what gaps (a)-(d) in
`test_structural_key_known_gaps_qa.py` enumerate). What it DOES establish is a genuine
lower bound — the match rate when the learning tier sees the identical query — and it is
measured through production code rather than a regex.

Measured result: every canon blueprint round-trips to its own structural key EXCEPT two
(`_KNOWN_ROUNDTRIP_MISSES`), which are pinned individually below with the reason. That
was 8 of 10 when first measured and is 9 of 11 since plan §2b extracted
`bp-employee-check-detail-for-period`; the ratio moves with corpus size, the miss SET
does not, so the miss set is what is asserted.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from data_agent.learning.generalize.rewrite import rewrite_sql_to_template
from data_agent.runtime.blueprint.structural_key import structural_key_from_templates
from data_agent.runtime.retrieval.corpus_loader import (
    corpus_seeds_from_export,
    resolve_blueprint_references,
)

_REAL_CANON_DIR = Path("/Users/kalpeshmulye/Development/clickhouse-api/app/corpus/data/blueprints")
_SLOT_RE = re.compile(r"\{(\w+)\}")

# Blueprints whose round-trip does NOT reproduce the canon key, with the reason. Pinned
# rather than skipped so the count cannot silently grow.
_KNOWN_ROUNDTRIP_MISSES = {
    # Both use a slot in a NON-COMPARISON position (`INTERVAL {window_months} MONTH`,
    # `COUNT(...) / {window_months}`). `rewrite.py::_find_literal` only searches inside
    # comparison/membership predicates (`_COMPARISONS`), so such a literal is never
    # replaced by a Placeholder and the learning template keeps the literal inline.
    # That is a REWRITER limitation, not a structural-key one, and it means these two
    # canon blueprints are unreachable by the learning tier for a reason the key cannot
    # fix.
    "bp-hires-per-month",
    "bp-hires-projection",
}


def _yaml() -> Any:
    return pytest.importorskip("yaml")


def _canon_docs() -> dict[str, Any]:
    """The real canon YAMLs, with `composes[].ref` RESOLVED through the production
    loader (plan §2b) so each doc carries the SQL its nodes actually run.

    Resolving here is load-bearing, not tidiness. A referenced node has no
    `sql_template` of its own until the loader inlines one, so reading the raw YAML
    would silently drop those nodes from `_node_pairs` — and BOTH sides of the
    round-trip use `_node_pairs`, so the comparison would still "pass" while measuring
    a query neither tier runs."""
    if not _REAL_CANON_DIR.is_dir():
        pytest.skip(f"real MCP canon not checked out at {_REAL_CANON_DIR}")
    raw = {}
    for path in sorted(_REAL_CANON_DIR.glob("bp-*.yaml")):
        doc = _yaml().safe_load(path.read_text())
        raw[doc["id"]] = doc
    seeds, _ = corpus_seeds_from_export({"blueprints": raw})
    docs = dict(raw)
    for seed in resolve_blueprint_references(seeds):
        docs[seed.id] = {**raw[seed.id], "composes": seed.composes}
    return docs


def _node_pairs(doc: dict[str, Any]) -> list[tuple[int, str]]:
    pairs = []
    for node in doc.get("composes") or []:
        template, order = node.get("sql_template"), node.get("order")
        if isinstance(template, str) and template.strip() and isinstance(order, int):
            pairs.append((order, template))
    return pairs


def _accepted_sql_and_params(template: str) -> tuple[str, list[dict[str, Any]]]:
    """Turn a canon `{slot}` template back into an "accepted SQL" + the S3
    `parameterization` S4 would receive for it."""
    names = list(dict.fromkeys(_SLOT_RE.findall(template)))
    sql = template
    for name in names:
        sql = sql.replace("{" + name + "}", f"'__slot_{name}__'")

    params = []
    for name in names:
        literal = f"__slot_{name}__"
        match = re.search(
            r"([A-Za-z_][\w.]*)\s*\)?\s*(?:=|>=|<=|>|<|!=|<>|LIKE|IN)\s*'" + re.escape(literal) + r"'",
            sql,
        )
        column = match.group(1).split(".")[-1] if match else None
        params.append(
            {
                "role": "slot",
                "locator": {"column": column, "value": literal},
                "slot": {"name": name, "binds_to": f"db.t.{column}"},
            }
        )
    return sql, params


def _learning_twin_key(doc: dict[str, Any]) -> str:
    """The structural key a learning candidate would land, if S4 saw the exact query the
    canon blueprint describes."""
    top = None
    if doc.get("sql_template"):
        sql, params = _accepted_sql_and_params(doc["sql_template"])
        top = rewrite_sql_to_template(sql, params, strict=True)

    nodes = []
    for order, template in _node_pairs(doc):
        sql, params = _accepted_sql_and_params(template)
        nodes.append((order, rewrite_sql_to_template(sql, params, strict=False)))

    # The learning tier writes the D56 stamp with the LOWERCASE alias its SQL selects.
    grain = {
        "columns": [str(c).lower() for c in (doc.get("result_grain") or [])],
        "verifiable": True,
    }
    return structural_key_from_templates(grain, top, nodes)


def _canon_key(doc: dict[str, Any]) -> str:
    return structural_key_from_templates(
        doc.get("result_grain"), doc.get("sql_template"), _node_pairs(doc)
    )


@pytest.mark.parametrize("bp_id", sorted(set(_canon_docs()) - _KNOWN_ROUNDTRIP_MISSES))
def test_a_canon_blueprint_round_trips_through_the_real_s4_rewriter_to_its_own_key(bp_id) -> None:
    """The realistic lower bound, per blueprint. A canon template pushed through the real
    learning-side renderer must come back out with the SAME structural key — otherwise the
    learning loop would re-propose that exact canon blueprint even having seen the
    identical query."""
    doc = _canon_docs()[bp_id]
    canon = _canon_key(doc)
    assert canon, f"{bp_id} mints no canon key at all"
    assert _learning_twin_key(doc) == canon


@pytest.mark.parametrize("bp_id", sorted(_KNOWN_ROUNDTRIP_MISSES))
def test_the_known_round_trip_misses_are_still_exactly_these_two(bp_id) -> None:
    """Pinned divergence, NOT a skip. Both blueprints place a slot outside a comparison
    predicate (`INTERVAL {window_months} MONTH`, `COUNT(...) / {window_months}`), which
    `rewrite_sql_to_template` cannot parameterize — so the learning template keeps the
    literal inline and mints a different key.

    This is a REWRITER gap, upstream of the structural key, and it caps the achievable
    cross-tier match rate at 8/10 no matter how good the key gets. If a fix lands, this
    test fails and the blueprint moves into the passing set above.
    """
    doc = _canon_docs()[bp_id]
    assert _canon_key(doc)
    assert _learning_twin_key(doc) != _canon_key(doc)


def test_the_realistic_match_rate_is_all_but_the_two_known_rewriter_misses() -> None:
    """THE HEADLINE CORRECTION.

    The builder reported 10/10 against a hand-lowercased twin. Measured through the real
    S4 rewriter over the real canon corpus, the rate was 8/10. The two-blueprint
    shortfall is not a structural-key defect — it is the rewriter's inability to
    parameterize a slot outside a comparison — but the honest number for "the learning
    tier saw this exact query and recognized the canon blueprint" is that ratio, and it
    is an UPPER bound on real performance because a real candidate's SQL also diverges
    in the ways gaps (a)-(d) describe.

    HISTORY, and why the number is no longer a literal. 8/10 became 9/11 when plan §2b
    extracted `bp-employee-check-detail-for-period` from
    `bp-compare-employee-check-detail-two-periods` — a corpus-size change, NOT a
    measurement change. Pinning the two literals let a corpus edit look like a
    regression in the rewriter and vice versa. What the finding actually claims is
    "every canon blueprint round-trips EXCEPT the `_KNOWN_ROUNDTRIP_MISSES`", so that is
    what is asserted, with the misses named. A rewriter regression still fails here (the
    miss set grows); the earlier fixed ratio is recorded above so the delta stays
    readable.
    """
    docs = _canon_docs()
    # NON-VACUITY, not a size. The docstring above already argues that pinning a literal lets
    # a corpus edit look like a rewriter regression — and `len(docs) == 11` was that argument's
    # own leftover, which is what fired when the canon grew to 12 on 2026-08-28.
    #
    # What the assertion below actually needs is that the corpus LOADED and that the named
    # misses are really in it; without this it would pass vacuously on an empty read
    # (`set() - MISSES == set()`), which is the failure a bare count was standing in for.
    # Both properties are corpus-size independent, so a canon addition no longer reads as a
    # regression here — the guard that OWNS canon/mirror drift is
    # `test_corpus_loader_structural_key_qa.py`, and it derives from the two directories.
    assert docs, "the canon read returned nothing — the round-trip claim would be vacuous"
    assert _KNOWN_ROUNDTRIP_MISSES <= set(docs), (
        "a named round-trip miss is not in the canon any more; retire it from "
        "_KNOWN_ROUNDTRIP_MISSES rather than leaving it asserted against nothing"
    )
    matched = {bid for bid, doc in docs.items() if _learning_twin_key(doc) == _canon_key(doc)}
    assert matched == set(docs) - _KNOWN_ROUNDTRIP_MISSES, (
        f"round-trip match set moved: {len(matched)}/{len(docs)} matched; "
        f"unexpected misses {sorted(set(docs) - _KNOWN_ROUNDTRIP_MISSES - matched)}, "
        f"unexpected hits {sorted(matched & _KNOWN_ROUNDTRIP_MISSES)}"
    )


def test_every_canon_blueprint_at_least_mints_a_key_on_both_sides() -> None:
    """Weaker but broader: even the two misses produce a key on each side. A blueprint
    that minted NO key would be invisible to prior-art matching entirely, which is a
    strictly worse failure than a mismatched key."""
    for bp_id, doc in sorted(_canon_docs().items()):
        assert _canon_key(doc).startswith("sha256:"), bp_id
        assert _learning_twin_key(doc).startswith("sha256:"), bp_id


def test_the_round_trip_is_not_vacuous_the_twin_really_differs_textually() -> None:
    """Guard against the test above passing because the "twin" is byte-identical to canon.
    The round-trip must actually re-render the SQL (whitespace collapsed, `!=` -> `<>`,
    `toStartOfMonth` -> `dateTrunc`), so a match is the NORMALIZER agreeing, not the
    strings being the same."""
    doc = _canon_docs()["bp-active-headcount-by-department"]
    sql, params = _accepted_sql_and_params(doc["sql_template"])
    twin = rewrite_sql_to_template(sql, params, strict=True)
    assert twin != doc["sql_template"]
    assert "\n" in doc["sql_template"] and "\n" not in twin.strip()
    assert _learning_twin_key(doc) == _canon_key(doc)
