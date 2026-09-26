"""
Repository for the Execution Gate's HITL staged-action workflow (`staged_actions`
table) plus its audit trail.

create() and the approve/reject path in _decide() each touch two tables
(staged_actions + action_audit_log) as one logical unit; both use UnitOfWork so a
crash mid-write can't leave one table updated without the other, and _decide() takes
`SELECT ... FOR UPDATE` inside that transaction so two concurrent approve/reject
calls on the same action can't both pass the already-decided check before either
commits -- see unit_of_work.py's docstring for the full reasoning. This closes a real
gap in the original single-file storage.py, where the UPDATE and the audit INSERT
were two independent, unlocked pool.execute() calls.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncpg

from src.models import StagedAction
from src.storage.repositories.audit_repo import AuditLogRepository
from src.storage.unit_of_work import UnitOfWork

_STAGED_ACTION_COLUMNS = """
    action_id, alert_id, event_id, airport_code, service_name, severity, alert_type,
    recommended_action, rationale, critic_confidence, critic_pattern, status,
    idempotency_key, created_at, decided_at, decided_by, decision_note,
    overridden_recommendation, overridden_severity, condition_key,
    (SELECT count(*) FROM staged_action_alerts x WHERE x.action_id = staged_actions.action_id) AS occurrences,
    (SELECT min(attached_at) FROM staged_action_alerts x WHERE x.action_id = staged_actions.action_id) AS first_seen_at,
    (SELECT max(attached_at) FROM staged_action_alerts x WHERE x.action_id = staged_actions.action_id) AS last_seen_at
"""  # every column _row_to_staged_action() reads; kept as one constant so the
# get_pending/get_recent queries and the mapper stay in sync as an explicit contract
# instead of SELECT *. The idempotency-key lookup and the decide-path fetches below
# intentionally keep SELECT * (matching the original storage.py) since they feed
# full-object reconstruction on the write path rather than a dashboard hot-poll read.


def _row_to_staged_action(row) -> StagedAction:
    d = dict(row)
    return StagedAction(
        action_id=d["action_id"],
        alert_id=d.get("alert_id"),
        event_id=d.get("event_id"),
        airport_code=d.get("airport_code"),
        service_name=d.get("service_name"),
        severity=d.get("severity", "CRITICAL"),
        alert_type=d.get("alert_type"),
        recommended_action=d.get("recommended_action") or "",
        rationale=d.get("rationale") or "",
        critic_confidence=d.get("critic_confidence"),
        critic_pattern=d.get("critic_pattern"),
        status=d.get("status", "PENDING"),
        idempotency_key=d.get("idempotency_key") or "",
        created_at=d["created_at"],
        decided_at=d.get("decided_at"),
        decided_by=d.get("decided_by"),
        decision_note=d.get("decision_note"),
        overridden_recommendation=d.get("overridden_recommendation"),
        overridden_severity=d.get("overridden_severity"),
        condition_key=d.get("condition_key"),
        occurrences=d.get("occurrences") or 1,
        first_seen_at=d.get("first_seen_at"),
        last_seen_at=d.get("last_seen_at"),
    )


class StagedActionsRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool
        self._audit = AuditLogRepository(pool)

    async def create(
        self,
        action: StagedAction,
        note: str = "staged from CRITICAL alert",
        *,
        condition_window: timedelta | None = None,
    ) -> StagedAction:
        """
        Stage `action` for review, or return the action that already covers it.

        Every CRITICAL alert an action covers is a row in staged_action_alerts,
        so an alert is staged at most once. With `condition_window`, an alert
        joins the latest action for its condition when that condition last
        fired within the window -- one review per condition, not per reading --
        and a new action is opened only after that much quiet. A per-condition
        advisory lock serialises this, so concurrent passes cannot open two.

        The returned action is the one covering the alert: `action` itself only
        when this call opened it. Opening writes the "(new) -> PENDING" audit
        row in the same transaction; ``ON CONFLICT (idempotency_key) DO NOTHING``
        still returns the winner's action to a concurrent loser.
        """
        if not action.idempotency_key:
            raise ValueError("StagedAction.idempotency_key is required")

        async with UnitOfWork(self._pool) as uow:
            conn = uow.conn
            if condition_window is not None and action.condition_key:
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", action.condition_key)
            if action.alert_id:
                covering = await conn.fetchval(
                    "SELECT action_id FROM staged_action_alerts WHERE alert_id = $1", action.alert_id)
                if covering:
                    return await self._fetch(conn, covering)
            if condition_window is not None and action.condition_key:
                ongoing = await conn.fetchval(
                    """SELECT s.action_id FROM staged_actions s
                       JOIN staged_action_alerts x ON x.action_id = s.action_id
                       WHERE s.condition_key = $1
                       GROUP BY s.action_id, s.created_at
                       HAVING max(x.attached_at) >= $2
                       ORDER BY s.created_at DESC LIMIT 1""",
                    action.condition_key, action.created_at - condition_window,
                )
                if ongoing:
                    await self._cover(conn, action.alert_id, ongoing, action.created_at)
                    return await self._fetch(conn, ongoing)

            inserted = await conn.fetchval(
                """
                INSERT INTO staged_actions (
                    action_id, alert_id, event_id, airport_code, service_name,
                    severity, alert_type, recommended_action, rationale,
                    critic_confidence, critic_pattern, status, idempotency_key,
                    created_at, decided_at, decided_by, decision_note, condition_key
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING action_id
                """,
                action.action_id, action.alert_id, action.event_id, action.airport_code, action.service_name,
                action.severity, action.alert_type, action.recommended_action, action.rationale,
                action.critic_confidence, action.critic_pattern, action.status, action.idempotency_key,
                action.created_at, action.decided_at, action.decided_by, action.decision_note, action.condition_key,
            )
            if inserted is None:
                existing = await conn.fetchval(
                    "SELECT action_id FROM staged_actions WHERE idempotency_key = $1", action.idempotency_key)
                return await self._fetch(conn, existing)

            await self._cover(conn, action.alert_id, action.action_id, action.created_at)
            await self._audit.write(
                conn, action.action_id, previous_status="(new)", new_status=action.status,
                actor="system", note=note,
            )
        return action.model_copy(update={"occurrences": 1, "first_seen_at": action.created_at,
                                         "last_seen_at": action.created_at})

    @staticmethod
    async def _cover(conn, alert_id: str | None, action_id: str, at: datetime) -> None:
        if alert_id:
            await conn.execute(
                """INSERT INTO staged_action_alerts (alert_id, action_id, attached_at) VALUES ($1, $2, $3)
                   ON CONFLICT (alert_id) DO NOTHING""",
                alert_id, action_id, at,
            )

    @staticmethod
    async def _fetch(conn, action_id: str) -> StagedAction:
        row = await conn.fetchrow(
            f"SELECT {_STAGED_ACTION_COLUMNS} FROM staged_actions WHERE action_id = $1", action_id)
        return _row_to_staged_action(row)

    async def covered_alert_ids(self, action_id: str) -> list[str]:
        """The CRITICAL alerts this review covers, oldest first."""
        rows = await self._pool.fetch(
            "SELECT alert_id FROM staged_action_alerts WHERE action_id = $1 ORDER BY attached_at, alert_id",
            action_id)
        return [r["alert_id"] for r in rows]

    async def merge_pending_duplicates(self, condition_window: timedelta) -> int:
        """
        Fold PENDING actions of one condition, staged within `condition_window`
        of each other, into the earliest: its readings move to it, and each
        folded action becomes MERGED with an audit row naming the survivor.
        Brings actions staged before one-review-per-condition (2026-09-25: 340
        reviews for two low-RVR episodes) to it; idempotent. Returns the number
        merged.
        """
        merged = 0
        async with UnitOfWork(self._pool) as uow:
            conn = uow.conn
            rows = await conn.fetch(
                """SELECT action_id, condition_key, created_at FROM staged_actions
                   WHERE status = 'PENDING' AND condition_key IS NOT NULL
                   ORDER BY condition_key, created_at, action_id FOR UPDATE"""
            )
            survivor, survivor_key, last_at = None, None, None
            now = datetime.now(timezone.utc)
            for r in rows:
                if r["condition_key"] == survivor_key and r["created_at"] - last_at <= condition_window:
                    await conn.execute(
                        "UPDATE staged_action_alerts SET action_id = $1 WHERE action_id = $2",
                        survivor, r["action_id"])
                    note = f"merged into {survivor}: the same condition ({survivor_key}), one review"
                    await conn.execute(
                        """UPDATE staged_actions SET status = 'MERGED', decided_at = $2, decided_by = 'system',
                           decision_note = $3 WHERE action_id = $1""",
                        r["action_id"], now, note)
                    await self._audit.write(conn, r["action_id"], previous_status="PENDING",
                                            new_status="MERGED", actor="system", note=note)
                    merged += 1
                else:
                    survivor, survivor_key = r["action_id"], r["condition_key"]
                last_at = r["created_at"]
        return merged

    async def get_pending(self) -> list[StagedAction]:
        rows = await self._pool.fetch(
            f"SELECT {_STAGED_ACTION_COLUMNS} FROM staged_actions WHERE status = 'PENDING' ORDER BY created_at DESC"
        )
        return [_row_to_staged_action(r) for r in rows]

    async def get_by_ids(self, action_ids: list[str]) -> list[StagedAction]:
        if not action_ids:
            return []
        rows = await self._pool.fetch(
            f"SELECT {_STAGED_ACTION_COLUMNS} FROM staged_actions WHERE action_id = ANY($1::text[])",
            list(action_ids),
        )
        return [_row_to_staged_action(r) for r in rows]

    async def get_recent(self, limit: int = 25) -> list[StagedAction]:
        rows = await self._pool.fetch(
            f"SELECT {_STAGED_ACTION_COLUMNS} FROM staged_actions ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        return [_row_to_staged_action(r) for r in rows]

    async def _decide(self, action_id: str, new_status: str, actor: str, note: str) -> StagedAction | None:
        if new_status not in ("APPROVED", "REJECTED"):
            raise ValueError(f"new_status must be APPROVED or REJECTED, got {new_status}")

        async with UnitOfWork(self._pool) as uow:
            action, _ = await self.decide_in(uow.conn, action_id, new_status, actor, note)
            return action

    async def decide_in(
        self,
        conn: asyncpg.Connection,
        action_id: str,
        new_status: str,
        actor: str,
        note: str,
        *,
        overridden_recommendation: str | None = None,
        overridden_severity: str | None = None,
    ) -> tuple[StagedAction | None, bool]:
        """
        Record a decision inside the caller's transaction.

        Returns the action as it now stands and whether this call changed it --
        False when it does not exist or was already decided.
        """
        if new_status not in ("APPROVED", "REJECTED", "OVERRIDDEN"):
            raise ValueError(f"new_status must be APPROVED, REJECTED or OVERRIDDEN, got {new_status}")

        row = await conn.fetchrow("SELECT * FROM staged_actions WHERE action_id = $1 FOR UPDATE", action_id)
        if row is None:
            return None, False

        current_status = row["status"]
        if current_status in ("APPROVED", "REJECTED", "OVERRIDDEN", "EXPIRED", "MERGED"):
            return _row_to_staged_action(row), False

        now = datetime.now(timezone.utc)
        if new_status == "OVERRIDDEN":
            await conn.execute(
                """UPDATE staged_actions
                   SET status = 'OVERRIDDEN', decided_at = $1, decided_by = $2, decision_note = $3,
                       overridden_recommendation = $4, overridden_severity = $5
                   WHERE action_id = $6""",
                now, actor, note, overridden_recommendation, overridden_severity, action_id,
            )
        else:
            await conn.execute(
                """UPDATE staged_actions
                   SET status = $1, decided_at = $2, decided_by = $3, decision_note = $4
                   WHERE action_id = $5""",
                new_status, now, actor, note, action_id,
            )
        await self._audit.write(
            conn, action_id, previous_status=current_status, new_status=new_status,
            actor=actor, note=note,
        )

        updated = await conn.fetchrow("SELECT * FROM staged_actions WHERE action_id = $1", action_id)
        return (_row_to_staged_action(updated) if updated else None), True

    async def approve(self, action_id: str, actor: str = "dashboard_operator", note: str = "") -> StagedAction | None:
        return await self._decide(action_id, "APPROVED", actor, note)

    async def reject(self, action_id: str, actor: str = "dashboard_operator", note: str = "") -> StagedAction | None:
        return await self._decide(action_id, "REJECTED", actor, note)

    async def override(
        self,
        action_id: str,
        overridden_recommendation: str | None,
        overridden_severity: str | None,
        actor: str = "dashboard_operator",
        note: str = "",
    ) -> StagedAction | None:
        """
        Sign off on a human-edited version of the AI recommendation/severity,
        instead of a plain approve/reject.

        Kept separate from _decide() because it writes two extra columns
        alongside the status transition. recommended_action/severity are left
        untouched as the AI's original suggestion; overridden_recommendation/
        overridden_severity record what the operator actually signed off on
        instead, so the audit trail can show both. Like approve/reject, this is
        purely a record of a human decision -- it never triggers any automated
        execution (broker reconnects, feed cutoffs, etc.).
        """
        async with UnitOfWork(self._pool) as uow:
            action, _ = await self.decide_in(
                uow.conn, action_id, "OVERRIDDEN", actor, note,
                overridden_recommendation=overridden_recommendation,
                overridden_severity=overridden_severity,
            )
            return action

    async def get_audit_log(self, action_id: str | None = None, limit: int = 50) -> list[dict]:
        return await self._audit.get_log(action_id, limit)
