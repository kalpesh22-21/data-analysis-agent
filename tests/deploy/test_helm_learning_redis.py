"""The in-chart Redis IS the learning transport — so its Service name is configuration.

`data-agent-learning` deploys exactly one backing service of its own: a single-replica
Redis carrying the job stream (sweeper -> consumer group) and the dead-letter stream.
Everything else (Couchbase, Neo4j, ClickHouse, the MCP) stays external, because
everything else is SHARED with the agent; this stream is not.

Three things about it can break silently, and none of them are visible to `helm lint`:

  1. LEARNING_REDIS_URL is DERIVED from the Service this same chart renders. Derive it
     wrong — or keep deriving it after `redis.enabled=false` — and every daemon fails on
     a DNS lookup for a Service that does not exist, while the chart installs clean.
  2. `redis.enabled=false` is the "bring your own Redis" path. The URL must fall back to
     something external rather than to the ghost Service, or disabling the in-chart Redis
     silently breaks the plane instead of handing it over.
  3. Persistence is off by default and that is a considered trade (a lost stream costs
     re-work: the sweeper re-enqueues idle sessions on its next scan). What does NOT come
     back is the consumer group's PEL and the DEAD-LETTER stream. Turning persistence on
     therefore has to do BOTH halves — the PVC and `--appendonly yes` — because a volume
     with no AOF persists nothing, and AOF with no volume writes a journal that dies with
     the pod. Either half alone renders fine and looks configured.

Skipped when the `helm` binary is unavailable (a dev box without it).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "data-agent-learning"

# The helper's last-resort literal when there is no in-chart Redis and no explicit
# `config.LEARNING_REDIS_URL`. Deliberately the pre-existing default, so a release that
# already ran without an in-chart Redis keeps talking to whatever it was talking to.
_EXTERNAL_FALLBACK = "redis://redis:6379/0"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="Requires the 'helm' binary."
)


def _docs(*sets: str) -> list[dict]:
    args = ["helm", "template", "t", str(_CHART)]
    for s in sets:
        args += ["--set", s]
    rendered = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return [doc for doc in yaml.safe_load_all(rendered) if doc]


def _component(docs: list[dict], kind: str, component: str) -> dict | None:
    for doc in docs:
        labels = (doc.get("metadata") or {}).get("labels") or {}
        if doc.get("kind") == kind and labels.get("app.kubernetes.io/component") == component:
            return doc
    return None


def _config(docs: list[dict]) -> dict:
    return next(d for d in docs if d["kind"] == "ConfigMap")["data"]


def test_the_derived_redis_url_names_the_service_this_render_creates() -> None:
    """The derivation and the Service must agree by construction, not by coincidence:
    this asserts the literal URL AND that a Service of that exact name and port exists in
    the same render."""
    docs = _docs()
    assert _config(docs)["LEARNING_REDIS_URL"] == "redis://t-data-agent-learning-redis:6379/0"

    service = _component(docs, "Service", "redis")
    assert service is not None, "redis.enabled defaults to true but no Service rendered"
    assert service["metadata"]["name"] == "t-data-agent-learning-redis"
    assert service["spec"]["ports"][0]["port"] == 6379
    # ClusterIP is load-bearing, not a default worth drifting: this Redis has no
    # `requirepass`, so a NodePort/LoadBalancer here publishes an unauthenticated store
    # holding the plane's job stream.
    assert service["spec"]["type"] == "ClusterIP"


def test_the_service_port_knob_moves_the_url_with_it() -> None:
    """A port set on the Service but not in the URL is a transport that connects to
    nothing — the classic way a derivation rots."""
    docs = _docs("redis.service.port=6380")
    assert _config(docs)["LEARNING_REDIS_URL"] == "redis://t-data-agent-learning-redis:6380/0"
    assert _component(docs, "Service", "redis")["spec"]["ports"][0]["port"] == 6380


def test_disabling_the_in_chart_redis_falls_back_to_the_external_literal() -> None:
    """`redis.enabled=false` means "I brought my own". Nothing redis-shaped may remain,
    and the URL must stop naming the Service that is gone."""
    docs = _docs("redis.enabled=false", "redis.persistence.enabled=true")
    assert _config(docs)["LEARNING_REDIS_URL"] == _EXTERNAL_FALLBACK
    for kind in ("Deployment", "Service", "PersistentVolumeClaim"):
        assert _component(docs, kind, "redis") is None, f"orphaned redis {kind}"


def test_an_explicit_redis_url_wins_over_the_in_chart_service() -> None:
    """Cutting over to an external Redis must not require tearing the in-chart one down
    first (in-flight jobs do not migrate; you drain the old stream, then remove it). So an
    explicit URL wins even while `redis.enabled` is still true."""
    docs = _docs("config.LEARNING_REDIS_URL=redis://redis.other.svc.cluster.local:6379/3")
    assert _config(docs)["LEARNING_REDIS_URL"] == "redis://redis.other.svc.cluster.local:6379/3"
    # ...and the in-chart Redis is still rendered, unreferenced but running.
    assert _component(docs, "Deployment", "redis") is not None


def test_the_learning_redis_url_is_never_omitted() -> None:
    """Unlike every other blank value in this ConfigMap, this key is always rendered: a
    learning plane with no LEARNING_REDIS_URL has no transport at all, and the fallback is
    at least a usable address."""
    for sets in ((), ("redis.enabled=false",), ("config.LEARNING_REDIS_URL=",)):
        assert _config(_docs(*sets)).get("LEARNING_REDIS_URL")


def test_persistence_off_is_an_emptydir_and_no_aof() -> None:
    """The default. Asserted so the trade stays deliberate: no PVC anywhere, and no
    `--appendonly` writing a journal onto a volume that dies with the pod."""
    docs = _docs()
    assert not [d for d in docs if d["kind"] == "PersistentVolumeClaim"]
    pod = _component(docs, "Deployment", "redis")["spec"]["template"]["spec"]
    assert pod["containers"][0]["command"] == ["redis-server"]
    assert pod["volumes"] == [{"name": "data", "emptyDir": {}}]


def test_persistence_on_renders_a_pvc_the_pod_mounts_and_turns_on_the_aof() -> None:
    """Both halves, or the setting is theatre: the claim must exist, the pod must mount
    THAT claim by name, and the server must actually be told to append."""
    docs = _docs("redis.persistence.enabled=true", "redis.persistence.size=5Gi")

    claim = _component(docs, "PersistentVolumeClaim", "redis")
    assert claim is not None, "persistence.enabled=true rendered no PVC"
    assert claim["spec"]["resources"]["requests"]["storage"] == "5Gi"
    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    # The claim OUTLIVES the release on purpose: the reason to enable persistence is the
    # dead-letter stream, and `helm uninstall` deleting the record of what poisoned the
    # loop is exactly when you would want to read it. The cost is real and is why this is
    # asserted rather than assumed — reclaiming the storage now needs a manual
    # `kubectl delete pvc`, and a reinstall re-attaches the old volume.
    assert claim["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"

    deployment = _component(docs, "Deployment", "redis")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["containers"][0]["command"] == ["redis-server", "--appendonly", "yes"]
    assert pod["volumes"] == [
        {"name": "data", "persistentVolumeClaim": {"claimName": claim["metadata"]["name"]}}
    ]
    assert pod["containers"][0]["volumeMounts"][0]["mountPath"] == "/data"
    # RWO + a rolling update is a second pod that can never mount the volume, so the
    # rollout wedges. Recreate is what makes persistence workable at all.
    assert deployment["spec"]["strategy"]["type"] == "Recreate"


def test_the_storage_class_is_omitted_rather_than_blank_when_unset() -> None:
    """`storageClassName: ""` is NOT "use the default": it explicitly requests the
    empty-string class, which disables dynamic provisioning and leaves the claim Pending
    forever. Omitting the field is what selects the cluster default."""
    docs = _docs("redis.persistence.enabled=true")
    assert "storageClassName" not in _component(docs, "PersistentVolumeClaim", "redis")["spec"]

    docs = _docs("redis.persistence.enabled=true", "redis.persistence.storageClass=gp3")
    assert _component(docs, "PersistentVolumeClaim", "redis")["spec"]["storageClassName"] == "gp3"


def test_redis_is_pinned_to_one_replica_and_holds_none_of_the_planes_credentials() -> None:
    """Two replicas behind one Service are two INDEPENDENT Redises: jobs land in one and
    are consumed from the other at random, and the loop just looks slow. And the Redis pod
    is deliberately given neither the ConfigMap nor the Secret — a third-party image has
    no business holding the plane's Couchbase/Neo4j/OpenAI credentials, and it reads none
    of them."""
    pod = _component(_docs(), "Deployment", "redis")["spec"]["template"]["spec"]
    assert _component(_docs(), "Deployment", "redis")["spec"]["replicas"] == 1
    for container in pod["containers"]:
        assert not (container.get("envFrom") or []), "the redis pod mounts the plane's env"
        assert not (container.get("env") or []), "the redis pod carries plane env vars"
