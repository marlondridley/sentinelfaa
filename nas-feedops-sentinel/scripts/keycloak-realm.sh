#!/usr/bin/env bash
# Runs INSIDE the nas-keycloak container (copied in by Setup-Keycloak.ps1).
# Creates or updates the nas-sentinel realm, the dashboard's confidential OIDC
# client and one login user. Idempotent: safe to re-run.
#
# Secrets arrive as environment variables passed through by `docker exec -e
# NAME` (no value on the command line) and are never echoed.
set -euo pipefail
: "${KC_ADMIN_PW:?}" "${CLIENT_SECRET:?}" "${APP_USERNAME:?}" "${APP_USER_PW:?}" "${APP_EMAIL:?}"

REALM=nas-sentinel
CLIENT=nas-feedops-sentinel
REDIRECT=http://localhost:8501/oauth2callback
ORIGIN=http://localhost:8501
CFG=/tmp/kcadm-setup.config
kc() { /opt/keycloak/bin/kcadm.sh "$@" --config "$CFG"; }
trap 'rm -f "$CFG"' EXIT

kc config credentials --server http://localhost:8080 --realm master --user admin --password "$KC_ADMIN_PW" >/dev/null

if kc get "realms/$REALM" >/dev/null 2>&1; then
    echo "realm $REALM: already exists"
else
    kc create realms -s realm="$REALM" -s enabled=true >/dev/null
    echo "realm $REALM: created"
fi

CID=$(kc get clients -r "$REALM" -q clientId="$CLIENT" --fields id --format csv --noquotes)
CLIENT_SETTINGS=(
    -s enabled=true -s publicClient=false -s clientAuthenticatorType=client-secret
    -s standardFlowEnabled=true -s directAccessGrantsEnabled=false
    -s "redirectUris=[\"$REDIRECT\"]" -s "webOrigins=[\"$ORIGIN\"]"
    -s "secret=$CLIENT_SECRET"
)
if [ -z "$CID" ]; then
    kc create clients -r "$REALM" -s clientId="$CLIENT" "${CLIENT_SETTINGS[@]}" >/dev/null
    echo "client $CLIENT: created (secret taken from secrets.toml)"
else
    kc update "clients/$CID" -r "$REALM" "${CLIENT_SETTINGS[@]}"
    echo "client $CLIENT: updated (secret re-synced from secrets.toml)"
fi

UID_=$(kc get users -r "$REALM" -q username="$APP_USERNAME" -q exact=true --fields id --format csv --noquotes)
if [ -z "$UID_" ]; then
    kc create users -r "$REALM" -s username="$APP_USERNAME" -s enabled=true \
        -s email="$APP_EMAIL" -s emailVerified=true \
        -s firstName="$APP_USERNAME" -s lastName=Operator >/dev/null
    echo "user $APP_USERNAME: created"
else
    echo "user $APP_USERNAME: already exists"
fi
kc set-password -r "$REALM" --username "$APP_USERNAME" --new-password "$APP_USER_PW"
echo "user $APP_USERNAME: password set"
echo "done"
