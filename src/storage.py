"""
SQLite persistence layer for NAS FeedOps Sentinel.

Uses a single module-level connection (suitable for single-process use).
Call reset_db() in tests to start with a clean slate.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from src import config
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

_CONN: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _CONN = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _init_schema(_CONN)
    return _CONN


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS nas_events (
            event_id            TEXT PRIMARY KEY,
            airport_code        TEXT,
            tracon              TEXT,
            service_name        TEXT,
            message_type        TEXT,
            event_time_utc      TEXT,
            received_time_utc   TEXT,
            latency_ms          REAL,
            runway              TEXT,
            rvr_touchdown       INTEGER,
            rvr_midpoint        INTEGER,
            rvr_rollout         INTEGER,
            track_id            TEXT,
            aircraft_id         TEXT,
            vehicle_id          TEXT,
            surface_event_type  TEXT,
            lat                 REAL,
            lon                 REAL,
            altitude_ft         INTEGER,
            ground_speed_kt     INTEGER,
            vertical_speed_fpm  INTEGER,
            flight_phase        TEXT,
            flight_rules        TEXT,
            beacon_code         TEXT,
            feed_status         TEXT,
            validation_status   TEXT,
            validation_errors   TEXT,
            raw_message_path    TEXT
        );

        -- Lightweight migration: add spatial cols to pre-existing DBs.
        -- Each ALTER is a no-op if the column already exists; failure is silent.
        """)
    for col_def in (
        "ALTER TABLE nas_events ADD COLUMN lat REAL",
        "ALTER TABLE nas_events ADD COLUMN lon REAL",
        "ALTER TABLE nas_events ADD COLUMN altitude_ft INTEGER",
        "ALTER TABLE nas_events ADD COLUMN ground_speed_kt INTEGER",
        "ALTER TABLE nas_events ADD COLUMN vertical_speed_fpm INTEGER",
        "ALTER TABLE nas_events ADD COLUMN flight_phase TEXT",
        "ALTER TABLE nas_events ADD COLUMN flight_rules TEXT",
        "ALTER TABLE nas_events ADD COLUMN beacon_code TEXT",
    ):
        try:
            conn.execute(col_def)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.executescript("""

        CREATE TABLE IF NOT EXISTS alert_events (
            alert_id     TEXT PRIMARY KEY,
            alert_type   TEXT,
            severity     TEXT,
            airport_code TEXT,
            service_name TEXT,
            description  TEXT,
            triggered_at TEXT,
            event_id     TEXT
        );

        CREATE TABLE IF NOT EXISTS critic_results (
            alert_id     TEXT PRIMARY KEY,
            confidence   REAL,
            reasoning    TEXT,
            citations    TEXT,
            pattern      TEXT,
            escalate     INTEGER,
            evaluated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS planner_results (
            result_id         TEXT PRIMARY KEY,
            question          TEXT,
            answer            TEXT,
            sub_queries       TEXT,
            confidence        REAL,
            citations         TEXT,
            recommended_actions TEXT,
            evaluated_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS staged_actions (
            action_id           TEXT PRIMARY KEY,
            alert_id            TEXT,
            event_id            TEXT,
            airport_code        TEXT,
            service_name        TEXT,
            severity            TEXT,
            alert_type          TEXT,
            recommended_action  TEXT,
            rationale           TEXT,
            critic_confidence   REAL,
            critic_pattern      TEXT,
            status              TEXT NOT NULL DEFAULT 'PENDING',
            idempotency_key     TEXT UNIQUE,
            created_at          TEXT,
            decided_at          TEXT,
            decided_by          TEXT,
            decision_note       TEXT
        );

        CREATE TABLE IF NOT EXISTS action_audit_log (
            audit_id        TEXT PRIMARY KEY,
            action_id       TEXT,
            previous_status TEXT,
            new_status      TEXT,
            actor           TEXT,
            note            TEXT,
            created_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS watch_officer_briefs (
            brief_id                       TEXT PRIMARY KEY,
            generated_at                   TEXT,
            posture                        TEXT,
            trust_assessment               TEXT,
            headline                       TEXT,
            what_changed_json              TEXT,
            why_it_matters_json            TEXT,
            recommended_human_review_json  TEXT,
            evidence_json                  TEXT,
            affected_airports_json         TEXT,
            pending_actions_count          INTEGER,
            critical_alert_count           INTEGER,
            confidence                     REAL,
            llm_used                       INTEGER,
            limitations_json               TEXT
        );

        CREATE TABLE IF NOT EXISTS operational_tickets (
            ticket_id              TEXT PRIMARY KEY,
            action_id              TEXT,
            alert_id               TEXT,
            brief_id               TEXT,
            generated_at           TEXT,
            title                  TEXT,
            severity               TEXT,
            affected_airport       TEXT,
            affected_service       TEXT,
            posture                TEXT,
            trust_assessment       TEXT,
            summary                TEXT,
            what_changed_json      TEXT,
            why_it_matters_json    TEXT,
            evidence_json          TEXT,
            recommended_review_json TEXT,
            critic_rationale       TEXT,
            status                 TEXT NOT NULL DEFAULT 'DRAFT',
            created_by_agent       TEXT,
            decided_by             TEXT,
            decided_at             TEXT,
            decision_note          TEXT,
            idempotency_key        TEXT UNIQUE
        );
    """)
    conn.commit()


# ── Write operations ──────────────────────────────────────────────────────────

_INSERT_EVENT_SQL = """
INSERT OR REPLACE INTO nas_events (
    event_id, airport_code, tracon, service_name, message_type,
    event_time_utc, received_time_utc, latency_ms, runway,
    rvr_touchdown, rvr_midpoint, rvr_rollout, track_id,
    aircraft_id, vehicle_id, surface_event_type,
    lat, lon, altitude_ft, ground_speed_kt, vertical_speed_fpm,
    flight_phase, flight_rules, beacon_code,
    feed_status, validation_status, validation_errors, raw_message_path
) VALUES (
    :event_id, :airport_code, :tracon, :service_name, :message_type,
    :event_time_utc, :received_time_utc, :latency_ms, :runway,
    :rvr_touchdown, :rvr_midpoint, :rvr_rollout, :track_id,
    :aircraft_id, :vehicle_id, :surface_event_type,
    :lat, :lon, :altitude_ft, :ground_speed_kt, :vertical_speed_fpm,
    :flight_phase, :flight_rules, :beacon_code,
    :feed_status, :validation_status, :validation_errors, :raw_message_path
)
"""

_INSERT_ALERT_SQL = """
INSERT OR REPLACE INTO alert_events VALUES (
    :alert_id, :alert_type, :severity, :airport_code, :service_name,
    :description, :triggered_at, :event_id
)
"""


def _event_to_row(event: NASEvent) -> dict:
    return {
        **event.model_dump(),
        "event_time_utc": event.event_time_utc.isoformat(),
        "received_time_utc": event.received_time_utc.isoformat(),
        "validation_errors": json.dumps(event.validation_errors),
    }


def _alert_to_row(alert: AlertEvent) -> dict:
    return {
        **alert.model_dump(),
        "triggered_at": alert.triggered_at.isoformat(),
    }


def save_event(event: NASEvent) -> None:
    conn = _get_conn()
    conn.execute(_INSERT_EVENT_SQL, _event_to_row(event))
    conn.commit()


def save_alert(alert: AlertEvent) -> None:
    conn = _get_conn()
    conn.execute(_INSERT_ALERT_SQL, _alert_to_row(alert))
    conn.commit()


def save_events(events: list[NASEvent]) -> None:
    if not events:
        return
    conn = _get_conn()
    rows = [_event_to_row(e) for e in events]
    conn.executemany(_INSERT_EVENT_SQL, rows)
    conn.commit()


def save_alerts(alerts: list[AlertEvent]) -> None:
    if not alerts:
        return
    conn = _get_conn()
    rows = [_alert_to_row(a) for a in alerts]
    conn.executemany(_INSERT_ALERT_SQL, rows)
    conn.commit()


# ── Read operations ───────────────────────────────────────────────────────────


def get_recent_events(limit: int = 500) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM nas_events ORDER BY received_time_utc DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_alerts(limit: int = 200) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM alert_events ORDER BY triggered_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_latest_rvr() -> list[dict]:
    """Latest RVR reading per airport+runway combination."""
    conn = _get_conn()
    rows = conn.execute("""
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
    """).fetchall()
    return [dict(r) for r in rows]


def get_event_counts_by_service() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("""
        SELECT service_name, airport_code, COUNT(*) AS count
        FROM nas_events
        GROUP BY service_name, airport_code
        ORDER BY service_name, airport_code
    """).fetchall()
    return [dict(r) for r in rows]


def get_validation_summary() -> dict:
    conn = _get_conn()
    row = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN validation_status = 'PASSED' THEN 1 ELSE 0 END) AS passed,
            SUM(CASE WHEN validation_status = 'FAILED' THEN 1 ELSE 0 END) AS failed
        FROM nas_events
    """).fetchone()
    return dict(row) if row else {"total": 0, "passed": 0, "failed": 0}


def get_events_for_critic(
    airport_code: str | None,
    window_minutes: int = 30,
    limit: int = 50,
) -> list[dict]:
    """Return a lightweight event summary for the critic context window."""
    conn = _get_conn()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    ).isoformat()
    if airport_code:
        rows = conn.execute(
            """SELECT event_id, airport_code, service_name, latency_ms, feed_status,
                      rvr_touchdown, rvr_midpoint, rvr_rollout, runway,
                      received_time_utc, validation_status
               FROM nas_events
               WHERE airport_code = ? AND received_time_utc >= ?
               ORDER BY received_time_utc DESC LIMIT ?""",
            (airport_code, cutoff, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT event_id, airport_code, service_name, latency_ms, feed_status,
                      rvr_touchdown, rvr_midpoint, rvr_rollout, runway,
                      received_time_utc, validation_status
               FROM nas_events
               WHERE received_time_utc >= ?
               ORDER BY received_time_utc DESC LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_alerts_for_critic(
    airport_code: str | None,
    window_minutes: int = 60,
    limit: int = 10,
) -> list[dict]:
    """Return recent alerts for the critic/planner context window."""
    conn = _get_conn()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    ).isoformat()
    if airport_code:
        rows = conn.execute(
            """SELECT alert_id, alert_type, severity, airport_code, service_name,
                      description, triggered_at
               FROM alert_events
               WHERE airport_code = ? AND triggered_at >= ?
               ORDER BY triggered_at DESC LIMIT ?""",
            (airport_code, cutoff, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT alert_id, alert_type, severity, airport_code, service_name,
                      description, triggered_at
               FROM alert_events
               WHERE triggered_at >= ?
               ORDER BY triggered_at DESC LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_critic_results(limit: int = 50) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM critic_results ORDER BY evaluated_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def save_planner_result(result: PlannerResult) -> None:
    import uuid

    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO planner_results VALUES (
            :result_id, :question, :answer, :sub_queries, :confidence,
            :citations, :recommended_actions, :evaluated_at
        )""",
        {
            "result_id": str(uuid.uuid4()),
            "question": result.question,
            "answer": result.answer,
            "sub_queries": json.dumps(result.sub_queries),
            "confidence": result.confidence,
            "citations": json.dumps(result.citations),
            "recommended_actions": json.dumps(result.recommended_actions),
            "evaluated_at": result.evaluated_at.isoformat(),
        },
    )
    conn.commit()


def get_recent_planner_results(limit: int = 10) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM planner_results ORDER BY evaluated_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def save_critic_result(result: CriticResult) -> None:
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO critic_results VALUES (
            :alert_id, :confidence, :reasoning, :citations,
            :pattern, :escalate, :evaluated_at
        )""",
        {
            **result.model_dump(),
            "citations": json.dumps(result.citations),
            "escalate": int(result.escalate),
            "evaluated_at": result.evaluated_at.isoformat(),
        },
    )
    conn.commit()


def get_active_aircraft(window_minutes: int = 5, limit: int = 500) -> list[dict]:
    """Latest position per aircraft seen in the last N minutes (for airspace map)."""
    conn = _get_conn()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    ).isoformat()
    rows = conn.execute(
        """
        SELECT e.*
        FROM nas_events e
        INNER JOIN (
            SELECT aircraft_id, MAX(received_time_utc) AS max_time
            FROM nas_events
            WHERE aircraft_id IS NOT NULL
              AND lat IS NOT NULL AND lon IS NOT NULL
              AND received_time_utc >= ?
            GROUP BY aircraft_id
        ) latest
          ON e.aircraft_id = latest.aircraft_id
         AND e.received_time_utc = latest.max_time
        ORDER BY e.received_time_utc DESC
        LIMIT ?
        """,
        (cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_flight_phase_counts(window_minutes: int = 15) -> dict:
    """Count distinct aircraft by flight_phase within window (per airport)."""
    conn = _get_conn()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    ).isoformat()
    rows = conn.execute(
        """
        SELECT airport_code,
               COALESCE(flight_phase, 'UNKNOWN') AS phase,
               COUNT(DISTINCT aircraft_id) AS n
        FROM nas_events
        WHERE service_name = 'TERMINAL_AUTO'
          AND received_time_utc >= ?
          AND aircraft_id IS NOT NULL
        GROUP BY airport_code, phase
        """,
        (cutoff,),
    ).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["airport_code"], {})[r["phase"]] = r["n"]
    return out


def get_altitude_distribution(window_minutes: int = 15) -> list[dict]:
    """Altitudes for current aircraft (for histogram)."""
    conn = _get_conn()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    ).isoformat()
    rows = conn.execute(
        """
        SELECT airport_code, altitude_ft, ground_speed_kt
        FROM nas_events
        WHERE service_name = 'TERMINAL_AUTO'
          AND received_time_utc >= ?
          AND altitude_ft IS NOT NULL
        """,
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_latency_stats() -> dict:
    conn = _get_conn()
    row = conn.execute("""
        SELECT
            AVG(latency_ms) AS avg_ms,
            MIN(latency_ms) AS min_ms,
            MAX(latency_ms) AS max_ms
        FROM nas_events
        WHERE latency_ms IS NOT NULL
    """).fetchone()
    return dict(row) if row else {"avg_ms": None, "min_ms": None, "max_ms": None}


# ── Test helpers ──────────────────────────────────────────────────────────────


def reset_db() -> None:
    """Drop and recreate all tables. Use only in tests."""
    global _CONN
    conn = _get_conn()
    conn.executescript("""
        DROP TABLE IF EXISTS nas_events;
        DROP TABLE IF EXISTS alert_events;
        DROP TABLE IF EXISTS critic_results;
        DROP TABLE IF EXISTS planner_results;
        DROP TABLE IF EXISTS staged_actions;
        DROP TABLE IF EXISTS action_audit_log;
        DROP TABLE IF EXISTS hitl_staged_actions;
        DROP TABLE IF EXISTS watch_officer_briefs;
        DROP TABLE IF EXISTS operational_tickets;
    """)
    conn.commit()
    _init_schema(conn)


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
        created_at=datetime.fromisoformat(d["created_at"])
        if d.get("created_at")
        else datetime.now(timezone.utc),
        decided_at=datetime.fromisoformat(d["decided_at"])
        if d.get("decided_at")
        else None,
        decided_by=d.get("decided_by"),
        decision_note=d.get("decision_note"),
    )


def create_staged_action(action: StagedAction) -> StagedAction:
    """
    Insert a staged action. Idempotent on idempotency_key — if a row already
    exists with the same key, returns the existing row UNCHANGED rather than
    overwriting. Caller can compare action.action_id == returned.action_id to
    detect a duplicate.
    """
    if not action.idempotency_key:
        raise ValueError("StagedAction.idempotency_key is required")

    conn = _get_conn()
    existing = conn.execute(
        "SELECT * FROM staged_actions WHERE idempotency_key = ?",
        (action.idempotency_key,),
    ).fetchone()
    if existing:
        return _row_to_staged_action(existing)

    conn.execute(
        """
        INSERT INTO staged_actions (
            action_id, alert_id, event_id, airport_code, service_name,
            severity, alert_type, recommended_action, rationale,
            critic_confidence, critic_pattern, status, idempotency_key,
            created_at, decided_at, decided_by, decision_note
        ) VALUES (
            :action_id, :alert_id, :event_id, :airport_code, :service_name,
            :severity, :alert_type, :recommended_action, :rationale,
            :critic_confidence, :critic_pattern, :status, :idempotency_key,
            :created_at, :decided_at, :decided_by, :decision_note
        )
        """,
        {
            **action.model_dump(),
            "created_at": action.created_at.isoformat(),
            "decided_at": action.decided_at.isoformat() if action.decided_at else None,
        },
    )
    _write_audit(
        action.action_id,
        previous_status="(new)",
        new_status=action.status,
        actor="system",
        note="staged from CRITICAL alert",
    )
    conn.commit()
    return action


def _write_audit(
    action_id: str, previous_status: str, new_status: str, actor: str, note: str = ""
) -> None:
    """Internal: append a row to action_audit_log. Single source of truth for state transitions."""
    entry = ActionAuditEntry(
        action_id=action_id,
        previous_status=previous_status,
        new_status=new_status,
        actor=actor,
        note=note,
    )
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO action_audit_log (
            audit_id, action_id, previous_status, new_status, actor, note, created_at
        ) VALUES (
            :audit_id, :action_id, :previous_status, :new_status, :actor, :note, :created_at
        )
        """,
        {**entry.model_dump(), "created_at": entry.created_at.isoformat()},
    )


def get_pending_staged_actions() -> list[StagedAction]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM staged_actions WHERE status = 'PENDING' ORDER BY created_at DESC"
    ).fetchall()
    return [_row_to_staged_action(r) for r in rows]


def get_recent_staged_actions(limit: int = 25) -> list[StagedAction]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM staged_actions ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_staged_action(r) for r in rows]


def _decide_staged_action(
    action_id: str, new_status: str, actor: str, note: str
) -> StagedAction | None:
    """
    Transition a staged action to APPROVED or REJECTED.
    Idempotent: if the action is already in a terminal state, returns the
    current state unchanged (does NOT overwrite or re-log).
    """
    if new_status not in ("APPROVED", "REJECTED"):
        raise ValueError(f"new_status must be APPROVED or REJECTED, got {new_status}")

    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM staged_actions WHERE action_id = ?", (action_id,)
    ).fetchone()
    if row is None:
        return None

    current_status = row["status"]
    if current_status in ("APPROVED", "REJECTED", "EXPIRED"):
        # Terminal — return current state, do not modify or log.
        return _row_to_staged_action(row)

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE staged_actions
           SET status = ?, decided_at = ?, decided_by = ?, decision_note = ?
           WHERE action_id = ?""",
        (new_status, now, actor, note, action_id),
    )
    _write_audit(
        action_id,
        previous_status=current_status,
        new_status=new_status,
        actor=actor,
        note=note,
    )
    conn.commit()

    updated = conn.execute(
        "SELECT * FROM staged_actions WHERE action_id = ?", (action_id,)
    ).fetchone()
    return _row_to_staged_action(updated) if updated else None


def approve_staged_action(
    action_id: str, actor: str = "dashboard_operator", note: str = ""
) -> StagedAction | None:
    return _decide_staged_action(action_id, "APPROVED", actor, note)


def reject_staged_action(
    action_id: str, actor: str = "dashboard_operator", note: str = ""
) -> StagedAction | None:
    return _decide_staged_action(action_id, "REJECTED", actor, note)


# ── Watch Officer Brief persistence ──────────────────────────────────────────


def _row_to_brief(row) -> WatchOfficerBrief:
    d = dict(row)
    return WatchOfficerBrief(
        brief_id=d["brief_id"],
        generated_at=datetime.fromisoformat(d["generated_at"])
        if d.get("generated_at")
        else datetime.now(timezone.utc),
        posture=d.get("posture", "UNKNOWN"),
        trust_assessment=d.get("trust_assessment", "UNKNOWN"),
        headline=d.get("headline") or "",
        what_changed=json.loads(d.get("what_changed_json") or "[]"),
        why_it_matters=json.loads(d.get("why_it_matters_json") or "[]"),
        recommended_human_review=json.loads(
            d.get("recommended_human_review_json") or "[]"
        ),
        evidence=json.loads(d.get("evidence_json") or "[]"),
        affected_airports=json.loads(d.get("affected_airports_json") or "[]"),
        pending_actions_count=d.get("pending_actions_count") or 0,
        critical_alert_count=d.get("critical_alert_count") or 0,
        confidence=d.get("confidence") or 0.5,
        llm_used=bool(d.get("llm_used")),
        limitations=json.loads(d.get("limitations_json") or "[]"),
    )


def save_watch_officer_brief(brief: WatchOfficerBrief) -> WatchOfficerBrief:
    conn = _get_conn()
    conn.execute(
        """
        INSERT OR REPLACE INTO watch_officer_briefs (
            brief_id, generated_at, posture, trust_assessment, headline,
            what_changed_json, why_it_matters_json, recommended_human_review_json,
            evidence_json, affected_airports_json,
            pending_actions_count, critical_alert_count, confidence,
            llm_used, limitations_json
        ) VALUES (
            :brief_id, :generated_at, :posture, :trust_assessment, :headline,
            :what_changed_json, :why_it_matters_json, :recommended_human_review_json,
            :evidence_json, :affected_airports_json,
            :pending_actions_count, :critical_alert_count, :confidence,
            :llm_used, :limitations_json
        )
        """,
        {
            "brief_id": brief.brief_id,
            "generated_at": brief.generated_at.isoformat(),
            "posture": brief.posture,
            "trust_assessment": brief.trust_assessment,
            "headline": brief.headline,
            "what_changed_json": json.dumps(brief.what_changed),
            "why_it_matters_json": json.dumps(brief.why_it_matters),
            "recommended_human_review_json": json.dumps(brief.recommended_human_review),
            "evidence_json": json.dumps(brief.evidence),
            "affected_airports_json": json.dumps(brief.affected_airports),
            "pending_actions_count": brief.pending_actions_count,
            "critical_alert_count": brief.critical_alert_count,
            "confidence": brief.confidence,
            "llm_used": int(brief.llm_used),
            "limitations_json": json.dumps(brief.limitations),
        },
    )
    conn.commit()
    return brief


def get_latest_watch_officer_brief() -> WatchOfficerBrief | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM watch_officer_briefs ORDER BY generated_at DESC LIMIT 1"
    ).fetchone()
    return _row_to_brief(row) if row else None


def get_recent_watch_officer_briefs(limit: int = 10) -> list[WatchOfficerBrief]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM watch_officer_briefs ORDER BY generated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_brief(r) for r in rows]


# ── Operational Ticket persistence ────────────────────────────────────────────


def _row_to_ticket(row) -> OperationalTicket:
    d = dict(row)
    return OperationalTicket(
        ticket_id=d["ticket_id"],
        action_id=d.get("action_id"),
        alert_id=d.get("alert_id"),
        brief_id=d.get("brief_id"),
        generated_at=datetime.fromisoformat(d["generated_at"])
        if d.get("generated_at")
        else datetime.now(timezone.utc),
        title=d.get("title") or "",
        severity=d.get("severity") or "CRITICAL",
        affected_airport=d.get("affected_airport"),
        affected_service=d.get("affected_service"),
        posture=d.get("posture") or "ACTION",
        trust_assessment=d.get("trust_assessment") or "DEGRADED",
        summary=d.get("summary") or "",
        what_changed=json.loads(d.get("what_changed_json") or "[]"),
        why_it_matters=json.loads(d.get("why_it_matters_json") or "[]"),
        evidence=json.loads(d.get("evidence_json") or "[]"),
        recommended_review=json.loads(d.get("recommended_review_json") or "[]"),
        critic_rationale=d.get("critic_rationale"),
        status=d.get("status") or "DRAFT",
        created_by_agent=d.get("created_by_agent") or "watch_officer",
        decided_by=d.get("decided_by"),
        decided_at=datetime.fromisoformat(d["decided_at"])
        if d.get("decided_at")
        else None,
        decision_note=d.get("decision_note"),
        idempotency_key=d.get("idempotency_key") or "",
    )


def create_operational_ticket(ticket: OperationalTicket) -> OperationalTicket:
    """Idempotent on idempotency_key. Returns existing ticket if one exists."""
    if not ticket.idempotency_key:
        raise ValueError("OperationalTicket.idempotency_key is required")

    conn = _get_conn()
    existing = conn.execute(
        "SELECT * FROM operational_tickets WHERE idempotency_key = ?",
        (ticket.idempotency_key,),
    ).fetchone()
    if existing:
        return _row_to_ticket(existing)

    conn.execute(
        """
        INSERT INTO operational_tickets (
            ticket_id, action_id, alert_id, brief_id, generated_at,
            title, severity, affected_airport, affected_service,
            posture, trust_assessment, summary,
            what_changed_json, why_it_matters_json, evidence_json,
            recommended_review_json, critic_rationale, status,
            created_by_agent, decided_by, decided_at, decision_note,
            idempotency_key
        ) VALUES (
            :ticket_id, :action_id, :alert_id, :brief_id, :generated_at,
            :title, :severity, :affected_airport, :affected_service,
            :posture, :trust_assessment, :summary,
            :what_changed_json, :why_it_matters_json, :evidence_json,
            :recommended_review_json, :critic_rationale, :status,
            :created_by_agent, :decided_by, :decided_at, :decision_note,
            :idempotency_key
        )
        """,
        {
            "ticket_id": ticket.ticket_id,
            "action_id": ticket.action_id,
            "alert_id": ticket.alert_id,
            "brief_id": ticket.brief_id,
            "generated_at": ticket.generated_at.isoformat(),
            "title": ticket.title,
            "severity": ticket.severity,
            "affected_airport": ticket.affected_airport,
            "affected_service": ticket.affected_service,
            "posture": ticket.posture,
            "trust_assessment": ticket.trust_assessment,
            "summary": ticket.summary,
            "what_changed_json": json.dumps(ticket.what_changed),
            "why_it_matters_json": json.dumps(ticket.why_it_matters),
            "evidence_json": json.dumps(ticket.evidence),
            "recommended_review_json": json.dumps(ticket.recommended_review),
            "critic_rationale": ticket.critic_rationale,
            "status": ticket.status,
            "created_by_agent": ticket.created_by_agent,
            "decided_by": ticket.decided_by,
            "decided_at": ticket.decided_at.isoformat() if ticket.decided_at else None,
            "decision_note": ticket.decision_note,
            "idempotency_key": ticket.idempotency_key,
        },
    )
    # First state-transition row: nothing -> DRAFT, written by the creating agent.
    _write_audit(
        action_id=f"ticket:{ticket.ticket_id}",
        previous_status="(new)",
        new_status="DRAFT",
        actor=ticket.created_by_agent,
        note=f"escalation package drafted: {ticket.title[:80]}",
    )
    conn.commit()
    return ticket


def get_draft_operational_tickets() -> list[OperationalTicket]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM operational_tickets WHERE status = 'DRAFT' ORDER BY generated_at DESC"
    ).fetchall()
    return [_row_to_ticket(r) for r in rows]


def get_recent_operational_tickets(limit: int = 25) -> list[OperationalTicket]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM operational_tickets ORDER BY generated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_ticket(r) for r in rows]


def get_ticket_by_action_id(action_id: str) -> OperationalTicket | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM operational_tickets WHERE action_id = ? ORDER BY generated_at DESC LIMIT 1",
        (action_id,),
    ).fetchone()
    return _row_to_ticket(row) if row else None


def _transition_ticket(
    ticket_id: str, new_status: str, actor: str, note: str
) -> OperationalTicket | None:
    if new_status not in ("APPROVED_FOR_ESCALATION", "REJECTED", "MONITORING"):
        raise ValueError(f"new_status {new_status} not permitted")

    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM operational_tickets WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    if row is None:
        return None

    current = row["status"]
    # Terminal states (APPROVED_FOR_ESCALATION, REJECTED) are sticky.
    # MONITORING is non-terminal — can later be promoted to APPROVED or REJECTED.
    if current in ("APPROVED_FOR_ESCALATION", "REJECTED"):
        return _row_to_ticket(row)

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE operational_tickets
           SET status = ?, decided_at = ?, decided_by = ?, decision_note = ?
           WHERE ticket_id = ?""",
        (new_status, now, actor, note, ticket_id),
    )
    _write_audit(
        action_id=f"ticket:{ticket_id}",
        previous_status=current,
        new_status=new_status,
        actor=actor,
        note=note,
    )
    conn.commit()
    updated = conn.execute(
        "SELECT * FROM operational_tickets WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    return _row_to_ticket(updated) if updated else None


def approve_operational_ticket(
    ticket_id: str, actor: str = "dashboard_operator", note: str = ""
) -> OperationalTicket | None:
    return _transition_ticket(ticket_id, "APPROVED_FOR_ESCALATION", actor, note)


def reject_operational_ticket(
    ticket_id: str, actor: str = "dashboard_operator", note: str = ""
) -> OperationalTicket | None:
    return _transition_ticket(ticket_id, "REJECTED", actor, note)


def mark_operational_ticket_monitoring(
    ticket_id: str, actor: str = "dashboard_operator", note: str = ""
) -> OperationalTicket | None:
    return _transition_ticket(ticket_id, "MONITORING", actor, note)


def get_action_audit_log(action_id: str | None = None, limit: int = 50) -> list[dict]:
    conn = _get_conn()
    if action_id:
        rows = conn.execute(
            """SELECT * FROM action_audit_log WHERE action_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (action_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM action_audit_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def override_db_path(path: str) -> None:
    """Redirect the connection to a different DB file (e.g. :memory: in tests)."""
    global _CONN
    _CONN = sqlite3.connect(path, check_same_thread=False)
    _CONN.row_factory = sqlite3.Row
    _init_schema(_CONN)
