"""The CONSUMER process gets a neo4j driver (PriorArt Slice 2).

Before this slice the learning consumer had no neo4j driver AT ALL — only the scheduler
and the inbox service did — which is precisely why the dedup stage could see nothing
beyond the `learning_corpus` bucket it seeds itself.

Two things are pinned here, and they pull in opposite directions on purpose:

  * when neo4j + the embedding API are configured, the entrypoint builds ONE driver,
    hands the prior-art index the SAME embedding client the dedup soft layer uses, and
    CLOSES the driver on shutdown;
  * when either is absent, the consumer starts anyway with `prior_art=None`. The
    prior-art index is deliberately OUTSIDE the factory's all-or-nothing extraction
    unit: making it required would mean a neo4j outage stops the loop draining its
    queue, and fail-open is the whole posture.

Slug: PA-consumer-driver-wiring.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import data_agent.learning.factory as factory_module

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_learning_consumer.py"


def _load_entrypoint():
    spec = importlib.util.spec_from_file_location("_run_consumer_prior_art_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _learning_settings() -> SimpleNamespace:
    return SimpleNamespace(
        learning_extractor_api_key="sk-test",
        learning_extractor_model="gpt-x",
        learning_extractor_base_url="",
        learning_extractor_max_retries=2,
        learning_audit_username="u", learning_audit_password="p",
        learning_candidates_username="u", learning_candidates_password="p",
        learning_corpus_username="u", learning_corpus_password="p",
        learning_dedup_merge_threshold=0.95,
        learning_recurrence_similarity_threshold=0.90,
        learning_dedup_conflict_threshold=0.83,
        # Coverage judge (plan §3b) — a faithful stand-in for `LearningSettings`.
        learning_judge_enabled=True,
        learning_judge_shadow_mode=False,
        learning_judge_model="",
        learning_judge_pre_drop_confidence=0.90,
        learning_judge_post_drop_confidence=0.75,
        learning_judge_band_low=0.70,
        learning_judge_band_high=0.97,
        learning_judge_timeout_seconds=30.0,
        otlp_endpoint="", learning_service_name="learning-loop",
        learning_consumer_group="g", learning_consumer_name="w", learning_batch_size=10,
        learning_trace_verbose=False,
    )


def _runtime_settings(*, neo4j: bool, embedding: bool) -> SimpleNamespace:
    return SimpleNamespace(
        embedding_api_url="http://embed" if embedding else "",
        embedding_api_key="",
        embedding_model="all-mpnet-base-v2",
        embedding_timeout_seconds=10.0,
        neo4j_url="bolt://neo4j:7687" if neo4j else "",
        neo4j_username="neo4j" if neo4j else "",
        neo4j_password="pw" if neo4j else "",
        neo4j_timeout_seconds=10.0,
    )


class _FakeDriver:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def _run(*, neo4j: bool, embedding: bool) -> dict:
    module = _load_entrypoint()
    captured: dict = {}

    module.get_runtime_settings = lambda: _runtime_settings(neo4j=neo4j, embedding=embedding)
    module.LearningSettings = _learning_settings
    module.UserKnowledgeStoreConfig = lambda: SimpleNamespace(
        user_knowledge_username="u", user_knowledge_password="p"
    )
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
    module.HttpEmbeddingClient = lambda **k: SimpleNamespace(kind="embedder", **k)
    module.catalog_dict = lambda settings=None: {}
    module.catalog_fixture_path = lambda settings=None: "<test-fixture>"
    module.build_sqlglot_schema_from_catalog = lambda catalog: {}
    module.known_rule_ids_from_catalog = lambda catalog: frozenset()

    # The driver is created by a LOCAL `from neo4j import AsyncGraphDatabase` inside the
    # branch, so patch the neo4j module itself rather than a module attribute.
    import neo4j as neo4j_module

    real_driver_factory = neo4j_module.AsyncGraphDatabase.driver
    driver = _FakeDriver()

    def _fake_driver(*args, **kwargs):
        captured["driver_args"] = (args, kwargs)
        return driver

    neo4j_module.AsyncGraphDatabase.driver = staticmethod(_fake_driver)  # type: ignore[assignment]

    real_build = factory_module.build_learning_consumer

    def _wrapped_build(*args, **kwargs):
        captured["prior_art"] = kwargs.get("prior_art")
        captured["embedder"] = kwargs.get("embedder")
        consumer = real_build(*args, **kwargs)

        async def _noop(*, sleep):
            return None

        consumer.run_forever = _noop
        captured["consumer"] = consumer
        return consumer

    module.build_learning_consumer = _wrapped_build
    try:
        rc = await module._main()
    finally:
        neo4j_module.AsyncGraphDatabase.driver = real_driver_factory  # type: ignore[assignment]
    assert rc == 0
    captured["driver"] = driver
    return captured


async def test_a_configured_graph_wires_the_prior_art_index_and_closes_its_driver():
    from data_agent.learning.priorart.neo4j_index import Neo4jPriorArtIndex

    captured = await _run(neo4j=True, embedding=True)

    prior_art = captured["prior_art"]
    assert isinstance(prior_art, Neo4jPriorArtIndex)
    # ONE pool per process, with the connection budget bounding the "unreachable host"
    # degrade so a down neo4j raises inside the budget instead of hanging a candidate.
    _args, kwargs = captured["driver_args"]
    assert kwargs["connection_timeout"] == 10.0
    assert kwargs["connection_acquisition_timeout"] == 10.0
    # The driver is closed on shutdown — mirroring how the scheduler entrypoint owns and
    # closes its own driver in a `finally`.
    assert captured["driver"].closed is True


async def test_the_prior_art_index_reuses_the_dedup_embedding_client_and_model():
    """One embedding client, one model id. The prior-art query text must be embedded with
    the model the CORPUS was built with, and `RuntimeSettings.embedding_model` is the
    single source of truth for that (it is what `build_hydrator` passes as both the
    client's `model` and the vector index's `expected_model`). A second client — or a
    second model id — would produce a 100% `model_matched=False` rate indistinguishable
    from a genuine corpus skew."""
    captured = await _run(neo4j=True, embedding=True)
    prior_art = captured["prior_art"]
    assert prior_art._embedding_client is captured["embedder"]
    assert prior_art._expected_model == "all-mpnet-base-v2"
    assert prior_art._embedding_client.model == prior_art._expected_model


async def test_no_graph_configured_still_starts_the_consumer_with_no_prior_art():
    """FAIL-OPEN, and outside the all-or-nothing unit. A missing graph must never stop
    the loop draining its queue — the factory logs loudly and dedup falls back to exactly
    its pre-slice behaviour."""
    captured = await _run(neo4j=False, embedding=True)
    assert captured["prior_art"] is None
    assert len(captured["consumer"]._stages) == 6  # the pipeline is still fully built


async def test_no_embedder_means_no_prior_art_index_even_with_neo4j_configured():
    """The index cannot embed a query without an embedding client, so building it would
    guarantee a `PriorArtUnavailableError` on every candidate — a permanently fail-open
    loop that LOOKS wired. Refuse to build it instead; the warning names the missing piece."""
    captured = await _run(neo4j=True, embedding=False)
    assert captured["prior_art"] is None
    assert captured["driver"].closed is False  # no driver was ever created
