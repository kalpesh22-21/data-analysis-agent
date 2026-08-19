"""corpus_loader — the writer ALWAYS stamps `source`/`verified` (PriorArt Slice 2).

**The hole.** `_UPSERT_BLUEPRINT` does `SET b.source = $source`, and neo4j REMOVES a
property that is set to null. So an MCP export entry carrying an explicit
`"source": null` produced a SOURCELESS node. Recall never noticed — its trust gate is
bare `= 'mcp'` equality and fails closed — but the prior-art read deliberately DROPS
that gate, so it sees such nodes. (A sourceless `:Blueprint` was found in the live dev
graph while building this slice.)

**The fix direction.** Nothing is in production, so the WRITER is made to always stamp
rather than teaching every reader to coalesce. After this, a sourceless or unstamped
node can only come from a hand edit or a foreign writer — which is exactly what
`priorart.models.TIER_UNSOURCED` exists to surface.

**Why the fallback is the trusted default and not fail-closed.** Everything this loader
projects came from the MCP canon export over the service-key-authenticated route; trust
rests on the TRANSPORT, and `source` is provenance metadata the export echoes back. The
dataclass already treats "absent" as canon for that reason — this only extends the same
rule to "present but the wrong type", which is otherwise a silent property-DELETING
write. Failing closed instead would silently drop a real canon blueprint out of recall.

Slug: corpus-loader-always-stamps-the-trust-partition.
"""

from __future__ import annotations

import pytest

from data_agent.corpus.seeds import (
    _seed_from_entry,
    _seeds_from_entries,
)
from data_agent.runtime.retrieval.corpus_loader import schema_statements

_MIN_BLUEPRINT = {
    "intent": "total earnings by department",
    "slots_summary": "department, year",
    "uses": ["dbpcm_warehouse.payroll.Amount"],
}
_MIN_KNOWLEDGE = {
    "title": "overtime",
    "text": "overtime is paid at 1.5x",
    "doc_id": "doc-overtime",
}


@pytest.mark.parametrize(
    "bad_source",
    [None, "", "   ", 0, False, 123, ["mcp"], {"tier": "mcp"}],
)
def test_a_malformed_source_falls_back_to_the_trusted_default(bad_source):
    seed = _seed_from_entry("bp-x", {**_MIN_BLUEPRINT, "source": bad_source}, kind="blueprint")
    assert seed.source == "mcp"
    assert isinstance(seed.source, str) and seed.source.strip()


@pytest.mark.parametrize("bad_verified", [None, "true", "false", 1, 0, [], {}])
def test_a_malformed_verified_is_stamped_false_not_defaulted_to_true(bad_verified):
    """The `source` fallback argument does NOT transfer to `verified`, and the asymmetry
    is deliberate.

    For `source`, defaulting to `mcp` is trust-neutral: an exporter emitting `null` is
    indistinguishable from one omitting the key, and anyone controlling that value could
    have written `"mcp"` — trust rests on the service-key transport.

    For `verified` it is NOT neutral. A present `"verified": "false"` plausibly MEANT
    false, and defaulting would silently INVERT it to true. `False` is a legal, honest
    value ("landed but nobody has verified it"), costs nothing today (recall does not
    read `verified`), and stays correct when `recheck_verified_only` starts reading it.

    A stored non-bool also reads as `verified=None` to the prior-art mapper ("the node
    does not say") while reading TRUTHY to anything doing a bare `if node.verified` —
    two readers disagreeing about one property is worse than either answer."""
    seed = _seed_from_entry("bp-x", {**_MIN_BLUEPRINT, "verified": bad_verified}, kind="blueprint")
    assert seed.verified is False


def test_an_absent_verified_still_defaults_to_true():
    """Only a MALFORMED value gets the untrusting stamp. An absent key means the export
    simply does not carry the field, which the dataclass has always read as trusted
    canon — changing that would flip every existing fixture blueprint to unverified."""
    seed = _seed_from_entry("bp-x", dict(_MIN_BLUEPRINT), kind="blueprint")
    assert seed.verified is True


def test_a_well_formed_stamp_still_comes_through_verbatim():
    """The fallback must not swallow a REAL value — including the learning-tier one, which
    is how a promoted node would be re-exported."""
    seed = _seed_from_entry(
        "bp-x", {**_MIN_BLUEPRINT, "source": "learning", "verified": False}, kind="blueprint"
    )
    assert seed.source == "learning"
    assert seed.verified is False


def test_the_knowledge_side_is_stamped_identically():
    """The `:KnowledgeChunk` upsert has the same `SET k.source = $source` null-deleting
    behaviour and the same fail-closed recall gate, so it needs the same guarantee."""
    seed = _seed_from_entry(
        "kn-x", {**_MIN_KNOWLEDGE, "source": None, "verified": "yes"}, kind="knowledge"
    )
    assert seed.source == "mcp"
    assert seed.verified is False


def test_every_seed_from_a_hostile_export_carries_a_usable_trust_stamp():
    """The INVARIANT, stated over the whole entry-set path rather than one entry: after
    this loader, `source` is a non-empty `str` and `verified` is a `bool` on every seed —
    which is what makes an unsourced node in the graph a genuine anomaly signal."""
    raw = {
        "bp-null-source": {**_MIN_BLUEPRINT, "source": None},
        "bp-int-source": {**_MIN_BLUEPRINT, "source": 5},
        "bp-str-verified": {**_MIN_BLUEPRINT, "verified": "true"},
        "bp-clean": {**_MIN_BLUEPRINT, "source": "mcp", "verified": True},
        "bp-learning": {**_MIN_BLUEPRINT, "source": "learning", "verified": False},
    }
    seeds = _seeds_from_entries(raw, kind="blueprint")
    assert len(seeds) == len(raw)
    for seed in seeds:
        assert isinstance(seed.source, str)
        assert seed.source.strip()
        assert isinstance(seed.verified, bool)


def test_unknown_export_keys_are_still_dropped():
    """The whitelist behaviour the trust-stamp guard sits inside must be unchanged — a
    separate repo's canon may grow fields, and spreading them blindly would TypeError."""
    seed = _seed_from_entry(
        "bp-x", {**_MIN_BLUEPRINT, "some_future_field": 1}, kind="blueprint"
    )
    assert seed.id == "bp-x"


# --- the structural_key lookup index ------------------------------------------


def test_the_schema_creates_a_range_index_for_the_structural_key():
    """`PriorArtIndex.get_by_structural_key` matches a candidate against every tier by
    this key. Without a RANGE index the MATCH is a `NodeByLabelScan` over every
    :Blueprint — cheap at 12 nodes and quietly linear at 12,000. (EXPLAIN against the
    live graph: `NodeIndexSeek` with the index, `NodeByLabelScan` without.)"""
    statements = schema_statements(768)
    index_ddl = [s for s in statements if "blueprint_structural_key" in s]
    assert len(index_ddl) == 1
    ddl = index_ddl[0]
    assert "IF NOT EXISTS" in ddl  # every schema statement must be re-runnable
    assert "FOR (b:Blueprint) ON (b.structural_key)" in ddl


def test_the_structural_key_index_is_not_unique():
    """Two tiers legitimately carry the SAME structural key — a canon blueprint and the
    learning node that re-derived it — which is precisely the collision the key exists to
    DETECT. A UNIQUE constraint would refuse to land the very thing we want to find."""
    ddl = next(s for s in schema_statements(768) if "blueprint_structural_key" in s)
    assert "CONSTRAINT" not in ddl
    assert "UNIQUE" not in ddl
