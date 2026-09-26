"""
One-shot schema job: `python -m src.storage.migrate`.

Runs as the schema owner, before any application service starts:

  1. schema bootstrap + forward-only migrations (schema.py)
  2. create or update the application login role (APP_DB_USER), if
     APP_DB_PASSWORD is provided
  3. grant that role exactly what the application needs, re-applied on
     every run so tables added by new migrations are covered

The application role can read and write operational tables, but on the
evidence tables -- action_audit_log, investigations and model_calls -- it can
only SELECT and INSERT. It owns nothing, so it cannot drop the append-only triggers,
TRUNCATE, or change the schema. Before this, the application connected as
the Postgres superuser that owned every table, which made the append-only
triggers a convention rather than a control.
"""
from __future__ import annotations

import asyncio
import os
import re

import asyncpg

from src.config import settings
from src.logging_config import configure_logging, get_logger
from src.storage.schema import init_schema, run_migrations

_log = get_logger(__name__)

# Evidence tables: rows may be added and read, never changed or removed.
APPEND_ONLY_TABLES = ("action_audit_log", "investigations", "model_calls")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


async def ensure_app_role(conn: asyncpg.Connection, role: str, password: str) -> None:
    if not _IDENT.match(role):
        raise ValueError(f"invalid role name {role!r}")
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
    verb = "ALTER" if exists else "CREATE"
    # Identifiers cannot be bound as parameters; the name is validated above.
    # The password goes through quote_literal on the server side.
    literal = await conn.fetchval("SELECT quote_literal($1::text)", password)
    await conn.execute(
        f"{verb} ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
        f"PASSWORD {literal}"
    )
    _log.info("app_role_ensured", extra={"role": role, "role_created": not exists})


async def apply_grants(conn: asyncpg.Connection, role: str) -> None:
    if not _IDENT.match(role):
        raise ValueError(f"invalid role name {role!r}")
    db = await conn.fetchval("SELECT current_database()")
    tables = [r["tablename"] for r in await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]
    await conn.execute(f'GRANT CONNECT ON DATABASE "{db}" TO {role}')
    await conn.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
    await conn.execute(f"REVOKE CREATE ON SCHEMA public FROM {role}")
    for table in tables:
        await conn.execute(f'REVOKE ALL ON TABLE "{table}" FROM {role}')
        if table in APPEND_ONLY_TABLES:
            await conn.execute(f'GRANT SELECT, INSERT ON TABLE "{table}" TO {role}')
        elif table == "schema_migrations":
            await conn.execute(f'GRANT SELECT ON TABLE "{table}" TO {role}')
        else:
            await conn.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "{table}" TO {role}')
    await conn.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")
    _log.info("app_role_grants_applied", extra={"role": role, "tables": len(tables)})


async def migrate(app_role: str | None, app_password: str | None) -> None:
    pool = await asyncpg.create_pool(dsn=settings.DATABASE_URL, min_size=1, max_size=2,
                                     statement_cache_size=0)
    try:
        await init_schema(pool)
        await run_migrations(pool)
        async with pool.acquire() as conn:
            if app_role and app_password:
                await ensure_app_role(conn, app_role, app_password)
            if app_role and await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", app_role):
                await apply_grants(conn, app_role)
    finally:
        await pool.close()


def main() -> None:
    configure_logging()
    role = os.getenv("APP_DB_USER", "sentinel_app")
    asyncio.run(migrate(role, os.getenv("APP_DB_PASSWORD") or None))
    _log.info("migrate_complete")


if __name__ == "__main__":
    main()
