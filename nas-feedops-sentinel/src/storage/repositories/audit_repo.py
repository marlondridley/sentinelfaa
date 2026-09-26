"""
Shared write/read path for the `action_audit_log` table.

Both staged-action decisions and operational-ticket transitions append to this same
table (a ticket transition is recorded under the synthetic action_id
`"ticket:{ticket_id}"`), so the insert and the read-back live here once instead of
being duplicated across staged_actions_repo.py and tickets_repo.py.
"""
from __future__ import annotations

import asyncpg

from src.models import ActionAuditEntry


class AuditLogRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def write(
        self,
        conn: asyncpg.Connection | asyncpg.Pool,
        action_id: str,
        previous_status: str,
        new_status: str,
        actor: str,
        note: str = "",
    ) -> None:
        """
        Append one audit entry.

        Pass an active UnitOfWork's `conn` when this write must be atomic with another
        statement -- e.g. the status UPDATE it is recording -- rather than the bare
        pool, which would run this insert as its own separate implicit transaction
        and could commit even if the paired UPDATE later fails.
        """
        entry = ActionAuditEntry(
            action_id=action_id,
            previous_status=previous_status,
            new_status=new_status,
            actor=actor,
            note=note,
        )
        query = """
            INSERT INTO action_audit_log (
                audit_id, action_id, previous_status, new_status, actor, note, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
        """
        await conn.execute(
            query,
            entry.audit_id, entry.action_id, entry.previous_status, entry.new_status,
            entry.actor, entry.note, entry.created_at,
        )

    async def get_log(self, action_id: str | None = None, limit: int = 50) -> list[dict]:
        columns = "audit_id, action_id, previous_status, new_status, actor, note, created_at"
        if action_id:
            rows = await self._pool.fetch(
                f"""SELECT {columns} FROM action_audit_log WHERE action_id = $1
                   ORDER BY created_at DESC LIMIT $2""",
                action_id, limit,
            )
        else:
            rows = await self._pool.fetch(
                f"SELECT {columns} FROM action_audit_log ORDER BY created_at DESC LIMIT $1", limit
            )
        return [dict(r) for r in rows]

    async def get_for_actions(self, action_ids: list[str]) -> list[dict]:
        """Every entry for these actions, oldest first."""
        if not action_ids:
            return []
        rows = await self._pool.fetch(
            """SELECT audit_id, action_id, previous_status, new_status, actor, note, created_at
               FROM action_audit_log WHERE action_id = ANY($1::text[]) ORDER BY created_at, audit_id""",
            list(action_ids),
        )
        return [dict(r) for r in rows]
