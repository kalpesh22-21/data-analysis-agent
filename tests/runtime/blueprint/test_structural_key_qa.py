"""QA adversarial suite for the cross-tier `structural_key` (PriorArtIndex Slice 1).

`test_structural_key.py` pins the INTENDED behavior. This file attacks the edges the
builder's suite does not reach: Unicode normalization forms, case-fold collisions,
non-string grain entries, boundary grain shapes, scale, and cross-PROCESS determinism.

Two conventions used throughout:

  * A test named `..._is_a_known_limitation` asserts the CURRENT, divergent behavior on
    purpose. It is a tripwire, not an endorsement — if a later slice tightens the key,
    the test FAILS and the author is forced to decide deliberately rather than discover
    the change in production.
  * Everything else asserts behavior the key must not lose.
"""

from __future__ import annotations

import subprocess
import sys
import unicodedata

import pytest
import sqlglot.errors

from data_agent.runtime.blueprint.structural_key import (
    canonical_ast_norm_one,
    normalize_structural_grain,
    structural_key,
    structural_key_from_templates,
)

_NORM = "SELECT 1"


# --- boundary grain shapes ---------------------------------------------------


@pytest.mark.parametrize(
    "grain",
    [
        None,
        [],
        (),
        {},
        {"columns": []},
        {"columns": None},
        {"columns": (), "verifiable": True},
        {"verifiable": False},
        "month",  # a bare string is NOT a one-column grain
        "",
        0,
        False,
        {"columns": "month"},  # a string is not a column LIST
    ],
    ids=[
        "none",
        "empty-list",
        "empty-tuple",
        "empty-dict",
        "columns-empty-list",
        "columns-none",
        "columns-empty-tuple",
        "verifiable-only",
        "bare-string",
        "empty-string",
        "int-zero",
        "false",
        "columns-is-a-string",
    ],
)
def test_every_empty_or_unrecognized_grain_shape_collapses_to_one_key(grain) -> None:
    """`None` / `[]` / `{}` / `{"columns": []}` and every unrecognized shape must all
    mean "no grain" and mint the IDENTICAL key. If any of these split, a canon blueprint
    that simply omits `result_grain` stops matching a learning candidate whose S4 stamp
    is `{"columns": [], "verifiable": false}` — the commonest shape in the frozen S4
    fixture."""
    assert normalize_structural_grain(grain) == []
    assert structural_key(grain, _NORM) == structural_key(None, _NORM)


def test_a_bare_string_grain_is_not_silently_split_into_characters() -> None:
    """`str` is a `Sequence`; a naive isinstance check would turn `"month"` into
    `["h", "m", "n", "o", "t"]`. The guard is deliberate — keep it."""
    assert normalize_structural_grain("month") == []


# --- Unicode ------------------------------------------------------------------


def test_unicode_normalization_forms_agree() -> None:
    """FIXED (was a QA-flagged limitation): the grain fold now NFC-normalizes.

    A precomposed `é` (U+00E9, NFC) and a decomposed `e` + COMBINING ACUTE (U+0065
    U+0301, NFD) are the same grapheme and render identically in a YAML file, but they
    are different `str` values. The two authoring paths can genuinely differ here: a
    hand-typed canon YAML may carry NFD (macOS filesystem/IME input) while a grain label
    read back from ClickHouse arrives NFC. They must mint ONE key.

    NFC is applied AFTER the lowercase fold, because `.lower()` on an NFD string leaves
    it decomposed — normalizing first and folding second would silently reintroduce the
    split.
    """
    nfc = unicodedata.normalize("NFC", "Département")
    nfd = unicodedata.normalize("NFD", "Département")
    assert nfc != nfd  # different code points, identical rendering
    assert normalize_structural_grain([nfc]) == normalize_structural_grain([nfd])
    assert structural_key([nfc], _NORM) == structural_key([nfd], _NORM)
    # ...and the fold still runs on top of the normalization.
    assert structural_key([unicodedata.normalize("NFD", "DÉPARTEMENT")], _NORM) == structural_key(
        [nfc], _NORM
    )


def test_mixed_script_homoglyphs_mint_different_keys() -> None:
    """A Cyrillic `а` (U+0430) inside an otherwise-Latin identifier must NOT collide
    with the Latin spelling. This is the CORRECT behavior (they are different columns);
    it is asserted so a future "normalize harder" change cannot quietly introduce a
    homoglyph collision in a security-adjacent identity key."""
    latin = "department"
    cyrillic = "depаrtment"
    assert latin != cyrillic
    assert structural_key([latin], _NORM) != structural_key([cyrillic], _NORM)


def test_non_ascii_grain_labels_round_trip_through_the_digest() -> None:
    """`canonical_json` uses `ensure_ascii=False`, so non-ASCII grain labels are hashed
    as UTF-8 bytes rather than `\\uXXXX` escapes. The digest must still be a stable,
    well-formed key (and must not raise on encoding)."""
    keys = set()
    for label in ["Département", "部門", "отдел", "قسم", "🏢"]:
        key = structural_key([label], _NORM)
        assert key.startswith("sha256:")
        assert len(key) == len("sha256:") + 64
        # The `.lower()` fold is a no-op for caseless scripts, so an already-lowercase
        # non-ASCII label is stable under it.
        assert structural_key([label], _NORM) == key
        keys.add(key)
    assert len(keys) == 5  # distinct labels stay distinct


def test_case_fold_uses_lower_not_casefold_is_a_known_limitation() -> None:
    """KNOWN LIMITATION: the fold is `str.lower()`, not `str.casefold()`.

    `casefold()` is the Unicode-correct aggressive fold (`ß` -> `ss`); `lower()` leaves
    `ß` alone. So `STRASSE` and `STRAßE` mint different keys. Irrelevant for the ASCII
    HR corpus and arguably the safer default (casefold is lossier), but pinned so the
    choice is visible if a non-ASCII corpus ever lands.
    """
    assert "STRAßE".lower() != "STRASSE".lower()
    assert "STRAßE".casefold() == "STRASSE".casefold()
    assert structural_key(["STRASSE"], _NORM) != structural_key(["STRAßE"], _NORM)


# --- case-fold collisions and duplicates --------------------------------------


def test_columns_are_deduplicated_after_the_case_fold() -> None:
    """FIXED (was a QA-flagged limitation): the grain is `sorted(set(...))` AFTER the
    fold, matching the QA-Q5 treatment `compute_canonical_key` already applies to
    `uses_rules`.

    Order matters: the FOLD is what creates the duplicate (pre-fold `Department` and
    `department` are two distinct strings), so deduplicating before folding would leave
    both. A real grain can never legitimately carry two columns differing only in case —
    they would name the same output column."""
    assert normalize_structural_grain(["Department", "department"]) == ["department"]
    assert structural_key(["Department", "department"], _NORM) == structural_key(
        ["department"], _NORM
    )


def test_exact_duplicate_columns_are_also_collapsed() -> None:
    """The same rule without any case involvement — a plainly repeated column."""
    assert normalize_structural_grain(["a", "a"]) == ["a"]
    assert structural_key(["a", "a"], _NORM) == structural_key(["a"], _NORM)


# --- empty-string and whitespace columns --------------------------------------


def test_an_empty_string_column_is_dropped() -> None:
    """FIXED (was a QA-flagged limitation): empty and whitespace-only entries are
    dropped. Previously `["department", ""]` normalized to `["", "department"]` — the
    empty entry sorts FIRST and changed the digest, so a canon YAML with a stray trailing
    `-` in a block list silently minted a key no learning candidate could ever match."""
    assert normalize_structural_grain(["department", ""]) == ["department"]
    assert normalize_structural_grain(["department", "   "]) == ["department"]
    assert structural_key(["department", ""], _NORM) == structural_key(["department"], _NORM)
    # An all-empty grain therefore collapses to "no grain", which is the right reading:
    # the author declared nothing usable.
    assert structural_key([""], _NORM) == structural_key([], _NORM)


# --- non-string grain entries -------------------------------------------------


def test_a_yaml_null_grain_entry_makes_the_whole_grain_unusable() -> None:
    """FIXED (was a QA-flagged sharp edge): `normalize_structural_grain` no longer
    coerces with `str(col)`. A YAML null entry (`result_grain: [~]`, or a dangling `-`)
    used to become the literal grain column `"none"` and COLLIDE with a real column
    named `none`/`None` — a confident WRONG key, the worst possible outcome for an
    identity index.

    A non-string member now makes the WHOLE grain unusable (`None`), which propagates to
    NO key. Dropping just the bad member was rejected: it would still mint a confident
    key, from a guess at what the author meant."""
    assert normalize_structural_grain([None]) is None
    assert structural_key([None], _NORM) == ""
    assert structural_key([None], _NORM) != structural_key(["none"], _NORM)
    # One bad member poisons the whole grain, not just itself.
    assert normalize_structural_grain(["department", None]) is None
    assert structural_key(["department", None], _NORM) == ""


def test_numeric_grain_entries_make_the_grain_unusable() -> None:
    """Same rule, applied to an unquoted YAML scalar (`result_grain: [2024]`). Previously
    `str(2024)` silently made it the column `"2024"`. It is now unusable rather than
    guessed — a grain member that is not a string is an authoring bug."""
    assert normalize_structural_grain([1, 2]) is None
    assert structural_key([2024], _NORM) == ""
    assert structural_key([2024], _NORM) != structural_key(["2024"], _NORM)


def test_a_structured_grain_entry_yields_no_key_and_does_not_raise() -> None:
    """A nested dict/list smuggled into `columns` must not blow up the seeder. It no
    longer stringifies into a plausible-looking column either — it yields NO key."""
    assert structural_key([{"a": 1}], _NORM) == ""
    assert structural_key([["a"]], _NORM) == ""


def test_an_unusable_grain_is_distinguishable_from_an_absent_one() -> None:
    """The load-bearing consequence of the container/member split: a MALFORMED grain
    yields no key at all, while an ABSENT or empty grain is a legitimate shape
    (`bp-hires-projection` ships `result_grain: []`) that keys normally. Collapsing the
    two would let a broken blueprint masquerade as a grainless one."""
    assert structural_key([], _NORM).startswith("sha256:")
    assert structural_key(None, _NORM).startswith("sha256:")
    assert structural_key([None], _NORM) == ""


# --- scale --------------------------------------------------------------------


def test_a_very_long_identifier_is_handled() -> None:
    long_col = "a" * 10_000
    key = structural_key([long_col], _NORM)
    assert key.startswith("sha256:")
    assert len(key) == len("sha256:") + 64
    # Still order/case normalized at that length.
    assert structural_key([long_col.upper()], _NORM) == key


def test_a_grain_with_hundreds_of_columns_is_order_independent() -> None:
    cols = [f"col_{i:04d}" for i in range(500)]
    shuffled = list(reversed(cols))
    assert structural_key(cols, _NORM) == structural_key(shuffled, _NORM)
    assert structural_key({"columns": shuffled, "verifiable": True}, _NORM) == structural_key(
        cols, _NORM
    )


def test_the_grain_sort_is_fold_then_sort_not_sort_then_fold() -> None:
    """ASCII orders every capital before every lowercase, so sorting BEFORE folding and
    folding BEFORE sorting give different lists. Only fold-then-sort is stable against a
    canon YAML that capitalizes some labels and not others."""
    mixed = ["month", "Department", "YEAR", "region"]
    assert normalize_structural_grain(mixed) == ["department", "month", "region", "year"]
    # Every capitalization permutation of the same columns is one key.
    assert structural_key(mixed, _NORM) == structural_key(
        ["MONTH", "department", "year", "REGION"], _NORM
    )


# --- cross-PROCESS determinism ------------------------------------------------

_SUBPROCESS_SNIPPET = """
import sys
sys.path.insert(0, {src!r})
from data_agent.runtime.blueprint.structural_key import structural_key_from_templates
tpl = (
    "SELECT e.department_name AS department, SUM(p.amount) AS earnings "
    "FROM db.payroll AS p JOIN db.employee AS e ON e.employee_code = p.employee_code "
    "WHERE p.register_type = 'EARN' AND (p.a = 1 OR p.b = 2) "
    "AND e.department_name = {{department}} GROUP BY e.department_name"
)
print(structural_key_from_templates(["Department", "Month", "R\\u00e9gion"], tpl))
"""


@pytest.mark.parametrize("hashseed", ["0", "1", "12345", "999999"])
def test_the_digest_is_identical_across_separate_python_processes(hashseed, tmp_path) -> None:
    """Determinism must hold across PROCESSES, not just across calls in one interpreter.

    Two calls in one process share `str.__hash__`'s per-process salt, so an
    order-dependence introduced by set/dict iteration anywhere in the chain (the grain
    normalization, `canonical_json`, or sqlglot's own internals) would be INVISIBLE to a
    same-process assertion. `PYTHONHASHSEED` is varied to force different salts.

    This is the property that lets a key minted by the offline learning worker be looked
    up by the online hydrator in a different process on a different host.
    """
    src = str(__import__("pathlib").Path(__file__).resolve().parents[2] / "src")
    script = tmp_path / "mint.py"
    script.write_text(_SUBPROCESS_SNIPPET.format(src=src))

    def run(seed: str) -> str:
        env = {**__import__("os").environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, env=env, check=True
        )
        return out.stdout.strip()

    baseline = run("0")
    assert baseline.startswith("sha256:")
    assert run(hashseed) == baseline


# --- fail-soft ----------------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "SELECT FROM WHERE ((",
        "not sql at all",
        "",
        "   ",
        "{{{",
        "SELECT * FROM t WHERE x = {a",  # unclosed brace -> unclosed placeholder
        "SELECT `unclosed",  # TokenError, not ParseError
        "SELECT a FROM t WHERE x = 'unterminated",
    ],
)
def test_an_unparseable_template_yields_no_key_and_never_raises(template) -> None:
    """The seeder helper must swallow EVERY sqlglot failure mode, not just `ParseError`
    — `SELECT \\`unclosed` raises `TokenError`, a different class. A raise here would
    abort corpus loading for the whole corpus."""
    assert structural_key_from_templates(["month"], template) == ""


def test_the_raw_normalizer_still_raises_so_callers_choose_the_policy() -> None:
    """`canonical_ast_norm_one` is the strict primitive; only
    `structural_key_from_templates` is fail-soft. Keeping the strict form is what lets
    the S4 producer surface a genuine rewrite bug instead of silently dropping a key."""
    with pytest.raises(sqlglot.errors.ParseError):
        canonical_ast_norm_one("SELECT FROM WHERE ((")
    with pytest.raises(sqlglot.errors.TokenError):
        canonical_ast_norm_one("SELECT `unclosed")


def test_a_composite_with_one_unparseable_node_yields_no_key_at_all() -> None:
    """A composite whose nodes are joined must be all-or-nothing: a partial join over
    the surviving nodes would mint a CONFIDENT key for a DIFFERENT blueprint (one
    missing a stage), which is worse than no key."""
    good = "SELECT AVG(x) AS a FROM db.t"
    bad = "SELECT FROM WHERE (("
    assert structural_key_from_templates(["d"], None, [(0, good), (1, bad)]) == ""
    # ...and the good-only composite is a real, different key (proving the join ran).
    assert structural_key_from_templates(["d"], None, [(0, good)]).startswith("sha256:")


def test_an_empty_node_list_composite_yields_no_key() -> None:
    """A composite seed with no usable node templates has nothing to hash; it must fall
    to the no-key path rather than hash the empty join."""
    assert structural_key_from_templates(["d"], None, []) == ""
    assert structural_key_from_templates(["d"], None, [(0, "   ")]) == ""


def test_no_key_is_the_empty_string_never_none_at_this_layer() -> None:
    """The function-level contract is `""`; converting to an ABSENT graph property is
    the CALLER's job (`_dag_properties` maps `"" -> None`). Pinned because a caller that
    stored the empty string would make a naive `MATCH (b {structural_key: $k})` match
    every keyless blueprint as false prior art."""
    assert structural_key_from_templates(["m"], "SELECT FROM WHERE ((") == ""
    assert structural_key(["m"], "") == ""
    assert structural_key(["m"], None) == ""
    assert structural_key_from_templates(["m"], None, []) == ""
