import logging
import subprocess
import sys

import pytest

from data_agent.access_logging import PROBE_PATHS, HealthyProbeAccessFilter


def record(path, status=200, method="GET", *, name="uvicorn.access", level=logging.INFO):
    return logging.LogRecord(name, level, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                             ("127.0.0.1:1234", method, path, "1.1", status), None)


@pytest.mark.parametrize("path", sorted(PROBE_PATHS))
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_successful_probe_access_is_suppressed(path, method):
    assert not HealthyProbeAccessFilter().filter(record(path, method=method))
    assert not HealthyProbeAccessFilter().filter(record(path + "?probe=1", method=method))


@pytest.mark.parametrize("status", [301, 307, 400, 401, 404, 429, 500, 503])
def test_unsuccessful_probes_remain_visible(status):
    assert HealthyProbeAccessFilter().filter(record(next(iter(PROBE_PATHS)), status))


def test_normal_requests_and_application_errors_are_not_hidden():
    probe = next(iter(PROBE_PATHS))
    for entry in [record("/turn"), record("/inbox"), record("/query?path=" + probe),
                  record(probe + "/details"), record(probe, method="POST"),
                  record(probe, name="app.error"), record(probe, level=logging.ERROR)]:
        assert HealthyProbeAccessFilter().filter(entry)


def test_unrecognized_access_log_shapes_are_not_hidden():
    filt = HealthyProbeAccessFilter()
    for args in [(), ("GET", "/ready"), {"path": "/ready"}]:
        entry = record(next(iter(PROBE_PATHS)))
        entry.args = args
        assert filt.filter(entry)


def test_installed_filter_suppresses_only_successes_in_standard_output():
    script = """
import logging
import uvicorn
from data_agent.access_logging import install_probe_access_filter, HealthyProbeAccessFilter, PROBE_PATHS
uvicorn.Config(lambda: None)
install_probe_access_filter()
install_probe_access_filter()
logger = logging.getLogger('uvicorn.access')
assert sum(isinstance(f, HealthyProbeAccessFilter) for f in logger.filters) == 1
path = min(PROBE_PATHS)
for request_path, status in [(path, 200), (path, 503), ('/ordinary', 200)]:
    logger.info('%s - "%s %s HTTP/%s" %d', 'client', 'GET', request_path, '1.1', status)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    probe = min(PROBE_PATHS)
    assert f'GET {probe} HTTP/1.1" 200' not in result.stdout
    assert f'GET {probe} HTTP/1.1" 503' in result.stdout
    assert 'GET /ordinary HTTP/1.1" 200' in result.stdout
