import asyncio

import pytest

from data_agent.sqlparse.scratch_namespace import (
    bind_scratch_database,
    reset_scratch_database,
    scratch_databases,
)


async def test_api_namespaces_are_isolated_between_concurrent_sessions():
    async def request(database):
        token = bind_scratch_database(database)
        try:
            await asyncio.sleep(0)
            assert scratch_databases() == frozenset({'scratch', database})
        finally:
            reset_scratch_database(token)
    await asyncio.gather(request('first_uploads'), request('second_uploads'))


def test_missing_metadata_does_not_guess_physical_database():
    token = bind_scratch_database(None)
    try:
        assert scratch_databases() == frozenset({'scratch'})
    finally:
        reset_scratch_database(token)


@pytest.mark.parametrize('value', ['bad.db', 'bad;sql', '', 123])
def test_invalid_api_namespace_is_rejected(value):
    with pytest.raises(ValueError):
        bind_scratch_database(value)
