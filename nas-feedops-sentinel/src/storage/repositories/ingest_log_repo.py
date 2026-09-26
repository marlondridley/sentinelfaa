"""
Repository for the SWIM file-ingest audit trail (`ingest_log` table).

Replaces what used to be `data/processed/ingest_log.jsonl` -- a plain append-only
file that every long-running `--watch` process (see swim_file_ingest.py) grew
forever and fully re-read/re-parsed into memory on every single scan pass, just to
answer "has this file name been seen before?". Two problems with that: unbounded
disk growth over the life of a long-running deployment, and a per-scan cost that
kept getting worse as the file grew, since the whole history had to be loaded to
answer a handful of per-file lookups.

This repository fixes both: is_processed() is a single indexed point lookup per
candidate file (cheap and O(1) regardless of how much history exists) rather than
loading every name ever seen, and append() keeps the table bounded by occasionally
pruning down to the most recent MAX_ROWS entries rather than growing without limit.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone

import asyncpg

# Cap on retained rows. Enforced probabilistically (not on every insert) so a hot
# ingest loop isn't paying for a DELETE on every single file -- see append()'s
# docstring. This number is generous for FAA SWIM/STDDS message volumes; raise it
# if operational history needs to reach further back than that implies.
MAX_ROWS = 200_000

# Probability of running the prune check on any given append() call. At typical
# ingest rates this still prunes many times a day without adding a DELETE to every
# single insert.
_PRUNE_PROBABILITY = 0.01


class IngestLogRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def is_processed(self, file_name: str) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM ingest_log WHERE file_name = $1 LIMIT 1", file_name
        )
        return row is not None

    async def get_processed_subset(self, file_names: list[str]) -> set[str]:
        """
        Given a list of candidate file names, return the subset already present in
        the log -- one indexed, bounded query keyed to whatever is currently sitting
        in the watched folder (small and capped by the caller's max_files), rather
        than loading the whole historical log to compute the same answer.
        """
        if not file_names:
            return set()
        rows = await self._pool.fetch(
            "SELECT file_name FROM ingest_log WHERE file_name = ANY($1::text[])", file_names
        )
        return {r["file_name"] for r in rows}

    async def append(self, entry: dict) -> None:
        """
        Record one ingest attempt. `entry` matches the shape scan_swim_folder() has
        always built: {"file", "status", "timestamp", plus one of "error" (failed)
        or "event_count"/"services"/"parse_errors" (processed)}. A "withheld" entry
        (a message flagged blockAtFAA=true) carries its reason in "error".

        Prunes down to MAX_ROWS most-recent rows with the same probability on every
        call, rather than only on a dedicated schedule -- so the table self-bounds
        under normal operation with no separate cron/maintenance task required to
        keep it that way.
        """
        timestamp = entry.get("timestamp")
        created_at = datetime.fromisoformat(timestamp) if timestamp else datetime.now(timezone.utc)
        services = entry.get("services")

        await self._pool.execute(
            """
            INSERT INTO ingest_log (
                file_name, status, event_count, services, parse_errors, error, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            entry.get("file", ""),
            entry.get("status", ""),
            entry.get("event_count"),
            json.dumps(services) if services is not None else None,
            entry.get("parse_errors"),
            entry.get("error"),
            created_at,
        )

        if random.random() < _PRUNE_PROBABILITY:
            await self._prune()

    async def _prune(self) -> None:
        await self._pool.execute(
            """
            DELETE FROM ingest_log
            WHERE id IN (
                SELECT id FROM ingest_log
                ORDER BY id DESC
                OFFSET $1
            )
            """,
            MAX_ROWS,
        )

    async def get_recent(self, limit: int = 100) -> list[dict]:
        rows = await self._pool.fetch(
            """SELECT file_name, status, event_count, services, parse_errors, error, created_at
               FROM ingest_log ORDER BY id DESC LIMIT $1""",
            limit,
        )
        out = []
        for r in rows:
            d = dict(r)
            services = d.get("services")
            if isinstance(services, str):
                services = json.loads(services)
            # Preserve the original JSONL entry shape ("file"/"timestamp") so
            # dashboard/app.py and anything else reading this back don't need to
            # change how it interprets an entry.
            out.append({
                "file": d["file_name"],
                "status": d["status"],
                "event_count": d.get("event_count"),
                "services": services,
                "parse_errors": d.get("parse_errors"),
                "error": d.get("error"),
                "timestamp": d["created_at"].isoformat() if d.get("created_at") else None,
            })
        return out
