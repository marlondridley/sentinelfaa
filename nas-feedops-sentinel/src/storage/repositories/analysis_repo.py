"""
Repository for agent-generated analysis: Critic verdicts (`critic_results`) and
Planner answers (`planner_results`).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

import asyncpg

from src.logging_config import get_logger
from src.models import CriticResult, PlannerResult

_log = get_logger(__name__)


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
                alert_id, confidence, reasoning, citations, pattern, escalate, evaluated_at, bluf{columns}
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8{params})
            ON CONFLICT (alert_id) DO UPDATE SET
                bluf = EXCLUDED.bluf,
                confidence = EXCLUDED.confidence,
                reasoning = EXCLUDED.reasoning,
                citations = EXCLUDED.citations,
                pattern = EXCLUDED.pattern,
                escalate = EXCLUDED.escalate,
                evaluated_at = EXCLUDED.evaluated_at{update};
        """
        values = (
            result.alert_id, result.confidence, result.reasoning, json.dumps(result.citations),
            result.pattern, bool(result.escalate), result.evaluated_at, result.bluf,
        )
        try:
            await self._pool.execute(
                query.format(columns=", call_id", params=", $9", update=",\n                call_id = EXCLUDED.call_id"),
                *values, result.call_id,
            )
        except asyncpg.UndefinedColumnError:
            # A database the provenance migration has not reached: the verdict still saves, without its call id.
            _log.warning("critic_results_call_id_column_missing_run_migrations")
            await self._pool.execute(query.format(columns="", params="", update=""), *values)

    async def get_critic_verdicts_since(self, since: datetime) -> list[dict]:
        """
        Critic results evaluated since `since`, each with the status of the
        staged action raised for the same alert (None if there is none).

        A model verdict that passed the output guardrail is the only kind with
        a confidence; degraded results (disabled, errored, rejected) have none.
        """
        rows = await self._pool.fetch(
            """SELECT c.alert_id, c.confidence, c.escalate, c.reasoning, c.evaluated_at,
                      s.status AS action_status, s.decided_by, s.action_id, s.airport_code, s.alert_type
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

    async def get_latest_verdicts_for_actions(self, action_ids: list[str]) -> dict[str, dict]:
        """
        For each review, the Critic's newest verdict on any reading it covers, with how many of its
        readings the Critic assessed (`verdicts`) and how many of those said escalate (`escalations`).
        A review covers every reading of its condition; its own alert_id is only the first.
        """
        if not action_ids:
            return {}
        rows = await self._pool.fetch(
            """SELECT DISTINCT ON (x.action_id)
                      x.action_id, c.alert_id, c.confidence, c.escalate, c.pattern, c.bluf, c.evaluated_at,
                      count(*) OVER (PARTITION BY x.action_id) AS verdicts,
                      count(*) FILTER (WHERE c.escalate) OVER (PARTITION BY x.action_id) AS escalations
               FROM staged_action_alerts x JOIN critic_results c ON c.alert_id = x.alert_id
               WHERE x.action_id = ANY($1::text[])
               ORDER BY x.action_id, c.evaluated_at DESC""",
            list(action_ids),
        )
        return {r["action_id"]: dict(r) for r in rows}

    async def get_recent_critic_results(self, limit: int = 50) -> list[dict]:
        # NOTE: column list mirrors the full critic_results schema, including the citations
        # JSONB blob (dropping it here requires confirming the dashboard doesn't render it).
        rows = await self._pool.fetch(
            """SELECT alert_id, confidence, reasoning, citations, pattern, escalate, bluf, evaluated_at
               FROM critic_results ORDER BY evaluated_at DESC LIMIT $1""",
            limit,
        )
        return [dict(r) for r in rows]
