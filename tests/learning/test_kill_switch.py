"""D58c-learning-kill-switch-halts-writes + D58c-kill-switch-reads-unaffected
(design §7, matrix rows 1 & 2, task item 10).

`LEARNING_ENABLED` is read UNCACHED, per cycle, from BOTH the process env AND a
`.env` file (process env wins — MEDIUM-2):
  - disabled  => sweeper enqueues NOTHING and consumer processes NOTHING;
  - flipping it back on resumes on the very next cycle (no restart);
  - an UNRECOGNIZED value is fail-safe DISABLED; unset/blank is ENABLED.
Reads are unaffected STRUCTURALLY: no request-path module imports the flag or
the learning package.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from data_agent.learning.config import (
    learning_enabled,
    unrecognized_learning_env_vars,
    warn_unrecognized_learning_env_vars,
)
from data_agent.learning.consumer import LearningConsumer
from data_agent.learning.models import LearningJob, LearningStatus, compute_content_hash
from data_agent.learning.sweeper import LearningSweeper

from .conftest import make_message

# --- learning_enabled() truth table (fresh read every call) -----------------


def test_unset_is_enabled(monkeypatch):
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    assert learning_enabled() is True


def test_blank_is_enabled(monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "   ")
    assert learning_enabled() is True


@pytest.mark.parametrize("val", ["true", "TRUE", "1", "yes", "on", " On "])
def test_truthy_values_enabled(monkeypatch, val):
    monkeypatch.setenv("LEARNING_ENABLED", val)
    assert learning_enabled() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "off", "FALSE"])
def test_falsy_values_disabled(monkeypatch, val):
    monkeypatch.setenv("LEARNING_ENABLED", val)
    assert learning_enabled() is False


@pytest.mark.parametrize("val", ["maybe", "yepp", "enabled?", "2", "yeah"])
def test_unrecognized_is_fail_safe_disabled(monkeypatch, val):
    # A typo'd override HALTS (fail-safe), it does not silently run.
    monkeypatch.setenv("LEARNING_ENABLED", val)
    assert learning_enabled() is False


def test_flip_takes_effect_without_restart(monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "false")
    assert learning_enabled() is False
    monkeypatch.setenv("LEARNING_ENABLED", "true")  # same process, no reimport
    assert learning_enabled() is True


# --- Sweeper halts enqueue --------------------------------------------------


async def test_sweeper_enqueues_nothing_when_disabled(store, queue, settings, seed_session, monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "false")
    seed_session(store, "idle-1", messages=[make_message(0, "user", "hi")])
    sweeper = LearningSweeper(store, queue, settings)

    result = await sweeper.run_once()

    assert result.disabled is True
    assert result.scanned == 0
    assert result.claimed == 0
    assert result.enqueued == 0
    assert queue.stream_length() == 0
    # No scan, no claim: the session is untouched (still active).
    assert store._docs["idle-1"].learning_status == LearningStatus.ACTIVE


async def test_consumer_processes_nothing_when_disabled(store, queue, settings, seed_session, monkeypatch):
    # Enqueue while enabled, then disable and try to consume.
    doc = seed_session(store, "sess-1", learning_status=LearningStatus.QUEUED,
                       messages=[make_message(0, "user", "hi")])
    await queue.enqueue(LearningJob.from_doc(doc, content_hash=compute_content_hash(doc)))
    monkeypatch.setenv("LEARNING_ENABLED", "false")

    consumer = LearningConsumer(store, queue, settings)
    result = await consumer.run_once()

    assert result.disabled is True
    assert result.done == 0
    assert result.dead_letters == 0
    # Work simply WAITS in the stream — no loss, no XREADGROUP.
    assert queue.new_count() == 1
    assert store._docs["sess-1"].learning_status == LearningStatus.QUEUED


async def test_reenabling_resumes_on_next_cycle(store, queue, settings, seed_session, monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "false")
    seed_session(store, "idle-1", messages=[make_message(0, "user", "hi")])
    sweeper = LearningSweeper(store, queue, settings)
    consumer = LearningConsumer(store, queue, settings)

    assert (await sweeper.run_once()).disabled is True
    assert queue.stream_length() == 0

    # Flip the switch ON — same process objects, no restart.
    monkeypatch.setenv("LEARNING_ENABLED", "true")
    sweep = await sweeper.run_once()
    assert sweep.disabled is False
    assert sweep.enqueued == 1
    consume = await consumer.run_once()
    assert consume.done == 1
    assert store._docs["idle-1"].learning_status == LearningStatus.DONE


# --- A halted loop SAYS SO (the silent no-op the wiring audit found) --------
#
# `run_once` answers a disabled switch with `disabled=True` and a `learning.disabled`
# span — and a span is invisible unless OTLP is configured, which by default it is not.
# So a consumer/sweeper held off by the kill-switch was indistinguishable, in the log,
# from a healthy idle one. The loops now log the STATE CHANGE (once per change, never
# per cycle: at a 5s idle interval a per-cycle line is ~17k/day).


class _StopLoop(BaseException):
    """Breaks out of the otherwise-infinite `run_forever`.

    A `BaseException` on purpose: both loops wrap their cycle in `except Exception`
    (MEDIUM-1, never die on a transient blip), so an ordinary exception raised to stop
    the test would be swallowed and the loop would spin forever."""


def _sleep_after(n: int):
    """An injected `sleep` that lets the loop turn *n* times, then breaks out."""
    state = {"turns": 0}

    async def _sleep(_seconds):
        state["turns"] += 1
        if state["turns"] >= n:
            raise _StopLoop
    return _sleep


async def test_consumer_run_forever_logs_the_disabled_switch_once(
    store, queue, settings, monkeypatch, caplog
):
    monkeypatch.setenv("LEARNING_ENABLED", "false")
    consumer = LearningConsumer(store, queue, settings)

    with caplog.at_level(logging.INFO, logger="data_agent.learning.consumer"):
        with pytest.raises(_StopLoop):
            await consumer.run_forever(sleep=_sleep_after(3))

    idle_lines = [r for r in caplog.records if "kill-switch is OFF" in r.getMessage()]
    assert len(idle_lines) == 1  # three cycles, ONE line
    assert idle_lines[0].levelno == logging.WARNING
    assert "LEARNING_ENABLED" in idle_lines[0].getMessage()


async def test_consumer_run_forever_logs_the_resume(
    store, queue, settings, monkeypatch, caplog
):
    """The other half of a state change: flipping the switch back on says so, so an
    operator can see the loop actually resumed rather than inferring it. NB an ENABLED
    cycle does not sleep (only a disabled one paces itself), so the stop has to come
    from the cycle, not from the injected sleep."""
    monkeypatch.setenv("LEARNING_ENABLED", "false")
    consumer = LearningConsumer(store, queue, settings)
    real_run_once = consumer.run_once
    cycles = {"n": 0}

    async def counting_run_once():
        cycles["n"] += 1
        if cycles["n"] == 2:
            monkeypatch.setenv("LEARNING_ENABLED", "true")  # operator flips it back on
        if cycles["n"] > 2:
            raise _StopLoop
        return await real_run_once()

    consumer.run_once = counting_run_once

    async def _noop_sleep(_seconds):
        return None

    with caplog.at_level(logging.INFO, logger="data_agent.learning.consumer"):
        with pytest.raises(_StopLoop):
            await consumer.run_forever(sleep=_noop_sleep)

    assert any("kill-switch is OFF" in r.getMessage() for r in caplog.records)
    assert any("RESUMED" in r.getMessage() for r in caplog.records)


async def test_sweeper_run_forever_logs_the_disabled_switch_once(
    store, queue, settings, monkeypatch, caplog
):
    monkeypatch.setenv("LEARNING_ENABLED", "false")
    sweeper = LearningSweeper(store, queue, settings)

    with caplog.at_level(logging.INFO, logger="data_agent.learning.sweeper"):
        with pytest.raises(_StopLoop):
            await sweeper.run_forever(sleep=_sleep_after(3))

    idle_lines = [r for r in caplog.records if "kill-switch is OFF" in r.getMessage()]
    assert len(idle_lines) == 1
    assert idle_lines[0].levelno == logging.WARNING


async def test_sweeper_run_forever_survives_a_failing_cycle(
    store, queue, settings, monkeypatch, caplog
):
    """The disabled-state bookkeeping sits in the `else` of the try, so a cycle that
    RAISED never reads a `result` that does not exist — the daemon still retries."""
    monkeypatch.setenv("LEARNING_ENABLED", "true")
    sweeper = LearningSweeper(store, queue, settings)

    async def _boom():
        raise RuntimeError("transient scan failure")

    sweeper.run_once = _boom

    with caplog.at_level(logging.INFO, logger="data_agent.learning.sweeper"):
        with pytest.raises(_StopLoop):
            await sweeper.run_forever(sleep=_sleep_after(2))

    assert any("sweep cycle failed" in r.getMessage() for r in caplog.records)


# --- `extra="ignore"` swallows typos: name them ------------------------------


def test_a_typod_learning_var_is_reported(monkeypatch, caplog):
    """`LEARNING_MAX_DELIVERES` is accepted by the environment, dropped by
    pydantic-settings (`extra="ignore"`), and the shipped default applies — so a knob
    the operator believes they turned does not exist. The entrypoints now name it."""
    monkeypatch.setenv("LEARNING_MAX_DELIVERES", "2")
    logger = logging.getLogger("test.envscan")

    with caplog.at_level(logging.WARNING, logger="test.envscan"):
        unknown = warn_unrecognized_learning_env_vars(logger)

    assert unknown == ("LEARNING_MAX_DELIVERES",)
    (record,) = caplog.records
    assert "LEARNING_MAX_DELIVERES" in record.getMessage()


def test_real_fields_and_the_kill_switch_are_never_reported(monkeypatch):
    """No false positives: every real field is known, and `LEARNING_ENABLED` — which
    is deliberately NOT a model field (it must never be cached) — is known-good."""
    monkeypatch.setenv("LEARNING_ENABLED", "true")
    monkeypatch.setenv("LEARNING_MAX_DELIVERIES", "7")
    monkeypatch.setenv("LEARNING_BATCH_SIZE", "3")
    monkeypatch.setenv("COUCHBASE_URL", "couchbase://x")  # a different surface
    assert unrecognized_learning_env_vars() == ()


def test_the_scan_is_scoped_to_the_learning_prefix():
    """It can only flag a variable that LOOKS like it was meant for this settings
    surface — an unrelated environment can never make it noisy."""
    assert unrecognized_learning_env_vars(
        {"PATH": "/bin", "USER_KNOWLEDGE_USERNAME": "u", "OTLP_ENDPOINT": "x"}
    ) == ()
    assert unrecognized_learning_env_vars({"LEARNING_NOPE": "1"}) == ("LEARNING_NOPE",)


# --- Reads unaffected: no request-path module imports the flag --------------


def test_request_path_never_imports_the_learning_flag():
    """Structural guarantee (design §7): the kill-switch is consulted ONLY inside
    the sweeper/consumer. No module under `runtime/` may import the learning
    package or reference `LEARNING_ENABLED`, so already-published read tools keep
    serving with the switch off."""
    runtime_root = Path(__file__).resolve().parents[2] / "src" / "data_agent" / "runtime"
    assert runtime_root.is_dir()

    offenders: list[str] = []
    for path in runtime_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "data_agent.learning" in source or "LEARNING_ENABLED" in source:
            offenders.append(str(path))

    assert offenders == [], (
        "request-path modules must not import the learning flag/package: "
        f"{offenders}"
    )


# --- MEDIUM-2: the kill-switch also honors a mounted `.env` ------------------
#
# Reading ONLY `os.environ` silently ignored a `.env` override, which fails
# DANGEROUS (an operator disabling learning via a mounted `.env` would be
# ignored). `_KillSwitchSettings` reads `.env` from the cwd too, with the process
# env taking precedence. These tests `chdir` into a tmp dir so the `.env` is
# fully controlled and never collides with the repo's.


def _write_dotenv(tmp_path, contents: str) -> None:
    (tmp_path / ".env").write_text(contents, encoding="utf-8")


def test_dotenv_false_disables(tmp_path, monkeypatch):
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    monkeypatch.chdir(tmp_path)
    _write_dotenv(tmp_path, "LEARNING_ENABLED=false\n")
    assert learning_enabled() is False


def test_dotenv_true_enables(tmp_path, monkeypatch):
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    monkeypatch.chdir(tmp_path)
    _write_dotenv(tmp_path, "LEARNING_ENABLED=true\n")
    assert learning_enabled() is True


def test_process_env_overrides_dotenv_true_wins(tmp_path, monkeypatch):
    # `.env` says OFF, but the operator exported ON in the process env → ON wins.
    monkeypatch.chdir(tmp_path)
    _write_dotenv(tmp_path, "LEARNING_ENABLED=false\n")
    monkeypatch.setenv("LEARNING_ENABLED", "true")
    assert learning_enabled() is True


def test_process_env_overrides_dotenv_false_wins(tmp_path, monkeypatch):
    # `.env` says ON, but the operator exported OFF in the process env → OFF wins.
    monkeypatch.chdir(tmp_path)
    _write_dotenv(tmp_path, "LEARNING_ENABLED=true\n")
    monkeypatch.setenv("LEARNING_ENABLED", "false")
    assert learning_enabled() is False


def test_dotenv_unrecognized_is_fail_safe_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    monkeypatch.chdir(tmp_path)
    _write_dotenv(tmp_path, "LEARNING_ENABLED=maybe\n")
    assert learning_enabled() is False


def test_no_dotenv_and_no_env_is_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env present here
    assert learning_enabled() is True


def test_dotenv_read_is_fresh_per_call_no_restart(tmp_path, monkeypatch):
    """A `.env` edit (or a process-env export layered over it) is honored on the
    NEXT call — no reimport, no restart (the switch is never cached)."""
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    monkeypatch.chdir(tmp_path)
    _write_dotenv(tmp_path, "LEARNING_ENABLED=false\n")
    assert learning_enabled() is False
    # Operator edits the mounted .env in place → true.
    _write_dotenv(tmp_path, "LEARNING_ENABLED=true\n")
    assert learning_enabled() is True
