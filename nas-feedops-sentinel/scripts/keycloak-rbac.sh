#!/usr/bin/env bash
# Runs INSIDE the nas-keycloak container. Idempotent.
#
# Brings a live nas-sentinel realm up to the RBAC defined in
# keycloak/realm-nas-sentinel.json -- realm import is skip-if-exists, so a
# realm imported before the roles existed never gets them otherwise:
#   * realm roles watch_officer, supervisor, auditor
#   * a protocol mapper putting realm roles in the dashboard's ID token
#     (src/keycloak_auth.roles_from_claims reads realm_access.roles)
#   * optionally, grants GRANT_ROLES (comma-separated) to GRANT_USERS
#     (comma-separated usernames, or "*" for every user in the realm)
#
# The admin password arrives via `docker exec -e KC_ADMIN_PW` and is never echoed.
set -euo pipefail
: "${KC_ADMIN_PW:?}"
KC_SERVER="${KC_SERVER:-http://localhost:8080}"
REALM=nas-sentinel
CLIENT_ID=nas-feedops-sentinel
CFG=/tmp/kcadm-rbac.config
kc() { /opt/keycloak/bin/kcadm.sh "$@" --config "$CFG"; }
trap 'rm -f "$CFG"' EXIT

kc config credentials --server "$KC_SERVER" --realm master --user admin --password "$KC_ADMIN_PW" >/dev/null

for role in watch_officer supervisor auditor; do
    if kc get "roles/$role" -r "$REALM" >/dev/null 2>&1; then
        echo "role $role: exists"
    else
        kc create roles -r "$REALM" -s name="$role" >/dev/null
        echo "role $role: created"
    fi
done

CLIENT_UUID=$(kc get clients -r "$REALM" -q clientId="$CLIENT_ID" --fields id --format csv --noquotes)
[ -n "$CLIENT_UUID" ] || { echo "ERROR: client $CLIENT_ID missing" >&2; exit 1; }
if kc get "clients/$CLIENT_UUID/protocol-mappers/models" -r "$REALM" --fields name --format csv --noquotes \
        | grep -qx "realm roles in ID token"; then
    echo "mapper: exists"
else
    kc create "clients/$CLIENT_UUID/protocol-mappers/models" -r "$REALM" \
        -s name="realm roles in ID token" -s protocol=openid-connect \
        -s protocolMapper=oidc-usermodel-realm-role-mapper \
        -s 'config."multivalued"=true' -s 'config."claim.name"=realm_access.roles' \
        -s 'config."jsonType.label"=String' -s 'config."id.token.claim"=true' \
        -s 'config."access.token.claim"=true' -s 'config."userinfo.token.claim"=true' >/dev/null
    echo "mapper: created"
fi

if [ -n "${GRANT_USERS:-}" ] && [ -n "${GRANT_ROLES:-}" ]; then
    if [ "$GRANT_USERS" = "*" ]; then
        USERS=$(kc get users -r "$REALM" --fields username --format csv --noquotes)
    else
        USERS=$(echo "$GRANT_USERS" | tr ',' '\n')
    fi
    for user in $USERS; do
        for role in $(echo "$GRANT_ROLES" | tr ',' ' '); do
            kc add-roles -r "$REALM" --uusername "$user" --rolename "$role"
        done
        echo "user $user: granted $GRANT_ROLES"
    done
fi
