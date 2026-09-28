#!/bin/sh
# Workload-identity PKI: an offline root, keys generated where they are used,
# requests carried to the root and certificates carried back.
#
#   root CA (10 y, P-384, passphrase-encrypted)   OFFLINE: USB + backup USB
#     +-- SPIRE upstream CA (1 y, P-384)            certs/spire  (spire-server)
#     |     +-- SPIRE's own CA (7 d; spire-server rotates it)
#     |           +-- X.509-SVIDs (1 h; spire-agent rotates them)
#     +-- Vault server certificate (1 y, P-256)     certs/vault  (vault)
#
# Commands -- OpenSSL 3, POSIX sh; instraclustr/certs/README.md has the exact
# docker commands for each step and the custody matrix:
#   root-init <root-dir>            OFFLINE, once. root-ca.key (asks for the
#                                   passphrase) and root-ca.crt.
#   request   <certs-dir>           HOST. A fresh key per certificate
#                                   (*.key.pending, never leaves the host) and
#                                   its CSR (*.csr, public).
#   sign      <root-dir> <certs-dir>
#                                   WITH THE ROOT. Signs every CSR under
#                                   certs/policy/signing.cnf -- never the
#                                   extensions a CSR asks for -- writes
#                                   *.crt.pending, deletes the CSR, and puts
#                                   the root's certificate in certs/trust if it
#                                   is not there yet. <certs-dir> may be the
#                                   host's own (root USB plugged in) or a copy
#                                   of its spire/ and vault/ CSRs carried to an
#                                   offline machine.
#   install   <certs-dir>           HOST. Checks each pending certificate
#                                   against its pending key, the root and the
#                                   policy, then puts both in place.
#   check     <certs-dir>           Exit 0 current, 1 renewal due (under 60
#                                   days) or root near expiry, 2 anything
#                                   missing, expired, mismatched, or not issued
#                                   by this root.
#
# PKI_ROOT_PASS, when set, is handed to OpenSSL as the root key's passphrase
# source (env:NAME or file:PATH): for the throwaway roots tests and the smoke
# harness create. A real ceremony leaves it unset and types the passphrase.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
POLICY="${PKI_POLICY:-$HERE/../certs/policy}"
TRUST_DOMAIN="${PKI_TRUST_DOMAIN:-sentinelfaa.internal}"
ROOT_DAYS=3650
ISSUED_DAYS=365
RENEW_DAYS=60
# hashicorp/vault runs its server as vault: uid 100, gid 1000.
VAULT_UID=100
VAULT_GID=1000

die() { echo "ERROR: $*" >&2; exit 2; }

# `openssl ... $(pass -passin)`: the passphrase source flag, or nothing (prompt).
pass() { if [ -n "${PKI_ROOT_PASS:-}" ]; then echo "$1 $PKI_ROOT_PASS"; fi; }

# issued <function>: calls <function> once per certificate the root issues,
# as <function> <dir> <name> <request section> <extension section> <curve>.
issued() {
    "$1" spire intermediate-ca req_spire spire_upstream_ext P-384
    "$1" vault vault req_vault vault_server_ext P-256
}

pubkey_of_cert() { openssl x509 -in "$1" -noout -pubkey; }
pubkey_of_key() { openssl pkey -in "$1" -pubout; }

# conforms <cert> <extension section>: the certificate carries this policy's
# defining properties (what `install` and `check` will not take on trust).
conforms() {
    case "$2" in
        spire_upstream_ext)
            ext=$(openssl x509 -in "$1" -noout -ext basicConstraints,nameConstraints 2>/dev/null) || return 1
            echo "$ext" | grep -q "CA:TRUE, pathlen:1" \
                && echo "$ext" | grep -q "URI:$TRUST_DOMAIN\$" \
                && echo "$ext" | grep -q "DNS:invalid\$" ;;
        vault_server_ext)
            ext=$(openssl x509 -in "$1" -noout -ext subjectAltName,basicConstraints 2>/dev/null) || return 1
            echo "$ext" | grep -q "DNS:vault, DNS:localhost, IP Address:127.0.0.1" \
                && echo "$ext" | grep -q "CA:FALSE" ;;
        *) return 1 ;;
    esac
}

# from_root <cert>: verifies to certs/trust/root-ca.crt, strictly.
from_root() { openssl verify -x509_strict -CAfile "$CERTS/trust/root-ca.crt" "$1" >/dev/null 2>&1; }

secure_key() {  # secure_key <dir> <key file>
    if [ "$1" = vault ]; then
        chmod 400 "$2"
        if [ "$(id -u)" = 0 ]; then chown "$VAULT_UID:$VAULT_GID" "$2"; fi
    else
        chmod 600 "$2"            # spire-server runs as root
    fi
}

# ── root-init ────────────────────────────────────────────────────────────────
root_init() {
    root=$1
    mkdir -p "$root"
    [ ! -e "$root/root-ca.key" ] || die "$root/root-ca.key already exists: a root is created once"
    # shellcheck disable=SC2046
    ( umask 077
      openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-384 -aes-256-cbc \
          $(pass -pass) -out "$root/root-ca.key" )
    # shellcheck disable=SC2046
    openssl req -x509 -new -key "$root/root-ca.key" $(pass -passin) \
        -config "$POLICY/root.cnf" -days "$ROOT_DAYS" -out "$root/root-ca.crt"
    echo "root created: $root/root-ca.crt, $root/root-ca.key"
    echo "Copy root-ca.key to the backup USB and put the passphrase in the password manager."
}

# ── request ──────────────────────────────────────────────────────────────────
request_one() {
    d="$CERTS/$1"
    mkdir -p "$d"
    ( umask 077; openssl genpkey -algorithm EC -pkeyopt "ec_paramgen_curve:$5" -out "$d/$2.key.pending" )
    openssl req -new -key "$d/$2.key.pending" -config "$POLICY/signing.cnf" -section "$3" -out "$d/$2.csr"
    echo "request: $1/$2.csr (its key stays here as $1/$2.key.pending)"
}

# ── sign ─────────────────────────────────────────────────────────────────────
sign_one() {
    csr="$CERTS/$1/$2.csr"
    if [ ! -f "$csr" ]; then echo "sign: no $1/$2.csr, skipped"; return 0; fi
    openssl req -in "$csr" -noout -verify >/dev/null 2>&1 || die "$csr: its own signature does not verify"
    curve=$(openssl req -in "$csr" -noout -text | sed -n 's/.*NIST CURVE: *//p')
    [ "$curve" = "$5" ] || die "$csr: key is ${curve:-not EC}, policy wants $5"
    # Into a scratch name first: OpenSSL creates -out before it has signed
    # anything, so a wrong passphrase would otherwise leave an empty
    # certificate that looks pending.
    out="$CERTS/$1/$2.crt.signing"
    # shellcheck disable=SC2046
    if ! openssl x509 -req -in "$csr" -CA "$ROOT/root-ca.crt" -CAkey "$ROOT/root-ca.key" $(pass -passin) \
            -CAserial "$ROOT/root-ca.srl" -CAcreateserial -days "$ISSUED_DAYS" -sha384 \
            -extfile "$POLICY/signing.cnf" -extensions "$4" -out "$out" 2>/dev/null; then
        rm -f "$out"
        die "signing $csr failed (wrong passphrase?)"
    fi
    mv -f "$out" "$CERTS/$1/$2.crt.pending"
    rm -f "$csr"
    echo "signed: $1/$2.crt.pending"
}

# ── install ──────────────────────────────────────────────────────────────────
install_one() {
    d="$CERTS/$1"
    crt="$d/$2.crt.pending"
    key="$d/$2.key.pending"
    if [ ! -f "$crt" ]; then echo "install: nothing pending for $1/$2"; return 0; fi
    [ -f "$key" ] || die "$crt has no $key beside it: request again"
    from_root "$crt" || die "$crt does not verify to certs/trust/root-ca.crt"
    conforms "$crt" "$4" || die "$crt does not carry the $4 policy"
    [ "$(pubkey_of_cert "$crt")" = "$(pubkey_of_key "$key")" ] || die "$crt was not issued for $key"
    mv -f "$key" "$d/$2.key"
    mv -f "$crt" "$d/$2.crt"
    secure_key "$1" "$d/$2.key"
    echo "installed: $1/$2.crt"
}

# ── check ────────────────────────────────────────────────────────────────────
WORST=0
worse() { if [ "$1" -gt "$WORST" ]; then WORST=$1; fi; }

check_one() {
    crt="$CERTS/$1/$2.crt"
    key="$CERTS/$1/$2.key"
    if [ ! -f "$crt" ] || [ ! -f "$key" ]; then
        echo "BROKEN $1/$2: missing"; worse 2
    elif ! from_root "$crt"; then
        echo "BROKEN $1/$2: expired, or not issued by certs/trust/root-ca.crt"; worse 2
    elif ! conforms "$crt" "$4"; then
        echo "BROKEN $1/$2: does not carry the $4 policy"; worse 2
    elif [ "$(pubkey_of_cert "$crt")" != "$(pubkey_of_key "$key")" ]; then
        echo "BROKEN $1/$2: certificate and key do not match"; worse 2
    elif ! openssl x509 -in "$crt" -noout -checkend $((RENEW_DAYS * 86400)) >/dev/null; then
        echo "DUE $1/$2: expires $(openssl x509 -in "$crt" -noout -enddate | cut -d= -f2) -- request, sign, install"
        worse 1
    else
        echo "OK $1/$2: until $(openssl x509 -in "$crt" -noout -enddate | cut -d= -f2)"
    fi
}

check() {
    root="$CERTS/trust/root-ca.crt"
    if [ ! -f "$root" ] || ! openssl x509 -in "$root" -noout -checkend 0 >/dev/null 2>&1; then
        echo "BROKEN trust/root-ca.crt: missing or expired"
        return 2
    fi
    if ! openssl x509 -in "$root" -noout -checkend $((RENEW_DAYS * 86400)) >/dev/null; then
        echo "DUE trust/root-ca.crt: the root itself expires soon -- plan a new root"
        worse 1
    fi
    issued check_one
    return "$WORST"
}

# ── main ─────────────────────────────────────────────────────────────────────
command -v openssl >/dev/null || die "openssl not found"
cmd="${1:-}"
case "$cmd" in
    root-init)
        [ $# -eq 2 ] || die "usage: $0 root-init <root-dir>"
        root_init "$2" ;;
    request)
        [ $# -eq 2 ] || die "usage: $0 request <certs-dir>"
        CERTS=$2; issued request_one ;;
    sign)
        [ $# -eq 3 ] || die "usage: $0 sign <root-dir> <certs-dir>"
        ROOT=$2; CERTS=$3
        [ -f "$ROOT/root-ca.crt" ] && [ -f "$ROOT/root-ca.key" ] || die "no root-ca.crt/root-ca.key in $ROOT"
        mkdir -p "$CERTS/trust"
        if [ ! -f "$CERTS/trust/root-ca.crt" ]; then
            cp "$ROOT/root-ca.crt" "$CERTS/trust/root-ca.crt"
            echo "trust: installed root-ca.crt"
        elif ! cmp -s "$ROOT/root-ca.crt" "$CERTS/trust/root-ca.crt"; then
            die "$CERTS/trust/root-ca.crt is a different root than $ROOT/root-ca.crt"
        fi
        issued sign_one ;;
    install)
        [ $# -eq 2 ] || die "usage: $0 install <certs-dir>"
        CERTS=$2
        [ -f "$CERTS/trust/root-ca.crt" ] || die "no $CERTS/trust/root-ca.crt: sign puts it there"
        issued install_one ;;
    check)
        [ $# -eq 2 ] || die "usage: $0 check <certs-dir>"
        CERTS=$2
        status=0; check || status=$?
        exit "$status" ;;
    *)
        sed -n '2,/^set -eu/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//' >&2
        exit 2 ;;
esac
