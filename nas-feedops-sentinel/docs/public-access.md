# Public access (Cloudflare Tunnel)

How to let invited guests use the dashboard from the internet, without opening any port on this machine or its router. Guests get **read-only** accounts: they can see everything the dashboard shows, and decide nothing.

**Before you invite anyone:** guests see live FAA SWIM data. Check that your SWIFT/SCDS data agreement allows showing it to people outside your organisation.

## What is and is not reachable

```
browser ─HTTPS─> Cloudflare (WAF rules) ─tunnel─> nas-cloudflared ─> nas-edge:8450 ─> dashboard
                                                                  └> nas-edge:8451 ─> Keycloak sign-in pages only
```

| Reachable from the internet | Never reachable from the internet |
|---|---|
| The dashboard, at `https://sentinel.<your-domain>` | Keycloak's admin console and `master` realm |
| Keycloak's sign-in pages for the `nas-sentinel` realm, at `https://auth.<your-domain>` | The account console, Keycloak's metrics and health endpoints |
| | Grafana, Prometheus, the Temporal UI, the API, Vault, SPIRE, Postgres |

The tunnel only makes outbound connections from `nas-cloudflared`, and publishes no port. Its two destinations are listeners on the edge proxy that are not published on this machine either (`docker/edge/nginx.conf`, 8450 and 8451). Those listeners decide which paths exist at all: a mistaken rule in Cloudflare can't expose anything else. `scripts/smoke/05_public_exposure_probe.sh` proves that on the live stack.

**Cost:** Cloudflare Tunnel and the WAF rules below are free. Cloudflare Access (an email one-time-code gate in front of the site) is optional. It's free up to 50 users, with billing above that.

## 1. Cloudflare: the tunnel routes

Zero Trust → Networks → **Tunnels** → your tunnel → **Routes** → **Add route** → **Published application**, twice. Older dashboards call these *Public Hostname* routes. Don't add DNS records by hand: saving a route creates its DNS record (a proxied CNAME to `<tunnel-id>.cfargotunnel.com`). Networking → **Routes**, and the *Tunnel Hostname* / *Tunnel CIDR* choices, are private routes for WARP clients: not these.

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
- points the dashboard's sign-in return address at the public URL, asking before it changes that one line of `.streamlit/secrets.toml`;
- lets Keycloak accept it;
- restarts Keycloak, the edge and the dashboard;
- starts the tunnel;
- runs the exposure probe.

From then on, every launch that should be public:

```powershell
.\launch_nas_sentinel.ps1 -Public
```

Without `-Public`, the launcher stops the tunnel: the site is public only when you ask for it.

## 5. Guests

```powershell
.\nas-feedops-sentinel\scripts\Add-Guest.ps1 -Username jdoe -Email jdoe@example.com
```

- **What a guest gets:** a read-only account and a temporary password, shown once. Give it to them privately, not in the same message as the link. Keycloak makes them choose their own at first sign-in (12+ characters).
- **Turning a guest off:** `-Disable` does it and ends their sessions immediately.
- **Operators** (people who decide) are still created with `scripts/Setup-Keycloak.ps1`.

## Adding Cloudflare Access later (optional)

Access is a second gate: only listed emails even see the sign-in page, and each visit needs a code sent to that email. To add it: Zero Trust → Access → Applications → Self-hosted. Use both hostnames, identity provider **One-time PIN**, and an **Allow** policy that includes the guests' emails. Nothing on this machine changes.

## Checking it

```sh
scripts/smoke/05_public_exposure_probe.sh      # what the tunnel can reach, and what it cannot
scripts/smoke/run_phase6_smoke_suite.sh        # the full suite
```
