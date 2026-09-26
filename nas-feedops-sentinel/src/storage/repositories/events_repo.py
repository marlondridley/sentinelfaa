"""
Repository for raw NAS telemetry (`nas_events`, `alert_events`) -- the
highest-volume tables in this schema, written on every ingest pipeline run and read
by the dashboard, the Critic agent, and the aircraft/latency panels.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import asyncpg

from src.models import AlertEvent, NASEvent


class EventsRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def save_event(self, event: NASEvent) -> None:
        query = """
            INSERT INTO nas_events (
                event_id, airport_code, tracon, service_name, message_type,
                event_time_utc, received_time_utc, latency_ms, runway,
                rvr_touchdown, rvr_midpoint, rvr_rollout, track_id,
                aircraft_id, vehicle_id, surface_event_type,
                lat, lon, altitude_ft, ground_speed_kt, vertical_speed_fpm,
                flight_phase, flight_rules, beacon_code,
                feed_status, validation_status, validation_errors, raw_message_path,
                status_code, data_origin
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28,
                $29, $30
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
                raw_message_path = EXCLUDED.raw_message_path,
                status_code = EXCLUDED.status_code,
                data_origin = EXCLUDED.data_origin;
        """
        await self._pool.execute(
            query,
            event.event_id, event.airport_code, event.tracon, event.service_name, event.message_type,
            event.event_time_utc, event.received_time_utc, event.latency_ms, event.runway,
            event.rvr_touchdown, event.rvr_midpoint, event.rvr_rollout, event.track_id,
            event.aircraft_id, event.vehicle_id, event.surface_event_type,
            event.lat, event.lon, event.altitude_ft, event.ground_speed_kt, event.vertical_speed_fpm,
            event.flight_phase, event.flight_rules, event.beacon_code,
            event.feed_status, event.validation_status, json.dumps(event.validation_errors), event.raw_message_path,
            event.status_code, event.data_origin,
        )

    async def save_alert(self, alert: AlertEvent) -> None:
        query = """
            INSERT INTO alert_events (
                alert_id, alert_type, severity, airport_code, service_name,
                description, triggered_at, event_id, data_origin
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (alert_id) DO UPDATE SET
                alert_type = EXCLUDED.alert_type,
                severity = EXCLUDED.severity,
                airport_code = EXCLUDED.airport_code,
                service_name = EXCLUDED.service_name,
                description = EXCLUDED.description,
                triggered_at = EXCLUDED.triggered_at,
                event_id = EXCLUDED.event_id,
                data_origin = EXCLUDED.data_origin;
        """
        await self._pool.execute(
            query,
            alert.alert_id, alert.alert_type, alert.severity, alert.airport_code,
            alert.service_name, alert.description, alert.triggered_at, alert.event_id,
            alert.data_origin,
        )

    async def save_events(self, events: list[NASEvent]) -> None:
        for event in events:
            await self.save_event(event)

    async def save_alerts(self, alerts: list[AlertEvent]) -> None:
        for alert in alerts:
            await self.save_alert(alert)

    async def get_recent_events(self, limit: int = 500) -> list[dict]:
        # NOTE: column list mirrors the full nas_events schema (see schema.py) so the
        # query is an explicit contract rather than SELECT *. If dashboard/metrics callers
        # are confirmed not to need validation_errors (JSONB) or raw_message_path, drop them
        # here to cut bytes-over-wire on this hot path.
        rows = await self._pool.fetch(
            """SELECT event_id, airport_code, tracon, service_name, message_type,
                      event_time_utc, received_time_utc, latency_ms, runway,
                      rvr_touchdown, rvr_midpoint, rvr_rollout, track_id,
                      aircraft_id, vehicle_id, surface_event_type,
                      lat, lon, altitude_ft, ground_speed_kt, vertical_speed_fpm,
                      flight_phase, flight_rules, beacon_code,
                      feed_status, validation_status, validation_errors, raw_message_path,
                      status_code, data_origin
               FROM nas_events ORDER BY received_time_utc DESC LIMIT $1""",
            limit,
        )
        return [dict(r) for r in rows]

    async def get_recent_alerts(self, limit: int = 200) -> list[dict]:
        # NOTE: column list mirrors the full alert_events schema; no JSON blobs on this table,
        # so this is purely about making the read an explicit contract rather than SELECT *.
        rows = await self._pool.fetch(
            """SELECT alert_id, alert_type, severity, airport_code, service_name,
                      description, triggered_at, event_id, data_origin
               FROM alert_events ORDER BY triggered_at DESC LIMIT $1""",
            limit,
        )
        return [dict(r) for r in rows]

    async def get_origin_counts(self, window_minutes: int = 60) -> dict[str, int]:
        """Events received in the window, by data_origin (for the dashboard's provenance banner)."""
        rows = await self._pool.fetch(
            """SELECT data_origin, count(*) AS n FROM nas_events
               WHERE received_time_utc >= now() - make_interval(mins => $1)
               GROUP BY data_origin""",
            window_minutes,
        )
        return {r["data_origin"]: r["n"] for r in rows}

    async def get_latest_rvr(self) -> list[dict]:
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
        rows = await self._pool.fetch(query)
        return [dict(r) for r in rows]

    async def get_by_ids(self, event_ids: list[str]) -> dict[str, dict]:
        """The named events, keyed by id (ids no longer stored are absent)."""
        if not event_ids:
            return {}
        rows = await self._pool.fetch(
            """SELECT event_id, event_time_utc, airport_code, service_name, message_type, runway,
                      rvr_touchdown, rvr_midpoint, rvr_rollout, aircraft_id, flight_phase, altitude_ft,
                      surface_event_type, feed_status
               FROM nas_events WHERE event_id = ANY($1::text[])""",
            list(event_ids),
        )
        return {r["event_id"]: dict(r) for r in rows}

    async def get_event_counts_by_service(self) -> list[dict]:
        query = """
            SELECT service_name, airport_code, COUNT(*) AS count
            FROM nas_events
            GROUP BY service_name, airport_code
            ORDER BY service_name, airport_code
        """
        rows = await self._pool.fetch(query)
        return [dict(r) for r in rows]

    async def get_validation_summary(self) -> dict:
        query = """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN validation_status = 'PASSED' THEN 1 ELSE 0 END), 0) AS passed,
                COALESCE(SUM(CASE WHEN validation_status = 'FAILED' THEN 1 ELSE 0 END), 0) AS failed
            FROM nas_events
        """
        row = await self._pool.fetchrow(query)
        return dict(row) if row else {"total": 0, "passed": 0, "failed": 0}

    async def get_events_for_critic(
        self, airport_code: str | None, window_minutes: int = 30, limit: int = 50
    ) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        if airport_code:
            rows = await self._pool.fetch(
                """SELECT event_id, airport_code, service_name, latency_ms, feed_status,
                          rvr_touchdown, rvr_midpoint, rvr_rollout, runway,
                          received_time_utc, validation_status
                   FROM nas_events
                   WHERE airport_code = $1 AND received_time_utc >= $2
                   ORDER BY received_time_utc DESC LIMIT $3""",
                airport_code, cutoff, limit,
            )
        else:
            rows = await self._pool.fetch(
                """SELECT event_id, airport_code, service_name, latency_ms, feed_status,
                          rvr_touchdown, rvr_midpoint, rvr_rollout, runway,
                          received_time_utc, validation_status
                   FROM nas_events
                   WHERE received_time_utc >= $1
                   ORDER BY received_time_utc DESC LIMIT $2""",
                cutoff, limit,
            )
        return [dict(r) for r in rows]

    async def get_alerts_for_critic(
        self, airport_code: str | None, window_minutes: int = 60, limit: int = 10
    ) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        if airport_code:
            rows = await self._pool.fetch(
                """SELECT alert_id, alert_type, severity, airport_code, service_name,
                          description, triggered_at
                   FROM alert_events
                   WHERE airport_code = $1 AND triggered_at >= $2
                   ORDER BY triggered_at DESC LIMIT $3""",
                airport_code, cutoff, limit,
            )
        else:
            rows = await self._pool.fetch(
                """SELECT alert_id, alert_type, severity, airport_code, service_name,
                          description, triggered_at
                   FROM alert_events
                   WHERE triggered_at >= $1
                   ORDER BY triggered_at DESC LIMIT $2""",
                cutoff, limit,
            )
        return [dict(r) for r in rows]

    async def get_active_aircraft(self, window_minutes: int = 5, limit: int = 500) -> list[dict]:
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
        rows = await self._pool.fetch(query, cutoff, limit)
        return [dict(r) for r in rows]

    async def get_flight_phase_counts(self, window_minutes: int = 15) -> dict:
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
        rows = await self._pool.fetch(query, cutoff)
        out: dict = {}
        for r in rows:
            out.setdefault(r["airport_code"], {})[r["phase"]] = r["n"]
        return out

    async def get_altitude_distribution(self, window_minutes: int = 15) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        query = """
            SELECT airport_code, altitude_ft, ground_speed_kt
            FROM nas_events
            WHERE service_name = 'TERMINAL_AUTO'
              AND received_time_utc >= $1
              AND altitude_ft IS NOT NULL
        """
        rows = await self._pool.fetch(query, cutoff)
        return [dict(r) for r in rows]

    async def get_latency_stats(self) -> dict:
        query = """
            SELECT
                AVG(latency_ms) AS avg_ms,
                MIN(latency_ms) AS min_ms,
                MAX(latency_ms) AS max_ms
            FROM nas_events
            WHERE latency_ms IS NOT NULL
        """
        row = await self._pool.fetchrow(query)
        return dict(row) if row else {"avg_ms": None, "min_ms": None, "max_ms": None}
