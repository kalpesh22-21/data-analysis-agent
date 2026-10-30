"""Corpus-wide S4 rendering parity, separate from extraction-plan coverage.

A canon template may contain CASE selectors, repeated slots, and structural parameters
that a column-predicate extraction plan cannot reconstruct from concrete literals.
Replacing every slot with a string and inventing a column locator does not simulate S3.
These tests exercise real S4 rewriting at supported predicate sites and preserve other
existing placeholders, as S4 does for composite inputs. They do not claim that S3 can
learn every authored blueprint from a concrete query. Unsupported column-plan cases
remain explicit below; their distinct keys must not be normalized away.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlglot import exp

from data_agent.learning.generalize.rewrite import rewrite_sql_to_template
from data_agent.runtime.blueprint.structural_key import structural_key_from_templates
from data_agent.runtime.blueprint.template import parse_template, referenced_slots
from data_agent.runtime.retrieval.corpus_loader import (
    corpus_seeds_from_export,
    resolve_blueprint_references,
)

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "corpus_snapshot" / "blueprints.yaml"
_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.ILike, exp.In)


@lru_cache(maxsize=1)
def _canon_docs() -> dict[str, Any]:
    """Use the committed corpus in CI too; separate parity tests guard external drift."""
    raw = {doc["id"]: doc for doc in yaml.safe_load(_FIXTURE.read_text())}
    seeds, _ = corpus_seeds_from_export({"blueprints": raw})
    return {
        seed.id: {**raw[seed.id], "composes": seed.composes}
        for seed in resolve_blueprint_references(seeds)
    }


def _node_pairs(doc):
    return [
        (node["order"], node["sql_template"])
        for node in doc.get("composes") or []
        if isinstance(node.get("order"), int) and node.get("sql_template")
    ]


def _accepted_sql_and_params(template):
    """Materialize each locatable occurrence with its own unambiguous literal.

    CASE/interval/arithmetic sites stay placeholders: a column locator cannot name them.
    Repeated predicate slots need separate plan entries, not a single replacement.
    """
    ast = parse_template(template)
    params = []
    for occurrence, placeholder in enumerate(list(ast.find_all(exp.Placeholder))):
        predicate = placeholder.find_ancestor(*_COMPARISONS)
        columns = list(predicate.find_all(exp.Column)) if predicate is not None else []
        if not columns:
            continue
        value = f"__slot_{placeholder.name}_{occurrence}__"
        params.append(
            {
                "role": "slot",
                "locator": {"column": columns[0].name, "value": value},
                "slot": {"name": placeholder.name, "binds_to": f"db.t.{columns[0].name}"},
            }
        )
        placeholder.replace(exp.Literal.string(value))
    sql = ast.sql(dialect="clickhouse")
    for name in referenced_slots(template):
        sql = sql.replace("{" + name + ": }", "{" + name + "}")
    return sql, params


def _rewritten_key(doc, *, parameterize):
    def rewrite(template):
        sql, params = _accepted_sql_and_params(template) if parameterize else (template, [])
        return rewrite_sql_to_template(sql, params, strict=True)

    return structural_key_from_templates(
        doc.get("result_grain"),
        rewrite(doc["sql_template"]) if doc.get("sql_template") else None,
        [(order, rewrite(template)) for order, template in _node_pairs(doc)],
    )


def _canon_key(doc):
    return structural_key_from_templates(
        doc.get("result_grain"), doc.get("sql_template"), _node_pairs(doc)
    )


@pytest.mark.parametrize("bp_id", sorted(_canon_docs()))
@pytest.mark.parametrize("parameterize", [False, True], ids=["render", "predicate_rewrite"])
def test_corpus_templates_keep_their_keys_through_s4(bp_id, parameterize):
    doc = _canon_docs()[bp_id]
    assert _canon_key(doc).startswith("sha256:")
    assert _rewritten_key(doc, parameterize=parameterize) == _canon_key(doc)


@pytest.mark.parametrize(
    "template",
    [
        "SELECT CASE {group_by} WHEN 'department' THEN department ELSE 'All' END FROM db.t",
        "SELECT department FROM db.t ORDER BY CASE WHEN {direction} = 'DESC' THEN department END DESC",
        "SELECT count(*) / {months} FROM db.t",
    ],
)
def test_column_locators_do_not_claim_to_recover_non_column_slots(template):
    """Missing extraction support is not evidence of equivalent SQL; never fold it away."""
    (name,) = referenced_slots(template)
    accepted = template.replace("{" + name + "}", "'sentinel'")
    rewritten = rewrite_sql_to_template(
        accepted,
        [
            {
                "role": "slot",
                "locator": {"column": None, "value": "sentinel"},
                "slot": {"name": name, "binds_to": None},
            }
        ],
    )
    assert name not in referenced_slots(rewritten)
    assert structural_key_from_templates(None, rewritten) != structural_key_from_templates(
        None, template
    )


def test_predicate_rewrite_is_nonvacuous_and_recovers_repeated_sites():
    template = "SELECT department FROM db.t\nWHERE department = {dept} OR backup = {dept}"
    sql, params = _accepted_sql_and_params(template)
    assert len(params) == 2
    assert "{dept}" not in sql
    rewritten = rewrite_sql_to_template(sql, params)
    assert rewritten.count("{dept}") == 2
    assert "__slot_" not in rewritten
    assert structural_key_from_templates(None, rewritten) == structural_key_from_templates(
        None, template
    )


def test_corpus_exercises_both_rendering_and_real_literal_replacement():
    docs = _canon_docs()
    assert docs
    plans = [
        _accepted_sql_and_params(template)[1]
        for doc in docs.values()
        for template in (
            [doc["sql_template"]]
            if doc.get("sql_template")
            else [template for _, template in _node_pairs(doc)]
        )
    ]
    assert any(plans), "the corpus test must exercise literal replacement, not only rendering"
