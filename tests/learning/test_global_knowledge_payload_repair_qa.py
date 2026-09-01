"""Adversarial QA on `scripts/fix_global_knowledge_payload_keys.py::rewrite_payload`.

The script is the one-shot for candidates that were WRITTEN BEFORE intake checked
anything: a `global_knowledge` envelope whose payload never carried a `statement`, sitting
at `in_review`, answering 503 to every approve. Nothing re-validates a stored envelope, so
this pure function is the whole of the repair's decision-making — which is what makes it
worth testing without a Couchbase.

It is loaded BY PATH (`scripts/` is not a package), the way `tests/test_http_launchers.py`
loads the chart launchers. The import is safe: the module builds no client at import time.

WHAT THESE PIN, in order of how expensive getting them wrong is:

  * IDEMPOTENCE. `--apply` writes into the live queue and a partial failure has to be
    resumable, so a second run over an already-repaired payload must be a no-op — not a
    re-derive from a `definition` a human has since edited.
  * NEVER INVENT. A payload with nothing usable as a statement returns `None` rather than
    a fabricated line, because the reviewer who approves it is attesting to text they
    believe the model wrote.
  * THE CLOSED SET STAYS CLOSED. What the script writes must be exactly what intake would
    now accept, or the repair simply moves the jam one gate later.

Two findings from the first QA pass are now FIXED and pinned as such below: the
`knowledge`/`knowledge_update` shapes the live store actually holds are repaired (they are
statement sources), and a present-but-unusable `statement` no longer shadows a usable
`definition` beside it (one `_usable` predicate decides both the keep and the derive).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "fix_global_knowledge_payload_keys.py"
)


def _load() -> ModuleType:
    name = "_repair_script_under_test"
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


@pytest.fixture(scope="module")
def script() -> ModuleType:
    return _load()


@pytest.fixture(scope="module")
def rewrite(script: ModuleType):
    return script.rewrite_payload


# The payload that actually jammed the inbox — four plausible names, none of them the
# contract's. Every test below is a variation on it.
STUCK = {
    "definition": "an active employee is one with no termination date",
    "fact_type": "business_rule",
    "intent": "define active employee",
    "scope": "employee",
}


# --- the shape it was written for --------------------------------------------------


def test_the_stuck_shape_is_rewritten_onto_the_contract_names(rewrite) -> None:
    out = rewrite(dict(STUCK))

    assert out == {
        "statement": "an active employee is one with no termination date",
        "knowledge_type": "business_rule",
        "scope": "employee",
    }


def test_intent_is_dropped_rather_than_carried_along(rewrite) -> None:
    """The key a reader misses, and the reason the leakage gate reported `pass` on a
    payload it had never read. Any key outside the contract five is a text surface
    nothing scans, so "keep it just in case" is how the hole gets back in."""
    out = rewrite(dict(STUCK))

    assert "intent" not in out
    # Nor does its TEXT survive under another name.
    assert "define active employee" not in str(out)


def test_what_it_writes_is_a_subset_of_the_key_set_intake_accepts(rewrite, script) -> None:
    """The repair must produce a payload the CURRENT intake check would accept, or all
    it has done is move the jam from approve to the next extraction."""
    from data_agent.learning.extractor.validation import _GLOBAL_KNOWLEDGE_KEYS

    assert set(script._KEPT) == set(_GLOBAL_KNOWLEDGE_KEYS)
    out = rewrite(dict(STUCK))
    assert set(out) <= set(_GLOBAL_KNOWLEDGE_KEYS)
    # And every derivation lands INSIDE that set — the statement sources feed `statement`
    # and `fact_type` feeds `knowledge_type`, never an off-contract name that would be a
    # fresh unscanned surface.
    assert set(script._STATEMENT_SOURCES).isdisjoint(_GLOBAL_KNOWLEDGE_KEYS)


def test_the_repaired_payload_passes_the_real_intake_reader(rewrite) -> None:
    """End to end against the check itself, not against a restatement of it."""
    from data_agent.learning.extractor.validation import _global_knowledge_payload

    assert _global_knowledge_payload(rewrite(dict(STUCK))) is None


def test_the_repaired_payload_maps_onto_a_real_landing_seed(rewrite) -> None:
    """The other end: the approve that used to raise now produces a seed whose text is
    the statement. This is the defect's actual terminus (`knowledge_seed_from_candidate`),
    so the repair is only proven by getting through it."""
    from dataclasses import replace

    from data_agent.learning.generalize.mapping import knowledge_seed_from_candidate

    from .promotion.helpers import make_blueprint_candidate, with_type

    env = with_type(make_blueprint_candidate(), "global_knowledge")
    before = replace(env, payload=dict(STUCK))
    with pytest.raises(ValueError):
        knowledge_seed_from_candidate(before, id="k::1")

    after = replace(env, payload=rewrite(dict(STUCK)))
    seed = knowledge_seed_from_candidate(after, id="k::1")
    assert seed.text.startswith("an active employee")
    assert seed.title == "employee"
    assert "define active employee" not in seed.text


# --- precedence: an existing contract key is never overwritten by a guess ----------


def test_statement_wins_over_definition_and_is_left_untouched(rewrite) -> None:
    """BOTH present. The contract name is the one a human or a fixed extractor wrote;
    the off-contract one is the guess. The statement is preserved VERBATIM — never
    re-derived — but the row is still rewritten, because the off-contract keys beside it
    (`definition`, `intent`) are unscanned surfaces and dropping them is the point."""
    out = rewrite({**STUCK, "statement": "the real, human-attested statement"})

    assert out["statement"] == "the real, human-attested statement"
    assert "definition" not in out
    assert "intent" not in out


def test_knowledge_type_wins_over_fact_type(rewrite) -> None:
    out = rewrite({**STUCK, "knowledge_type": "on_contract_label"})

    assert out["knowledge_type"] == "on_contract_label"
    assert out["statement"] == STUCK["definition"]


# --- idempotence: `--apply` runs against a live queue and must be resumable --------


def test_running_the_rewrite_twice_is_the_same_as_running_it_once(rewrite) -> None:
    once = rewrite(dict(STUCK))
    twice = rewrite(dict(once))

    # The second pass reports NOTHING TO DO — which is what makes the tally honest and
    # what stops a resumed run from touching a row it already fixed.
    assert twice is None


def test_a_re_run_does_not_re_derive_from_a_definition_a_human_edited_back_in(
    rewrite,
) -> None:
    """The failure idempotence is actually protecting against: a repaired candidate that
    someone then annotated. A second run must not overwrite the attested `statement`
    with the stale `definition` sitting beside it — the statement survives verbatim and
    the stale key is dropped (it is an unscanned surface like any other extra)."""
    fixed = rewrite(dict(STUCK))
    annotated = {**fixed, "definition": "a stale, superseded definition"}

    again = rewrite(annotated)
    assert again["statement"] == fixed["statement"]
    assert "definition" not in again


def test_the_function_does_not_mutate_the_payload_it_was_given(rewrite) -> None:
    """`run()` logs `env.payload` AFTER calling this (the `_diff` before/after line), so
    an in-place mutation would make the dry-run report describe a change that never
    happened."""
    payload = dict(STUCK)
    snapshot = dict(payload)

    rewrite(payload)

    assert payload == snapshot


# --- nothing usable: report, never invent -----------------------------------------


def test_an_empty_string_definition_yields_no_repair(rewrite) -> None:
    """`""` is not a statement, and `knowledge_seed_from_candidate` refuses it — so a
    rewrite that produced one would only relocate the same landing failure."""
    assert rewrite({**STUCK, "definition": ""}) is None


def test_a_whitespace_only_definition_yields_no_repair(rewrite) -> None:
    assert rewrite({**STUCK, "definition": "   \n\t "}) is None


def test_a_null_statement_beside_a_definition_is_repaired(rewrite) -> None:
    """`statement: null` is "not stated": it fails `_usable`, so the source fills it —
    the same path every other unusable statement now takes (the test below)."""
    out = rewrite({**STUCK, "statement": None})

    assert out["statement"] == STUCK["definition"]


@pytest.mark.parametrize("shadow", ["", "   \n\t ", 123, ["x"], {"k": "v"}, True])
def test_a_present_but_unusable_statement_no_longer_shadows_the_definition(
    rewrite, shadow
) -> None:
    """FINDING F2, now FIXED: one `_usable` predicate (`isinstance(str) and .strip()`)
    decides BOTH "is this already repaired" and "does this value survive the copy", so a
    present-but-unusable `statement` — `""` is exactly what an "emit null" instruction
    routinely produces, the same empty-string-vs-null split that bit `binds_to` in
    `validation.py::_validate_roles` — falls through and the `definition` beside it is
    applied instead of being shadowed by the junk value."""
    out = rewrite({**STUCK, "statement": shadow})

    assert out["statement"] == STUCK["definition"]


def test_a_payload_with_nothing_usable_yields_no_repair(rewrite) -> None:
    assert rewrite({"intent": "define active employee", "scope": "employee"}) is None


def test_an_empty_payload_yields_no_repair(rewrite) -> None:
    assert rewrite({}) is None


@pytest.mark.parametrize("bad", [123, ["a statement"], {"text": "a statement"}, True, None])
def test_a_non_string_definition_is_never_coerced_into_a_statement(rewrite, bad) -> None:
    """`str(["a"])` is a Python repr, and this text is about to be the WHOLE of a landed
    global-knowledge chunk. A coercion here lands a repr in the recallable index under a
    human approve."""
    out = rewrite({**STUCK, "definition": bad})

    assert out is None


# --- the payloads actually stuck in the live store ---------------------------------


@pytest.mark.parametrize("key", ["knowledge", "knowledge_update"])
def test_the_live_knowledge_keyed_payloads_are_repaired(rewrite, key) -> None:
    """FINDING F1, now FIXED: `knowledge` and `knowledge_update` are statement sources,
    so the two payload shapes actually sitting in the live store — `{intent, knowledge}`
    and `{intent, knowledge_update}` — are repaired rather than tallied into a bucket
    whose label ("already has a statement") was false on both halves. The candidate
    stays `in_review` for a human re-review before approval — that re-review is the
    attestation that makes deriving a statement from a delta-flavoured key safe."""
    out = rewrite({"intent": "define active employee", key: "a fact worth keeping"})

    assert out == {"statement": "a fact worth keeping"}


def test_the_statement_source_table_covers_the_live_store_keys(rewrite, script) -> None:
    """Pinned against the table itself, so removing a source is a failing test to
    re-decide, not a silent regression back to the misreport."""
    assert script._STATEMENT_SOURCES == ("definition", "knowledge", "knowledge_update")


def test_statement_source_precedence_is_definition_first(rewrite) -> None:
    """Several sources present: FIRST USABLE WINS, in the documented order —
    `definition` reads most like a settled fact, `knowledge_update` most like a delta."""
    out = rewrite(
        {
            "definition": "the settled fact",
            "knowledge": "the fact, stated another way",
            "knowledge_update": "a delta on the fact",
        }
    )

    assert out == {"statement": "the settled fact"}

    skipping_definition = rewrite(
        {
            "definition": "   ",
            "knowledge": "the fact, stated another way",
            "knowledge_update": "a delta on the fact",
        }
    )
    assert skipping_definition == {"statement": "the fact, stated another way"}


# --- shapes a stored envelope can carry that model output cannot -------------------


def test_a_list_shaped_related_terms_is_carried_through_verbatim(rewrite) -> None:
    """The script renames KEYS; it does not repair values. Anything else would put it in
    the business of authoring corpus text."""
    out = rewrite({**STUCK, "related_terms": ["active", "headcount"]})

    assert out["related_terms"] == ["active", "headcount"]


def test_a_malformed_related_terms_is_carried_through_rather_than_dropped(rewrite) -> None:
    """A string-shaped `related_terms` is off-contract and intake now rejects it — but
    the repair must not silently DELETE it either, because the delete would be a content
    edit nobody reviewed. It rides through, and the reviewer sees it on the card.

    (Consequence worth stating: a stored payload carrying one is repaired into something
    the intake reader would still refuse. Landing tolerates it — the mapper's
    `_collect_text` walks a bare string — so the approve succeeds; the terms just land as
    one term rather than two.)"""
    out = rewrite({**STUCK, "related_terms": "active, headcount"})

    assert out["related_terms"] == "active, headcount"


def test_a_falsy_but_present_contract_key_is_dropped_not_kept_as_a_false_value(
    rewrite,
) -> None:
    """`payload.get(key) is not None` keeps `""`/`[]`/`{}`. That is deliberate for the
    non-statement fields (an empty `structured` is information the reviewer sent), and it
    is pinned here because the alternative — truthiness — would silently normalize them
    away."""
    out = rewrite({**STUCK, "structured": {}, "related_terms": []})

    assert out["structured"] == {}
    assert out["related_terms"] == []


def test_a_null_valued_contract_key_is_dropped(rewrite) -> None:
    """`None` is "not stated", and carrying it forward would write an explicit null into
    a payload whose readers all treat absent and null alike anyway."""
    out = rewrite({**STUCK, "structured": None})

    assert "structured" not in out


def test_a_unicode_statement_survives_the_rewrite_unchanged(rewrite) -> None:
    text = "un employé actif n'a pas de date de fin — 従業員 ✅"
    out = rewrite({**STUCK, "definition": text})

    assert out["statement"] == text


def test_a_very_long_statement_is_never_truncated(rewrite) -> None:
    """This is the artifact's whole text, not a log line: a repair that silently clipped
    it would land a half-sentence under a human approve."""
    text = "an active employee is one with no termination date. " * 400
    out = rewrite({**STUCK, "definition": text})

    assert out["statement"] == text
