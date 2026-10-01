"""
Suite-wide fixtures.

The storage pool is per event loop (src/storage/pool.py), and pytest-asyncio
gives every async test a fresh loop. Without this fixture each async test
that touches storage would leave a pool of open connections behind on a loop
that is about to be closed, and a full run would climb toward Postgres's
max_connections. Closing the test loop's pool on the way out keeps the suite
at one live pool per loop that is actually running.

Tests that never touch storage have no pool on their loop, and
close_db_pool() is a no-op for them.
"""
from __future__ import annotations

import pytest

from src.storage.pool import close_db_pool


@pytest.fixture(autouse=True)
def _fresh_circuit_breakers():
    """Breakers are process-wide; one test's failing gateway must not open another test's circuit."""
    from src import llm_guard

    llm_guard.reset_breakers()
    yield
    llm_guard.reset_breakers()


@pytest.fixture(autouse=True)
async def _close_this_loops_db_pool():
    yield
    await close_db_pool()
