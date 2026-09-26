#!/usr/bin/env bash
# Runs INSIDE the nas-keycloak container. Idempotent.
#
# Brings a live nas-sentinel realm up to the RBAC defined in
# keycloak/realm-nas-sentinel.json -- realm import is skip-if-exists, so a
# realm imported before the roles existed never gets them otherwise:
#   * realm roles watch_officer, supervisor, auditor
#   * the dashboard client's OIDC settings: redirect, web origin and
#     post-logout target on DASHBOARD_URL, PKCE (S256) required, no direct
#     access grant -- operators sign in on the realm's page, never the dashboard's
#   * a protocol mapper putting realm roles in the dashboard's ID token
#     (src/keycloak_auth.roles_from_claims reads realm_access.roles)
#   * optionally, grants GRANT_ROLES (comma-separated) to GRANT_USERS
#     (comma-separated usernames, or "*" for every user in the realm)
#
# The admin password arrives via `docker exec -e KC_ADMIN_PW` and is never echoed.
set -euo pipefail
: "${KC_ADMIN_PW:?}"
KC_SERVER="${KC_SERVER:-https://localhost:8443}"
# Keycloak serves HTTPS only (docker-compose.yml). kcadm is a Java client, so it
# trusts the local authority through a throwaway PKCS12 truststore built from
# the mounted certificate; the store holds only that public certificate.
KC_CA_FILE="${KC_CA_FILE:-/opt/keycloak/conf/tls/edge-ca.crt}"
REALM=nas-sentinel
CLIENT_ID=nas-feedops-sentinel
CFG=/tmp/kcadm-rbac.config
kc() { /opt/keycloak/bin/kcadm.sh "$@" --config "$CFG"; }
TRUST=/tmp/kcadm-rbac-trust.p12
trap 'rm -f "$CFG" "$TRUST"' EXIT
TRUSTPASS=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
keytool -importcert -noprompt -alias sentinel-local-ca -file "$KC_CA_FILE" \
    -keystore "$TRUST" -storetype PKCS12 -storepass "$TRUSTPASS" >/dev/null
kc config truststore --trustpass "$TRUSTPASS" "$TRUST" >/dev/null

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

# The dashboard signs in with OIDC: the realm's login page, then back through
# the edge proxy (src/keycloak_auth.py). These are the realm file's client
# settings; set on every run, so a realm imported earlier converges on them.
DASHBOARD_URL="${DASHBOARD_URL:-https://localhost:8443}"
kc update "clients/$CLIENT_UUID" -r "$REALM" \
    -s standardFlowEnabled=true -s directAccessGrantsEnabled=false \
    -s "redirectUris=[\"$DASHBOARD_URL/oauth2callback\"]" \
    -s "webOrigins=[\"$DASHBOARD_URL\"]" \
    -s "attributes.\"post.logout.redirect.uris\"=$DASHBOARD_URL/oauth2callback" \
    -s 'attributes."pkce.code.challenge.method"=S256'
echo "client $CLIENT_ID: OIDC via $DASHBOARD_URL/oauth2callback, PKCE S256, no direct access grant"
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
