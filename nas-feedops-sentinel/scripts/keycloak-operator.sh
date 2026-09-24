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
