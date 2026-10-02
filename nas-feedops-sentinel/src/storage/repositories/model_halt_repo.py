"""
Repository for the runtime halt of the AI layer (`model_halt` table, one row).

The row is read before every model call and written by a supervisor's halt or resume, or by the automatic
trip (src/model_halt.py). Every update is audited by a trigger in schema.py, not by this code, and a second
trigger refuses DELETE, so the row cannot go missing.
"""
from __future__ import annotations

import asyncpg


class ModelHaltRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def get(self) -> dict | None:
        """The state, or None if the row is absent (a database the migrate job has not reached)."""
        row = await self._pool.fetchrow(
            "SELECT halted, reason, source, actor, changed_at FROM model_halt WHERE id = 1")
        return dict(row) if row else None

    async def set(self, *, halted: bool, reason: str, source: str, actor: str) -> dict:
        """Write the state and return it. The audit row is the trigger's."""
        row = await self._pool.fetchrow(
            """UPDATE model_halt SET halted = $1, reason = $2, source = $3, actor = $4, changed_at = now()
               WHERE id = 1 RETURNING halted, reason, source, actor, changed_at""",
            halted, reason, source, actor,
        )
        if row is None:
            raise RuntimeError("model_halt has no row: run the migrate job")
        return dict(row)
