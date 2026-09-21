"""
Repository for Watch Officer-generated operational tickets (`operational_tickets`
table), the human-facing escalation packages built from staged actions.

_transition() (used by approve/reject/mark-monitoring) wraps its UPDATE and its
action_audit_log write in one UnitOfWork transaction with `SELECT ... FOR UPDATE`,
for the same atomicity/race reason as StagedActionsRepository._decide() -- see that
class's docstring and unit_of_work.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import asyncpg

from src.models import OperationalTicket
from src.storage.repositories.audit_repo import AuditLogRepository
from src.storage.unit_of_work import UnitOfWork


def _row_to_ticket(row) -> OperationalTicket:
    d = dict(row)
    return OperationalTicket(
        ticket_id=d["ticket_id"],
        action_id=d.get("action_id"),
        alert_id=d.get("alert_id"),
        brief_id=d.get("brief_id"),
        generated_at=d["generated_at"],
        title=d.get("title") or "",
        severity=d.get("severity") or "CRITICAL",
        affected_airport=d.get("affected_airport"),
        affected_service=d.get("affected_service"),
        posture=d.get("posture") or "ACTION",
        trust_assessment=d.get("trust_assessment") or "DEGRADED",
        summary=d.get("summary") or "",
        what_changed=json.loads(d.get("what_changed_json") or "[]") if isinstance(d.get("what_changed_json"), str) else d.get("what_changed_json", []),
        why_it_matters=json.loads(d.get("why_it_matters_json") or "[]") if isinstance(d.get("why_it_matters_json"), str) else d.get("why_it_matters_json", []),
        evidence=json.loads(d.get("evidence_json") or "[]") if isinstance(d.get("evidence_json"), str) else d.get("evidence_json", []),
        recommended_review=json.loads(d.get("recommended_review_json") or "[]") if isinstance(d.get("recommended_review_json"), str) else d.get("recommended_review_json", []),
        critic_rationale=d.get("critic_rationale"),
        status=d.get("status") or "DRAFT",
        created_by_agent=d.get("created_by_agent") or "watch_officer",
        decided_by=d.get("decided_by"),
        decided_at=d.get("decided_at"),
        decision_note=d.get("decision_note"),
        idempotency_key=d.get("idempotency_key") or "",
    )


class TicketsRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool
        self._audit = AuditLogRepository(pool)

    async def create(self, ticket: OperationalTicket) -> OperationalTicket:
        """
        Insert a new ticket and record its creation in action_audit_log, atomically.

        Creation is a state transition like any other -- nothing -> DRAFT -- and
        the human-facing escalation artefact is the last thing that should be
        able to appear without a trace. _transition() already audits every later
        change under the same ``ticket:{ticket_id}`` key; this closes the gap at
        the start of the ticket's life, so the audit log alone can reconstruct
        when each ticket appeared, who produced it, and every decision after.

        The insert is ``ON CONFLICT (idempotency_key) DO NOTHING`` rather than a
        SELECT followed by an INSERT: with check-then-insert, two concurrent
        pipeline runs escalating the same staged action could both pass the
        SELECT and the second would fail on the UNIQUE constraint. Here the
        loser simply gets the winner's ticket back, and only the call that
        actually inserted writes an audit row -- so a retried or duplicated
        escalation cannot produce a second "(new) -> DRAFT" entry.
        """
        if not ticket.idempotency_key:
            raise ValueError("OperationalTicket.idempotency_key is required")

        query = """
            INSERT INTO operational_tickets (
                ticket_id, action_id, alert_id, brief_id, generated_at,
                title, severity, affected_airport, affected_service,
                posture, trust_assessment, summary,
                what_changed_json, why_it_matters_json, evidence_json,
                recommended_review_json, critic_rationale, status,
                created_by_agent, decided_by, decided_at, decision_note,
                idempotency_key
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING ticket_id
        """
        async with UnitOfWork(self._pool) as uow:
            inserted = await uow.conn.fetchval(
                query,
                ticket.ticket_id, ticket.action_id, ticket.alert_id, ticket.brief_id, ticket.generated_at,
                ticket.title, ticket.severity, ticket.affected_airport, ticket.affected_service,
                ticket.posture, ticket.trust_assessment, ticket.summary, json.dumps(ticket.what_changed),
                json.dumps(ticket.why_it_matters), json.dumps(ticket.evidence), json.dumps(ticket.recommended_review),
                ticket.critic_rationale, ticket.status, ticket.created_by_agent, ticket.decided_by,
                ticket.decided_at, ticket.decision_note, ticket.idempotency_key,
            )
            if inserted is None:
                existing = await uow.conn.fetchrow(
                    "SELECT * FROM operational_tickets WHERE idempotency_key = $1",
                    ticket.idempotency_key,
                )
                return _row_to_ticket(existing)

            await self._audit.write(
                uow.conn,
                action_id=f"ticket:{ticket.ticket_id}",
                previous_status="(new)",
                new_status=ticket.status,
                actor=ticket.created_by_agent,
                note=f"ticket created for staged action {ticket.action_id}",
            )
        return ticket

    async def save(self, ticket: OperationalTicket) -> OperationalTicket:
        query = """
            INSERT INTO operational_tickets (
                ticket_id, action_id, alert_id, brief_id, generated_at,
                title, severity, affected_airport, affected_service, posture,
                trust_assessment, summary, what_changed_json, why_it_matters_json,
                evidence_json, recommended_review_json, critic_rationale,
                status, created_by_agent, decided_by, decided_at,
                decision_note, idempotency_key
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23)
            ON CONFLICT (ticket_id) DO UPDATE SET
                action_id = EXCLUDED.action_id,
                alert_id = EXCLUDED.alert_id,
                brief_id = EXCLUDED.brief_id,
                generated_at = EXCLUDED.generated_at,
                title = EXCLUDED.title,
                severity = EXCLUDED.severity,
                affected_airport = EXCLUDED.affected_airport,
                affected_service = EXCLUDED.affected_service,
                posture = EXCLUDED.posture,
                trust_assessment = EXCLUDED.trust_assessment,
                summary = EXCLUDED.summary,
                what_changed_json = EXCLUDED.what_changed_json,
                why_it_matters_json = EXCLUDED.why_it_matters_json,
                evidence_json = EXCLUDED.evidence_json,
                recommended_review_json = EXCLUDED.recommended_review_json,
                critic_rationale = EXCLUDED.critic_rationale,
                status = EXCLUDED.status,
                created_by_agent = EXCLUDED.created_by_agent,
                decided_by = EXCLUDED.decided_by,
                decided_at = EXCLUDED.decided_at,
                decision_note = EXCLUDED.decision_note;
        """
        await self._pool.execute(
            query,
            ticket.ticket_id,
            ticket.action_id,
            ticket.alert_id,
            ticket.brief_id,
            ticket.generated_at,
            ticket.title,
            ticket.severity,
            ticket.affected_airport,
            ticket.affected_service,
            ticket.posture,
            ticket.trust_assessment,
            ticket.summary,
            json.dumps(ticket.what_changed),
            json.dumps(ticket.why_it_matters),
            json.dumps(ticket.evidence),
            json.dumps(ticket.recommended_review),
            ticket.critic_rationale,
            ticket.status,
            ticket.created_by_agent,
            ticket.decided_by,
            ticket.decided_at,
            ticket.decision_note,
            ticket.idempotency_key,
        )
        return ticket

    async def get_recent(self, limit: int = 25) -> list[OperationalTicket]:
        rows = await self._pool.fetch(
            """SELECT ticket_id, action_id, alert_id, brief_id, generated_at, title,
                      severity, affected_airport, affected_service, posture, trust_assessment,
                      summary, what_changed_json, why_it_matters_json, evidence_json,
                      recommended_review_json, critic_rationale, status, created_by_agent,
                      decided_by, decided_at, decision_note, idempotency_key
               FROM operational_tickets ORDER BY generated_at DESC LIMIT $1""",
            limit,
        )
        return [_row_to_ticket(r) for r in rows]

    async def get_drafts(self) -> list[OperationalTicket]:
        rows = await self._pool.fetch("SELECT * FROM operational_tickets WHERE status = 'DRAFT' ORDER BY generated_at DESC")
        return [_row_to_ticket(r) for r in rows]

    async def get_by_id(self, ticket_id: str) -> OperationalTicket | None:
        row = await self._pool.fetchrow("SELECT * FROM operational_tickets WHERE ticket_id = $1", ticket_id)
        return _row_to_ticket(row) if row else None

    async def get_by_action_id(self, action_id: str) -> OperationalTicket | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM operational_tickets WHERE action_id = $1 ORDER BY generated_at DESC LIMIT 1",
            action_id,
        )
        return _row_to_ticket(row) if row else None

    async def _transition(self, ticket_id: str, new_status: str, actor: str, note: str) -> OperationalTicket | None:
        if new_status not in ("APPROVED_FOR_ESCALATION", "REJECTED", "MONITORING", "OVERRIDDEN"):
            raise ValueError(f"new_status {new_status} not permitted")

        async with UnitOfWork(self._pool) as uow:
            row = await uow.conn.fetchrow(
                "SELECT * FROM operational_tickets WHERE ticket_id = $1 FOR UPDATE", ticket_id
            )
            if row is None:
                return None

            current = row["status"]
            if current in ("APPROVED_FOR_ESCALATION", "REJECTED", "OVERRIDDEN"):
                return _row_to_ticket(row)

            now = datetime.now(timezone.utc)
            await uow.conn.execute(
                """UPDATE operational_tickets
                   SET status = $1, decided_at = $2, decided_by = $3, decision_note = $4
                   WHERE ticket_id = $5""",
                new_status, now, actor, note, ticket_id,
            )
            await self._audit.write(
                uow.conn,
                action_id=f"ticket:{ticket_id}",
                previous_status=current,
                new_status=new_status,
                actor=actor,
                note=note,
            )
            updated = await uow.conn.fetchrow("SELECT * FROM operational_tickets WHERE ticket_id = $1", ticket_id)
            return _row_to_ticket(updated) if updated else None

    async def approve(self, ticket_id: str, actor: str = "dashboard_operator", note: str = "") -> OperationalTicket | None:
        return await self._transition(ticket_id, "APPROVED_FOR_ESCALATION", actor, note)

    async def reject(self, ticket_id: str, actor: str = "dashboard_operator", note: str = "") -> OperationalTicket | None:
        return await self._transition(ticket_id, "REJECTED", actor, note)

    async def mark_monitoring(self, ticket_id: str, actor: str = "dashboard_operator", note: str = "") -> OperationalTicket | None:
        return await self._transition(ticket_id, "MONITORING", actor, note)

    async def mark_overridden(self, ticket_id: str, actor: str = "dashboard_operator", note: str = "") -> OperationalTicket | None:
        return await self._transition(ticket_id, "OVERRIDDEN", actor, note)
