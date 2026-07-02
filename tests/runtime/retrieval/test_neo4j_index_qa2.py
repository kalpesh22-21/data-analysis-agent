"""QA2 adversarial Layer-1 tests for Neo4jVectorIndex (neo4j-corpus-design §2).

Attacks the recall/mapping contracts using the `_run` monkeypatch seam and a
fake async driver — NO live neo4j. Complements (does not duplicate)
`test_neo4j_index.py`:

  * Candidate-mapping malformed-record matrix (missing keys, non-str `uses`,
    `uses` as a bare string, NaN/None score, empty id, extra fields).
  * Never-raises across every failure flavor (ServiceUnavailable, TimeoutError,
    TypeError) AND the deliberate CancelledError-propagation carve-out.
  * The model-mismatch probe raising during the empty path degrades silently.
  * `close()` lifecycle: double-close, recall-after-close, close-never-opened.
  * Determinism: recall is a pass-through of `_run` row order (no client sort).

Several tests PIN legal-but-questionable behavior (frozenset of non-str, a bare
string exploding into a char-set, a single bad row nuking the whole batch) so a
future change to those surfaces is a conscious one — see the module report.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

import pytest
from neo4j.exceptions import ServiceUnavailable

from data_agent.runtime.retrieval.vector_index import (
    Neo4jVectorIndex,
    map_blueprint_record,
    map_knowledge_record,
)

# --------------------------------------------------------------------------
# Fakes: an index whose `_run` is swapped, and a real-ish driver for close()
# --------------------------------------------------------------------------


def _index(run: Any, *, driver: Any | None = None) -> Neo4jVectorIndex:
    index = Neo4jVectorIndex(
        url="bolt://unused:7687",
        auth=("u", "p"),
        expected_model="all-mpnet-base-v2",
        driver=driver if driver is not None else object(),
    )
    if run is not None:
        index._run = run  # type: ignore[method-assign, assignment]
    return index


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def data(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeSession:
    def __init__(self, driver: _FakeDriver) -> None:
        self._driver = driver

    async def __aenter__(self) -> _FakeSession:
        if self._driver.closed:
            raise RuntimeError("session opened on a closed driver")
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def run(
        self, query: str, parameters: dict[str, Any] | None = None, **_: Any
    ) -> _FakeResult:
        # A count probe returns a count row; a vector query returns seeded rows.
        if "count(" in query:
            return _FakeResult([{"c": len(self._driver.rows)}])
        return _FakeResult(list(self._driver.rows))


class _FakeDriver:
    """Enough of AsyncDriver for the REAL `_run`/`close` to be exercised."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.closed = False
        self.close_calls = 0

    def session(self, *, database: str) -> _FakeSession:
        return _FakeSession(self)

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def _bp_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": "bp-1",
        "text": "intent text",
        "slots_summary": "dept",
        "uses": ["dbpcm_warehouse.payroll.Amount"],
        "score": 0.9,
    }
    row.update(over)
    return row


async def _recall_bp(rows: list[dict[str, Any]]):
    async def fake_run(query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return rows

    return await _index(fake_run).recall(query_vector=[0.0] * 768, kind="blueprint", k=30)


# --------------------------------------------------------------------------
# Mapping — malformed records at the MAPPER level (pin exact behavior)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["id", "text", "uses", "score"])
def test_map_blueprint_missing_required_key_raises_keyerror(missing: str) -> None:
    # id/text/uses/score are read with `record[...]` (NOT .get) -> KeyError.
    # This is the raw mapper; recall() wraps it and degrades (see below).
    record = _bp_row()
    del record[missing]
    with pytest.raises(KeyError):
        map_blueprint_record(record)


@pytest.mark.parametrize("missing", ["id", "text", "score"])
def test_map_knowledge_missing_required_key_raises_keyerror(missing: str) -> None:
    record = {
        "id": "kn-1",
        "text": "chunk",
        "title": "t",
        "doc_id": "d",
        "score": 0.5,
    }
    del record[missing]
    with pytest.raises(KeyError):
        map_knowledge_record(record)


def test_map_blueprint_slots_summary_optional() -> None:
    # slots_summary uses .get -> missing degrades to "" (payload never KeyErrors).
    record = _bp_row()
    del record["slots_summary"]
    candidate = map_blueprint_record(record)
    assert candidate.payload["slots_summary"] == ""


def test_map_knowledge_optional_fields_default_to_none() -> None:
    record = {"id": "kn", "text": "c", "score": 0.1}
    candidate = map_knowledge_record(record)
    assert candidate.payload == {"title": None, "chunk": "c", "doc_id": None}


def test_map_blueprint_extra_unknown_fields_ignored() -> None:
    candidate = map_blueprint_record(_bp_row(mystery=123, drift_status="suspect"))
    assert candidate.id == "bp-1"
    assert "mystery" not in candidate.payload


def test_map_blueprint_empty_string_id_is_preserved_not_rejected() -> None:
    # PIN: an empty id is accepted verbatim (no validation) — downstream dedup
    # would collapse all empty-id candidates. Legal-but-questionable.
    candidate = map_blueprint_record(_bp_row(id=""))
    assert candidate.id == ""


def test_map_blueprint_score_none_and_nan_preserved() -> None:
    assert map_blueprint_record(_bp_row(score=None)).score is None
    nan_candidate = map_blueprint_record(_bp_row(score=float("nan")))
    assert nan_candidate.score is not None and math.isnan(nan_candidate.score)


# --- uses that is not a clean list[str] is coerced to None (B1 fail-closed) ---


def test_map_blueprint_non_str_uses_is_coerced_to_none() -> None:
    # B1 / QA flag 1 FIX (was: pinned a frozenset of non-str pass-through). A
    # corrupted row with ints/None is UNDETERMINED -> uses=None, so the scope
    # filter DROPs it fail-closed rather than admitting a non-str frozenset.
    candidate = map_blueprint_record(_bp_row(uses=[1, None, "a.b.c"]))
    assert candidate.uses is None


def test_map_blueprint_uses_as_bare_string_is_coerced_to_none() -> None:
    # B1 FIX (was: pinned a char-set explosion). A bare string is not a list ->
    # UNDETERMINED -> None, so it can never char-explode into an unrelated set
    # that silently scope-drops the blueprint.
    candidate = map_blueprint_record(_bp_row(uses="dbpcm_warehouse.payroll.Amount"))
    assert candidate.uses is None


def test_map_blueprint_uses_nested_list_is_coerced_to_none() -> None:
    # B1 FIX (was: pinned a TypeError from frozenset() on an unhashable nested
    # list). A nested-list element is non-str -> UNDETERMINED -> None, no raise.
    candidate = map_blueprint_record(_bp_row(uses=[["a.b.c"]]))
    assert candidate.uses is None


# --------------------------------------------------------------------------
# recall() — malformed records degrade to [] (never raise)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["id", "text", "uses", "score"])
async def test_recall_degrades_to_empty_on_missing_key(missing: str) -> None:
    row = _bp_row()
    del row[missing]
    assert await _recall_bp([row]) == []


async def test_recall_null_uses_maps_to_none_not_error() -> None:
    # B1: null uses -> Candidate.uses=None (fail-closed). The row is still MAPPED
    # (kept in recall output); only the pipeline's scope filter drops it.
    got = await _recall_bp([_bp_row(uses=None)])
    assert got and got[0].uses is None


async def test_recall_one_bad_row_is_skipped_good_rows_kept() -> None:
    # QA flag 2 FIX (was: pinned all-or-nothing degrade). Mapping is per-row now:
    # a single malformed row is skipped (server-side warn) while the good
    # neighbours survive. Only a QUERY-level failure degrades the whole recall.
    good = _bp_row(id="good")
    bad = _bp_row(id="bad")
    del bad["id"]
    got = await _recall_bp([good, bad])
    assert [c.id for c in got] == ["good"]


# --------------------------------------------------------------------------
# Never-raises — every failure flavor degrades to [] EXCEPT CancelledError
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        ServiceUnavailable("neo4j unreachable"),
        TimeoutError(),  # asyncio.TimeoutError is aliased to builtin TimeoutError (3.11+)
        TypeError("malformed result shape"),
        RuntimeError("generic driver error"),
        ValueError("bad value"),
    ],
)
async def test_recall_returns_empty_on_every_exception_flavor(exc: Exception) -> None:
    async def boom(query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        raise exc

    assert await _index(boom).recall(query_vector=[0.0] * 768, kind="blueprint", k=30) == []


async def test_recall_propagates_cancelled_error() -> None:
    # DOCUMENTED CARVE-OUT: `except Exception` does NOT catch CancelledError
    # (a BaseException since 3.8), so cooperative cancellation propagates rather
    # than being swallowed as a spurious empty recall. This is correct.
    async def cancel(query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _index(cancel).recall(query_vector=[0.0] * 768, kind="blueprint", k=30)


async def test_recall_knowledge_degrades_to_empty_on_failure() -> None:
    async def boom(query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        raise ServiceUnavailable("down")

    assert await _index(boom).recall(query_vector=[0.0] * 768, kind="knowledge", k=30) == []


# --------------------------------------------------------------------------
# Model-mismatch probe robustness (design §2.3)
# --------------------------------------------------------------------------


async def test_mismatch_probe_raising_degrades_silently() -> None:
    # queryNodes returns [] (parity WHERE dropped all); the count() probe then
    # RAISES. `_flag_model_mismatch` swallows its own probe failure, so recall
    # still returns [] without crashing and without flagging (it can't know).
    async def fake_run(query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "queryNodes" in query:
            return []
        raise ServiceUnavailable("probe query failed")

    got = await _index(fake_run).recall(query_vector=[0.0] * 768, kind="blueprint", k=30)
    assert got == []


async def test_mismatch_probe_empty_rows_treated_as_count_zero() -> None:
    # Probe returns no rows at all -> count defaults to 0 -> no mismatch flag,
    # no crash (the `rows[0]` guard holds).
    async def fake_run(query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return []  # both the query and the probe return nothing

    assert await _index(fake_run).recall(query_vector=[0.0] * 768, kind="blueprint", k=30) == []


# --------------------------------------------------------------------------
# close() lifecycle (design §2.4) — exercised through the REAL _run/close
# --------------------------------------------------------------------------


async def test_close_never_opened_is_safe() -> None:
    driver = _FakeDriver()
    await _index(None, driver=driver).close()
    assert driver.close_calls == 1


async def test_double_close_does_not_raise() -> None:
    driver = _FakeDriver()
    index = _index(None, driver=driver)
    await index.close()
    await index.close()
    assert driver.close_calls == 2  # idempotent at our layer; driver owns dedup


async def test_recall_after_close_returns_empty_not_crash() -> None:
    # PIN: recall on a closed index degrades to [] (the closed-driver session
    # error is caught by the broad-except), it does not raise. Whether recall
    # AFTER close should be a hard error is a design choice — currently a degrade.
    driver = _FakeDriver(rows=[_bp_row()])
    index = _index(None, driver=driver)  # REAL _run over the fake driver
    # sanity: it works before close
    before = await index.recall(query_vector=[0.0] * 768, kind="blueprint", k=30)
    assert [c.id for c in before] == ["bp-1"]
    await index.close()
    after = await index.recall(query_vector=[0.0] * 768, kind="blueprint", k=30)
    assert after == []


# --------------------------------------------------------------------------
# Determinism — recall is a pass-through of `_run` row order (no client sort)
# --------------------------------------------------------------------------


async def test_recall_preserves_run_order_exactly_even_for_equal_scores() -> None:
    # recall relies on Cypher ORDER BY; it does NOT re-sort client-side. Pin that
    # the returned order is byte-identical to the `_run` order (stable for ties).
    rows = [
        _bp_row(id="c", score=0.5),
        _bp_row(id="a", score=0.5),
        _bp_row(id="b", score=0.5),
    ]
    got1 = await _recall_bp(rows)
    got2 = await _recall_bp(list(rows))
    assert [c.id for c in got1] == ["c", "a", "b"]
    assert [c.id for c in got1] == [c.id for c in got2]


async def test_recall_does_not_reorder_descending_score_input() -> None:
    # If `_run` (hypothetically) returned ascending, recall would NOT fix it —
    # proving recall trusts the DB order and adds no client-side ranking.
    rows = [_bp_row(id="low", score=0.1), _bp_row(id="high", score=0.9)]
    got = await _recall_bp(rows)
    assert [c.id for c in got] == ["low", "high"]  # input order, unchanged
