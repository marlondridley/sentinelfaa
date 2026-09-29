# Public access (Cloudflare Tunnel)

How to let invited guests use the dashboard from the internet, without opening any port on this machine or its router. Guests get **read-only** accounts: they can see everything the dashboard shows, and decide nothing.

**Before you invite anyone:** guests see live FAA SWIM data. Check that your SWIFT/SCDS data agreement allows showing it to people outside your organisation.

## What is and is not reachable

```
browser ─HTTPS─> Cloudflare (WAF rules) ─tunnel─> nas-cloudflared ─> nas-edge:8452 ─> the story site (static files)
                                                                  ├> nas-edge:8450 ─> dashboard
                                                                  └> nas-edge:8451 ─> Keycloak sign-in pages only
```

| Reachable from the internet | Never reachable from the internet |
|---|---|
| The story site, at `https://<your-domain>`: static pages about the system, with a Sign in button (`site/`) | Keycloak's admin console and `master` realm |
| The dashboard, at `https://sentinel.<your-domain>` | The account console, Keycloak's metrics and health endpoints |
| Keycloak's sign-in pages for the `nas-sentinel` realm, at `https://auth.<your-domain>` | Grafana, Prometheus, the Temporal UI, the API, Vault, SPIRE, Postgres |

The tunnel only makes outbound connections from `nas-cloudflared`, and publishes no port. Its destinations are listeners on the edge proxy that are not published on this machine either (`docker/edge/nginx.conf`, 8450-8452). Those listeners decide which paths exist at all: a mistaken rule in Cloudflare can't expose anything else. `scripts/smoke/05_public_exposure_probe.sh` proves that on the live stack.

**Cost:** Cloudflare Tunnel and the WAF rules below are free. Cloudflare Access (an email one-time-code gate in front of the site) is optional. It's free up to 50 users, with billing above that.

## 1. Cloudflare: the tunnel routes

Zero Trust → Networks → **Tunnels** → your tunnel → **Routes** → **Add route** → **Published application**, once per route below. Older dashboards call these *Public Hostname* routes. Don't add DNS records by hand: saving a route creates its DNS record (a proxied CNAME to `<tunnel-id>.cfargotunnel.com`). Networking → **Routes**, and the *Tunnel Hostname* / *Tunnel CIDR* choices, are private routes for WARP clients: not these.

| | Route 1: dashboard | Route 2: sign-in |
|---|---|---|
| Subdomain | `sentinel` | `auth` |
| Domain | your domain | your domain |
| Path | *(empty)* | *(empty)* |
| Service URL | `https://nas-edge:8450` | `https://nas-edge:8451` |
| TLS → **Origin Server Name** | `localhost` | `localhost` |
| TLS → **CA Pool** | `/etc/cloudflared/trust/edge-ca.crt` | `/etc/cloudflared/trust/edge-ca.crt` |
| TLS → **Disable TLS certificate verification** (*No TLS Verify*) | **Off** | **Off** |
| HTTP, Connection, Access | defaults | defaults |

Two more routes for the story site, with the same TLS settings (Origin Server Name `localhost`, the same CA Pool, verification on):

| | Route 3: story site | Route 4: www |
|---|---|---|
| Subdomain | *(empty: the bare domain)* | `www` |
| Service URL | `https://nas-edge:8452` | `https://nas-edge:8452` |

The site listener sends `www.<your-domain>` to the bare domain itself. The site is static (`nas-feedops-sentinel/site/`): the edge serves its files and nothing else, under a Content-Security-Policy that allows only its own origin, and its Sign in button opens the dashboard.

Service URL needs the `https://`: the listeners speak only TLS. The TLS settings make the tunnel verify the edge, as every other hop in this stack is verified. The edge's certificate comes from the local authority and is issued for `localhost`, and `nas-cloudflared` has that authority's public certificate (never a key) at `/etc/cloudflared/trust`. Visitors connect to Cloudflare's own certificate. Probe 05 fails if a route turns verification off.

## 2. Cloudflare: free protections (in place of Access)

In the domain's dashboard (not Zero Trust):

- **Security → WAF → Custom rules → Create rule** ("Allow my country only"). Expression, via *Edit expression*:
  ```
  (http.host in {"sentinel.<your-domain>" "auth.<your-domain>"} and ip.geoip.country ne "US")
  ```
  Action **Block**. Change `US` to your guests' country, or add countries.
- **Security → WAF → Rate limiting rules → Create rule** ("Sign-in rate limit"). Expression:
  ```
  (http.host eq "auth.<your-domain>" and starts_with(http.request.uri.path, "/realms/nas-sentinel/login-actions"))
  ```
  Same characteristics: IP. **10 requests per 10 seconds**, action **Block** for 10 seconds. This is on top of Keycloak's own lockout, 5 failed passwords per account.
- **Security → Bots → Bot Fight Mode**: On.
- **SSL/TLS → Edge Certificates**: **Always Use HTTPS** on, **Minimum TLS Version** 1.2.

## 3. This machine: settings

In `instraclustr/.env`, with the other secrets:

```
# The tunnel's connector token: Zero Trust -> Tunnels -> your tunnel -> the long value after --token
CLOUDFLARE_TUNNEL_TOKEN=...
```

In `nas-feedops-sentinel/.env`:

```
PUBLIC_DASHBOARD_URL=https://sentinel.<your-domain>
KEYCLOAK_PUBLIC_URL=https://auth.<your-domain>
```

If you started cloudflared by hand before (`docker run cloudflare/cloudflared ... --token ...`), remove that container. It runs on the wrong Docker network, can't reach the edge, and Cloudflare would send some visitors to it:

```powershell
docker ps --filter ancestor=cloudflare/cloudflared --format "{{.Names}}"   # then: docker rm -f <that name>
```

## 4. This machine: turn it on

```powershell
.\nas-feedops-sentinel\scripts\Enable-PublicAccess.ps1
```

It does the following, and can be run again safely:

- checks the three settings;
- lets Keycloak accept the public return address, alongside the local one;
- makes sure the edge's public listeners are up;
- switches public access on (below);
- runs the exposure probe.

## 5. On and off

Public access is a mode. `scripts/Set-PublicAccess.ps1 -Mode On|Off` switches three things together, because sign-in only works when they agree:

| | On | Off |
|---|---|---|
| Keycloak's sign-in address | `https://auth.<your-domain>` | `https://localhost:8081` |
| The dashboard's return address (`redirect_uri` in `.streamlit/secrets.toml`) | `https://sentinel.<your-domain>/oauth2callback` | `https://localhost:8443/oauth2callback` |
| The tunnel | running | stopped |
| Where everyone signs in, operators on this machine too | `https://sentinel.<your-domain>` | `https://localhost:8443` |

The launcher switches it on every launch:

```powershell
.\launch_nas_sentinel.ps1 -Public    # on
.\launch_nas_sentinel.ps1            # off: local only
```

The mode is kept in the files (Keycloak's address as `KEYCLOAK_BROWSER_URL` in `nas-feedops-sentinel/.env`, which the script writes and removes; don't edit that line). So any `docker compose` run starts Keycloak with the same address. The script restarts only what changed. Probe 05 fails if the tunnel and the mode disagree.

## 6. Logins

```powershell
.\nas-feedops-sentinel\scripts\Add-Guest.ps1 -Username jdoe -Email jdoe@example.com                       # read-only
.\nas-feedops-sentinel\scripts\Add-Guest.ps1 -Username jdoe -Email jdoe@example.com -Role watch_officer   # a demo operator
```

| `-Role` | Can do |
|---|---|
| `none` (the default) | see everything, decide nothing |
| `auditor` | the same, as a named auditor |
| `watch_officer` | approve, reject, monitor |
| `supervisor` | those, and override the AI's recommendation or severity |

- **What a login gets:** exactly that role, and a temporary password shown once. Give it to them privately, not in the same message as the link. Keycloak makes them choose their own at first sign-in (12+ characters). Running it again with another role replaces the role.
- **Every login expires.** `-Days` (default 30, at most 365) sets its last day, in UTC. After that day, the dashboard shows "your access ended" instead of the console, on every page. The next launch also turns the login off at Keycloak (`scripts/Disable-ExpiredLogins.ps1`). Running `Add-Guest.ps1` again renews it.
- **Decisions are real.** Each one is signed with the person's name in the audit trail and acts on live data. Give each person their own login, never a shared one.
- **Turning a login off:** `-Disable` does it and ends their sessions immediately.
- **Your own operator login** (you choose the password) is still `scripts/Setup-Keycloak.ps1`. It keeps the public return address and doesn't expire.

## 7. Who has looked

```powershell
.\nas-feedops-sentinel\scripts\Get-SignIns.ps1                         # the last 7 days
.\nas-feedops-sentinel\scripts\Get-SignIns.ps1 -Days 30 -Username jdoe
```

The report lists sign-ins, failed attempts (wrong password, locked out, turned off, an unregistered return address) and sign-outs, with the visitor's address as Cloudflare saw it, then a summary. Keycloak keeps these events, and every administrator change (a login created, a role changed, a login turned off), for 90 days. The admin changes are in the admin console, under Events → Admin events.

Before anyone signs in, the sign-in page says the site is for invited users only and that sign-ins and decisions are recorded. Both public hostnames tell search engines not to index them (`X-Robots-Tag: noindex, nofollow`), and allow no camera, microphone, location, payment or USB access (`Permissions-Policy`).

## Adding Cloudflare Access later (optional)

Access is a second gate: only listed emails even see the sign-in page, and each visit needs a code sent to that email. To add it: Zero Trust → Access → Applications → Self-hosted. Use both hostnames, identity provider **One-time PIN**, and an **Allow** policy that includes the guests' emails. Nothing on this machine changes.

## Checking it

```sh
scripts/smoke/05_public_exposure_probe.sh      # what the tunnel can reach, and what it cannot
scripts/smoke/run_phase6_smoke_suite.sh        # the full suite
```
