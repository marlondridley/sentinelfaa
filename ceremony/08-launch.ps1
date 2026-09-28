# Step 8 of 9: start everything, the normal way from now on.
#
# The launcher checks the certificates, the infrastructure and spire-agent,
# then starts the application. Each service waits for its Vault Agent sidecar
# to render its secrets. Add -WithLiveFeed to also start the FAA SWIM kit.
#
# Success: "Launch sequence complete!". If a service waits forever:
#   docker logs nas-<service>-vault-agent   and   docker logs nas-<service>-spiffe
& (Join-Path (Split-Path -Parent $PSScriptRoot) "launch_nas_sentinel.ps1") @args
exit $LASTEXITCODE
