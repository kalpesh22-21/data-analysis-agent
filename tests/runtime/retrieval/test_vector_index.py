"""Layer-1 tests for FakeVectorIndex (design §3.1) — cosine recall, kind filter."""

from __future__ import annotations

from data_agent.runtime.retrieval.models import Candidate
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex


def _c(id: str, kind: str, vec: list[float]) -> tuple[Candidate, list[float]]:
    return (Candidate(id=id, kind=kind, text=id, uses=None), vec)  # type: ignore[arg-type]


async def test_recall_filters_by_kind() -> None:
    index = FakeVectorIndex(
        [_c("bp", "blueprint", [1.0, 0.0]), _c("kn", "knowledge", [1.0, 0.0])]
    )
    got = await index.recall(query_vector=[1.0, 0.0], kind="blueprint", k=10)
    assert [c.id for c in got] == ["bp"]


async def test_recall_orders_by_descending_cosine_and_sets_score() -> None:
    index = FakeVectorIndex(
        [
            _c("near", "blueprint", [1.0, 0.0]),
            _c("mid", "blueprint", [1.0, 1.0]),
            _c("far", "blueprint", [0.0, 1.0]),
        ]
    )
    got = await index.recall(query_vector=[1.0, 0.0], kind="blueprint", k=10)
    assert [c.id for c in got] == ["near", "mid", "far"]
    assert got[0].score is not None and got[0].score > (got[2].score or 0.0)


async def test_recall_respects_k() -> None:
    index = FakeVectorIndex([_c(f"c{i}", "blueprint", [1.0, i / 10]) for i in range(5)])
    got = await index.recall(query_vector=[1.0, 0.0], kind="blueprint", k=2)
    assert len(got) == 2


async def test_recall_fail_flag_returns_empty() -> None:
    index = FakeVectorIndex([_c("bp", "blueprint", [1.0, 0.0])], fail=True)
    assert await index.recall(query_vector=[1.0, 0.0], kind="blueprint", k=10) == []
