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

from src.logging_config import get_logger

_log = get_logger(__name__)

# What the table held before the provenance migration. A rolling deploy can run this code against a
# database the migrate job has not reached yet; the row is then written without the new columns
# rather than lost (the call itself is never failed by its record).
_LEGACY_COLUMNS = (
    "call_id, created_at, correlation_id, trace_id, subject, role, model_alias, served_model, provider, "
    "outcome, attempts, repaired, latency_ms, prompt_tokens, completion_tokens, cost_usd, "
    "policy_action, policy_rule, error_type"
)
_PROVENANCE_COLUMNS = "prompt_hash, prompt_version, input_hash, redaction_count"
_COLUMNS = f"{_LEGACY_COLUMNS}, {_PROVENANCE_COLUMNS}"


class ModelCallsRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def insert(self, row: dict) -> None:
        values = (
            row["call_id"], row["created_at"], row.get("correlation_id"), row.get("trace_id"),
            row.get("subject"), row["role"], row["model_alias"], row.get("served_model"), row["provider"],
            row["outcome"], row["attempts"],
            row.get("repaired", False), row["latency_ms"], row.get("prompt_tokens"),
            row.get("completion_tokens"), row.get("cost_usd"), row["policy_action"],
            row.get("policy_rule"), row.get("error_type"),
        )
        provenance = (
            row.get("prompt_hash"), row.get("prompt_version"), row.get("input_hash"), row.get("redaction_count"),
        )
        try:
            await self._insert(_COLUMNS, values + provenance)
        except asyncpg.UndefinedColumnError:
            _log.warning("model_calls_provenance_columns_missing_run_migrations")
            await self._insert(_LEGACY_COLUMNS, values)

    async def _insert(self, columns: str, values: tuple) -> None:
        placeholders = ", ".join(f"${i}" for i in range(1, len(values) + 1))
        await self._pool.execute(
            f"INSERT INTO model_calls ({columns}) VALUES ({placeholders}) ON CONFLICT (call_id) DO NOTHING",
            *values,
        )

    async def _select(self, where_and_order: str, arg) -> list[dict]:
        try:
            rows = await self._pool.fetch(f"SELECT {_COLUMNS} FROM model_calls {where_and_order}", arg)
        except asyncpg.UndefinedColumnError:
            _log.warning("model_calls_provenance_columns_missing_run_migrations")
            rows = await self._pool.fetch(f"SELECT {_LEGACY_COLUMNS} FROM model_calls {where_and_order}", arg)
        return [dict(r) for r in rows]

    async def get_since(self, since: datetime) -> list[dict]:
        return await self._select("WHERE created_at >= $1 ORDER BY created_at", since)

    async def get_for_subjects(self, subjects: list[str]) -> list[dict]:
        """Every call made about any of `subjects` (investigation or alert ids)."""
        if not subjects:
            return []
        return await self._select("WHERE subject = ANY($1::text[]) ORDER BY created_at", list(subjects))
