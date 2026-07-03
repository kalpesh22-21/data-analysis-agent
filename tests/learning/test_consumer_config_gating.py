"""HIGH-2 config gating — the consumer entrypoint (`scripts/run_learning_consumer.py`)
treats (extractor model, audit store, candidate store) as ONE unit.

Real extraction writes entity-bearing evidence to `learning_audit` AND persists
candidates to `learning_candidates`; running it with a missing durable store would
silently lose candidates / leave dangling evidence_refs. So the extractor is
enabled ONLY when the model AND both durable stores are configured; otherwise the
KEEP path FALLS BACK to the S2 `would_extract` stub (no extraction, NO audit
writes) and logs it loudly.

This drives the composition root `_main()` with fakes for every heavy collaborator
and asserts what the `LearningConsumer` is constructed with (extractor / audit /
candidates) under each config, plus the loud warning on partial config.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "run_learning_consumer.py"
)


def _load_entrypoint():
    """Load the entrypoint as a FRESH module object (not cached in sys.modules) so
    per-test attribute patching never leaks."""
    spec = importlib.util.spec_from_file_location("_run_learning_consumer_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_settings(*, extractor: bool, audit: bool, candidates: bool) -> SimpleNamespace:
    return SimpleNamespace(
        learning_extractor_api_key="sk-test" if extractor else "",
        learning_extractor_model="gpt-x",
        learning_extractor_base_url="",
        learning_extractor_max_retries=2,
        learning_audit_username="u" if audit else "",
        learning_audit_password="p" if audit else "",
        learning_candidates_username="u" if candidates else "",
        learning_candidates_password="p" if candidates else "",
        otlp_endpoint="",
        learning_service_name="learning-loop",
        learning_consumer_group="learning-workers",
        learning_consumer_name="worker-test",
        learning_batch_size=10,
    )


class _CapturingConsumer:
    """Stand-in `LearningConsumer` that records its construction kwargs and whose
    `run_forever` returns immediately (no real loop)."""

    last_kwargs: dict = {}

    def __init__(self, store, queue, settings, **kwargs):
        type(self).last_kwargs = kwargs

    async def run_forever(self, *, sleep):
        return None


def _patch(module, settings):
    module.get_runtime_settings = lambda: object()
    module.LearningSettings = lambda: settings
    module.configure_learning_tracing = lambda **k: object()
    module.set_global_tracer_provider = lambda p: None
    module.get_learning_tracer = lambda p: None
    module.CouchbaseSessionStore = lambda *a, **k: object()
    module.RedisStreamsLearningQueue = SimpleNamespace(from_settings=lambda s: object())
    module.CouchbaseAuditStore = lambda *a, **k: SimpleNamespace(kind="audit")
    module.CouchbaseCandidateStore = lambda *a, **k: SimpleNamespace(kind="candidates")
    module.build_openai_model_client = lambda **k: object()
    module.LearningExtractor = lambda *a, **k: SimpleNamespace(kind="extractor")
    module.load_known_rule_ids = lambda: frozenset({"active_employee"})
    module.LearningConsumer = _CapturingConsumer


async def _run(settings):
    module = _load_entrypoint()
    _patch(module, settings)
    _CapturingConsumer.last_kwargs = {}
    rc = await module._main()
    assert rc == 0
    return _CapturingConsumer.last_kwargs


async def test_all_three_configured_enables_real_extraction():
    kwargs = await _run(_fake_settings(extractor=True, audit=True, candidates=True))
    assert getattr(kwargs["extractor"], "kind", None) == "extractor"
    assert getattr(kwargs["audit"], "kind", None) == "audit"
    assert getattr(kwargs["candidates"], "kind", None) == "candidates"


async def test_extractor_but_no_candidate_store_falls_back_to_stub(caplog):
    with caplog.at_level(logging.WARNING):
        kwargs = await _run(_fake_settings(extractor=True, audit=True, candidates=False))
    # Partial config ⇒ NO extraction unit wired at all (the safe fallback).
    assert kwargs["extractor"] is None
    assert kwargs["audit"] is None
    assert kwargs["candidates"] is None
    # A loud warning names the missing store.
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings
    assert any("FALLING BACK" in r.getMessage() for r in warnings)
    assert any("candidates_ready=False" in r.getMessage() for r in warnings)


async def test_extractor_but_no_audit_store_falls_back_to_stub(caplog):
    with caplog.at_level(logging.WARNING):
        kwargs = await _run(_fake_settings(extractor=True, audit=False, candidates=True))
    assert kwargs["extractor"] is None
    assert kwargs["audit"] is None
    assert kwargs["candidates"] is None
    assert any("audit_ready=False" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.WARNING)


async def test_no_extractor_key_uses_stub_without_warning(caplog):
    with caplog.at_level(logging.WARNING):
        kwargs = await _run(_fake_settings(extractor=False, audit=True, candidates=True))
    assert kwargs["extractor"] is None
    # Not a partial-config hazard → info, not a warning.
    assert not [r for r in caplog.records
                if r.levelno >= logging.WARNING and "FALLING BACK" in r.getMessage()]
