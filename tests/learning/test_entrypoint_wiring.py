"""Wiring guards for the learning-loop ENTRYPOINTS — the things that were silently
no-oping in production while every unit test stayed green.

Three findings from the wiring audit, each pinned here:

  * TRACING WAS SILENTLY OFF. `LearningSettings.otlp_endpoint` defaults to `""` and
    `configure_tracing` answers that with a NO-OP provider — deliberately (zero infra
    to run), but with nothing said. A full day of live runs produced ZERO Phoenix
    spans and the cause was only findable by reading source for the env var's name.
    Every learning entrypoint now states the posture at startup.

  * `--once` DID NOT EXIST on the sweeper: it could only loop forever on a 60s
    interval, so a controlled test (enqueue a known set, inspect, stop) had no way to
    run exactly one sweep.

  * `rule_index` WAS PASSED BY THE CONSUMER ENTRYPOINT ONLY. The two demo scripts
    passed `known_rules` without it, which leaves the unknown-rule-id hint machinery
    inert — announced by a single INFO line from the factory and otherwise invisible.
    Guarded here as an AST invariant over EVERY `build_learning_consumer` call site in
    `scripts/`, so the next entrypoint cannot regress it by omission.
"""

from __future__ import annotations

import ast
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from data_agent.learning.observability import log_tracing_status

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


# --- the startup tracing posture ---------------------------------------------


def test_tracing_on_logs_the_endpoint(caplog):
    logger = logging.getLogger("test.tracing.on")
    with caplog.at_level(logging.INFO, logger="test.tracing.on"):
        log_tracing_status(
            logger,
            otlp_endpoint="http://localhost:6006/v1/traces",
            service_name="learning-loop",
            process="consumer",
        )
    (record,) = caplog.records
    assert record.levelno == logging.INFO
    message = record.getMessage()
    assert "http://localhost:6006/v1/traces" in message
    assert "consumer" in message


def test_tracing_off_warns_and_names_the_env_var(caplog):
    """The ONE fact that was missing when nothing showed up in Phoenix: which
    variable turns it on. WARNING, not INFO — an operator must not have to infer
    'no spans anywhere' from an absence of log lines."""
    logger = logging.getLogger("test.tracing.off")
    with caplog.at_level(logging.INFO, logger="test.tracing.off"):
        log_tracing_status(
            logger, otlp_endpoint="", service_name="learning-loop", process="sweeper"
        )
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "OTLP_ENDPOINT" in message
    assert "ZERO spans" in message
    assert "sweeper" in message


@pytest.mark.parametrize(
    "script",
    [
        "run_learning_sweeper.py",
        "run_learning_consumer.py",
        "run_learning_scheduler.py",
        "run_inbox_service.py",
    ],
)
def test_every_learning_entrypoint_reports_its_tracing_posture(script):
    """Static guard: every learning-plane entrypoint must run the shared startup
    preamble, which is what configures tracing AND says whether it is ON. Checked as
    source rather than by running each daemon, because most of them need
    Couchbase/Redis to reach their first log line.

    This used to scan for `configure_learning_tracing(` + `log_tracing_status(`
    directly, back when each script carried its own ~20-line copy of the block. The
    copies are gone; asserting on the call to `configure_daemon_process` is the same
    guard one level up, and `test_the_daemon_preamble_does_all_four_things` below
    holds the helper itself to the full contract.

    `run_inbox_service.py` is in the list as of the dedup — it is a learning-plane
    daemon that had NEITHER the tracing posture line nor the env-var typo warning."""
    source = (_SCRIPTS / script).read_text(encoding="utf-8")
    assert "configure_daemon_process(" in source


def test_the_daemon_preamble_does_all_four_things(caplog, monkeypatch):
    """The contract the per-script guard above now delegates to. All four steps are
    diagnostics-or-wiring that fail SILENTLY when omitted, which is why they are pinned
    behaviourally rather than by reading source.

    `basicConfig` is asserted as a CALL rather than by inspecting root-logger handlers,
    and that is deliberate: `basicConfig` is a documented no-op once the root logger has
    any handler, and under pytest it always does (caplog installs one). A handler-state
    assertion would therefore pass or fail on the harness rather than on the code. The
    claim being pinned is "the preamble configures logging before it logs" — patching
    the function states exactly that and nothing more."""
    from types import SimpleNamespace

    from data_agent.learning import entrypoint

    basic_config_calls: list[dict] = []
    installed: list[object] = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: basic_config_calls.append(kw))
    monkeypatch.setattr(entrypoint, "set_global_tracer_provider", installed.append)
    monkeypatch.setenv("LEARNING_MAX_DELIVERES", "3")  # a plausible typo
    settings = SimpleNamespace(otlp_endpoint="", learning_service_name="learning-loop")
    logger = logging.getLogger("test.daemon.preamble")

    with caplog.at_level(logging.INFO, logger="test.daemon.preamble"):
        tracer = entrypoint.configure_daemon_process("inbox", settings, logger)

    # 1. logging configured, at INFO, so the three lines below are actually visible
    assert basic_config_calls == [{"level": logging.INFO}]
    # 2. the provider became the process-global one (+ a tracer is returned for
    #    callers that thread one into their components)
    assert len(installed) == 1
    assert tracer is not None
    messages = [r.getMessage() for r in caplog.records]
    # 3. tracing posture, named for THIS process
    assert any("tracing OFF" in m and "inbox" in m for m in messages)
    # 4. the ignored-env-var warning
    assert any("LEARNING_MAX_DELIVERES" in m for m in messages)


# --- the sweeper's --once flag ------------------------------------------------


class _FakeQueue:
    def __init__(self) -> None:
        self.groups = 0

    async def ensure_group(self) -> None:
        self.groups += 1


class _FakeSweeper:
    """Records which loop the entrypoint chose."""

    instances: list[_FakeSweeper] = []

    def __init__(self, store, queue, settings, *, tracer=None) -> None:
        self.queue = queue
        self.once_calls = 0
        self.forever_calls = 0
        _FakeSweeper.instances.append(self)

    async def run_once(self):
        self.once_calls += 1
        return SimpleNamespace(scanned=3, claimed=2, enqueued=2, disabled=False)

    async def run_forever(self, *, sleep):
        self.forever_calls += 1


def _load_sweeper_entrypoint():
    spec = importlib.util.spec_from_file_location(
        "_run_learning_sweeper_under_test", _SCRIPTS / "run_learning_sweeper.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _patch_sweeper_entrypoint(module) -> _FakeQueue:
    queue = _FakeQueue()
    module.get_runtime_settings = lambda: SimpleNamespace()
    module.LearningSettings = lambda: SimpleNamespace(
        otlp_endpoint="",
        learning_service_name="learning-loop",
        learning_sweep_interval_seconds=60.0,
        learning_idle_threshold_seconds=1800,
    )
    # One patch where there used to be three: the whole tracing preamble is now a
    # single call the script makes (`learning/entrypoint.py`), covered on its own by
    # `test_the_daemon_preamble_does_all_four_things`.
    module.configure_daemon_process = lambda *a, **k: None
    module.CouchbaseSessionStore = lambda *a, **k: object()
    module.RedisStreamsLearningQueue = SimpleNamespace(from_settings=lambda s: queue)
    module.LearningSweeper = _FakeSweeper
    return queue


async def test_once_flag_runs_exactly_one_sweep_and_exits(caplog):
    _FakeSweeper.instances.clear()
    module = _load_sweeper_entrypoint()
    queue = _patch_sweeper_entrypoint(module)

    with caplog.at_level(logging.INFO):
        rc = await module._main(["--once"])

    assert rc == 0
    (sweeper,) = _FakeSweeper.instances
    assert sweeper.once_calls == 1
    assert sweeper.forever_calls == 0
    # `run_forever` normally guarantees the group exists before the first cycle; the
    # single-shot path must do the same or the XADDed entries are undeliverable.
    assert queue.groups == 1
    assert any("scanned=3" in r.getMessage() for r in caplog.records)


async def test_default_still_loops_forever():
    _FakeSweeper.instances.clear()
    module = _load_sweeper_entrypoint()
    _patch_sweeper_entrypoint(module)

    rc = await module._main([])

    assert rc == 0
    (sweeper,) = _FakeSweeper.instances
    assert sweeper.forever_calls == 1
    assert sweeper.once_calls == 0


# --- rule_index travels with known_rules, at every call site ------------------


def _consumer_call_kwargs(path: Path) -> list[set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        {kw.arg for kw in node.keywords if kw.arg}
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_learning_consumer"
    ]


def test_every_script_passing_known_rules_also_passes_rule_index():
    """`known_rules` without `rule_index` is a SUPPORTED but inert configuration: an
    unknown rule id declines `missing_rule` terminally with no corrective turn, even
    when the catalog names the same concept under a different id. The factory says so
    at INFO and carries on — which is exactly why this needs a test rather than a
    reader noticing. Enforced across `scripts/` so a new entrypoint inherits it."""
    offenders: list[str] = []
    seen = 0
    for path in sorted(_SCRIPTS.glob("*.py")):
        for kwargs in _consumer_call_kwargs(path):
            seen += 1
            if "known_rules" in kwargs and "rule_index" not in kwargs:
                offenders.append(path.name)
    assert seen >= 3, "expected the consumer entrypoint + both demos to build a consumer"
    assert offenders == [], (
        "these scripts ground the `rule` role but leave the unknown-id hint machinery "
        f"inert (pass rule_index=rule_index_from_catalog(catalog)): {offenders}"
    )
