#!/bin/sh
# Tripwire: the ways an application credential used to reach a container, and
# the ways Vault used to be reached, must not come back. Scans git-tracked
# files; exits 1 on any hit, naming the file, line and rule.
#
#   .ci/check_no_legacy_vault_vars.sh             scan the repository
#   .ci/check_no_legacy_vault_vars.sh --self-test  prove every rule fires
#
# Each rule: a name, an extended regex, the few paths where it is legitimate,
# and what a hit means. Widening an allowlist is a review decision -- the
# reason goes in the comment above the rule.
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT" || exit 2

# Fields are separated by "~" (patterns use "|").
# Files that name a forbidden thing in order to forbid it -- this script, the
# wiring tests that assert its absence -- are allowed where they must be.
RULES='
# Application AppRole credentials: retired, services use SPIFFE identities.
approle-app-credential~VAULT_ROLE_ID|VAULT_SECRET_ID|VAULT_APPROLE_DIR~.ci/* instraclustr/tests/test_identity_wiring.py~an application AppRole credential (retired: services use SPIFFE identities)
# A Vault token belongs only to the bootstrap jobs, the root ceremony (and the
# Terraform provider it feeds), the probes, the retired vault-init.sh, and the
# throwaway dev Vault in the guardrails workflow.
vault-token~VAULT_TOKEN~instraclustr/vault-unseal-and-seed.bash instraclustr/litellm-bootstrap.sh instraclustr/terraform/ceremony.sh instraclustr/terraform/main.tf scripts/smoke/*.sh instraclustr/scripts/vault-init.sh .ci/* instraclustr/tests/test_identity_wiring.py .github/workflows/guardrails.yml~a Vault token outside the bootstrap jobs, the root ceremony and the probes
approle-login~auth/approle/login~instraclustr/vault-unseal-and-seed.bash instraclustr/litellm-bootstrap.sh .ci/*~an AppRole login outside the vault-bootstrap identity
# Only the ceremony names them, to delete them.
app-approle-role~approle/role/sentinel-(app|migrate)~instraclustr/terraform/ceremony.sh .ci/*~a retired application AppRole role
# The throwaway dev Vault in the guardrails workflow: loopback on a CI runner, gone
# with the job.
plaintext-vault~http://(vault|127\.0\.0\.1|localhost):8200~instraclustr/scripts/vault-init.sh .ci/* .github/workflows/guardrails.yml~Vault over plaintext HTTP (Vault is TLS only)
tls-disabled~tls_disable[[:space:]]*=[[:space:]]*(true|1|"true")|VAULT_SKIP_VERIFY|TLS_SKIP_VERIFY~.ci/*~TLS turned off, or verification skipped
insecure-spire-bootstrap~insecure_bootstrap[[:space:]]*=[[:space:]]*true~.ci/*~spire-agent trusting spire-server on first use
hvac-client~import hvac|hvac\.Client~.ci/* .semgrep/*~a direct Vault client in the application (the sidecar talks to Vault, never the service)
tls-verify-off~verify[[:space:]]*=[[:space:]]*False~.ci/* .semgrep/*~TLS verification disabled in Python
'

field() { printf '%s\n' "$RULES" | grep -v '^#' | awk -F'~' -v r="$1" -v f="$2" '$1 == r { print $f }'; }
rules() { printf '%s\n' "$RULES" | grep -v '^#' | awk -F'~' 'NF == 4 { print $1 }'; }

allowed() {  # allowed <path> <globs>
    for glob in $2; do
        # shellcheck disable=SC2254
        case "$1" in $glob) return 0 ;; esac
    done
    return 1
}

scan() {  # scan: file list on stdin; prints hits, returns 1 if any
    hits=0
    files=$(cat)
    for rule in $(rules); do
        pattern=$(field "$rule" 2)
        allow=$(field "$rule" 3)
        matches=$(printf '%s\n' "$files" | tr '\n' '\0' | xargs -0 grep -nIE -- "$pattern" 2>/dev/null || true)
        [ -z "$matches" ] && continue
        while IFS= read -r m; do
            path=${m%%:*}
            allowed "$path" "$allow" && continue
            echo "  $rule: $m"
            echo "      -> $(field "$rule" 4)"
            hits=1
        done <<EOF
$matches
EOF
    done
    return "$hits"
}

if [ "${1:-}" = "--self-test" ]; then
    # Every rule must fire on a line written the way the retired code wrote it,
    # in a file no allowlist covers.
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    mkdir -p "$tmp/nas-feedops-sentinel"
    cat > "$tmp/nas-feedops-sentinel/legacy.txt" <<'FIXTURE'
VAULT_APPROLE_DIR: /run/vault-approle
VAULT_TOKEN: change-me
vault write auth/approle/login role_id=x secret_id=y
vault write auth/approle/role/sentinel-app token_policies=sentinel-app
VAULT_ADDR: http://vault:8200
tls_disable = true
insecure_bootstrap = true
import hvac
client = httpx.Client(verify=False)
FIXTURE
    out=$(cd "$tmp" && echo "nas-feedops-sentinel/legacy.txt" | scan)
    failed=0
    for rule in $(rules); do
        if printf '%s\n' "$out" | grep -q "^  $rule:"; then echo "  fires: $rule"; else echo "  SILENT: $rule"; failed=1; fi
    done
    if [ "$failed" -eq 0 ]; then echo "self-test: every rule fires"; else echo "self-test FAILED"; fi
    exit "$failed"
fi

echo "Legacy credential / plaintext-Vault tripwire"
if git ls-files | grep -vE '^(llm-wiki|reference|jumpstart-latest)/' | scan; then
    echo "clean: no retired credential path or plaintext Vault access in tracked files"
    exit 0
fi
echo "FAILED: see above (rules and allowlists: .ci/check_no_legacy_vault_vars.sh)"
exit 1
