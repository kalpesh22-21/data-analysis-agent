"""Config gating for the consumer entrypoint (`scripts/run_learning_consumer.py`),
now adopting the Wave-3 composition-root factory (`build_learning_consumer`).

Extraction is a UNIT (the S3 precedent, extended to the full write-router): the
extractor model client + durable audit store + durable candidate store + durable
blueprint corpus + durable per-user store + the catalog. Three branches, never a
fourth (partial) one:

  * model client ABSENT (no extractor key) ⇒ the S2 `would_extract` stub — the
    consumer is built with `extractor=None` and an EMPTY `stages` pipeline;
  * ALL durable collaborators provisioned ⇒ the full six-stage write-router pipeline
    (extractor present, `stages` = the six frozen stages);
  * model client PRESENT but a durable collaborator's RBAC creds MISSING (a PARTIAL
    config) ⇒ `LearningWiringError` at composition (the factory fails fast) — never a
    half-wired pipeline that strands candidates mid-flow.

This drives the real composition root `_main()` with fakes for every heavy
collaborator, letting the REAL factory assemble the consumer (only `run_forever` is
stubbed so the loop never blocks), and asserts what the `LearningConsumer` is built
with under each config.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

import data_agent.learning.factory as factory_module
from data_agent.learning.factory import LearningWiringError

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


def _fake_learning_settings(
    *, extractor: bool, audit: bool, candidates: bool, corpus: bool
) -> SimpleNamespace:
    return SimpleNamespace(
        learning_extractor_api_key="sk-test" if extractor else "",
        learning_extractor_model="gpt-x",
        learning_extractor_base_url="",
        learning_extractor_max_retries=2,
        learning_audit_username="u" if audit else "",
        learning_audit_password="p" if audit else "",
        learning_candidates_username="u" if candidates else "",
        learning_candidates_password="p" if candidates else "",
        learning_corpus_username="u" if corpus else "",
        learning_corpus_password="p" if corpus else "",
        otlp_endpoint="",
        learning_service_name="learning-loop",
        learning_consumer_group="learning-workers",
        learning_consumer_name="worker-test",
        learning_batch_size=10,
    )


def _fake_user_config(*, user: bool) -> SimpleNamespace:
    return SimpleNamespace(
        user_knowledge_username="u" if user else "",
        user_knowledge_password="p" if user else "",
    )


def _fake_runtime_settings() -> SimpleNamespace:
    # embedding_api_url empty ⇒ the entrypoint passes embedder=None (factory defaults
    # to the insert-only embedder). No HttpEmbeddingClient is constructed.
    return SimpleNamespace(
        embedding_api_url="",
        embedding_api_key="",
        embedding_model="all-mpnet-base-v2",
        embedding_timeout_seconds=10.0,
    )


def _patch(module, *, learning_settings, user_config, captured):
    module.get_runtime_settings = lambda: _fake_runtime_settings()
    module.LearningSettings = lambda: learning_settings
    module.UserKnowledgeStoreConfig = lambda: user_config
    module.configure_learning_tracing = lambda **k: object()
    module.set_global_tracer_provider = lambda p: None
    module.get_learning_tracer = lambda p: None
    module.CouchbaseSessionStore = lambda *a, **k: object()
    module.RedisStreamsLearningQueue = SimpleNamespace(from_settings=lambda s: object())
    module.CouchbaseAuditStore = lambda *a, **k: SimpleNamespace(kind="audit")
    module.CouchbaseCandidateStore = lambda *a, **k: SimpleNamespace(kind="candidates")
    module.CouchbaseBlueprintCorpus = lambda *a, **k: SimpleNamespace(kind="corpus")
    module.CouchbaseUserKnowledgeStore = lambda *a, **k: SimpleNamespace(kind="user")
    module.build_openai_model_client = lambda **k: object()
    module.HttpEmbeddingClient = lambda **k: SimpleNamespace(kind="embedder")
    module.build_sqlglot_schema = lambda: {}
    module.load_known_rule_ids = lambda: frozenset({"active_employee"})

    # Let the REAL factory assemble the consumer, but capture it and neutralize
    # `run_forever` so the blocking loop never runs.
    real_build = factory_module.build_learning_consumer

    def _wrapped_build(*args, **kwargs):
        consumer = real_build(*args, **kwargs)

        async def _noop(*, sleep):
            return None

        consumer.run_forever = _noop
        captured["consumer"] = consumer
        return consumer

    module.build_learning_consumer = _wrapped_build


async def _run(*, learning_settings, user_config):
    module = _load_entrypoint()
    captured: dict = {}
    _patch(module, learning_settings=learning_settings, user_config=user_config, captured=captured)
    rc = await module._main()
    assert rc == 0
    return captured["consumer"]


async def test_all_collaborators_provisioned_builds_full_pipeline():
    consumer = await _run(
        learning_settings=_fake_learning_settings(
            extractor=True, audit=True, candidates=True, corpus=True
        ),
        user_config=_fake_user_config(user=True),
    )
    # Full write-router: the extractor is wired and all SIX frozen stages are present.
    assert consumer._extractor is not None
    assert len(consumer._stages) == 6
    assert [s.stage_id for s in consumer._stages] == [
        "generalize",
        "leakage",
        "dedup",
        "schema_edit_writer",
        "user_knowledge_writer",
        "writer",
    ]
    # The durable stores are threaded through (shared singleton — same instance).
    assert getattr(consumer._audit, "kind", None) == "audit"
    assert getattr(consumer._candidates, "kind", None) == "candidates"


async def test_no_extractor_key_builds_stub_with_empty_pipeline():
    consumer = await _run(
        learning_settings=_fake_learning_settings(
            extractor=False, audit=True, candidates=True, corpus=True
        ),
        user_config=_fake_user_config(user=True),
    )
    # No model client ⇒ the S2 would-extract stub: no extractor, EMPTY pipeline.
    assert consumer._extractor is None
    assert consumer._stages == ()


async def test_extractor_but_missing_corpus_creds_fails_fast():
    with pytest.raises(LearningWiringError):
        await _run(
            learning_settings=_fake_learning_settings(
                extractor=True, audit=True, candidates=True, corpus=False
            ),
            user_config=_fake_user_config(user=True),
        )


async def test_extractor_but_missing_user_creds_fails_fast():
    with pytest.raises(LearningWiringError):
        await _run(
            learning_settings=_fake_learning_settings(
                extractor=True, audit=True, candidates=True, corpus=True
            ),
            user_config=_fake_user_config(user=False),
        )


async def test_extractor_but_missing_candidate_creds_fails_fast():
    with pytest.raises(LearningWiringError):
        await _run(
            learning_settings=_fake_learning_settings(
                extractor=True, audit=True, candidates=False, corpus=True
            ),
            user_config=_fake_user_config(user=True),
        )


async def test_extractor_but_missing_audit_creds_fails_fast():
    with pytest.raises(LearningWiringError):
        await _run(
            learning_settings=_fake_learning_settings(
                extractor=True, audit=False, candidates=True, corpus=True
            ),
            user_config=_fake_user_config(user=True),
        )
