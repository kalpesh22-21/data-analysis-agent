"""Suppress successful probe access lines without hiding failures or API traffic."""

import logging

PROBE_PATHS = frozenset(('/ready',))


class HealthyProbeAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn supplies (client, method, path, HTTP version, status). Unknown
        # formats must remain visible rather than being guessed from message text.
        if record.name != "uvicorn.access" or record.levelno >= logging.WARNING:
            return True
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _, method, path, _, status = args
        if method not in ("GET", "HEAD") or not isinstance(path, str):
            return True
        if not isinstance(status, int):
            return True
        return not (200 <= status < 300 and path.split("?", 1)[0] in PROBE_PATHS)


def install_probe_access_filter() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, HealthyProbeAccessFilter) for item in logger.filters):
        logger.addFilter(HealthyProbeAccessFilter())
