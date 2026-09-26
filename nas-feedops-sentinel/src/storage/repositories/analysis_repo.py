"""
Repository for agent-generated analysis: Critic verdicts (`critic_results`) and
Planner answers (`planner_results`).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

import asyncpg

from src.models import CriticResult, PlannerResult


class AnalysisRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def save_planner_result(self, result: PlannerResult) -> None:
        query = """
            INSERT INTO planner_results (
                result_id, question, answer, sub_queries, confidence,
                citations, recommended_actions, evaluated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (result_id) DO UPDATE SET
                question = EXCLUDED.question,
                answer = EXCLUDED.answer,
                sub_queries = EXCLUDED.sub_queries,
                confidence = EXCLUDED.confidence,
                citations = EXCLUDED.citations,
                recommended_actions = EXCLUDED.recommended_actions,
                evaluated_at = EXCLUDED.evaluated_at;
        """
        await self._pool.execute(
            query,
            str(uuid.uuid4()), result.question, result.answer, json.dumps(result.sub_queries),
            result.confidence, json.dumps(result.citations), json.dumps(result.recommended_actions),
            result.evaluated_at,
        )

    async def get_recent_planner_results(self, limit: int = 10) -> list[dict]:
        rows = await self._pool.fetch(
            # Explicit column list -- see EventsRepository.get_recent_events()'s note on
            # why this repo avoids SELECT * on frequently-polled dashboard reads.
            """SELECT result_id, question, answer, sub_queries, confidence, citations,
                      recommended_actions, evaluated_at
               FROM planner_results ORDER BY evaluated_at DESC LIMIT $1""",
            limit,
        )
        return [dict(r) for r in rows]

    async def save_critic_result(self, result: CriticResult) -> None:
        query = """
            INSERT INTO critic_results (
                alert_id, confidence, reasoning, citations, pattern, escalate, evaluated_at, bluf
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (alert_id) DO UPDATE SET
                bluf = EXCLUDED.bluf,
                confidence = EXCLUDED.confidence,
                reasoning = EXCLUDED.reasoning,
                citations = EXCLUDED.citations,
                pattern = EXCLUDED.pattern,
                escalate = EXCLUDED.escalate,
                evaluated_at = EXCLUDED.evaluated_at;
        """
        await self._pool.execute(
            query,
            result.alert_id, result.confidence, result.reasoning, json.dumps(result.citations),
            result.pattern, bool(result.escalate), result.evaluated_at, result.bluf,
        )

    async def get_critic_verdicts_since(self, since: datetime) -> list[dict]:
        """
        Critic results evaluated since `since`, each with the status of the
        staged action raised for the same alert (None if there is none).

        A model verdict that passed the output guardrail is the only kind with
        a confidence; degraded results (disabled, errored, rejected) have none.
        """
        rows = await self._pool.fetch(
            """SELECT c.alert_id, c.confidence, c.escalate, c.reasoning, c.evaluated_at,
                      s.status AS action_status, s.decided_by
               FROM critic_results c
               LEFT JOIN staged_actions s ON s.alert_id = c.alert_id
               WHERE c.evaluated_at >= $1""",
            since,
        )
        return [dict(r) for r in rows]

    async def get_critic_verdicts_for_alerts(self, alert_ids: list[str]) -> list[dict]:
        """The Critic's result for each of these alerts, newest first."""
        if not alert_ids:
            return []
        rows = await self._pool.fetch(
            """SELECT alert_id, confidence, escalate, pattern, bluf, evaluated_at
               FROM critic_results WHERE alert_id = ANY($1::text[]) ORDER BY evaluated_at DESC""",
            list(alert_ids),
        )
        return [dict(r) for r in rows]

    async def get_recent_critic_results(self, limit: int = 50) -> list[dict]:
        # NOTE: column list mirrors the full critic_results schema, including the citations
        # JSONB blob (dropping it here requires confirming the dashboard doesn't render it).
        rows = await self._pool.fetch(
            """SELECT alert_id, confidence, reasoning, citations, pattern, escalate, bluf, evaluated_at
               FROM critic_results ORDER BY evaluated_at DESC LIMIT $1""",
            limit,
        )
        return [dict(r) for r in rows]
