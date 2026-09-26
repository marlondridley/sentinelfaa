"""
Test-only isolation helpers. Not entity-specific, so kept separate from the
repositories rather than bolted onto one of them.
"""
from __future__ import annotations

import os

from src.async_bridge import run_async
from src.storage.pool import get_db_pool


# Databases this helper is willing to wipe. reset_db() TRUNCATEs every table
# in the application schema, so aiming it at the wrong database is
# unrecoverable -- and the default config aims it straight at the live one.
#
# Postgres runs in a container on nas-network and is reached by service name,
# so these tests run INSIDE a container on that network, where .env's
# nas-pgbouncer:6432/sentinel_db resolves. That is the live database.
#
# The autouse fixtures in tests/ call override_db_path() before EVERY test, so
# there is no "I'll be careful this once" -- the first run wipes the database.
# TRUNCATE also bypasses the append-only trigger on action_audit_log (a
# row-level BEFORE UPDATE OR DELETE trigger does not fire on TRUNCATE, see
# src/storage/schema.py), so the HITL audit trail goes with everything else.
#
# Hence: refuse unless the database we ACTUALLY connected to looks disposable.
# The check reads current_database() rather than trusting config, because what
# matters is where the connection landed, not what the settings intended.
_DISPOSABLE_DB_MARKERS = ("test", "ci", "scratch", "tmp")

# Escape hatch for a CI database that does not follow the naming convention.
# Deliberately verbose: nobody sets this by accident.
_DESTRUCTIVE_OPT_IN = "SENTINEL_ALLOW_DESTRUCTIVE_DB_RESET"


class UnsafeTestDatabaseError(RuntimeError):
    """Raised when reset_db() is aimed at a database that is not disposable."""


# Targets already refused once this session. The autouse fixtures call
# override_db_path() before EVERY test, so a misconfigured run raises the
# same refusal once per test -- 92 identical multi-paragraph errors buried
# the four real failures in the last run. The explanation is worth printing
# in full exactly once; after that a single line is enough, since the cause
# and the fix are identical every time.
_REFUSALS_ALREADY_EXPLAINED: set[tuple[str, str]] = set()


async def _assert_database_is_disposable(conn) -> None:
    db_name = await conn.fetchval("SELECT current_database()")
    host = await conn.fetchval("SELECT inet_server_addr()::text")

    if os.getenv(_DESTRUCTIVE_OPT_IN) == "1":
        return

    lowered = (db_name or "").lower()
    if any(marker in lowered for marker in _DISPOSABLE_DB_MARKERS):
        return

    target = (db_name or "unknown", host or "unknown")
    if target in _REFUSALS_ALREADY_EXPLAINED:
        raise UnsafeTestDatabaseError(
            f"Refusing to TRUNCATE {db_name!r} (see the first occurrence "
            f"above for the full explanation and the fix)."
        )
    _REFUSALS_ALREADY_EXPLAINED.add(target)

    raise UnsafeTestDatabaseError(
        f"Refusing to TRUNCATE the application schema in database "
        f"{db_name!r} (server {host or 'unknown'}).\n"
        f"\n"
        f"reset_db() wipes nas_events, alert_events, critic_results, "
        f"planner_results, staged_actions, action_audit_log, "
        f"watch_officer_briefs, operational_tickets and ingest_log -- "
        f"including the HITL audit trail, which TRUNCATE removes despite the "
        f"append-only trigger.\n"
        f"\n"
        f"To run the database-backed tests safely, point them at a throwaway "
        f"database whose name contains one of "
        f"{', '.join(_DISPOSABLE_DB_MARKERS)}:\n"
        f"\n"
        f"    docker exec postgres sh -c \\\n"
        f"      'PGPASSWORD=$POSTGRES_PASSWORD createdb -U $POSTGRES_USER sentinel_test'\n"
        f"\n"
        f"(This container's pg_hba.conf requires a password on the local socket\n"
        f"too, and a non-interactive docker exec has no terminal to type one\n"
        f"into -- it loops on an empty Password: prompt. Reading the password\n"
        f"from the postgres container's own environment keeps it off your shell\n"
        f"history and out of any log.)\n"
        f"\n"
        f"then run the suite in a container on nas-network, with the repo\n"
        f"mounted (the image ships src/ but not tests/):\n"
        f"\n"
        f"    docker run --rm --network nas-network --env-file .env \\\n"
        f"      -v \"$PWD\":/app -w /app \\\n"
        f"      -e VAULT_ADDR= -e VAULT_TOKEN= \\\n"
        f"      -e POSTGRES_HOST=postgres -e POSTGRES_PORT=5432 \\\n"
        f"      -e POSTGRES_DB=sentinel_test -e DATABASE_URL= \\\n"
        f"      nas-feedops-sentinel:latest \\\n"
        f"      python -m pytest -p no:cacheprovider\n"
        f"\n"
        f"Every one of those overrides is load-bearing, and an empty value is\n"
        f"how each one is expressed:\n"
        f"  DATABASE_URL=           .env pins a full DSN at nas-pgbouncer/sentinel_db.\n"
        f"                          Emptying it makes src/config.py rebuild the DSN\n"
        f"                          from the POSTGRES_* settings below.\n"
        f"  VAULT_ADDR/VAULT_TOKEN= the Vault settings source outranks the environment\n"
        f"                          (src/config.py settings_customise_sources) and seeds\n"
        f"                          POSTGRES_USER/PASSWORD/DB from secret/postgres/\n"
        f"                          credentials, which would put you back on\n"
        f"                          sentinel_db. Emptying either one disables it.\n"
        f"  POSTGRES_HOST/PORT      pgbouncer proxies exactly one database, so the test\n"
        f"                          database is only reachable on postgres:5432 directly.\n"
        f"\n"
        f"If your CI database genuinely cannot be renamed, set "
        f"{_DESTRUCTIVE_OPT_IN}=1 to override this guard."
    )


async def reset_db() -> None:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await _assert_database_is_disposable(conn)
        await conn.execute("""
            TRUNCATE TABLE nas_events, alert_events, critic_results, planner_results,
                           staged_actions, action_audit_log, watch_officer_briefs,
                           operational_tickets, ingest_log, investigations,
                           model_calls
                           RESTART IDENTITY CASCADE;
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
