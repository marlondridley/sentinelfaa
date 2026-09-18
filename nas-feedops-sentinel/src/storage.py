"""
PostgreSQL async persistence layer for NAS FeedOps Sentinel via asyncpg.
Configured for compatibility with PgBouncer connection pooling.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
from src.async_bridge import run_async
from src.config import settings

from src.models import (
    ActionAuditEntry,
    AlertEvent,
    CriticResult,
    NASEvent,
    OperationalTicket,
    PlannerResult,
    StagedAction,
    WatchOfficerBrief,
)

_POOL: asyncpg.Pool | None = None

async def create_asyncpg_pool() -> asyncpg.Pool:
    """Create an asyncpg connection pool compatible with PgBouncer transaction pooling."""
    return await asyncpg.create_pool(
        dsn=settings.DATABASE_URL,
        statement_cache_size=0,  # Critical for PgBouncer compatibility
        min_size=5,
        max_size=20,
    )



async def get_db_pool() -> asyncpg.Pool:
    """Initialize or fetch the global asyncpg connection pool."""
    global _POOL
    if _POOL is None:
        _POOL = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL,
            min_size=2,
            max_size=20,
            statement_cache_size=0,  # Required for PgBouncer transaction pooling
        )
        await _init_schema(_POOL)
        await _run_migrations(_POOL)
    return _POOL


async def close_db_pool() -> None:
    """Gracefully close connection pool on application shutdown."""
    global _POOL
    if _POOL is not None:
        await _POOL.close()
        _POOL = None


# ── Schema migrations ─────────────────────────────────────────────────────────
#
# _init_schema below is CREATE TABLE IF NOT EXISTS: it establishes the schema for a
# brand-new database but is a no-op for every column added later against a database
# that already has the table. This registry is the forward-only path for those later
# changes on an existing deployment. Each entry is (id, sql), applied at most once and
# recorded in schema_migrations. IDs are permanent once shipped — never edit or reorder
# a migration that has run anywhere; add a new one instead.
#
# Kept deliberately dependency-free (no Alembic) since this backend has no ORM layer to
# hang it from; if the schema grows enough migrations to need branching/rollback, promote
# this list to real migration files at that point.

_MIGRATIONS: list[tuple[str, str]] = [
    ("2026_09_postgres_referential_integrity_fks", """
        -- Step 1: Nullify dangling foreign key references across existing tables
        UPDATE alert_events
        SET event_id = NULL
        WHERE event_id IS NOT NULL
          AND event_id NOT IN (SELECT event_id FROM nas_events);

        DELETE FROM critic_results
        WHERE alert_id NOT IN (SELECT alert_id FROM alert_events);

        UPDATE staged_actions
        SET alert_id = NULL
        WHERE alert_id IS NOT NULL
          AND alert_id NOT IN (SELECT alert_id FROM alert_events);

        UPDATE staged_actions
        SET event_id = NULL
        WHERE event_id IS NOT NULL
          AND event_id NOT IN (SELECT event_id FROM nas_events);

        UPDATE operational_tickets
        SET action_id = NULL
        WHERE action_id IS NOT NULL
          AND action_id NOT IN (SELECT action_id FROM staged_actions);

        UPDATE operational_tickets
        SET alert_id = NULL
        WHERE alert_id IS NOT NULL
          AND alert_id NOT IN (SELECT alert_id FROM alert_events);

        UPDATE operational_tickets
        SET brief_id = NULL
        WHERE brief_id IS NOT NULL
          AND brief_id NOT IN (SELECT brief_id FROM watch_officer_briefs);

        -- Step 2: Apply constraints to enforce referential integrity moving forward
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_alert_events_event') THEN
                ALTER TABLE alert_events
                ADD CONSTRAINT fk_alert_events_event
                FOREIGN KEY (event_id) REFERENCES nas_events(event_id) ON DELETE SET NULL;
            END IF;

            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_critic_results_alert') THEN
                ALTER TABLE critic_results
                ADD CONSTRAINT fk_critic_results_alert
                FOREIGN KEY (alert_id) REFERENCES alert_events(alert_id) ON DELETE CASCADE;
            END IF;

            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_staged_actions_alert') THEN
                ALTER TABLE staged_actions
                ADD CONSTRAINT fk_staged_actions_alert
                FOREIGN KEY (alert_id) REFERENCES alert_events(alert_id) ON DELETE SET NULL;
            END IF;

            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_staged_actions_event') THEN
                ALTER TABLE staged_actions
                ADD CONSTRAINT fk_staged_actions_event
                FOREIGN KEY (event_id) REFERENCES nas_events(event_id) ON DELETE SET NULL;
            END IF;

            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_operational_tickets_action') THEN
                ALTER TABLE operational_tickets
                ADD CONSTRAINT fk_operational_tickets_action
                FOREIGN KEY (action_id) REFERENCES staged_actions(action_id) ON DELETE SET NULL;
            END IF;

            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_operational_tickets_alert') THEN
                ALTER TABLE operational_tickets
                ADD CONSTRAINT fk_operational_tickets_alert
                FOREIGN KEY (alert_id) REFERENCES alert_events(alert_id) ON DELETE SET NULL;
            END IF;

            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_operational_tickets_brief') THEN
                ALTER TABLE operational_tickets
                ADD CONSTRAINT fk_operational_tickets_brief
                FOREIGN KEY (brief_id) REFERENCES watch_officer_briefs(brief_id) ON DELETE SET NULL;
            END IF;
        END $$;
    """),
]


async def _run_migrations(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id          TEXT PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        applied = {r["id"] for r in await conn.fetch("SELECT id FROM schema_migrations")}

        for migration_id, sql in _MIGRATIONS:
            if migration_id in applied:
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (id) VALUES ($1)", migration_id
                )


async def _init_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nas_events (
                event_id            TEXT PRIMARY KEY,
                airport_code        TEXT,
                tracon              TEXT,
                service_name        TEXT,
                message_type        TEXT,
                event_time_utc      TIMESTAMPTZ,
                received_time_utc   TIMESTAMPTZ,
                latency_ms          DOUBLE PRECISION,
                runway              TEXT,
                rvr_touchdown       INTEGER,
                rvr_midpoint        INTEGER,
                rvr_rollout         INTEGER,
                track_id            TEXT,
                aircraft_id         TEXT,
                vehicle_id          TEXT,
                surface_event_type  TEXT,
                lat                 DOUBLE PRECISION,
                lon                 DOUBLE PRECISION,
                altitude_ft         INTEGER,
                ground_speed_kt     INTEGER,
                vertical_speed_fpm INTEGER,
                flight_phase        TEXT,
                flight_rules        TEXT,
                beacon_code         TEXT,
                feed_status         TEXT,
                validation_status   TEXT,
                validation_errors   JSONB,
                raw_message_path    TEXT
            );

            CREATE TABLE IF NOT EXISTS alert_events (
                alert_id     TEXT PRIMARY KEY,
                alert_type   TEXT,
                severity     TEXT,
                airport_code TEXT,
                service_name TEXT,
                description  TEXT,
                triggered_at TIMESTAMPTZ,
                event_id     TEXT REFERENCES nas_events(event_id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS critic_results (
                alert_id     TEXT PRIMARY KEY REFERENCES alert_events(alert_id) ON DELETE CASCADE,
                confidence   DOUBLE PRECISION,
                reasoning    TEXT,
                citations    JSONB,
                pattern      TEXT,
                escalate     BOOLEAN,
                evaluated_at TIMESTAMPTZ
            );

            CREATE TABLE IF NOT EXISTS planner_results (
                result_id           TEXT PRIMARY KEY,
                question            TEXT,
                answer              TEXT,
                sub_queries         JSONB,
                confidence          DOUBLE PRECISION,
                citations           JSONB,
                recommended_actions JSONB,
                evaluated_at        TIMESTAMPTZ
            );

            CREATE TABLE IF NOT EXISTS staged_actions (
                action_id           TEXT PRIMARY KEY,
                alert_id            TEXT REFERENCES alert_events(alert_id) ON DELETE SET NULL,
                event_id            TEXT REFERENCES nas_events(event_id) ON DELETE SET NULL,
                airport_code        TEXT,
                service_name        TEXT,
                severity            TEXT,
                alert_type          TEXT,
                recommended_action  TEXT,
                rationale           TEXT,
                critic_confidence   DOUBLE PRECISION,
                critic_pattern      TEXT,
                status              TEXT NOT NULL DEFAULT 'PENDING',
                idempotency_key     TEXT UNIQUE,
                created_at          TIMESTAMPTZ,
                decided_at          TIMESTAMPTZ,
                decided_by          TEXT,
                decision_note       TEXT
            );

            CREATE TABLE IF NOT EXISTS action_audit_log (
                audit_id        TEXT PRIMARY KEY,
                action_id       TEXT,  -- Unconstrained to support composite keys like 'ticket:{id}'
                previous_status TEXT,
                new_status      TEXT,
                actor           TEXT,
                note            TEXT,
                created_at      TIMESTAMPTZ
            );

            CREATE TABLE IF NOT EXISTS watch_officer_briefs (
                brief_id                        TEXT PRIMARY KEY,
                generated_at                    TIMESTAMPTZ,
                posture                         TEXT,
                trust_assessment                TEXT,
                headline                        TEXT,
                what_changed_json               JSONB,
                why_it_matters_json             JSONB,
                recommended_human_review_json  JSONB,
                evidence_json                   JSONB,
                affected_airports_json          JSONB,
                pending_actions_count           INTEGER,
                critical_alert_count            INTEGER,
                confidence                      DOUBLE PRECISION,
                llm_used                        BOOLEAN,
                limitations_json                JSONB
            );

            CREATE TABLE IF NOT EXISTS operational_tickets (
                ticket_id               TEXT PRIMARY KEY,
                action_id               TEXT REFERENCES staged_actions(action_id) ON DELETE SET NULL,
                alert_id                TEXT REFERENCES alert_events(alert_id) ON DELETE SET NULL,
                brief_id                TEXT REFERENCES watch_officer_briefs(brief_id) ON DELETE SET NULL,
                generated_at            TIMESTAMPTZ,
                title                   TEXT,
                severity                TEXT,
                affected_airport        TEXT,
                affected_service        TEXT,
                posture                 TEXT,
                trust_assessment        TEXT,
                summary                 TEXT,
                what_changed_json       JSONB,
                why_it_matters_json     JSONB,
                evidence_json           JSONB,
                recommended_review_json JSONB,
                critic_rationale        TEXT,
                status                  TEXT NOT NULL DEFAULT 'DRAFT',
                created_by_agent        TEXT,
                decided_by              TEXT,
                decided_at              TIMESTAMPTZ,
                decision_note           TEXT,
                idempotency_key         TEXT UNIQUE
            );

            -- Indexes to optimize analytical reads and FK constraint checks
            CREATE INDEX IF NOT EXISTS idx_nas_events_airport_time ON nas_events (airport_code, received_time_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_nas_events_service_time ON nas_events (service_name, received_time_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_nas_events_aircraft_time ON nas_events (aircraft_id, received_time_utc DESC) WHERE aircraft_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_alert_events_airport_time ON alert_events (airport_code, triggered_at DESC);
            CREATE INDEX IF NOT EXISTS idx_alert_events_event_id ON alert_events (event_id);
            CREATE INDEX IF NOT EXISTS idx_staged_actions_status ON staged_actions (status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_staged_actions_alert_id ON staged_actions (alert_id);
            CREATE INDEX IF NOT EXISTS idx_staged_actions_event_id ON staged_actions (event_id);
            CREATE INDEX IF NOT EXISTS idx_operational_tickets_action_id ON operational_tickets (action_id);
            CREATE INDEX IF NOT EXISTS idx_operational_tickets_alert_id ON operational_tickets (alert_id);
            CREATE INDEX IF NOT EXISTS idx_operational_tickets_brief_id ON operational_tickets (brief_id);
            CREATE INDEX IF NOT EXISTS idx_operational_tickets_status ON operational_tickets (status, generated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_action_audit_action_id ON action_audit_log (action_id, created_at DESC);
        """)


# ── Write operations ──────────────────────────────────────────────────────────

async def save_event(event: NASEvent) -> None:
    pool = await get_db_pool()
    query = """
        INSERT INTO nas_events (
            event_id, airport_code, tracon, service_name, message_type,
            event_time_utc, received_time_utc, latency_ms, runway,
            rvr_touchdown, rvr_midpoint, rvr_rollout, track_id,
            aircraft_id, vehicle_id, surface_event_type,
            lat, lon, altitude_ft, ground_speed_kt, vertical_speed_fpm,
            flight_phase, flight_rules, beacon_code,
            feed_status, validation_status, validation_errors, raw_message_path
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
            $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28
        )
        ON CONFLICT (event_id) DO UPDATE SET
            airport_code = EXCLUDED.airport_code,
            tracon = EXCLUDED.tracon,
            service_name = EXCLUDED.service_name,
            message_type = EXCLUDED.message_type,
            event_time_utc = EXCLUDED.event_time_utc,
            received_time_utc = EXCLUDED.received_time_utc,
            latency_ms = EXCLUDED.latency_ms,
            runway = EXCLUDED.runway,
            rvr_touchdown = EXCLUDED.rvr_touchdown,
            rvr_midpoint = EXCLUDED.rvr_midpoint,
            rvr_rollout = EXCLUDED.rvr_rollout,
            track_id = EXCLUDED.track_id,
            aircraft_id = EXCLUDED.aircraft_id,
            vehicle_id = EXCLUDED.vehicle_id,
            surface_event_type = EXCLUDED.surface_event_type,
            lat = EXCLUDED.lat,
            lon = EXCLUDED.lon,
            altitude_ft = EXCLUDED.altitude_ft,
            ground_speed_kt = EXCLUDED.ground_speed_kt,
            vertical_speed_fpm = EXCLUDED.vertical_speed_fpm,
            flight_phase = EXCLUDED.flight_phase,
            flight_rules = EXCLUDED.flight_rules,
            beacon_code = EXCLUDED.beacon_code,
            feed_status = EXCLUDED.feed_status,
            validation_status = EXCLUDED.validation_status,
            validation_errors = EXCLUDED.validation_errors,
            raw_message_path = EXCLUDED.raw_message_path;
    """
    await pool.execute(
        query,
        event.event_id, event.airport_code, event.tracon, event.service_name, event.message_type,
        event.event_time_utc, event.received_time_utc, event.latency_ms, event.runway,
        event.rvr_touchdown, event.rvr_midpoint, event.rvr_rollout, event.track_id,
        event.aircraft_id, event.vehicle_id, event.surface_event_type,
        event.lat, event.lon, event.altitude_ft, event.ground_speed_kt, event.vertical_speed_fpm,
        event.flight_phase, event.flight_rules, event.beacon_code,
        event.feed_status, event.validation_status, json.dumps(event.validation_errors), event.raw_message_path
    )


async def save_alert(alert: AlertEvent) -> None:
    pool = await get_db_pool()
    query = """
        INSERT INTO alert_events (
            alert_id, alert_type, severity, airport_code, service_name,
            description, triggered_at, event_id
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (alert_id) DO UPDATE SET
            alert_type = EXCLUDED.alert_type,
            severity = EXCLUDED.severity,
            airport_code = EXCLUDED.airport_code,
            service_name = EXCLUDED.service_name,
            description = EXCLUDED.description,
            triggered_at = EXCLUDED.triggered_at,
            event_id = EXCLUDED.event_id;
    """
    await pool.execute(
        query,
        alert.alert_id, alert.alert_type, alert.severity, alert.airport_code,
        alert.service_name, alert.description, alert.triggered_at, alert.event_id
    )


async def save_events(events: list[NASEvent]) -> None:
    for event in events:
        await save_event(event)


async def save_alerts(alerts: list[AlertEvent]) -> None:
    for alert in alerts:
        await save_alert(alert)


# ── Read operations ───────────────────────────────────────────────────────────

async def get_recent_events(limit: int = 500) -> list[dict]:
    # NOTE: column list mirrors the full nas_events schema (see _init_schema) so the
    # query is an explicit contract rather than SELECT *. If dashboard/metrics callers
    # are confirmed not to need validation_errors (JSONB) or raw_message_path, drop them
    # here to cut bytes-over-wire on this hot path.
    pool = await get_db_pool()
    rows = await pool.fetch(
        """SELECT event_id, airport_code, tracon, service_name, message_type,
                  event_time_utc, received_time_utc, latency_ms, runway,
                  rvr_touchdown, rvr_midpoint, rvr_rollout, track_id,
                  aircraft_id, vehicle_id, surface_event_type,
                  lat, lon, altitude_ft, ground_speed_kt, vertical_speed_fpm,
                  flight_phase, flight_rules, beacon_code,
                  feed_status, validation_status, validation_errors, raw_message_path
           FROM nas_events ORDER BY received_time_utc DESC LIMIT $1""",
        limit,
    )
    return [dict(r) for r in rows]


async def get_recent_alerts(limit: int = 200) -> list[dict]:
    # NOTE: column list mirrors the full alert_events schema; no JSON blobs on this table,
    # so this is purely about making the read an explicit contract rather than SELECT *.
    pool = await get_db_pool()
    rows = await pool.fetch(
        """SELECT alert_id, alert_type, severity, airport_code, service_name,
                  description, triggered_at, event_id
           FROM alert_events ORDER BY triggered_at DESC LIMIT $1""",
        limit,
    )
    return [dict(r) for r in rows]


async def get_latest_rvr() -> list[dict]:
    pool = await get_db_pool()
    query = """
        SELECT e.*
        FROM nas_events e
        INNER JOIN (
            SELECT airport_code, runway, MAX(event_time_utc) AS max_time
            FROM nas_events
            WHERE service_name = 'APDS_RVR'
            GROUP BY airport_code, runway
        ) latest
          ON e.airport_code = latest.airport_code
         AND e.runway        = latest.runway
         AND e.event_time_utc = latest.max_time
        WHERE e.service_name = 'APDS_RVR'
        ORDER BY e.airport_code, e.runway
    """
    rows = await pool.fetch(query)
    return [dict(r) for r in rows]


async def get_event_counts_by_service() -> list[dict]:
    pool = await get_db_pool()
    query = """
        SELECT service_name, airport_code, COUNT(*) AS count
        FROM nas_events
        GROUP BY service_name, airport_code
        ORDER BY service_name, airport_code
    """
    rows = await pool.fetch(query)
    return [dict(r) for r in rows]


async def get_validation_summary() -> dict:
    pool = await get_db_pool()
    query = """
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN validation_status = 'PASSED' THEN 1 ELSE 0 END), 0) AS passed,
            COALESCE(SUM(CASE WHEN validation_status = 'FAILED' THEN 1 ELSE 0 END), 0) AS failed
        FROM nas_events
    """
    row = await pool.fetchrow(query)
    return dict(row) if row else {"total": 0, "passed": 0, "failed": 0}


async def get_events_for_critic(
    airport_code: str | None,
    window_minutes: int = 30,
    limit: int = 50,
) -> list[dict]:
    pool = await get_db_pool()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    if airport_code:
        rows = await pool.fetch(
            """SELECT event_id, airport_code, service_name, latency_ms, feed_status,
                      rvr_touchdown, rvr_midpoint, rvr_rollout, runway,
                      received_time_utc, validation_status
               FROM nas_events
               WHERE airport_code = $1 AND received_time_utc >= $2
               ORDER BY received_time_utc DESC LIMIT $3""",
            airport_code, cutoff, limit
        )
    else:
        rows = await pool.fetch(
            """SELECT event_id, airport_code, service_name, latency_ms, feed_status,
                      rvr_touchdown, rvr_midpoint, rvr_rollout, runway,
                      received_time_utc, validation_status
               FROM nas_events
               WHERE received_time_utc >= $1
               ORDER BY received_time_utc DESC LIMIT $2""",
            cutoff, limit
        )
    return [dict(r) for r in rows]


async def get_alerts_for_critic(
    airport_code: str | None,
    window_minutes: int = 60,
    limit: int = 10,
) -> list[dict]:
    pool = await get_db_pool()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    if airport_code:
        rows = await pool.fetch(
            """SELECT alert_id, alert_type, severity, airport_code, service_name,
                      description, triggered_at
               FROM alert_events
               WHERE airport_code = $1 AND triggered_at >= $2
               ORDER BY triggered_at DESC LIMIT $3""",
            airport_code, cutoff, limit
        )
    else:
        rows = await pool.fetch(
            """SELECT alert_id, alert_type, severity, airport_code, service_name,
                      description, triggered_at
               FROM alert_events
               WHERE triggered_at >= $1
               ORDER BY triggered_at DESC LIMIT $2""",
            cutoff, limit
        )
    return [dict(r) for r in rows]


async def get_recent_critic_results(limit: int = 50) -> list[dict]:
    # NOTE: column list mirrors the full critic_results schema, including the citations
    # JSONB blob (dropping it here requires confirming the dashboard doesn't render it).
    pool = await get_db_pool()
    rows = await pool.fetch(
        """SELECT alert_id, confidence, reasoning, citations, pattern, escalate, evaluated_at
           FROM critic_results ORDER BY evaluated_at DESC LIMIT $1""",
        limit,
    )
    return [dict(r) for r in rows]


async def save_planner_result(result: PlannerResult) -> None:
    pool = await get_db_pool()
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
    await pool.execute(
        query,
        str(uuid.uuid4()), result.question, result.answer, json.dumps(result.sub_queries),
        result.confidence, json.dumps(result.citations), json.dumps(result.recommended_actions),
        result.evaluated_at
    )


async def get_recent_planner_results(limit: int = 10) -> list[dict]:
    pool = await get_db_pool()
    rows = await pool.fetch("SELECT * FROM planner_results ORDER BY evaluated_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


async def save_critic_result(result: CriticResult) -> None:
    pool = await get_db_pool()
    query = """
        INSERT INTO critic_results (
            alert_id, confidence, reasoning, citations, pattern, escalate, evaluated_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (alert_id) DO UPDATE SET
            confidence = EXCLUDED.confidence,
            reasoning = EXCLUDED.reasoning,
            citations = EXCLUDED.citations,
            pattern = EXCLUDED.pattern,
            escalate = EXCLUDED.escalate,
            evaluated_at = EXCLUDED.evaluated_at;
    """
    await pool.execute(
        query,
        result.alert_id, result.confidence, result.reasoning, json.dumps(result.citations),
        result.pattern, bool(result.escalate), result.evaluated_at
    )


async def get_active_aircraft(window_minutes: int = 5, limit: int = 500) -> list[dict]:
    pool = await get_db_pool()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    query = """
        SELECT e.*
        FROM nas_events e
        INNER JOIN (
            SELECT aircraft_id, MAX(received_time_utc) AS max_time
            FROM nas_events
            WHERE aircraft_id IS NOT NULL
              AND lat IS NOT NULL AND lon IS NOT NULL
              AND received_time_utc >= $1
            GROUP BY aircraft_id
        ) latest
          ON e.aircraft_id = latest.aircraft_id
         AND e.received_time_utc = latest.max_time
        ORDER BY e.received_time_utc DESC
        LIMIT $2
    """
    rows = await pool.fetch(query, cutoff, limit)
    return [dict(r) for r in rows]


async def get_flight_phase_counts(window_minutes: int = 15) -> dict:
    pool = await get_db_pool()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    query = """
        SELECT airport_code,
               COALESCE(flight_phase, 'UNKNOWN') AS phase,
               COUNT(DISTINCT aircraft_id) AS n
        FROM nas_events
        WHERE service_name = 'TERMINAL_AUTO'
          AND received_time_utc >= $1
          AND aircraft_id IS NOT NULL
        GROUP BY airport_code, phase
    """
    rows = await pool.fetch(query, cutoff)
    out: dict = {}
    for r in rows:
        out.setdefault(r["airport_code"], {})[r["phase"]] = r["n"]
    return out


async def get_altitude_distribution(window_minutes: int = 15) -> list[dict]:
    pool = await get_db_pool()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    query = """
        SELECT airport_code, altitude_ft, ground_speed_kt
        FROM nas_events
        WHERE service_name = 'TERMINAL_AUTO'
          AND received_time_utc >= $1
          AND altitude_ft IS NOT NULL
    """
    rows = await pool.fetch(query, cutoff)
    return [dict(r) for r in rows]


async def get_latency_stats() -> dict:
    pool = await get_db_pool()
    query = """
        SELECT
            AVG(latency_ms) AS avg_ms,
            MIN(latency_ms) AS min_ms,
            MAX(latency_ms) AS max_ms
        FROM nas_events
        WHERE latency_ms IS NOT NULL
    """
    row = await pool.fetchrow(query)
    return dict(row) if row else {"avg_ms": None, "min_ms": None, "max_ms": None}


# ── Test helpers ──────────────────────────────────────────────────────────────

async def reset_db() -> None:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            TRUNCATE TABLE nas_events, alert_events, critic_results, planner_results,
                           staged_actions, action_audit_log, watch_officer_briefs,
                           operational_tickets RESTART IDENTITY CASCADE;
        """)


def override_db_path(path: str = ":memory:") -> None:
    """
    Synchronous test-isolation entry point, kept under this name for backward
    compatibility with test fixtures written for the earlier SQLite-backed
    storage module.

    This backend is asyncpg/PostgreSQL, so there is no real file "path" to
    swap in the way SQLite's ':memory:' convention implies -- ``path`` is
    accepted (and ignored) purely so existing ``storage.override_db_path(":memory:")``
    call sites keep working unmodified. What this actually does is bridge onto
    the live database configured via DATABASE_URL / the POSTGRES_* settings
    (src/config.py) and TRUNCATE every table through reset_db(), giving the
    caller a clean slate synchronously -- suitable for a pytest
    ``@pytest.fixture(autouse=True)`` that isn't itself async.

    Requires a reachable Postgres instance. Point DATABASE_URL (or the
    POSTGRES_HOST/PORT/USER/PASSWORD/DB settings) at a disposable test
    database before running the suite -- this truncates real tables on
    whatever database it connects to, so never point it at production.
    """
    run_async(reset_db())


# ── HITL Staged Action persistence ────────────────────────────────────────────

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
    )


async def create_staged_action(action: StagedAction) -> StagedAction:
    if not action.idempotency_key:
        raise ValueError("StagedAction.idempotency_key is required")

    pool = await get_db_pool()
    existing = await pool.fetchrow(
        "SELECT * FROM staged_actions WHERE idempotency_key = $1",
        action.idempotency_key,
    )
    if existing:
        return _row_to_staged_action(existing)

    query = """
        INSERT INTO staged_actions (
            action_id, alert_id, event_id, airport_code, service_name,
            severity, alert_type, recommended_action, rationale,
            critic_confidence, critic_pattern, status, idempotency_key,
            created_at, decided_at, decided_by, decision_note
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
    """
    await pool.execute(
        query,
        action.action_id, action.alert_id, action.event_id, action.airport_code, action.service_name,
        action.severity, action.alert_type, action.recommended_action, action.rationale,
        action.critic_confidence, action.critic_pattern, action.status, action.idempotency_key,
        action.created_at, action.decided_at, action.decided_by, action.decision_note
    )
    await _write_audit(action.action_id, previous_status="(new)", new_status=action.status,
                       actor="system", note="staged from CRITICAL alert")
    return action


async def _write_audit(action_id: str, previous_status: str, new_status: str,
                       actor: str, note: str = "") -> None:
    entry = ActionAuditEntry(
        action_id=action_id,
        previous_status=previous_status,
        new_status=new_status,
        actor=actor,
        note=note,
    )
    pool = await get_db_pool()
    query = """
        INSERT INTO action_audit_log (
            audit_id, action_id, previous_status, new_status, actor, note, created_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7)
    """
    await pool.execute(
        query,
        entry.audit_id, entry.action_id, entry.previous_status, entry.new_status,
        entry.actor, entry.note, entry.created_at
    )


_STAGED_ACTION_COLUMNS = """
    action_id, alert_id, event_id, airport_code, service_name, severity, alert_type,
    recommended_action, rationale, critic_confidence, critic_pattern, status,
    idempotency_key, created_at, decided_at, decided_by, decision_note
"""  # every column _row_to_staged_action() reads; kept as one constant so the two
# queries below and the mapper stay in sync as an explicit contract instead of SELECT *.


async def get_pending_staged_actions() -> list[StagedAction]:
    pool = await get_db_pool()
    rows = await pool.fetch(
        f"SELECT {_STAGED_ACTION_COLUMNS} FROM staged_actions WHERE status = 'PENDING' ORDER BY created_at DESC"
    )
    return [_row_to_staged_action(r) for r in rows]


async def get_recent_staged_actions(limit: int = 25) -> list[StagedAction]:
    pool = await get_db_pool()
    rows = await pool.fetch(
        f"SELECT {_STAGED_ACTION_COLUMNS} FROM staged_actions ORDER BY created_at DESC LIMIT $1",
        limit,
    )
    return [_row_to_staged_action(r) for r in rows]


async def _decide_staged_action(
    action_id: str, new_status: str, actor: str, note: str
) -> StagedAction | None:
    if new_status not in ("APPROVED", "REJECTED"):
        raise ValueError(f"new_status must be APPROVED or REJECTED, got {new_status}")

    pool = await get_db_pool()
    row = await pool.fetchrow("SELECT * FROM staged_actions WHERE action_id = $1", action_id)
    if row is None:
        return None

    current_status = row["status"]
    if current_status in ("APPROVED", "REJECTED", "EXPIRED"):
        return _row_to_staged_action(row)

    now = datetime.now(timezone.utc)
    await pool.execute(
        """UPDATE staged_actions
           SET status = $1, decided_at = $2, decided_by = $3, decision_note = $4
           WHERE action_id = $5""",
        new_status, now, actor, note, action_id
    )
    await _write_audit(action_id, previous_status=current_status, new_status=new_status,
                       actor=actor, note=note)

    updated = await pool.fetchrow("SELECT * FROM staged_actions WHERE action_id = $1", action_id)
    return _row_to_staged_action(updated) if updated else None


async def approve_staged_action(action_id: str, actor: str = "dashboard_operator",
                                note: str = "") -> StagedAction | None:
    return await _decide_staged_action(action_id, "APPROVED", actor, note)


async def reject_staged_action(action_id: str, actor: str = "dashboard_operator",
                               note: str = "") -> StagedAction | None:
    return await _decide_staged_action(action_id, "REJECTED", actor, note)


async def get_action_audit_log(action_id: str | None = None, limit: int = 50) -> list[dict]:
    pool = await get_db_pool()
    if action_id:
        rows = await pool.fetch(
            """SELECT * FROM action_audit_log WHERE action_id = $1
               ORDER BY created_at DESC LIMIT $2""",
            action_id, limit
        )
    else:
        rows = await pool.fetch("SELECT * FROM action_audit_log ORDER BY created_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


# ── Watch Officer Brief persistence ──────────────────────────────────────────

def _row_to_brief(row) -> WatchOfficerBrief:
    d = dict(row)
    return WatchOfficerBrief(
        brief_id=d["brief_id"],
        generated_at=d["generated_at"],
        posture=d.get("posture", "UNKNOWN"),
        trust_assessment=d.get("trust_assessment", "UNKNOWN"),
        headline=d.get("headline") or "",
        what_changed=json.loads(d.get("what_changed_json") or "[]") if isinstance(d.get("what_changed_json"), str) else d.get("what_changed_json", []),
        why_it_matters=json.loads(d.get("why_it_matters_json") or "[]") if isinstance(d.get("why_it_matters_json"), str) else d.get("why_it_matters_json", []),
        recommended_human_review=json.loads(d.get("recommended_human_review_json") or "[]") if isinstance(d.get("recommended_human_review_json"), str) else d.get("recommended_human_review_json", []),
        evidence=json.loads(d.get("evidence_json") or "[]") if isinstance(d.get("evidence_json"), str) else d.get("evidence_json", []),
        affected_airports=json.loads(d.get("affected_airports_json") or "[]") if isinstance(d.get("affected_airports_json"), str) else d.get("affected_airports_json", []),
        pending_actions_count=d.get("pending_actions_count") or 0,
        critical_alert_count=d.get("critical_alert_count") or 0,
        confidence=d.get("confidence") or 0.5,
        llm_used=bool(d.get("llm_used")),
        limitations=json.loads(d.get("limitations_json") or "[]") if isinstance(d.get("limitations_json"), str) else d.get("limitations_json", []),
    )


async def save_watch_officer_brief(brief: WatchOfficerBrief) -> WatchOfficerBrief:
    pool = await get_db_pool()
    query = """
        INSERT INTO watch_officer_briefs (
            brief_id, generated_at, posture, trust_assessment, headline,
            what_changed_json, why_it_matters_json, recommended_human_review_json,
            evidence_json, affected_airports_json,
            pending_actions_count, critical_alert_count, confidence,
            llm_used, limitations_json
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
        ON CONFLICT (brief_id) DO UPDATE SET
            generated_at = EXCLUDED.generated_at,
            posture = EXCLUDED.posture,
            trust_assessment = EXCLUDED.trust_assessment,
            headline = EXCLUDED.headline,
            what_changed_json = EXCLUDED.what_changed_json,
            why_it_matters_json = EXCLUDED.why_it_matters_json,
            recommended_human_review_json = EXCLUDED.recommended_human_review_json,
            evidence_json = EXCLUDED.evidence_json,
            affected_airports_json = EXCLUDED.affected_airports_json,
            pending_actions_count = EXCLUDED.pending_actions_count,
            critical_alert_count = EXCLUDED.critical_alert_count,
            confidence = EXCLUDED.confidence,
            llm_used = EXCLUDED.llm_used,
            limitations_json = EXCLUDED.limitations_json;
    """
    await pool.execute(
        query,
        brief.brief_id, brief.generated_at, brief.posture, brief.trust_assessment, brief.headline,
        json.dumps(brief.what_changed), json.dumps(brief.why_it_matters), json.dumps(brief.recommended_human_review),
        json.dumps(brief.evidence), json.dumps(brief.affected_airports), brief.pending_actions_count,
        brief.critical_alert_count, brief.confidence, bool(brief.llm_used), json.dumps(brief.limitations)
    )
    return brief


async def get_latest_watch_officer_brief() -> WatchOfficerBrief | None:
    pool = await get_db_pool()
    row = await pool.fetchrow("SELECT * FROM watch_officer_briefs ORDER BY generated_at DESC LIMIT 1")
    return _row_to_brief(row) if row else None


async def get_recent_watch_officer_briefs(limit: int = 10) -> list[WatchOfficerBrief]:
    pool = await get_db_pool()
    rows = await pool.fetch("SELECT * FROM watch_officer_briefs ORDER BY generated_at DESC LIMIT $1", limit)
    return [_row_to_brief(r) for r in rows]


# ── Operational Ticket persistence ────────────────────────────────────────────

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


async def create_operational_ticket(ticket: OperationalTicket) -> OperationalTicket:
    if not ticket.idempotency_key:
        raise ValueError("OperationalTicket.idempotency_key is required")

    pool = await get_db_pool()
    existing = await pool.fetchrow(
        "SELECT * FROM operational_tickets WHERE idempotency_key = $1",
        ticket.idempotency_key,
    )
    if existing:
        return _row_to_ticket(existing)

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
    """
    await pool.execute(
        query,
        ticket.ticket_id, ticket.action_id, ticket.alert_id, ticket.brief_id, ticket.generated_at,
        ticket.title, ticket.severity, ticket.affected_airport, ticket.affected_service,
        ticket.posture, ticket.trust_assessment, ticket.summary, json.dumps(ticket.what_changed),
        json.dumps(ticket.why_it_matters), json.dumps(ticket.evidence), json.dumps(ticket.recommended_review),
        ticket.critic_rationale, ticket.status, ticket.created_by_agent, ticket.decided_by,
        ticket.decided_at, ticket.decision_note, ticket.idempotency_key
    )
    return ticket

async def save_operational_ticket(ticket: OperationalTicket) -> OperationalTicket:
    pool = await get_db_pool()
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
    await pool.execute(
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


async def get_recent_operational_tickets(limit: int = 25) -> list[OperationalTicket]:
    pool = await get_db_pool()
    rows = await pool.fetch(
        "SELECT * FROM operational_tickets ORDER BY generated_at DESC LIMIT $1",
        limit,
    )
    return [_row_to_ticket(r) for r in rows]
    
async def get_draft_operational_tickets() -> list[OperationalTicket]:
    pool = await get_db_pool()
    rows = await pool.fetch("SELECT * FROM operational_tickets WHERE status = 'DRAFT' ORDER BY generated_at DESC")
    return [_row_to_ticket(r) for r in rows]


async def get_ticket_by_id(ticket_id: str) -> OperationalTicket | None:
    pool = await get_db_pool()
    row = await pool.fetchrow("SELECT * FROM operational_tickets WHERE ticket_id = $1", ticket_id)
    return _row_to_ticket(row) if row else None


async def get_ticket_by_action_id(action_id: str) -> OperationalTicket | None:
    pool = await get_db_pool()
    row = await pool.fetchrow(
        "SELECT * FROM operational_tickets WHERE action_id = $1 ORDER BY generated_at DESC LIMIT 1",
        action_id
    )
    return _row_to_ticket(row) if row else None


async def _transition_ticket(
    ticket_id: str, new_status: str, actor: str, note: str
) -> OperationalTicket | None:
    if new_status not in ("APPROVED_FOR_ESCALATION", "REJECTED", "MONITORING"):
        raise ValueError(f"new_status {new_status} not permitted")

    pool = await get_db_pool()
    row = await pool.fetchrow("SELECT * FROM operational_tickets WHERE ticket_id = $1", ticket_id)
    if row is None:
        return None

    current = row["status"]
    if current in ("APPROVED_FOR_ESCALATION", "REJECTED"):
        return _row_to_ticket(row)

    now = datetime.now(timezone.utc)
    await pool.execute(
        """UPDATE operational_tickets
           SET status = $1, decided_at = $2, decided_by = $3, decision_note = $4
           WHERE ticket_id = $5""",
        new_status, now, actor, note, ticket_id
    )
    await _write_audit(
        action_id=f"ticket:{ticket_id}",
        previous_status=current,
        new_status=new_status,
        actor=actor,
        note=note,
    )
    updated = await pool.fetchrow("SELECT * FROM operational_tickets WHERE ticket_id = $1", ticket_id)
    return _row_to_ticket(updated) if updated else None


async def approve_operational_ticket(
    ticket_id: str, actor: str = "dashboard_operator", note: str = ""
) -> OperationalTicket | None:
    return await _transition_ticket(ticket_id, "APPROVED_FOR_ESCALATION", actor, note)


async def reject_operational_ticket(
    ticket_id: str, actor: str = "dashboard_operator", note: str = ""
) -> OperationalTicket | None:
    return await _transition_ticket(ticket_id, "REJECTED", actor, note)


async def mark_operational_ticket_monitoring(
    ticket_id: str, actor: str = "dashboard_operator", note: str = ""
) -> OperationalTicket | None:
    return await _transition_ticket(ticket_id, "MONITORING", actor, note)