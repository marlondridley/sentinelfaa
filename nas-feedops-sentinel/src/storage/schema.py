"""
Schema bootstrap (CREATE TABLE IF NOT EXISTS) and the forward-only migration
registry.

init_schema() establishes the schema for a brand-new database but is a no-op for
every column added later against a database that already has the table. _MIGRATIONS
is the forward-only path for those later changes on an existing deployment. Each
entry is (id, sql), applied at most once and recorded in schema_migrations. IDs are
permanent once shipped -- never edit or reorder a migration that has run anywhere;
add a new one instead.

Kept deliberately dependency-free (no Alembic) since this backend has no ORM layer to
hang it from; if the schema grows enough migrations to need branching/rollback,
promote this list to real migration files at that point.
"""
from __future__ import annotations

import asyncpg

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
    ("2026_09_staged_actions_override_columns", """
        ALTER TABLE staged_actions ADD COLUMN IF NOT EXISTS overridden_recommendation TEXT;
        ALTER TABLE staged_actions ADD COLUMN IF NOT EXISTS overridden_severity TEXT;
    """),
    ("2026_09_nas_events_status_code", """
        ALTER TABLE nas_events ADD COLUMN IF NOT EXISTS status_code TEXT;
    """),
    # One row per finished investigation (src/investigation/): the whole
    # structured record as JSONB plus its SHA-256. Append-only for the same
    # reason as action_audit_log: it is the evidence behind what a reviewer
    # was shown, so rewriting it must not be an ordinary write path. The
    # partial unique index allows one COMPLETE record per staged action while
    # keeping failed attempts for audit.
    ("2026_09_investigations", """
        CREATE TABLE IF NOT EXISTS investigations (
            investigation_id  TEXT PRIMARY KEY,
            action_id         TEXT NOT NULL REFERENCES staged_actions(action_id) ON DELETE RESTRICT,
            alert_id          TEXT,
            status            TEXT NOT NULL,
            opened_at         TIMESTAMPTZ NOT NULL,
            completed_at      TIMESTAMPTZ,
            -- record_json is the exact text that was hashed; JSONB normalizes
            -- documents (key order, numbers), so it cannot be the evidence of
            -- integrity. record is the same document, for querying.
            record_json       TEXT NOT NULL,
            record            JSONB NOT NULL,
            record_sha256     TEXT NOT NULL,
            stored_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_investigations_complete_per_action
            ON investigations (action_id) WHERE status = 'COMPLETE';
        CREATE INDEX IF NOT EXISTS idx_investigations_opened_at ON investigations (opened_at DESC);

        CREATE OR REPLACE FUNCTION investigations_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'investigations is append-only; % is not permitted on this table', TG_OP;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS trg_investigations_append_only ON investigations;
        CREATE TRIGGER trg_investigations_append_only
            BEFORE UPDATE OR DELETE ON investigations
            FOR EACH ROW EXECUTE FUNCTION investigations_append_only();
    """),
    # Provenance of every event and alert (models.DataOrigin). Existing rows
    # get 'unknown': their origin cannot be reconstructed honestly.
    ("2026_09_data_origin", """
        ALTER TABLE nas_events   ADD COLUMN IF NOT EXISTS data_origin TEXT NOT NULL DEFAULT 'unknown';
        ALTER TABLE alert_events ADD COLUMN IF NOT EXISTS data_origin TEXT NOT NULL DEFAULT 'unknown';
        CREATE INDEX IF NOT EXISTS idx_nas_events_origin_time
            ON nas_events (data_origin, received_time_utc DESC);
    """),
    # One row per model call: the durable projection of the call's
    # OpenTelemetry chat span (src/telemetry.py, src/model_telemetry.py) --
    # the policy decision, outcome class, wall time, tokens and cost that
    # `python -m src.readiness` measures. trace_id links the row to the span;
    # `subject` is what the call was about (an investigation_id, or the
    # alert_id for the Critic). No prompt or response text is kept.
    # Append-only like the other evidence tables: a readiness verdict is only
    # worth something if the record behind it cannot be edited to look better.
    ("2026_09_model_calls", """
        CREATE TABLE IF NOT EXISTS model_calls (
            call_id            TEXT PRIMARY KEY,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            correlation_id     TEXT,
            trace_id           TEXT,
            subject            TEXT,
            role               TEXT NOT NULL,
            model_alias        TEXT NOT NULL,
            served_model       TEXT,
            provider           TEXT NOT NULL,
            outcome            TEXT NOT NULL,
            attempts           INTEGER NOT NULL,
            repaired           BOOLEAN NOT NULL DEFAULT FALSE,
            latency_ms         DOUBLE PRECISION NOT NULL,
            prompt_tokens      INTEGER,
            completion_tokens  INTEGER,
            cost_usd           DOUBLE PRECISION,
            policy_action      TEXT NOT NULL,
            policy_rule        TEXT,
            error_type         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_model_calls_created_at ON model_calls (created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_model_calls_subject ON model_calls (subject);

        CREATE OR REPLACE FUNCTION model_calls_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'model_calls is append-only; % is not permitted on this table', TG_OP;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS trg_model_calls_append_only ON model_calls;
        CREATE TRIGGER trg_model_calls_append_only
            BEFORE UPDATE OR DELETE ON model_calls
            FOR EACH ROW EXECUTE FUNCTION model_calls_append_only();
    """),
    # One review per condition, not per reading (src/execution_gate.py): a
    # staged action is an incident for its condition -- alert type, airport,
    # service -- and staged_action_alerts records every CRITICAL alert it
    # covers, its own first alert included, so "is this alert staged?" has one
    # answer. Existing actions cover the alert they were staged from.
    ("2026_09_staged_action_conditions", """
        ALTER TABLE staged_actions ADD COLUMN IF NOT EXISTS condition_key TEXT;
        UPDATE staged_actions SET condition_key = concat_ws('|', alert_type, airport_code, service_name)
            WHERE condition_key IS NULL;
        CREATE INDEX IF NOT EXISTS idx_staged_actions_condition ON staged_actions (condition_key, created_at DESC);

        CREATE TABLE IF NOT EXISTS staged_action_alerts (
            alert_id    TEXT PRIMARY KEY REFERENCES alert_events(alert_id) ON DELETE CASCADE,
            action_id   TEXT NOT NULL REFERENCES staged_actions(action_id) ON DELETE CASCADE,
            attached_at TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_staged_action_alerts_action ON staged_action_alerts (action_id, attached_at);
        INSERT INTO staged_action_alerts (alert_id, action_id, attached_at)
            SELECT alert_id, action_id, coalesce(created_at, now()) FROM staged_actions WHERE alert_id IS NOT NULL
            ON CONFLICT (alert_id) DO NOTHING;
    """),
]


async def run_migrations(pool: asyncpg.Pool) -> None:
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


async def init_schema(pool: asyncpg.Pool) -> None:
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
                decision_note       TEXT,
                overridden_recommendation TEXT,
                overridden_severity       TEXT
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

            CREATE TABLE IF NOT EXISTS ingest_log (
                id            BIGSERIAL PRIMARY KEY,
                file_name     TEXT NOT NULL,
                status        TEXT NOT NULL,
                event_count   INTEGER,
                services      JSONB,
                parse_errors  INTEGER,
                error         TEXT,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            -- Indexes to optimize analytical reads and FK constraint checks
            CREATE INDEX IF NOT EXISTS idx_ingest_log_file_name ON ingest_log (file_name);
            CREATE INDEX IF NOT EXISTS idx_ingest_log_created_at ON ingest_log (id DESC);
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

        # ── Append-only enforcement: action_audit_log ────────────────────────
        #
        # The HITL audit trail is the evidence that a human, not the system,
        # authorised each escalation decision. Two things previously weakened
        # that as evidence:
        #
        #   1. created_at was nullable and supplied by the application, so a
        #      row's timestamp was only as trustworthy as the process writing
        #      it -- including being backdatable or simply absent. DEFAULT
        #      now() makes the database the clock.
        #
        #   2. Nothing stopped an UPDATE or DELETE. The application only ever
        #      INSERTs (src/storage/repositories/audit_repo.py), but "our code
        #      does not do that" is a convention, not a control. A reviewer
        #      asking "what stops this table being rewritten?" deserves a
        #      better answer than a grep.
        #
        # Known limitation, stated rather than papered over: a row-level
        # BEFORE UPDATE OR DELETE trigger does not block TRUNCATE, and the
        # table owner can drop the trigger. TRUNCATE is in fact relied upon by
        # src/storage/test_support.py:reset_db(). Closing that hole properly
        # means the application role not owning this table -- a deployment/
        # privilege change, not a schema one. This guard raises the bar from
        # "nothing prevents it" to "an ordinary write path cannot do it".
        await conn.execute("""
            UPDATE action_audit_log SET created_at = now() WHERE created_at IS NULL;
            ALTER TABLE action_audit_log ALTER COLUMN created_at SET DEFAULT now();
            ALTER TABLE action_audit_log ALTER COLUMN created_at SET NOT NULL;

            CREATE OR REPLACE FUNCTION action_audit_log_append_only()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION
                    'action_audit_log is append-only; % is not permitted on this table',
                    TG_OP;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trg_action_audit_log_append_only ON action_audit_log;
            CREATE TRIGGER trg_action_audit_log_append_only
                BEFORE UPDATE OR DELETE ON action_audit_log
                FOR EACH ROW EXECUTE FUNCTION action_audit_log_append_only();
        """)
