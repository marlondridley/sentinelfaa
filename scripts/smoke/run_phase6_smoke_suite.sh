#!/bin/sh
# The production smoke suite, against the live stack, in order:
#   01  zero static credentials       nothing credential-shaped where it should not be
#   02  negative access                every unauthorized path to a secret is refused
#   03  rotation                       identities and Vault logins survive rotation
#                                      (briefly shortens the api identity's lifetime,
#                                      always restored; about four minutes)
#   05  public exposure                the Cloudflare tunnel reaches only the
#                                      dashboard and Keycloak's sign-in pages
# then SPIRE registrations against spire-entries.yaml (no drift).
#
#   scripts/smoke/run_phase6_smoke_suite.sh              all of it
#   SKIP_ROTATION=1 scripts/smoke/run_phase6_smoke_suite.sh   all but 03
#
# Each probe's full output is printed; a summary follows. Exit 0 only if every
# probe passed. Keep the output with the ceremony record.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
export MSYS_NO_PATHCONV=1
results=""
failed=0

run() {  # run <label> <command...>
    label=$1; shift
    printf '\n========== %s ==========\n' "$label"
    started=$(date +%s)
    "$@"
    code=$?
    took=$(( $(date +%s) - started ))
    if [ "$code" -eq 0 ]; then verdict=PASS; else verdict="FAIL (exit $code)"; failed=1; fi
    results="$results$(printf '  %-34s %-16s %4ss' "$label" "$verdict" "$took")
"
}

# The first Python that can import PyYAML (on Windows, "python3" may be the
# Microsoft Store stub, which cannot).
python_cmd() {
    for p in python3 python; do
        if "$p" -c "import yaml" >/dev/null 2>&1; then echo "$p"; return; fi
    done
    echo python3
}

# From instraclustr/, with relative paths: a Windows Python cannot open the
# /c/... paths Git Bash would otherwise hand it.
spire_drift() {
    ( cd "$REPO/instraclustr" && "$(python_cmd)" scripts/spire-entries-apply.py spire-entries.yaml \
        --exec-prefix "docker exec -i spire-server" --check )
}

echo "Phase 6 smoke suite: $(date -u +%Y-%m-%dT%H:%M:%SZ)  host=$(hostname)"
run "01 zero static credentials" sh "$HERE/01_zero_static_creds_probe.sh"
run "02 negative access" sh "$HERE/02_negative_access_probe.sh"
if [ "${SKIP_ROTATION:-0}" = 1 ]; then
    results="$results  03 rotation                        SKIPPED
"
else
    run "03 rotation" sh "$HERE/03_token_rotation_probe.sh"
fi
run "05 public exposure" sh "$HERE/05_public_exposure_probe.sh"
run "SPIRE registrations: no drift" spire_drift

printf '\n========== summary ==========\n%s' "$results"
if [ "$failed" -eq 0 ]; then echo "RESULT: the live stack passed the smoke suite"; else echo "RESULT: FAILED -- see the probe output above"; fi
exit "$failed"
