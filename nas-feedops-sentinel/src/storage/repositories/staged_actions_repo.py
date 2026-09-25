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

from datetime import datetime, timezone

import asyncpg

from src.models import StagedAction
from src.storage.repositories.audit_repo import AuditLogRepository
from src.storage.unit_of_work import UnitOfWork

_STAGED_ACTION_COLUMNS = """
    action_id, alert_id, event_id, airport_code, service_name, severity, alert_type,
    recommended_action, rationale, critic_confidence, critic_pattern, status,
    idempotency_key, created_at, decided_at, decided_by, decision_note,
    overridden_recommendation, overridden_severity
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
    )


class StagedActionsRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool
        self._audit = AuditLogRepository(pool)

    async def create(self, action: StagedAction, note: str = "staged from CRITICAL alert") -> StagedAction:
        """
        Insert a new staged action and its "(new) -> PENDING" audit row, atomically,
        or return the action already staged under the same idempotency key.

        ``ON CONFLICT (idempotency_key) DO NOTHING`` rather than a SELECT followed
        by an INSERT. With check-then-insert, concurrent pipeline passes staging
        the same CRITICAL alert all passed the SELECT, and every one but the first
        raised UniqueViolationError out of stage_critical_alert() -- which, in
        src/pipeline.py's staging loop, abandoned every remaining alert in that
        batch. Now the losers get the winner's action back, which is the contract
        stage_critical_alert() documents, and only the call that actually inserted
        writes the audit row.
        """
        if not action.idempotency_key:
            raise ValueError("StagedAction.idempotency_key is required")

        query = """
            INSERT INTO staged_actions (
                action_id, alert_id, event_id, airport_code, service_name,
                severity, alert_type, recommended_action, rationale,
                critic_confidence, critic_pattern, status, idempotency_key,
                created_at, decided_at, decided_by, decision_note
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING action_id
        """
        async with UnitOfWork(self._pool) as uow:
            inserted = await uow.conn.fetchval(
                query,
                action.action_id, action.alert_id, action.event_id, action.airport_code, action.service_name,
                action.severity, action.alert_type, action.recommended_action, action.rationale,
                action.critic_confidence, action.critic_pattern, action.status, action.idempotency_key,
                action.created_at, action.decided_at, action.decided_by, action.decision_note,
            )
            if inserted is None:
                existing = await uow.conn.fetchrow(
                    f"SELECT {_STAGED_ACTION_COLUMNS} FROM staged_actions WHERE idempotency_key = $1",
                    action.idempotency_key,
                )
                return _row_to_staged_action(existing)

            await self._audit.write(
                uow.conn, action.action_id, previous_status="(new)", new_status=action.status,
                actor="system", note=note,
            )
        return action

    async def get_pending(self) -> list[StagedAction]:
        rows = await self._pool.fetch(
            f"SELECT {_STAGED_ACTION_COLUMNS} FROM staged_actions WHERE status = 'PENDING' ORDER BY created_at DESC"
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
            row = await uow.conn.fetchrow(
                "SELECT * FROM staged_actions WHERE action_id = $1 FOR UPDATE", action_id
            )
            if row is None:
                return None

            current_status = row["status"]
            if current_status in ("APPROVED", "REJECTED", "OVERRIDDEN", "EXPIRED"):
                return _row_to_staged_action(row)

            now = datetime.now(timezone.utc)
            await uow.conn.execute(
                """UPDATE staged_actions
                   SET status = $1, decided_at = $2, decided_by = $3, decision_note = $4
                   WHERE action_id = $5""",
                new_status, now, actor, note, action_id,
            )
            await self._audit.write(
                uow.conn, action_id, previous_status=current_status, new_status=new_status,
                actor=actor, note=note,
            )

            updated = await uow.conn.fetchrow("SELECT * FROM staged_actions WHERE action_id = $1", action_id)
            return _row_to_staged_action(updated) if updated else None

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
            row = await uow.conn.fetchrow(
                "SELECT * FROM staged_actions WHERE action_id = $1 FOR UPDATE", action_id
            )
            if row is None:
                return None

            current_status = row["status"]
            if current_status in ("APPROVED", "REJECTED", "OVERRIDDEN", "EXPIRED"):
                return _row_to_staged_action(row)

            now = datetime.now(timezone.utc)
            await uow.conn.execute(
                """UPDATE staged_actions
                   SET status = 'OVERRIDDEN', decided_at = $1, decided_by = $2, decision_note = $3,
                       overridden_recommendation = $4, overridden_severity = $5
                   WHERE action_id = $6""",
                now, actor, note, overridden_recommendation, overridden_severity, action_id,
            )
            await self._audit.write(
                uow.conn, action_id, previous_status=current_status, new_status="OVERRIDDEN",
                actor=actor, note=note,
            )

            updated = await uow.conn.fetchrow("SELECT * FROM staged_actions WHERE action_id = $1", action_id)
            return _row_to_staged_action(updated) if updated else None

    async def get_audit_log(self, action_id: str | None = None, limit: int = 50) -> list[dict]:
        return await self._audit.get_log(action_id, limit)
