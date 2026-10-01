"""
asyncpg connection pool lifecycle, shared by every repository.

Kept separate from storage/__init__.py's public wrapper functions (and importing
schema as a direct submodule rather than via `from src.storage import schema`) so
this module has no dependency on the package's __init__.py having finished running --
storage/__init__.py imports this module during its own initialization, and a
back-reference through the partially-initialized parent package would risk a circular
import.

One pool per event loop
-----------------------
An asyncpg pool can only be used from the event loop that created it. This
module used to cache a single process-wide pool, so whichever loop called
get_db_pool() first owned it and every other loop that touched storage failed
with "another operation is in progress" or "attached to a different loop".

This process has more than one loop by design. src/async_bridge.py keeps a
long-lived loop for synchronous callers (the dashboard and the SWIM
file scanner), while async callers bring their own: the Temporal
worker, the FastAPI server, and pytest-asyncio's per-test loop. So pools are
cached per loop. Each loop gets its own pool, created once and reused for the
life of that loop, and close_db_pool() closes the calling loop's pool.
"""
from __future__ import annotations

import asyncio
import weakref

import asyncpg

from src.config import settings
from src.storage.schema import init_schema, run_migrations

# Weak on the loop so a pool whose loop has been garbage-collected does not
# keep the entry alive. (A pool references its loop, so in practice an entry
# lives until close_db_pool() removes it; the weak key just avoids pinning
# loops that were closed without calling it.)
_POOLS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncpg.Pool]" = (
    weakref.WeakKeyDictionary()
)
_CREATE_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


async def get_db_pool() -> asyncpg.Pool:
    """Return the calling event loop's pool, creating it on first use."""
    loop = asyncio.get_running_loop()
    pool = _POOLS.get(loop)
    if pool is not None:
        return pool

    # Without the lock, two coroutines on the same loop that both reach this
    # point before either finishes create_pool() would open two pools and
    # leak one of them.
    lock = _CREATE_LOCKS.setdefault(loop, asyncio.Lock())
    async with lock:
        pool = _POOLS.get(loop)
        if pool is None:
            pool = await asyncpg.create_pool(
                dsn=settings.DATABASE_URL,
                min_size=2,
                max_size=20,
                statement_cache_size=0,  # Required for PgBouncer transaction pooling
            )
            # Deployed services run with RUN_MIGRATIONS_ON_CONNECT=false: the
            # schema is owned and migrated by `python -m src.storage.migrate`,
            # and the application role has no DDL rights to run this with.
            if settings.RUN_MIGRATIONS_ON_CONNECT:
                try:
                    await init_schema(pool)
                    await run_migrations(pool)
                except BaseException:
                    # The pool was never stored, so nothing else can close it: every retry that failed here
                    # left a pool of open connections behind.
                    await pool.close()
                    raise
            _POOLS[loop] = pool
    return pool


async def close_db_pool() -> None:
    """Gracefully close the calling event loop's pool, if it has one."""
    loop = asyncio.get_running_loop()
    pool = _POOLS.pop(loop, None)
    _CREATE_LOCKS.pop(loop, None)
    if pool is not None:
        await pool.close()
