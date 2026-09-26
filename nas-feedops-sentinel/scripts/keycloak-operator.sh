#!/usr/bin/env bash
# Runs INSIDE the nas-keycloak container (copied in by Setup-Keycloak.ps1).
# Creates or updates ONE operator login in the nas-sentinel realm.
#
# The realm and the dashboard client are not created here: they are defined
# in keycloak/realm-nas-sentinel.json and imported by Keycloak on startup.
# People are not configuration, so their passwords never go in that file.
#
# Secrets arrive as environment variables passed through by `docker exec -e
# NAME` (no value on the command line) and are never echoed. Idempotent.
set -euo pipefail
: "${KC_ADMIN_PW:?}" "${APP_USERNAME:?}" "${APP_USER_PW:?}" "${APP_EMAIL:?}"
# Realm roles for this operator (comma-separated). Defaults to watch_officer.
APP_ROLES="${APP_ROLES:-watch_officer}"
KC_SERVER="${KC_SERVER:-http://localhost:8080}"

REALM=nas-sentinel
CFG=/tmp/kcadm-operator.config
kc() { /opt/keycloak/bin/kcadm.sh "$@" --config "$CFG"; }
trap 'rm -f "$CFG"' EXIT

kc config credentials --server "$KC_SERVER" --realm master --user admin --password "$KC_ADMIN_PW" >/dev/null

if ! kc get "realms/$REALM" >/dev/null 2>&1; then
    echo "ERROR: realm $REALM does not exist. It is imported from" >&2
    echo "keycloak/realm-nas-sentinel.json when Keycloak starts; check nas-keycloak's log." >&2
    exit 1
fi

# ── Dashboard client: in-app sign-in ──────────────────────────────────────────
# keycloak/realm-nas-sentinel.json sets directAccessGrantsEnabled on the
# dashboard client, but realm import is skip-if-exists: a realm that was
# already imported before that flag was added keeps the old client and the
# dashboard's own login form gets "Client not allowed for direct access
# grants" with no obvious cause. Reconciling it here means the fix ships with
# the script every operator already runs, instead of requiring the realm to be
# deleted and re-imported (which would take every user account with it).
CLIENT_ID=nas-feedops-sentinel
CLIENT_UUID=$(kc get clients -r "$REALM" -q clientId="$CLIENT_ID" --fields id --format csv --noquotes)
if [ -z "$CLIENT_UUID" ]; then
    echo "ERROR: client $CLIENT_ID is missing from realm $REALM." >&2
    echo "It is defined in keycloak/realm-nas-sentinel.json; check nas-keycloak's import log." >&2
    exit 1
fi
DAG=$(kc get "clients/$CLIENT_UUID" -r "$REALM" --fields directAccessGrantsEnabled --format csv --noquotes)
if [ "$DAG" = "true" ]; then
    echo "client $CLIENT_ID: direct access grants already enabled"
else
    kc update "clients/$CLIENT_UUID" -r "$REALM" -s directAccessGrantsEnabled=true
    echo "client $CLIENT_ID: direct access grants enabled"
fi

# ── Operator account ──────────────────────────────────────────────────────────
USER_ID=$(kc get users -r "$REALM" -q username="$APP_USERNAME" -q exact=true --fields id --format csv --noquotes)
if [ -z "$USER_ID" ]; then
    kc create users -r "$REALM" -s username="$APP_USERNAME" -s enabled=true \
        -s email="$APP_EMAIL" -s emailVerified=true \
        -s firstName="$APP_USERNAME" -s lastName=Operator >/dev/null
    echo "user $APP_USERNAME: created"
else
    # firstName/lastName are set here as well as on create: Keycloak's default
    # user profile requires them, and a user added through the admin console
    # without them is stopped by a "complete your profile" form at login.
    kc update "users/$USER_ID" -r "$REALM" -s email="$APP_EMAIL" -s emailVerified=true \
        -s enabled=true -s firstName="$APP_USERNAME" -s lastName=Operator
    # A console-created password may be marked temporary, which forces a reset
    # at first login; set-password below replaces it with a permanent one.
    echo "user $APP_USERNAME: already existed, profile updated"
fi
kc set-password -r "$REALM" --username "$APP_USERNAME" --new-password "$APP_USER_PW"
echo "user $APP_USERNAME: password set"

# ── Authorization ────────────────────────────────────────────────────────────
# Without a realm role the operator can sign in but decide nothing
# (src/auth.authorize). Roles must exist first: see scripts/keycloak-rbac.sh.
for role in $(echo "$APP_ROLES" | tr ',' ' '); do
    kc add-roles -r "$REALM" --uusername "$APP_USERNAME" --rolename "$role"
done
echo "user $APP_USERNAME: roles $APP_ROLES"
