"""
Repository for the per-call model record (`model_calls` table).

One row per logical model call, written by src/model_telemetry.py as the
durable projection of the call's OpenTelemetry chat span: the policy
decision, the outcome class, wall time, tokens and cost. No prompt or
response text is kept. The table is append-only (trigger in schema.py) and
the application role may only SELECT and INSERT it (src/storage/migrate.py).
"""
from __future__ import annotations

from datetime import datetime

import asyncpg

_COLUMNS = (
    "call_id, created_at, correlation_id, trace_id, subject, role, model_alias, served_model, provider, "
    "outcome, attempts, repaired, latency_ms, prompt_tokens, completion_tokens, cost_usd, "
    "policy_action, policy_rule, error_type"
)


class ModelCallsRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def insert(self, row: dict) -> None:
        await self._pool.execute(
            f"""
            INSERT INTO model_calls ({_COLUMNS})
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19)
            ON CONFLICT (call_id) DO NOTHING
            """,
            row["call_id"], row["created_at"], row.get("correlation_id"), row.get("trace_id"),
            row.get("subject"), row["role"], row["model_alias"], row.get("served_model"), row["provider"],
            row["outcome"], row["attempts"],
            row.get("repaired", False), row["latency_ms"], row.get("prompt_tokens"),
            row.get("completion_tokens"), row.get("cost_usd"), row["policy_action"],
            row.get("policy_rule"), row.get("error_type"),
        )

    async def get_since(self, since: datetime) -> list[dict]:
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM model_calls WHERE created_at >= $1 ORDER BY created_at",
            since,
        )
        return [dict(r) for r in rows]

    async def get_for_subjects(self, subjects: list[str]) -> list[dict]:
        """Every call made about any of `subjects` (investigation or alert ids)."""
        if not subjects:
            return []
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM model_calls WHERE subject = ANY($1::text[]) ORDER BY created_at",
            list(subjects),
        )
        return [dict(r) for r in rows]
