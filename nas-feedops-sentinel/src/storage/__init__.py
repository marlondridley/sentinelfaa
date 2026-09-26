"""
PostgreSQL async persistence layer for NAS FeedOps Sentinel via asyncpg.
Configured for compatibility with PgBouncer connection pooling.

This package replaces what used to be a single ~1200-line storage.py: one flat file
of free functions covering six unrelated entities (raw telemetry, critic/planner
analysis, HITL staged actions + their audit trail, Watch Officer briefs, operational
tickets) plus connection-pool and schema management, all sharing one implicit global.

The split:
  - pool.py            connection pool lifecycle (get_db_pool/close_db_pool)
  - schema.py           CREATE TABLE bootstrap + forward-only migrations
  - unit_of_work.py      an explicit transactional boundary (see its docstring) used
                         wherever a repository needs more than one statement to land
                         atomically -- e.g. an approve/reject decision plus its audit
                         log entry, which the original module wrote as two separate,
                         unlocked pool.execute() calls
  - repositories/*      one class per entity, each taking the shared pool

Every function below is a thin wrapper that builds the relevant repository from the
shared pool and delegates to it -- same names, same signatures, same return types as
the original storage.py, so every existing `from src import storage; storage.X(...)`
and `from src.storage import X` call site across api.py, pipeline.py,
execution_gate.py, dashboard/app.py, temporal_activities.py, and the test suite keeps
working unmodified. New code should generally prefer instantiating a repository
directly (see repositories/) over adding to this wrapper list, but the wrappers are
not deprecated -- they're the stable public surface other modules already depend on.
"""
from __future__ import annotations

from datetime import datetime

from src.models import (
    AlertEvent,
    CriticResult,
    NASEvent,
    OperationalTicket,
    PlannerResult,
    StagedAction,
    WatchOfficerBrief,
)
from src.storage.pool import close_db_pool, get_db_pool
from src.storage.repositories.analysis_repo import AnalysisRepository
from src.storage.repositories.briefs_repo import BriefsRepository
from src.storage.repositories.events_repo import EventsRepository
from src.storage.repositories.ingest_log_repo import IngestLogRepository
from src.storage.repositories.model_calls_repo import ModelCallsRepository
from src.storage.repositories.staged_actions_repo import StagedActionsRepository
from src.storage.repositories.tickets_repo import TicketsRepository
from src.storage.test_support import override_db_path, reset_db
from src.storage.unit_of_work import UnitOfWork

__all__ = [
    "close_db_pool",
    "get_db_pool",
    "override_db_path",
    "reset_db",
    "save_event",
    "save_alert",
    "save_events",
    "save_alerts",
    "get_recent_events",
    "get_recent_alerts",
    "get_latest_rvr",
    "get_origin_counts",
    "get_event_counts_by_service",
    "get_validation_summary",
    "get_events_for_critic",
    "get_alerts_for_critic",
    "get_recent_critic_results",
    "get_critic_verdicts_since",
    "record_model_call",
    "get_model_calls_since",
    "get_investigations_since",
    "get_investigation_for_action",
    "get_latest_investigation",
    "get_model_calls_for_subjects",
    "get_staged_actions_by_ids",
    "get_audit_log_for_actions",
    "get_critic_verdicts_for_alerts",
    "save_planner_result",
    "get_recent_planner_results",
    "save_critic_result",
    "get_active_aircraft",
    "get_flight_phase_counts",
    "get_altitude_distribution",
    "get_latency_stats",
    "is_file_ingested",
    "get_ingested_file_names",
    "record_ingest_log_entries",
    "get_recent_ingest_log",
    "create_staged_action",
    "get_pending_staged_actions",
    "get_recent_staged_actions",
    "approve_staged_action",
    "reject_staged_action",
    "override_staged_action",
    "get_action_audit_log",
    "save_watch_officer_brief",
    "get_latest_watch_officer_brief",
    "get_recent_watch_officer_briefs",
    "create_operational_ticket",
    "save_operational_ticket",
    "get_recent_operational_tickets",
    "get_draft_operational_tickets",
    "get_ticket_by_id",
    "get_ticket_by_action_id",
    "approve_operational_ticket",
    "reject_operational_ticket",
    "mark_operational_ticket_monitoring",
    "mark_operational_ticket_overridden",
    "decide_staged_action_with_ticket",
]


# ── Raw telemetry (nas_events / alert_events) ──────────────────────────────────

async def save_event(event: NASEvent) -> None:
    repo = EventsRepository(await get_db_pool())
    await repo.save_event(event)


async def save_alert(alert: AlertEvent) -> None:
    repo = EventsRepository(await get_db_pool())
    await repo.save_alert(alert)


async def save_events(events: list[NASEvent]) -> None:
    repo = EventsRepository(await get_db_pool())
    await repo.save_events(events)


async def save_alerts(alerts: list[AlertEvent]) -> None:
    repo = EventsRepository(await get_db_pool())
    await repo.save_alerts(alerts)


async def get_recent_events(limit: int = 500) -> list[dict]:
    repo = EventsRepository(await get_db_pool())
    return await repo.get_recent_events(limit)


async def get_recent_alerts(limit: int = 200) -> list[dict]:
    repo = EventsRepository(await get_db_pool())
    return await repo.get_recent_alerts(limit)


async def get_origin_counts(window_minutes: int = 60) -> dict[str, int]:
    repo = EventsRepository(await get_db_pool())
    return await repo.get_origin_counts(window_minutes)


async def get_latest_rvr() -> list[dict]:
    repo = EventsRepository(await get_db_pool())
    return await repo.get_latest_rvr()


async def get_event_counts_by_service() -> list[dict]:
    repo = EventsRepository(await get_db_pool())
    return await repo.get_event_counts_by_service()


async def get_validation_summary() -> dict:
    repo = EventsRepository(await get_db_pool())
    return await repo.get_validation_summary()


async def get_events_for_critic(
    airport_code: str | None, window_minutes: int = 30, limit: int = 50
) -> list[dict]:
    repo = EventsRepository(await get_db_pool())
    return await repo.get_events_for_critic(airport_code, window_minutes, limit)


async def get_alerts_for_critic(
    airport_code: str | None, window_minutes: int = 60, limit: int = 10
) -> list[dict]:
    repo = EventsRepository(await get_db_pool())
    return await repo.get_alerts_for_critic(airport_code, window_minutes, limit)


async def get_active_aircraft(window_minutes: int = 5, limit: int = 500) -> list[dict]:
    repo = EventsRepository(await get_db_pool())
    return await repo.get_active_aircraft(window_minutes, limit)


async def get_flight_phase_counts(window_minutes: int = 15) -> dict:
    repo = EventsRepository(await get_db_pool())
    return await repo.get_flight_phase_counts(window_minutes)


async def get_altitude_distribution(window_minutes: int = 15) -> list[dict]:
    repo = EventsRepository(await get_db_pool())
    return await repo.get_altitude_distribution(window_minutes)


async def get_latency_stats() -> dict:
    repo = EventsRepository(await get_db_pool())
    return await repo.get_latency_stats()


# ── SWIM file-ingest audit trail (ingest_log) ──────────────────────────────────
# Replaces data/processed/ingest_log.jsonl -- see IngestLogRepository's docstring.

async def is_file_ingested(file_name: str) -> bool:
    repo = IngestLogRepository(await get_db_pool())
    return await repo.is_processed(file_name)


async def get_ingested_file_names(file_names: list[str]) -> set[str]:
    repo = IngestLogRepository(await get_db_pool())
    return await repo.get_processed_subset(file_names)


async def record_ingest_log_entries(entries: list[dict]) -> None:
    repo = IngestLogRepository(await get_db_pool())
    await repo.append_many(entries)


async def get_recent_ingest_log(limit: int = 100) -> list[dict]:
    repo = IngestLogRepository(await get_db_pool())
    return await repo.get_recent(limit)


# ── Model behaviour (model_calls) and what readiness reads ─────────────────────

async def record_model_call(row: dict) -> None:
    repo = ModelCallsRepository(await get_db_pool())
    await repo.insert(row)


async def get_model_calls_since(since: datetime) -> list[dict]:
    repo = ModelCallsRepository(await get_db_pool())
    return await repo.get_since(since)


async def get_critic_verdicts_since(since: datetime) -> list[dict]:
    repo = AnalysisRepository(await get_db_pool())
    return await repo.get_critic_verdicts_since(since)


async def get_investigations_since(since: datetime) -> list:
    """Investigations (COMPLETE and FAILED) since `since`, each verified against its SHA-256."""
    from src.storage.repositories.investigations_repo import InvestigationsRepository

    repo = InvestigationsRepository(await get_db_pool())
    return await repo.get_since(since)


async def get_investigation_for_action(action_id: str):
    """The COMPLETE investigation of one staged action, verified; None if there is none."""
    from src.storage.repositories.investigations_repo import InvestigationsRepository

    repo = InvestigationsRepository(await get_db_pool())
    return await repo.get_by_action_id(action_id)


async def get_latest_investigation():
    from src.storage.repositories.investigations_repo import InvestigationsRepository

    repo = InvestigationsRepository(await get_db_pool())
    return await repo.get_latest()


async def get_model_calls_for_subjects(subjects: list[str]) -> list[dict]:
    repo = ModelCallsRepository(await get_db_pool())
    return await repo.get_for_subjects(subjects)


async def get_staged_actions_by_ids(action_ids: list[str]) -> list[StagedAction]:
    repo = StagedActionsRepository(await get_db_pool())
    return await repo.get_by_ids(action_ids)


async def get_audit_log_for_actions(action_ids: list[str]) -> list[dict]:
    from src.storage.repositories.audit_repo import AuditLogRepository

    repo = AuditLogRepository(await get_db_pool())
    return await repo.get_for_actions(action_ids)


async def get_critic_verdicts_for_alerts(alert_ids: list[str]) -> list[dict]:
    repo = AnalysisRepository(await get_db_pool())
    return await repo.get_critic_verdicts_for_alerts(alert_ids)


# ── Agent analysis (critic_results / planner_results) ─────────────────────────

async def get_recent_critic_results(limit: int = 50) -> list[dict]:
    repo = AnalysisRepository(await get_db_pool())
    return await repo.get_recent_critic_results(limit)


async def save_planner_result(result: PlannerResult) -> None:
    repo = AnalysisRepository(await get_db_pool())
    await repo.save_planner_result(result)


async def get_recent_planner_results(limit: int = 10) -> list[dict]:
    repo = AnalysisRepository(await get_db_pool())
    return await repo.get_recent_planner_results(limit)


async def save_critic_result(result: CriticResult) -> None:
    repo = AnalysisRepository(await get_db_pool())
    await repo.save_critic_result(result)


# ── HITL staged actions (staged_actions / action_audit_log) ───────────────────

async def create_staged_action(action: StagedAction, note: str = "staged from CRITICAL alert") -> StagedAction:
    repo = StagedActionsRepository(await get_db_pool())
    return await repo.create(action, note=note)


async def get_pending_staged_actions() -> list[StagedAction]:
    repo = StagedActionsRepository(await get_db_pool())
    return await repo.get_pending()


async def get_recent_staged_actions(limit: int = 25) -> list[StagedAction]:
    repo = StagedActionsRepository(await get_db_pool())
    return await repo.get_recent(limit)


async def approve_staged_action(
    action_id: str, actor: str = "dashboard_operator", note: str = ""
) -> StagedAction | None:
    repo = StagedActionsRepository(await get_db_pool())
    return await repo.approve(action_id, actor, note)


async def reject_staged_action(
    action_id: str, actor: str = "dashboard_operator", note: str = ""
) -> StagedAction | None:
    repo = StagedActionsRepository(await get_db_pool())
    return await repo.reject(action_id, actor, note)


async def override_staged_action(
    action_id: str,
    overridden_recommendation: str | None = None,
    overridden_severity: str | None = None,
    actor: str = "dashboard_operator",
    note: str = "",
) -> StagedAction | None:
    repo = StagedActionsRepository(await get_db_pool())
    return await repo.override(action_id, overridden_recommendation, overridden_severity, actor, note)


async def get_action_audit_log(action_id: str | None = None, limit: int = 50) -> list[dict]:
    repo = StagedActionsRepository(await get_db_pool())
    return await repo.get_audit_log(action_id, limit)


# ── Watch Officer briefs (watch_officer_briefs) ────────────────────────────────

async def save_watch_officer_brief(brief: WatchOfficerBrief) -> WatchOfficerBrief:
    repo = BriefsRepository(await get_db_pool())
    return await repo.save(brief)


async def get_latest_watch_officer_brief() -> WatchOfficerBrief | None:
    repo = BriefsRepository(await get_db_pool())
    return await repo.get_latest()


async def get_recent_watch_officer_briefs(limit: int = 10) -> list[WatchOfficerBrief]:
    repo = BriefsRepository(await get_db_pool())
    return await repo.get_recent(limit)


# ── Operational tickets (operational_tickets) ──────────────────────────────────

async def create_operational_ticket(ticket: OperationalTicket) -> OperationalTicket:
    repo = TicketsRepository(await get_db_pool())
    return await repo.create(ticket)


async def save_operational_ticket(ticket: OperationalTicket) -> OperationalTicket:
    repo = TicketsRepository(await get_db_pool())
    return await repo.save(ticket)


async def get_recent_operational_tickets(limit: int = 25) -> list[OperationalTicket]:
    repo = TicketsRepository(await get_db_pool())
    return await repo.get_recent(limit)


async def get_draft_operational_tickets() -> list[OperationalTicket]:
    repo = TicketsRepository(await get_db_pool())
    return await repo.get_drafts()


async def get_ticket_by_id(ticket_id: str) -> OperationalTicket | None:
    repo = TicketsRepository(await get_db_pool())
    return await repo.get_by_id(ticket_id)


async def get_ticket_by_action_id(action_id: str) -> OperationalTicket | None:
    repo = TicketsRepository(await get_db_pool())
    return await repo.get_by_action_id(action_id)


async def approve_operational_ticket(
    ticket_id: str, actor: str = "dashboard_operator", note: str = ""
) -> OperationalTicket | None:
    repo = TicketsRepository(await get_db_pool())
    return await repo.approve(ticket_id, actor, note)


async def reject_operational_ticket(
    ticket_id: str, actor: str = "dashboard_operator", note: str = ""
) -> OperationalTicket | None:
    repo = TicketsRepository(await get_db_pool())
    return await repo.reject(ticket_id, actor, note)


async def mark_operational_ticket_monitoring(
    ticket_id: str, actor: str = "dashboard_operator", note: str = ""
) -> OperationalTicket | None:
    repo = TicketsRepository(await get_db_pool())
    return await repo.mark_monitoring(ticket_id, actor, note)


async def mark_operational_ticket_overridden(
    ticket_id: str, actor: str = "dashboard_operator", note: str = ""
) -> OperationalTicket | None:
    repo = TicketsRepository(await get_db_pool())
    return await repo.mark_overridden(ticket_id, actor, note)


async def decide_staged_action_with_ticket(
    action_id: str,
    ticket_id: str | None,
    new_status: str,
    ticket_status: str,
    actor: str,
    note: str = "",
    *,
    overridden_recommendation: str | None = None,
    overridden_severity: str | None = None,
) -> StagedAction | None:
    """
    Decide a staged action and move its ticket in one transaction: both land or neither.

    The ticket moves only when the action itself changed, so a decision that arrives
    after the action was already decided cannot move the ticket to a status that
    disagrees with it.
    """
    pool = await get_db_pool()
    async with UnitOfWork(pool) as uow:
        action, changed = await StagedActionsRepository(pool).decide_in(
            uow.conn, action_id, new_status, actor, note,
            overridden_recommendation=overridden_recommendation,
            overridden_severity=overridden_severity,
        )
        if changed and ticket_id:
            await TicketsRepository(pool).transition_in(uow.conn, ticket_id, ticket_status, actor, note)
    return action
