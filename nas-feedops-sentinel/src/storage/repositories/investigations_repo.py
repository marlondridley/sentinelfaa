"""
Repository for completed investigations (`investigations` table).

A row is written once, when an investigation finishes, and never changed:
the table is append-only (trigger in schema.py), and the full structured
record is stored with its SHA-256 so a later reader can prove it is the
record the specialists produced. The hash covers record_json, the exact
canonical text; the JSONB copy exists for querying. One COMPLETE investigation per staged
action; a FAILED attempt is also kept, for audit, and does not block a retry.
"""
from __future__ import annotations

import hashlib

import asyncpg

from src.investigation.record import Investigation


class InvestigationsRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def save(self, inv: Investigation) -> bool:
        """Insert the record. Returns False if a COMPLETE one already exists for the action."""
        if inv.record_sha256 is None:
            raise ValueError("Investigation must be sealed (record_sha256 set) before it is stored")
        text = inv.canonical_json()
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != inv.record_sha256:
            raise IntegrityError("investigation changed after it was sealed")
        inserted = await self._pool.fetchval(
            """
            INSERT INTO investigations (
                investigation_id, action_id, alert_id, status, opened_at, completed_at,
                record_json, record, record_sha256
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
            ON CONFLICT DO NOTHING
            RETURNING investigation_id
            """,
            inv.investigation_id, inv.action_id, inv.alert_id, inv.status,
            inv.opened_at, inv.completed_at, text, text, inv.record_sha256,
        )
        return inserted is not None

    async def get_by_action_id(self, action_id: str) -> Investigation | None:
        row = await self._pool.fetchrow(
            """SELECT record_json, record_sha256 FROM investigations
               WHERE action_id = $1 AND status = 'COMPLETE'""",
            action_id,
        )
        return _to_record(row) if row else None

    async def get_recent(self, limit: int = 25) -> list[Investigation]:
        rows = await self._pool.fetch(
            """SELECT record_json, record_sha256 FROM investigations
               ORDER BY opened_at DESC LIMIT $1""",
            limit,
        )
        return [_to_record(r) for r in rows]


class IntegrityError(RuntimeError):
    """A stored record does not match its hash."""


def _to_record(row) -> Investigation:
    text, stored_hash = row["record_json"], row["record_sha256"]
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != stored_hash:
        raise IntegrityError("stored investigation record does not match its SHA-256")
    return Investigation.model_validate_json(text).model_copy(update={"record_sha256": stored_hash})
