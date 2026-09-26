#!/bin/sh
# One-shot, every infrastructure start, before PgBouncer. Idempotent.
#
# Moves the shared Postgres server from md5 to SCRAM-SHA-256 password auth,
# including a data directory that was initialised with md5. The order is what
# keeps anyone from being locked out: a scram-sha-256 rule refuses a role whose
# stored hash is md5, while an md5 rule already accepts SCRAM hashes. So new
# passwords are hashed with SCRAM first, every md5 login role is re-hashed, and
# pg_hba.conf switches only once no md5 hash is left.
set -eu
: "${POSTGRES_USER:?}" "${POSTGRES_PASSWORD:?}" "${APP_DB_USER:?}" "${APP_DB_PASSWORD:?}" "${LITELLM_DB_PASSWORD:?}"
export PGPASSWORD="$POSTGRES_PASSWORD"
PSQL="psql -h postgres -U $POSTGRES_USER -d postgres -v ON_ERROR_STOP=1 -qAt"
HBA=/var/lib/postgresql/data/pg_hba.conf

$PSQL -c "ALTER SYSTEM SET password_encryption = 'scram-sha-256'" -c "SELECT pg_reload_conf()" >/dev/null

rehash() {
  $PSQL -v role="$1" -v pw="$2" <<'SQL'
SET password_encryption = 'scram-sha-256';
SELECT EXISTS (SELECT 1 FROM pg_authid WHERE rolname = :'role' AND rolpassword LIKE 'md5%') AS needs \gset
\if :needs
ALTER ROLE :"role" PASSWORD :'pw';
\echo re-hashed :role with SCRAM-SHA-256
\endif
SQL
}
rehash "$POSTGRES_USER" "$POSTGRES_PASSWORD"
rehash "$APP_DB_USER" "$APP_DB_PASSWORD"
rehash litellm "$LITELLM_DB_PASSWORD"

remaining=$($PSQL -c "SELECT string_agg(rolname, ', ') FROM pg_authid WHERE rolcanlogin AND rolpassword LIKE 'md5%'")
if [ -n "$remaining" ]; then
  echo "WARNING: login roles still md5-hashed: $remaining. Re-set their passwords and run this" \
       "again; pg_hba.conf is switched to scram-sha-256 only once none remain." >&2
  exit 0
fi

if grep -Eq '^[^#]*[[:space:]]md5([[:space:]]|$)' "$HBA"; then
  cp "$HBA" /tmp/pg_hba.orig
  sed -E '/^[[:space:]]*#/! s/([[:space:]])md5([[:space:]]|$)/\1scram-sha-256\2/' /tmp/pg_hba.orig > /tmp/pg_hba.new
  cat /tmp/pg_hba.new > "$HBA"   # rewrite in place: keeps the file's owner and mode
  if [ "$($PSQL -c "SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL")" != 0 ]; then
    cat /tmp/pg_hba.orig > "$HBA"
    echo "rewritten pg_hba.conf did not parse; original restored" >&2
    exit 1
  fi
  $PSQL -c "SELECT pg_reload_conf()" >/dev/null
  echo "pg_hba.conf switched from md5 to scram-sha-256"
fi
echo "postgres password auth: scram-sha-256"
