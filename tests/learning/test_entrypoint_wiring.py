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

  * THE INBOX SERVICE COULD NOT START ITS FULL WRITE PLANE (ISSUES.md H7). Its app was
    built at MODULE IMPORT, and `acouchbase.Cluster(...)` raises
    `RuntimeError: Event loop is not running` when constructed outside a loop, so every
    correctly-provisioned deploy of the reviewer surface died on `import`. The Tier-3
    lazy Couchbase seam (`b7b21c1`) has since moved that constructor behind the first
    connect, which defuses THIS instance without removing the hazard: import-time
    composition of durable infra is still one eager `__init__` away from the same
    crash, and only ever in the full-plane configuration nothing else exercises.
    Pinned below as both a behavioural test (importing builds nothing) and an AST
    invariant (nothing runs at module scope), because what broke is the import-time
    execution itself rather than any particular value.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

# The exit code `http_daemon` reproduces. Imported from the MODULE, not via
# `uvicorn.main`: the package re-exports a click `Command` named `main` over its own
# submodule.
from uvicorn.main import STARTUP_FAILURE

from data_agent import http_daemon
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


# --- the inbox service is import-clean (H7) -----------------------------------


def _load_inbox_entrypoint():
    spec = importlib.util.spec_from_file_location(
        "_run_inbox_service_under_test", _SCRIPTS / "run_inbox_service.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_importing_the_inbox_entrypoint_constructs_nothing(monkeypatch):
    """H7's regression test. The app factory must not run at import.

    The stub raises the EXACT error the real one raised in production — acouchbase's
    `RuntimeError: Event loop is not running` — so this fails the way the deploy failed
    rather than on a bare "was it called". A passing import proves the factory moved;
    the recorded call list proves it did not merely move somewhere else at module scope.
    """
    from data_agent.learning.inbox import service

    calls: list[tuple] = []

    def _explode(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("Event loop is not running")

    monkeypatch.setattr(service, "create_inbox_app", _explode)

    module = _load_inbox_entrypoint()

    assert calls == [], "the app factory ran at import time"
    assert not hasattr(module, "app"), (
        "a module-level `app` is the thing that forced construction at import — the "
        "ASGI factory to point uvicorn at is "
        "`data_agent.learning.inbox.service:create_inbox_app --factory`"
    )


def test_no_call_at_module_scope_in_the_inbox_entrypoint():
    """The invariant behind the test above, stated structurally so the next edit cannot
    reintroduce it in some other guise (a different factory, a store built directly).
    Module scope may only bind imports, constants and defs — anything that RUNS at
    import is what H7 was."""
    tree = ast.parse((_SCRIPTS / "run_inbox_service.py").read_text(encoding="utf-8"))

    def _is_module_docstring(index: int, node: ast.stmt) -> bool:
        # ONLY the leading docstring. Exempting `ast.Expr` wholesale would wave through
        # every bare `create_inbox_app()` — a bare call IS an Expr, and that is exactly
        # the statement this test exists to catch.
        return index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)

    def _is_main_guard(node: ast.stmt) -> bool:
        # ONLY `if __name__ == "__main__":`. Any other module-scope `if` is a
        # conditional construction (`if os.environ.get(...): app = create_inbox_app()`),
        # which runs at import for whoever satisfies the condition.
        return isinstance(node, ast.If) and ast.unparse(node.test) in (
            "__name__ == '__main__'",
            "'__main__' == __name__",
        )

    def _is_module_logger(node: ast.stmt) -> bool:
        # `_logger = logging.getLogger(__name__)` — the one call the module makes at
        # scope. Matched STRUCTURALLY (a substring allowlist would pass anything with
        # `getLogger` anywhere in it, including a second call on the same line).
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            return False
        func = node.value.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "getLogger"
            and isinstance(func.value, ast.Name)
            and func.value.id == "logging"
            and [ast.unparse(a) for a in node.value.args] == ["__name__"]
            and not node.value.keywords
        )

    executable = [
        node
        for index, node in enumerate(tree.body)
        if not isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef))
        and not _is_module_docstring(index, node)
        and not _is_main_guard(node)
    ]
    offenders = [
        ast.unparse(node)
        for node in executable
        if any(isinstance(sub, ast.Call) for sub in ast.walk(node)) and not _is_module_logger(node)
    ]
    assert offenders == [], f"these run at import time in run_inbox_service.py: {offenders}"


def _stub_uvicorn(monkeypatch, recorded: dict, *, started: bool):
    """Stub `uvicorn.Config`/`Server` INSIDE `http_daemon` — the module that now builds
    them (C3 moved the serve call out of this script and into the shared wrapper, so
    the seam these tests poke moved with it; the CONTRACTS they pin did not).

    The stubs reproduce the ONE piece of uvicorn sequencing these tests stand on: with
    `factory=True` the app factory is called by `Config.load()`, and `load()` is called
    from inside `Server.serve()` — i.e. inside the running loop. That is not stub
    convenience, it is verbatim `uvicorn/server.py::_serve` (`if not config.loaded:
    config.load()`, under `capture_signals`), which is what makes the in-loop assertion
    below a claim about production rather than about the fake.
    """

    class _StubConfig:
        def __init__(self, app, *, factory=False, **kwargs):
            # `app` is the FACTORY now, not an app: the wrapper hands uvicorn the
            # callable and lets `load()` call it. Recorded under its own key so the
            # test can pin both halves — what was handed over, and what came back.
            recorded["factory_arg"] = app
            recorded["factory"] = factory
            recorded.update(kwargs)
            self._app = app
            self._factory = factory

        def get_loop_factory(self):
            # None = "the loop `asyncio.run` would have built anyway". The real Config
            # answers uvloop here when it is installed; which implementation gets picked
            # is `test_http_daemon.py`'s business, not this file's.
            return None

        def load(self):
            recorded["app"] = self._app() if self._factory else self._app

    class _StubServer:
        # uvicorn's own post-boot flag, which the wrapper reads to reproduce
        # `uvicorn.run()`'s STARTUP_FAILURE exit. It is never cleared on shutdown, so
        # True is the state after any successful boot.
        def __init__(self, config):
            recorded["config"] = config
            self.config = config
            self.started = started
            self.should_exit = False

        async def serve(self):
            self.config.load()
            recorded["served"] = True

    monkeypatch.setattr(http_daemon.uvicorn, "Config", _StubConfig)
    monkeypatch.setattr(http_daemon.uvicorn, "Server", _StubServer)


def test_the_inbox_app_is_built_inside_the_running_loop(monkeypatch):
    """The other half of the H7 fix: construction has to happen somewhere, and that
    somewhere must be inside a running loop. The stub factory calls
    `asyncio.get_running_loop()` — the check acouchbase itself makes — so this test
    fails with production's exact `RuntimeError` if the app is ever built before the
    loop is entered.

    Driven through `main()` rather than a private serve helper, which is what the C3
    rework left to poke: the script's whole remaining job is to hand
    `create_inbox_app` to `run_http_daemon` AS A FACTORY (not to call it and pass an
    app), and that is precisely the property under test. SYNC now, because
    `run_http_daemon` owns `asyncio.run` and cannot be re-entered from a running loop.

    The factory now travels all the way into uvicorn (`Config(..., factory=True)`)
    instead of being called by the wrapper, so "in the loop" is uvicorn's `load()`
    doing it. Both halves are pinned below, because passing a factory to a Config that
    does NOT have `factory=True` still "works" — uvicorn calls it anyway and merely
    logs a warning — and that near-miss would take the app composition back out of the
    captured-signal region without failing anything.
    """
    from data_agent.learning.inbox import service

    app_sentinel = object()
    built: list[object] = []

    def _factory():
        asyncio.get_running_loop()  # raises RuntimeError outside a loop, as acouchbase does
        built.append(app_sentinel)
        return app_sentinel

    monkeypatch.setattr(service, "create_inbox_app", _factory)
    module = _load_inbox_entrypoint()
    monkeypatch.setattr(module, "configure_daemon_process", lambda *a, **k: None)
    monkeypatch.setenv("INBOX_SERVICE_HOST", "0.0.0.0")
    monkeypatch.setenv("INBOX_SERVICE_PORT", "8100")

    recorded: dict = {}
    _stub_uvicorn(monkeypatch, recorded, started=True)

    assert module.main() == 0

    assert built == [app_sentinel]
    # Handed over as a callable, declared as one, and only THEN called: the app uvicorn
    # serves is the one `load()` just built, not a stale import-time object (which is
    # what the old module-level `app` would have supplied).
    assert recorded["factory_arg"] is _factory
    assert recorded["factory"] is True
    assert recorded["app"] is app_sentinel
    assert (recorded["host"], recorded["port"]) == ("0.0.0.0", 8100)
    assert recorded["served"] is True


def test_a_failed_boot_exits_nonzero_like_uvicorn_run(monkeypatch):
    """Driving `uvicorn.Server` by hand loses the one thing `uvicorn.run()` does AFTER
    serving. A lifespan `startup` hook that raises does NOT raise out of `serve()` —
    uvicorn logs "Application startup failed. Exiting." and returns normally — and
    `uvicorn.run` turns that silent return into `sys.exit(STARTUP_FAILURE)`.

    Without the check, a process that never served a request exits 0 and Kubernetes
    marks the pod `Completed` instead of restarting it. Pinned at 3 because that is
    uvicorn's own constant; matching it keeps the two ways of running this app
    indistinguishable to whatever reads the exit code. The code is now RETURNED
    (`__main__` does `raise SystemExit(main())`) rather than raised from inside the
    serve helper — same number, same reader, one fewer control-flow shape.

    Stubbed rather than driven through a real failing hook because the assertion is
    about OUR branch, not uvicorn's: the stub reproduces the trap exactly (`serve()`
    returns None, `started` is False), which a hook that raised out of `serve()` would
    not. That uvicorn really returns normally on this path was confirmed against a live
    server; the inbox app has no `startup` hook to break, which is why this is a forward
    guard (see `data_agent/http_daemon.py`).
    """
    from data_agent.learning.inbox import service

    monkeypatch.setattr(service, "create_inbox_app", lambda: object())
    module = _load_inbox_entrypoint()
    monkeypatch.setattr(module, "configure_daemon_process", lambda *a, **k: None)

    _stub_uvicorn(monkeypatch, {}, started=False)

    assert module.main() == STARTUP_FAILURE == 3
