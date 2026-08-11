"""The blocking read must outlive the socket, not race it.

`XREADGROUP ... BLOCK n` sets a deadline on the SERVER; the client sets a second
one on the same read. redis-py 8 defaults `socket_timeout` to 5s and
`LEARNING_BLOCK_MS` defaults to 5000, so at the SHIPPED configuration the two fire
together and the socket usually wins: every idle consume cycle raised
`redis.exceptions.TimeoutError`, was caught by `run_forever`, and logged a full
traceback for a cycle in which nothing had gone wrong.

These are hermetic: `from_url` does not connect, so the derived timeout can be read
straight off the connection pool with no server. The live counterpart (an idle
consume loop that raises nothing) is
`tests/integration/test_learning_redis_live.py::test_idle_consume_at_the_default_block_does_not_time_out`.
"""

from __future__ import annotations

import pytest

from data_agent.learning.config import LearningSettings
from data_agent.learning.redis_queue import (
    REDIS_AVAILABLE,
    RedisStreamsLearningQueue,
    _socket_timeout_seconds,
)

pytestmark = pytest.mark.skipif(
    not REDIS_AVAILABLE,
    reason="Requires the 'redis' package (no server — `from_url` does not connect).",
)


def _client_socket_timeout(settings: LearningSettings) -> float | None:
    queue = RedisStreamsLearningQueue.from_settings(settings)
    return queue._redis.connection_pool.connection_kwargs.get("socket_timeout")


def test_the_socket_outlives_the_block_at_the_shipped_defaults() -> None:
    """The exact configuration that failed: nothing set, everything defaulted."""
    settings = LearningSettings(_env_file=None)
    assert settings.learning_block_ms == 5000  # the value the default raced

    timeout = _client_socket_timeout(settings)
    assert timeout is not None
    assert timeout > settings.learning_block_ms / 1000.0


def test_the_derived_timeout_scales_with_a_tuned_block() -> None:
    """A margin, not a constant: an operator raising BLOCK must not have to know
    about a second timeout to keep the loop quiet."""
    settings = LearningSettings(_env_file=None, learning_block_ms=30_000)
    assert _client_socket_timeout(settings) == pytest.approx(35.0)


def test_block_forever_gets_no_read_deadline() -> None:
    """`BLOCK 0` is block-FOREVER to Redis, so ANY finite socket timeout is certain
    to fire. That configuration must have no read deadline at all."""
    settings = LearningSettings(_env_file=None, learning_block_ms=0)
    assert _socket_timeout_seconds(0) is None
    assert _client_socket_timeout(settings) is None


def test_connect_stays_bounded_even_when_the_read_deadline_is_removed() -> None:
    """The `block_ms=0` branch removes the READ deadline only. `socket_connect_timeout`
    is a separate knob, so an unreachable host still fails fast instead of hanging
    the daemon at startup — the cost of an infinite block is bounded to reads."""
    settings = LearningSettings(_env_file=None, learning_block_ms=0)
    queue = RedisStreamsLearningQueue.from_settings(settings)
    kwargs = queue._redis.connection_pool.connection_kwargs
    assert kwargs.get("socket_connect_timeout") is None  # left at redis-py's 5s default

    from redis.asyncio.connection import DEFAULT_SOCKET_CONNECT_TIMEOUT

    assert DEFAULT_SOCKET_CONNECT_TIMEOUT is not None
