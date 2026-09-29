#!/usr/bin/env bash
# Runs INSIDE the nas-keycloak container (copied in by Setup-Keycloak.ps1 and
# Add-Guest.ps1). Creates, updates or disables ONE login in the nas-sentinel
# realm.
#
# The realm and the dashboard client are not created here: they are defined
# in keycloak/realm-nas-sentinel.json and imported by Keycloak on startup.
# People are not configuration, so their passwords never go in that file.
#
# Secrets arrive as environment variables passed through by `docker exec -e
# NAME` (no value on the command line) and are never echoed. Idempotent.
#
#   APP_ROLES               realm roles, comma-separated; default watch_officer.
#                           "none": a read-only guest -- every role this realm
#                           defines is removed (a signed-in user with no role
#                           can see, never decide: src/auth.authorize).
#   APP_ROLES_EXACT         1: the user ends with exactly APP_ROLES (realm
#                           roles not listed are removed); default: added to.
#   APP_TEMPORARY_PASSWORD  1: the password is temporary, and Keycloak makes
#                           the user choose their own at first sign-in.
#   APP_LAST_NAME           default Operator (Keycloak's profile requires one).
#   APP_DISABLE             1: turn the login off and end its sessions; only
#                           KC_ADMIN_PW and APP_USERNAME are needed.
set -euo pipefail
: "${KC_ADMIN_PW:?}" "${APP_USERNAME:?}"
if [ "${APP_DISABLE:-0}" != 1 ]; then
    : "${APP_USER_PW:?}" "${APP_EMAIL:?}"
fi
APP_ROLES="${APP_ROLES:-watch_officer}"
APP_LAST_NAME="${APP_LAST_NAME:-Operator}"
KC_SERVER="${KC_SERVER:-https://localhost:8443}"
# Keycloak serves HTTPS only (docker-compose.yml). kcadm is a Java client, so it
# trusts the local authority through a throwaway PKCS12 truststore built from
# the mounted certificate; the store holds only that public certificate.
KC_CA_FILE="${KC_CA_FILE:-/opt/keycloak/conf/tls/edge-ca.crt}"
REALM_ROLES="watch_officer supervisor auditor"

REALM=nas-sentinel
CFG=/tmp/kcadm-operator.config
kc() { /opt/keycloak/bin/kcadm.sh "$@" --config "$CFG"; }
TRUST=/tmp/kcadm-operator-trust.p12
trap 'rm -f "$CFG" "$TRUST"' EXIT
TRUSTPASS=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
keytool -importcert -noprompt -alias sentinel-local-ca -file "$KC_CA_FILE" \
    -keystore "$TRUST" -storetype PKCS12 -storepass "$TRUSTPASS" >/dev/null
kc config truststore --trustpass "$TRUSTPASS" "$TRUST" >/dev/null

kc config credentials --server "$KC_SERVER" --realm master --user admin --password "$KC_ADMIN_PW" >/dev/null

if ! kc get "realms/$REALM" >/dev/null 2>&1; then
    echo "ERROR: realm $REALM does not exist. It is imported from" >&2
    echo "keycloak/realm-nas-sentinel.json when Keycloak starts; check nas-keycloak's log." >&2
    exit 1
fi

USER_ID=$(kc get users -r "$REALM" -q username="$APP_USERNAME" -q exact=true --fields id --format csv --noquotes)

# ── Turning a login off ──────────────────────────────────────────────────────
if [ "${APP_DISABLE:-0}" = 1 ]; then
    [ -n "$USER_ID" ] || { echo "ERROR: no user $APP_USERNAME in $REALM" >&2; exit 1; }
    kc update "users/$USER_ID" -r "$REALM" -s enabled=false
    kc create "users/$USER_ID/logout" -r "$REALM" >/dev/null 2>&1 || true
    echo "user $APP_USERNAME: disabled, sessions ended"
    exit 0
fi

# ── Account ──────────────────────────────────────────────────────────────────
if [ -z "$USER_ID" ]; then
    kc create users -r "$REALM" -s username="$APP_USERNAME" -s enabled=true \
        -s email="$APP_EMAIL" -s emailVerified=true \
        -s firstName="$APP_USERNAME" -s lastName="$APP_LAST_NAME" >/dev/null
    echo "user $APP_USERNAME: created"
else
    # firstName/lastName are set here as well as on create: Keycloak's default
    # user profile requires them, and a user added through the admin console
    # without them is stopped by a "complete your profile" form at login.
    kc update "users/$USER_ID" -r "$REALM" -s email="$APP_EMAIL" -s emailVerified=true \
        -s enabled=true -s firstName="$APP_USERNAME" -s lastName="$APP_LAST_NAME"
    echo "user $APP_USERNAME: already existed, profile updated"
fi
if [ "${APP_TEMPORARY_PASSWORD:-0}" = 1 ]; then
    kc set-password -r "$REALM" --username "$APP_USERNAME" --new-password "$APP_USER_PW" --temporary
    echo "user $APP_USERNAME: temporary password set (a new one is chosen at first sign-in)"
else
    # Permanent: replaces a console-created password that may be marked temporary.
    kc set-password -r "$REALM" --username "$APP_USERNAME" --new-password "$APP_USER_PW"
    echo "user $APP_USERNAME: password set"
fi

# ── Authorization ────────────────────────────────────────────────────────────
# Without a realm role the user can sign in but decide nothing
# (src/auth.authorize). Roles must exist first: see scripts/keycloak-rbac.sh.
if [ "$APP_ROLES" = none ]; then
    for role in $REALM_ROLES; do
        kc remove-roles -r "$REALM" --uusername "$APP_USERNAME" --rolename "$role" >/dev/null 2>&1 || true
    done
    echo "user $APP_USERNAME: no role (read-only)"
else
    if [ "${APP_ROLES_EXACT:-0}" = 1 ]; then   # exactly these: a change of role also takes the old one away
        for role in $REALM_ROLES; do
            case ",$APP_ROLES," in
                *",$role,"*) ;;
                *) kc remove-roles -r "$REALM" --uusername "$APP_USERNAME" --rolename "$role" >/dev/null 2>&1 || true ;;
            esac
        done
    fi
    for role in $(echo "$APP_ROLES" | tr ',' ' '); do
        kc add-roles -r "$REALM" --uusername "$APP_USERNAME" --rolename "$role"
    done
    echo "user $APP_USERNAME: roles $APP_ROLES${APP_ROLES_EXACT:+ (only)}"
fi
