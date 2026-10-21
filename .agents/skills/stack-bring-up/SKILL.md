---
name: stack-bring-up
description: Start or redeploy the local Sentinel Docker stack with launch_nas_sentinel.ps1, and recover it when containers are exited or unhealthy after Docker Desktop restarted (the SPIRE agent node identity expired). Use when the user says bring the stack up, redeploy, run the deployment, or the stack is down, or when you see exited (137) containers, unhealthy nas-*-vault-agent containers, a missing svid.pem file, or a join token was not provided error.
compatibility: Windows with Docker Desktop and PowerShell. Run from the repository root. Needs instraclustr/.env and nas-feedops-sentinel/.env to exist.
metadata:
  area: operations
---

# Bringing the Sentinel stack up

The stack is two Docker Compose projects joined by the `nas-network` network and the `nas-spire-agent-socket` volume:

- `instraclustr/`: Postgres, PgBouncer, Vault, LiteLLM, SPIRE server and agent.
- `nas-feedops-sentinel/`: the app (api, dashboard, sentinel, temporal worker, migrate), each with a `spiffe` and a `vault-agent` sidecar, plus Prometheus, Alertmanager, Grafana and Keycloak.

`launch_nas_sentinel.ps1` starts both, in order, and is safe to run again.

## Before you touch anything

1. **The checked-out branch is what runs.** The launcher builds from the working tree. Run `git branch --show-current` and tell the user which branch you are about to deploy. Deploying a feature branch by accident replaces the live build.
2. **Identity-plane work needs a yes first.** Recovering SPIRE (below) re-attests the agent and rewrites registrations. Say what you will run and wait for the user to approve it. A request to "run the deployment" does not by itself approve SPIRE recovery.
3. **Check the certificates under the offline root before the launcher does.** Run `.\instraclustr\scripts\pki.ps1 check` (it reads files and starts nothing). Exit 0 is fine and 1 means one is due soon. **Exit 2 means the launcher will stop at its step 1b**, and only the user can fix it: the ceremony needs their root USB and passphrase, which you never type. If it lists `BROKEN web/web-ca`, the one-time move in `nas-feedops-sentinel/docs/runbooks.md` ("Move the edge and Keycloak certificates to the offline root (once)") has not been done. Stop, tell the user, and do not start a launch.
4. **Tag a rollback image before a redeploy** so there is something to return to:
   ```powershell
   docker tag nas-feedops-sentinel:latest nas-feedops-sentinel:rollback-<yyyy-mm-dd>
   ```
5. **Leave other containers alone.** Containers named like `*-pg`, `harness-*` or `covwork-*` belong to test runs, not to the stack.
6. **Never type credentials for the user.** Never put a join token, key or password in a file, a log or your reply.

## Normal launch

```powershell
.\launch_nas_sentinel.ps1
```

| Flag | Effect | When |
|---|---|---|
| (none) | Public access off, tunnel stopped, sign-in on `https://localhost:8443`, no live feed | The default. Use it unless told otherwise. |
| `-Public` | Turns the Cloudflare tunnel and public sign-in on | Only when the user asks for public access in so many words. |
| `-WithLiveFeed` | Also starts the FAA SWIM Jumpstart Kit in its own window | Only when asked. It opens a live subscription to the FAA broker and needs Java 25. |
| `-NoJanitor` | Skips the kit-log cleanup window | Only with `-WithLiveFeed`. |

What the launcher does, so you can read its failures: it clears shell variables that shadow `.env` values, creates the network and volume, starts the infrastructure stack, waits for PgBouncer, Vault and LiteLLM, checks that the bootstrap jobs (`vault-unseal`, `litellm-db-init`, `litellm-bootstrap`) exited 0, checks that `spire-agent` is running, then builds and starts the app stack, stops the migrate sidecars, applies the public-access mode, and disables expired guest logins.

**The `nas-migrate-*` sidecars stopped after launch is correct.** They hold the schema owner's credentials and are stopped on purpose once migrate finishes. Do not "fix" them.

## When the stack is down after a Docker restart

Symptoms: app containers exited with code 137, every `nas-*-vault-agent` unhealthy with `open /run/spiffe/svid.pem: no such file`, and the launcher stopping at step 3c with `spire-agent is not running`.

Cause: `spire-agent` exited (1). Its node identity expired while Docker was down, and a plain restart has no join token, so its log says `join token was not provided`.

Confirm it first:

```powershell
docker inspect spire-agent --format '{{.State.Status}} {{.State.ExitCode}}'
docker logs --tail 20 spire-agent
```

If it shows `exited` and the log has the join-token line, and the user has approved, recover:

```powershell
.\instraclustr\scripts\spire-first-boot.ps1
```

This starts `spire-server`, mints a single-use join token held in that process's memory only, re-attests the agent, applies `instraclustr/spire-entries.yaml`, and checks for drift. It must end with the agent attested and no drift. If it throws, read the named container's logs before retrying; do not loop it.

Then give the sidecars time. After the agent returns, each `nas-*-spiffe` helper reconnects on its own 30 second cycle (about 2 minutes for all of them), and each Vault agent may be in a retry backoff of about 4 to 5 minutes from the time it was down.

- Wait until every `nas-*-spiffe` log says `X.509 certificates updated`.
- If a `nas-*-vault-agent` is still unhealthy, restart it: `docker restart nas-api-vault-agent nas-dashboard-vault-agent nas-sentinel-vault-agent nas-temporal-worker-vault-agent`. Add `nas-proof-vault-agent` if it is part of the running set; `nas-migrate-vault-agent` is expected to be stopped.
- Run `.\launch_nas_sentinel.ps1` again. If it fails on an unhealthy vault-agent, the sidecars were not ready yet; wait and re-run. It is idempotent and the cached image makes the second run quick (about a minute).

If the spire-agent is running but Vault rejects it, the launcher points at "the root ceremony in `instraclustr\README.md`". That is a separate, bigger procedure. Stop and tell the user rather than starting it.

## Verify, and show the evidence

Do not call it done until these pass. Use `curl -sk`: it skips certificate verification, which is enough to show the services answer. The edge's certificate comes from the web TLS branch of the offline root, so a browser or `curl` verifies it only where `instraclustr\certs\trust\root-ca.crt` is trusted (an agent's built-in browser is not). To check the certificate itself, run `curl --cacert instraclustr/certs/trust/root-ca.crt https://localhost:8443`.

```bash
docker ps --format '{{.Names}}\t{{.Status}}'          # app containers healthy
curl -sk -o /dev/null -w '%{http_code}\n' https://localhost:8443          # dashboard edge: 200
curl -sk https://localhost:8444/v1/ready                                    # API: every dependency ok
curl -sk -o /dev/null -w '%{http_code}\n' https://localhost:8081/           # Keycloak: 302
curl -sk https://localhost:8447/api/v1/targets                              # Prometheus: targets up
```

Also check that migrate completed (`docker logs nas-migrate 2>&1 | tail`) and that the app logs show no new error lines.

State plainly what you did not exercise (for example a supervisor halting a model from the console, or a Slack test alert). Do not imply it was tested.

## Report

Say: the branch and commit deployed, the image id now live and the rollback tag, which recovery steps you needed, and the verification results. If the stack is not healthy, say which container and the last log line instead of summarising.
