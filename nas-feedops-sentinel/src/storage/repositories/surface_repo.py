"""
Surface-movement reads over `nas_events` (SMES surface events).

A flight's ground movement is four events, all carried by SMES_SE messages
with the aircraft id:

    SPOT_OUT  (leaves the gate / spot)  ->  WHEELS_OFF  (departs the runway)
    WHEELS_ON (lands)                   ->  SPOT_IN     (reaches the gate / spot)

Taxi-out is SPOT_OUT to WHEELS_OFF; taxi-in is WHEELS_ON to SPOT_IN. These
reads return the raw pairs and lists; src/surface_metrics.py turns them into
numbers, so the arithmetic is testable without a database.

A flight that pushes back, returns to the gate and pushes back again (or a
message the feed repeats) has more than one SPOT_OUT before its WHEELS_OFF;
the pairs use the latest, so the time spent back at the gate is not counted as
taxi time and the flight is counted once.

Every read takes `as_of` (default now): the moment the question is asked
about, so a past hour can be asked as easily as the last fifteen minutes. The
event's own time (event_time_utc) is what is measured; received_time_utc is
only a bound the indexes can use, since a message is received after it
happened.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import asyncpg

from src.timeutil import as_utc

TAXI_OUT_MAX = timedelta(minutes=90)       # a SPOT_OUT with no WHEELS_OFF this long after is not a taxi-out
TAXI_IN_MAX = timedelta(minutes=60)
GATE_RETURN_MAX = timedelta(minutes=60)
QUEUE_LOOKBACK = timedelta(minutes=90)
TIMELINE_TYPES = (
    "SPOT_OUT", "DEPARTURE_CLEARANCE_DCL", "RUNWAY_ENTRY", "WHEELS_OFF",
    "WHEELS_ON", "RUNWAY_EXIT", "SPOT_IN",
)


class SurfaceRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def _pairs(
        self, airport_code: str | None, since: datetime, as_of: datetime | None, *,
        start_type: str, end_type: str, max_gap: timedelta, start_key: str, end_key: str,
    ) -> list[dict]:
        """
        Each `end_type` event in [since, as_of] paired with the latest `start_type` event of the
        same aircraft at the same airport before it (within max_gap): the shape of both taxi legs.
        """
        end = as_utc(as_of)
        since = as_utc(since)
        rows = await self._pool.fetch(
            """
            WITH s AS (
              SELECT airport_code, aircraft_id, event_time_utc AS t0
              FROM nas_events
              WHERE surface_event_type = $5 AND aircraft_id IS NOT NULL
                AND ($1::text IS NULL OR airport_code = $1)
                AND received_time_utc >= $2::timestamptz - $4::interval
                AND event_time_utc >= $2::timestamptz - $4::interval AND event_time_utc <= $3::timestamptz
            )
            SELECT * FROM (
              SELECT DISTINCT ON (s.airport_code, s.aircraft_id, e.t1)
                     s.airport_code, s.aircraft_id, s.t0 AS start_at, e.t1 AS end_at
              FROM s
              JOIN LATERAL (
                SELECT min(x.event_time_utc) AS t1 FROM nas_events x
                WHERE x.surface_event_type = $6 AND x.aircraft_id = s.aircraft_id
                  AND x.airport_code = s.airport_code
                  AND x.received_time_utc >= s.t0
                  AND x.event_time_utc > s.t0 AND x.event_time_utc < s.t0 + $4::interval
                  AND x.event_time_utc <= $3::timestamptz
              ) e ON e.t1 IS NOT NULL AND e.t1 >= $2::timestamptz
              ORDER BY s.airport_code, s.aircraft_id, e.t1, s.t0 DESC
            ) paired ORDER BY end_at
            """,
            airport_code, since, end, max_gap, start_type, end_type,
        )
        return [{"airport_code": r["airport_code"], "aircraft_id": r["aircraft_id"],
                 start_key: r["start_at"], end_key: r["end_at"]} for r in rows]

    async def get_taxi_out_pairs(
        self, airport_code: str | None, since: datetime, as_of: datetime | None = None,
    ) -> list[dict]:
        """Completed taxi-outs whose WHEELS_OFF fell in [since, as_of]: aircraft, spot_out_at, wheels_off_at."""
        return await self._pairs(airport_code, since, as_of, start_type="SPOT_OUT", end_type="WHEELS_OFF",
                                 max_gap=TAXI_OUT_MAX, start_key="spot_out_at", end_key="wheels_off_at")

    async def get_taxi_in_pairs(
        self, airport_code: str | None, since: datetime, as_of: datetime | None = None,
    ) -> list[dict]:
        """Completed taxi-ins whose SPOT_IN fell in [since, as_of]: aircraft, wheels_on_at, spot_in_at."""
        return await self._pairs(airport_code, since, as_of, start_type="WHEELS_ON", end_type="SPOT_IN",
                                 max_gap=TAXI_IN_MAX, start_key="wheels_on_at", end_key="spot_in_at")

    async def get_departure_queue(self, airport_code: str | None, as_of: datetime | None = None) -> list[dict]:
        """Aircraft that left the gate and have neither taken off nor returned to it: aircraft, spot_out_at."""
        end = as_utc(as_of)
        rows = await self._pool.fetch(
            """
            SELECT DISTINCT ON (so.airport_code, so.aircraft_id)
                   so.airport_code, so.aircraft_id, so.event_time_utc AS spot_out_at
            FROM nas_events so
            WHERE so.surface_event_type = 'SPOT_OUT' AND so.aircraft_id IS NOT NULL
              AND ($1::text IS NULL OR so.airport_code = $1)
              AND so.received_time_utc >= $2::timestamptz - $3::interval
              AND so.event_time_utc >= $2::timestamptz - $3::interval AND so.event_time_utc <= $2::timestamptz
              AND NOT EXISTS (
                SELECT 1 FROM nas_events x
                WHERE x.surface_event_type IN ('WHEELS_OFF', 'SPOT_IN') AND x.aircraft_id = so.aircraft_id
                  AND x.airport_code = so.airport_code
                  AND x.received_time_utc >= so.event_time_utc
                  AND x.event_time_utc > so.event_time_utc AND x.event_time_utc <= $2::timestamptz)
            ORDER BY so.airport_code, so.aircraft_id, so.event_time_utc DESC
            """,
            airport_code, end, QUEUE_LOOKBACK,
        )
        return [dict(r) for r in rows]

    async def get_gate_returns(
        self, airport_code: str | None, since: datetime, as_of: datetime | None = None,
    ) -> list[dict]:
        """SPOT_OUT followed by SPOT_IN with no WHEELS_OFF between: aircraft, spot_out_at, spot_in_at."""
        end = as_utc(as_of)
        since = as_utc(since)
        rows = await self._pool.fetch(
            """
            SELECT * FROM (
            SELECT DISTINCT ON (so.airport_code, so.aircraft_id, si.t1)
                   so.airport_code, so.aircraft_id, so.event_time_utc AS spot_out_at, si.t1 AS spot_in_at
            FROM nas_events so
            JOIN LATERAL (
              SELECT min(x.event_time_utc) AS t1 FROM nas_events x
              WHERE x.surface_event_type = 'SPOT_IN' AND x.aircraft_id = so.aircraft_id
                AND x.airport_code = so.airport_code
                AND x.received_time_utc >= so.event_time_utc
                AND x.event_time_utc > so.event_time_utc AND x.event_time_utc < so.event_time_utc + $4::interval
                AND x.event_time_utc <= $3::timestamptz
            ) si ON si.t1 IS NOT NULL
            WHERE so.surface_event_type = 'SPOT_OUT' AND so.aircraft_id IS NOT NULL
              AND ($1::text IS NULL OR so.airport_code = $1)
              AND so.received_time_utc >= $2::timestamptz
              AND so.event_time_utc >= $2::timestamptz AND so.event_time_utc <= $3::timestamptz
              AND NOT EXISTS (
                SELECT 1 FROM nas_events w
                WHERE w.surface_event_type = 'WHEELS_OFF' AND w.aircraft_id = so.aircraft_id
                  AND w.airport_code = so.airport_code
                  AND w.received_time_utc >= so.event_time_utc
                  AND w.event_time_utc > so.event_time_utc AND w.event_time_utc < si.t1)
            ORDER BY so.airport_code, so.aircraft_id, si.t1, so.event_time_utc DESC
            ) returned ORDER BY spot_out_at
            """,
            airport_code, since, end, GATE_RETURN_MAX,
        )
        return [dict(r) for r in rows]

    async def get_flight_surface_events(
        self, aircraft_id: str, lookback: timedelta, as_of: datetime | None = None,
    ) -> list[dict]:
        """One aircraft's surface events in order: time, airport, type, runway."""
        end = as_utc(as_of)
        rows = await self._pool.fetch(
            """
            SELECT event_time_utc, airport_code, surface_event_type, runway
            FROM nas_events
            WHERE aircraft_id = $1 AND surface_event_type = ANY($4::text[])
              AND received_time_utc >= $2::timestamptz - $3::interval
              AND event_time_utc >= $2::timestamptz - $3::interval AND event_time_utc <= $2::timestamptz
            ORDER BY event_time_utc, received_time_utc
            """,
            aircraft_id, end, lookback, list(TIMELINE_TYPES),
        )
        return [dict(r) for r in rows]

    async def get_latest_surface_event(self, airport_code: str | None, as_of: datetime | None = None) -> datetime | None:
        """
        When the newest SMES message happened: whether the surface feed is flowing, which is what
        the answers above depend on. Any SMES message counts (position reports arrive constantly);
        the sparse spot/wheels events alone would call a quiet airport's feed stale.

        The newest by arrival, one row off the (airport, received time) index: not a maximum over
        days of SMES rows, which every tool would repeat for every airport.
        """
        end = as_utc(as_of)
        return await self._pool.fetchval(
            """
            SELECT event_time_utc FROM nas_events
            WHERE service_name = 'SMES' AND ($1::text IS NULL OR airport_code = $1)
              AND received_time_utc <= $2::timestamptz
              AND received_time_utc >= $2::timestamptz - interval '7 days'
            ORDER BY received_time_utc DESC LIMIT 1
            """,
            airport_code, end,
        )
