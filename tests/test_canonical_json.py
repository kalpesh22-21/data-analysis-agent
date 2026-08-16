"""Byte-level freeze on `data_agent.canonical.canonical_json`.

The D48 hard key, the loose `structural_key`, and the D96 `content_hash` are all
SHA-256 digests of this serializer's output, and every one of them is PERSISTED
(Couchbase corpus bucket, Neo4j nodes, learning-queue idempotency records) rather
than re-derived. So the bytes are a wire format: change `sort_keys`, `separators`,
or `ensure_ascii` and every stored digest is orphaned — landed blueprints become
unreachable, the loop re-proposes the whole corpus, and enqueue/consume stops
being idempotent.

These digests are LITERALS on purpose. Do not regenerate them to make the file
green; a failure here means the change under test re-keys production data.
"""

from __future__ import annotations

import hashlib

import pytest

from data_agent.canonical import canonical_json

# Each case exercises one property of the convention that a plausible "cleanup"
# would break: key sorting, `ensure_ascii=False` (raw UTF-8, not \uXXXX escapes),
# and the compact `(",", ":")` separators over numbers/containers.
FROZEN_CASES: list[tuple[str, object, str, str]] = [
    (
        "sorted_keys_and_preserved_list_order",
        {"z": 1, "a": {"nested": [3, 2, 1], "b": None}, "m": "plain"},
        '{"a":{"b":null,"nested":[3,2,1]},"m":"plain","z":1}',
        "91229c0fe0ab82ce7fdf84be234b361ffc570fe5eacd7a04e87d69c2ef59837e",
    ),
    (
        "unicode_is_emitted_raw_not_escaped",
        {"unicode": "Département — naïve ß 日本語", "emoji": "x", "sym": "é"},
        '{"emoji":"x","sym":"é","unicode":"Département — naïve ß 日本語"}',
        "5b1a1d8dd4a2798a3487727eb05a9216c547c8fc8e9f8c13f97a9f8cff7e0c28",
    ),
    (
        "numbers_bools_and_empty_containers",
        {
            "numbers": [0, -1, 1.5, 1e20, 2.0],
            "bools": [True, False],
            "null": None,
            "empty": {},
            "list": [[], {}, ""],
        },
        '{"bools":[true,false],"empty":{},"list":[[],{},""],"null":null,'
        '"numbers":[0,-1,1.5,1e+20,2.0]}',
        "5db674731c4cbba54036376dd0036aaa419336155df825907af0dc3db1e32095",
    ),
]


@pytest.mark.parametrize(
    ("payload", "expected_text", "expected_digest"),
    [(payload, text, digest) for _, payload, text, digest in FROZEN_CASES],
    ids=[name for name, _, _, _ in FROZEN_CASES],
)
def test_canonical_json_bytes_are_frozen(
    payload: object, expected_text: str, expected_digest: str
) -> None:
    rendered = canonical_json(payload)
    assert rendered == expected_text
    assert hashlib.sha256(rendered.encode("utf-8")).hexdigest() == expected_digest


def test_key_insertion_order_does_not_change_the_digest() -> None:
    """`sort_keys` is what makes the digest a function of the CONTENT, not of how
    the producer happened to build the dict — the D48 key hashes `resolves`
    straight out of an LLM-shaped payload."""
    forward = {"a": 1, "b": 2, "c": 3}
    reverse = {"c": 3, "b": 2, "a": 1}
    assert canonical_json(forward) == canonical_json(reverse)


def test_the_three_key_modules_share_one_serializer() -> None:
    """The copies this module replaced were byte-identical BY REVIEW, which is the
    guarantee that failed. Identity, not equality: all three names must resolve to
    the same function object."""
    from data_agent.learning import models as learning_models
    from data_agent.learning.dedup import canonical_key as dedup_key
    from data_agent.runtime.blueprint import structural_key

    assert dedup_key._canonical_json is canonical_json
    assert learning_models._canonical_json is canonical_json
    assert structural_key.canonical_json is canonical_json
