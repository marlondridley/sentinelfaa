# Step 7 of 9: give spire-agent its identity, and register the six services.
#
# Issues a single-use join token (never written to disk), starts spire-agent
# with it, then applies instraclustr\spire-entries.yaml and checks there is no
# drift left. Needs Python 3 with PyYAML on this computer.
#
# Success: "spire-agent attested as this host." then "no drift".
& (Join-Path $PSScriptRoot "..\instraclustr\scripts\spire-first-boot.ps1")
exit $LASTEXITCODE
