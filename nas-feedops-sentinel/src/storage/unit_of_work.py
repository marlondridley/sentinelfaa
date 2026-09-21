"""
Explicit transactional boundary for multi-statement writes.

Several operations in this storage layer mutate more than one row across more than
one statement as a single logical unit -- e.g. approving a staged action updates
`staged_actions` AND appends to `action_audit_log`; those two writes need to either
both land or neither does, and the read-modify-write in between needs to be safe
against two concurrent decisions racing on the same row. Before this module, each of
those call sites issued separate `pool.execute()` calls with no surrounding
transaction and no row lock, so a crash between the two writes could leave the row
updated with no matching audit entry, and two concurrent approve/reject calls on the
same action could both pass the "already decided?" check before either commits.

Usage:
    async with UnitOfWork(pool) as uow:
        row = await uow.conn.fetchrow("SELECT ... FOR UPDATE", ...)
        await uow.conn.execute("UPDATE ...")
        await uow.conn.execute("INSERT INTO action_audit_log ...")
    # Commits on clean exit, rolls back on exception -- both statements land together
    # or neither does. The FOR UPDATE row lock (added by the caller's query, not this
    # class) is what closes the concurrent-decision race: a second transaction trying
    # to SELECT ... FOR UPDATE the same row blocks until this one commits or rolls
    # back, so it always sees the post-decision status rather than racing past it.
"""
from __future__ import annotations

from types import TracebackType

import asyncpg


class UnitOfWork:
    """Acquires one connection from `pool` and wraps it in a single asyncpg transaction."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool
        self._conn_ctx = None
        self._tx_ctx = None
        self.conn: asyncpg.Connection | None = None

    async def __aenter__(self) -> "UnitOfWork":
        self._conn_ctx = self._pool.acquire()
        self.conn = await self._conn_ctx.__aenter__()
        self._tx_ctx = self.conn.transaction()
        await self._tx_ctx.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        # conn.transaction()'s __aexit__ commits when exc_type is None and rolls back
        # otherwise; the connection is released back to the pool either way.
        try:
            await self._tx_ctx.__aexit__(exc_type, exc, tb)
        finally:
            await self._conn_ctx.__aexit__(exc_type, exc, tb)
        return False
