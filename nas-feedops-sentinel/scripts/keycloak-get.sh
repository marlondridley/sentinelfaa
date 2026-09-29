#!/usr/bin/env bash
# Runs INSIDE the nas-keycloak container (copied in by Get-SignIns.ps1 and
# Disable-ExpiredLogins.ps1). One read-only admin query against the
# nas-sentinel realm, printed as JSON; nothing is changed.
#
#   KC_ADMIN_PW   the admin password, passed by name (`docker exec -e`), never echoed
#   KC_PATH       the admin endpoint under the realm: events | users | ...
#   KC_QUERY      optional, space-separated key=value query parameters
set -euo pipefail
: "${KC_ADMIN_PW:?}" "${KC_PATH:?}"
case "$KC_PATH" in
    events|admin-events|users|users/profile) ;;
    *) echo "ERROR: KC_PATH $KC_PATH is not one of the read-only queries this helper serves" >&2; exit 2 ;;
esac
KC_SERVER="${KC_SERVER:-https://localhost:8443}"
KC_CA_FILE="${KC_CA_FILE:-/opt/keycloak/conf/tls/edge-ca.crt}"
REALM=nas-sentinel
CFG=/tmp/kcadm-get.config
TRUST=/tmp/kcadm-get-trust.p12
trap 'rm -f "$CFG" "$TRUST"' EXIT
kc() { /opt/keycloak/bin/kcadm.sh "$@" --config "$CFG"; }
TRUSTPASS=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
keytool -importcert -noprompt -alias sentinel-local-ca -file "$KC_CA_FILE" \
    -keystore "$TRUST" -storetype PKCS12 -storepass "$TRUSTPASS" >/dev/null 2>&1
kc config truststore --trustpass "$TRUSTPASS" "$TRUST" >/dev/null
kc config credentials --server "$KC_SERVER" --realm master --user admin --password "$KC_ADMIN_PW" >/dev/null 2>&1

args=()
for pair in ${KC_QUERY:-}; do args+=(-q "$pair"); done
kc get "$KC_PATH" -r "$REALM" "${args[@]}"
