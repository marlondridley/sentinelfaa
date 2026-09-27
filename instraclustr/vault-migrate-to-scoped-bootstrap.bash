#!/bin/sh
# Run this ONCE, by hand, against a Vault whose vault-bootstrap AppRole was set up
# under the OLD, root-equivalent policy (sys/policies/acl/sentinel-* plus create/
# update on the bare auth/approle/role/sentinel-* path -- a policy vault-bootstrap
# could write and bind a role to is root-equivalent, whatever paths it names).
# See vault-unseal-and-seed.bash's header for the narrow replacement.
#
# What this does, in one pass:
#   1. Re-authors the vault-bootstrap, sentinel-app and sentinel-migrate policies
#      to their narrow form, and reconfigures all three AppRole roles
#      (secret_id_ttl / secret_id_num_uses included) -- root_setup(), dot-sourced
#      from vault-unseal-and-seed.bash so both scripts share one definition.
#   2. Destroys and re-mints EVERY one of the three roles' secret-ids. Treat any
#      secret-id or token minted while the old policy was live as potentially
#      over-permissioned, whether or not you have reason to think it leaked --
#      the point is to stop trusting anything issued under the old grant, not
#      to prove one particular copy was compromised. Rewriting the policy body
#      (step 1) already closes the escalation path for every token bound to it,
#      immediately, before this script does anything else -- Vault checks a
#      token's policy live on every request, not a snapshot from when it was
#      issued. This step is about not leaving old secret-id FILES sitting
#      around valid, not about closing the hole a second time.
#   3. Revokes the root token this ran with. Nothing from this run is left on
#      disk or in Vault beyond the new policies/roles/secret-ids.
#
# You need, right now: VAULT_ADDR pointing at the real Vault, and a fresh root
# token in VAULT_TOKEN. Regenerate one from two of the three unseal shares
# (never store it):
#   vault operator generate-root -init
#   vault operator generate-root                    # with share 1, then share 2
#   vault operator generate-root -decode=<encoded_token> -otp=<otp>
# (`vault operator generate-root` with no flags walks you through this
# interactively and prints the exact commands for your Vault version.)
#
# After this runs, restart every service that reads vault-init-data/approle/
# sentinel-*/{role_id,secret_id} (they authenticate once, at process start --
# see src/vault_config.py -- so a service already running on an old secret-id
# keeps working until its OWN restart, but should still be restarted here,
# now, rather than left to pick up a fresh secret-id at some unplanned later
# restart).
set -eu

: "${VAULT_ADDR:?Missing VAULT_ADDR}"
: "${VAULT_TOKEN:?Missing VAULT_TOKEN -- a root token, from vault operator generate-root; see the header above.}"
export VAULT_TOKEN

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
INIT_DIR="${INIT_DIR:-/vault-init-data}"

echo "=== Confirming this is a root (or root-equivalent) token before doing anything ==="
TOKEN_INFO="$(vault token lookup -format=json)"
echo "$TOKEN_INFO" | grep -q '"root"' \
    || { echo "ERROR: this token does not hold the root policy:" >&2; echo "$TOKEN_INFO" >&2; echo "Regenerate a real root token (see this script's header) -- do not run this with vault-bootstrap's own token, which is exactly the credential being rotated away from." >&2; exit 1; }

echo "=== Re-authoring vault-bootstrap/sentinel-app/sentinel-migrate under the narrow policy ==="
VAULT_BOOTSTRAP_SOURCED=1 . "$SCRIPT_DIR/vault-unseal-and-seed.bash"
root_setup

# A static check, not a live login: logging in as vault-bootstrap here to test it would
# consume the single-use secret-id root_setup just minted for the NEXT real boot, and
# then need to rotate it again to undo that -- fragile for what this only needs to prove.
# The dynamic red-team proof (mint, log in, attempt to escalate, confirm denied) already
# ran against a disposable Vault while this fix was being built; this is a sanity check
# that root_setup's own heredoc landed as written, not a second independent proof.
echo "=== Verifying the policy vault-bootstrap now holds has no policy-authoring or role-reconfiguration capability ==="
POLICY_TEXT=$(vault policy read vault-bootstrap)
# Comments are stored and returned as part of the policy text (this file's own header
# above says "sys/policies/acl writes" in describing what is refused) -- strip them
# before checking for an actual grant, not just the words appearing anywhere.
POLICY_RULES=$(echo "$POLICY_TEXT" | grep -v '^[[:space:]]*#')
if echo "$POLICY_RULES" | grep -qE 'sys/policies|path "auth/approle/role/[a-z-]+"[[:space:]]*\{'; then
    echo "ERROR: vault-bootstrap's policy still grants policy-authoring or bare role-write capability:" >&2
    echo "$POLICY_TEXT" >&2
    exit 1
fi
echo "confirmed: no policy-authoring or role-reconfiguration path in vault-bootstrap's policy"

revoke_root
echo "=== Migration complete: restart every service that reads $INIT_DIR/approle/sentinel-*/ now ==="
