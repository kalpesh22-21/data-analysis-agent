"""Request-local namespace metadata supplied by the API, never an access grant.

`scratch` is the blueprint authoring placeholder, not a configured physical database.
The API remains responsible for session ownership and all query authorization.
"""
import re
from contextvars import ContextVar, Token

_database: ContextVar[str | None] = ContextVar('api_scratch_database', default=None)


def bind_scratch_database(database: str | None) -> Token:
    if database is not None and (not isinstance(database, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', database)):
        raise ValueError('Invalid API scratch database')
    return _database.set(database)


def reset_scratch_database(token: Token) -> None:
    _database.reset(token)


def scratch_databases() -> frozenset[str]:
    database = _database.get()
    return frozenset({'scratch', database}) if database else frozenset({'scratch'})
