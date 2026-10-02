# Runbooks

What to do when something needs attention:
- one runbook per alert rule in `monitoring/alert-rules.yml` (a test holds it
  to that), which Prometheus shows at https://localhost:8447/alerts and
  Alertmanager posts to Slack, naming the runbook in each message;
- then the recurring procedures.

How people use the system is in `docs/playbook.md`. Deployment and
configuration are in `docs/operations.md`.

Conventions: `docker compose` commands run in `nas-feedops-sentinel/`.
Logs are JSON; search them for the event names given.

## Alert runbooks

### CriticalAlertsUnstaged (critical)

A CRITICAL alert has no staged action. This is the headline invariant: every
CRITICAL alert must reach a person.
1. `docker logs nas-sentinel --since 30m | grep -E "reconcile_failed|stage"`
2. Run one reconcile pass by hand:
   `docker exec nas-sentinel python -m src.reconcile`. Each unstaged alert
   under 60 minutes old is staged PENDING; older ones are recorded EXPIRED.
3. If staging itself fails, check the database: `docker logs nas-pgbouncer`,
   then the migrate job's last run.
4. Tell the watch officer which alerts were late. An EXPIRED action records
   that an alert was never reviewed in time; it does not hide it.

### HumanReviewQueueStale (warning)

The oldest PENDING action has waited over an hour.
1. Check the dashboard's operator sidebar: is anyone signed in with
   `watch_officer` or `supervisor`?
2. Check sign-in works (see "Sign-in fails").
3. Hand over to the on-shift supervisor. Sentinel never decides a stale
   action on its own.

### AirportFeedDegraded (warning)

An airport's feed has been STALE or worse for 10 minutes.
1. On Feed Health, check the matrix for that airport. Is it one service or
   all of them?
2. All services: the Jumpstart kit. `data/raw/swim_live/log` should keep
   growing; check the kit's console and the SWIM subscription.
3. One service: check `data/failed/` for quarantined files of that service,
   and the ingest log on Ingest & Replay.

### NoEventsIngested (warning)

Nothing ingested in 30 minutes.
1. Is the kit running and writing to `data/raw/swim_live/log`?
2. Is `nas-sentinel` running and watching that folder?
   `docker logs nas-sentinel --since 30m`
3. `data/raw/swim_live/log/.inflight/` should be empty or nearly so. Claims
   older than 4 minutes (a crashed scanner's) are released automatically; a
   failed scan releases its own at once.

### LLMErrorRateHigh (warning)

Over half of model calls fail (the long-standing `nas_llm_calls_total`
series). The AI layer is falling back to rules. This is safe, but the advice is gone.
1. `docker logs nas-litellm --since 30m`: provider keys, quota (429) and
   model availability.
2. Break down by outcome on Agent Evaluation, under Reliability → outcome
   classes: `quota_exhausted`, `rate_limited`, `auth_error`, `timeout`,
   `provider_error`.
3. Quota: there is no fallback provider. Wait for the quota to reset, raise it
   with Anthropic, or raise the budget (`LITELLM_APP_BUDGET_USD`). Meanwhile
   every alert is still staged and the Critic reports itself unavailable.

### ModelCircuitOpen (warning)

The gateway failed `LLM_BREAKER_THRESHOLD` calls in a row, so calls to that
model are refused without being sent and the Critic says "unavailable" at
once (`src/llm_guard.py`). Alerts are still staged for a person; only the
advisory is missing. One probe call goes through every
`LLM_BREAKER_COOLDOWN_SECONDS`, and the circuit closes when it is answered.
1. `docker logs nas-litellm --since 30m`: provider keys, quota and availability
   (the same checks as LLMErrorRateHigh).
2. The worker log names the moment it opened (`llm_circuit_opened`) and closed
   (`llm_circuit_closed`).

### LLMPassBudgetExhausted (warning)

One pipeline pass spent `LLM_TOKEN_BUDGET_PER_PASS` tokens across the Critic
and the investigations; later calls in that pass were refused and fell back to
rules. Staging is unaffected. Search the logs for `llm_pass_budget_exhausted`
for the pass. Raise the cap only if the burst was legitimate.

### AgentBlockedFromDecidingOrWriting (critical)

An agent tried to decide, write or stage. The tool-use policy refused it and
nothing happened, but it should never be tried at all.
1. `docker logs <service> | grep tool_call_refused_by_policy` gives the
   agent (`role`), the tool, and the rule (`agents-never-decide-or-write`
   or `escalation-belongs-to-the-rules`).
2. Find the code path. Something is calling `stage_critical_alert` or
   `decide_staged_action` inside an agent's `acting_as()` context
   (`src/agent_tools.py`). That is a code defect: open an issue and roll
   back the change that introduced it.
3. Do not work around it by granting the tool in `policies/tool-use.yaml`.
   The rule exists so that no agent can reach the human gate.

### ProtectedValueBlockedFromPrompt (critical)

A model call was refused because a value the caller had read from its own
records, and must not send, was still in the finished prompt. Nothing was
sent and the agent fell back to its rules, but the redaction missed it.
1. The alert names the role (`agt_agent_id`). Search the logs for
   `model_call_refused_by_policy` with `protected_values_found` above zero:
   it says how many values, never which. Neither the prompt nor the values
   are logged.
2. Find where the identifier entered the prompt. The usual cause is a free-text
   field (a description, a note, an error message) carrying an identifier in a
   shape `src/redaction.py` has no pattern for, such as a registration written
   `G-ABCD`. Reproduce it from the record the agent was reading.
3. Fix at the source: stop passing that field, or project it away
   (`pseudonymize_structured`), or add the shape to `src/redaction.py` with a
   case in `tests/test_outbound_leak_check.py`. Do not loosen the rule in
   `policies/model-use.yaml`: the check is the control that caught it.
4. Expect more fallbacks to deterministic briefs until it is fixed; that is
   the safe direction, and each one is recorded as a refused call.

### ModelCallsRefusedByPolicy (warning)

The model-use policy refused model calls. Nothing was sent, and the agent
fell back to its rules.
1. The alert names the role (`agt_agent_id`) and rule (`agent_policy_name`).
   Search the logs for `model_call_refused_by_policy`.
2. By rule:
   - **`no-credentials-in-prompts`**: a credential pattern was in a prompt.
     Find its source; the prompt itself is never logged. Treat the
     credential as exposed to the process and rotate it.
   - **`gateway-aliases-only`**: a setting names a model outside the
     gateway aliases. Fix `LLM_MODEL`, `CRITIC_MODEL` or
     `INVESTIGATION_MODEL`.
   - **`prompt-size-ceiling`** or **`output-budget-ceiling`**: an input grew
     past its limit. Check what feeds that agent's context.
   - **no rule, "(default)"**: an unregistered role, or a policy that did
     not load. Check for `policy_unavailable` in the logs (see "Policy file
     refused").

### ModelCallErrorRateHigh (warning)

Over a quarter of model calls end in an error class (GenAI
`error.type`). Grafana's Agents row breaks it down by provider and class. The
steps are as for LLMErrorRateHigh. `invalid_output` means the model's replies
do not parse. That is a prompt or schema problem to fix, not a quota.

### ModelCallLatencyHigh (warning)

p95 model call time is over 30 s, the readiness objective.
1. Grafana → Agents → "Model call p95 by provider": is it one provider?
2. Check `LLM_TIMEOUT_SECONDS` and the gateway's routing.
3. Investigations over their SLA show as Latency gate FAIL on Agent
   Evaluation.

### AgentFallbackRateHigh (warning)

Over 20% of agent invocations fall back to rules. Investigations are mostly
rules-only.
1. Agent Evaluation → Reliability → Agents: which agent?
2. Its fallback reason is in the investigation's step log (Review page →
   investigation → steps), for example "model call failed", "model
   conclusion not one of …", or "model produced no claim grounded in the
   evidence".
3. "No claim grounded" repeatedly means the model is inventing evidence
   ids. Unsupported claims will be red too. Review the prompt or the model.

### ScrapeTargetDown (critical)

Prometheus cannot scrape a service. `docker ps` and the service's logs. The
agent metrics (`gen_ai_*`, `agent_*`) are on the same `/metrics` endpoint, so
they stop too.

### SwimBacklogNearJanitorAge (warning)

The oldest SWIM message waiting to be ingested is over 3 minutes old. The kit
folder's janitors delete by age (default 5 minutes) whether or not a message
was ingested, so after that it is gone.
1. Is `nas-sentinel` running and keeping up?
   `docker logs nas-sentinel --since 10m`. The watcher survives a failing scan
   and backs off, so look for `swim_watch_scan_failures_total` rising.
2. `nas_swim_folder_backlog_files` says how many are waiting. A backlog that
   only grows means ingest is slower than the feed; check the database
   (`docker logs nas-pgbouncer`).
3. If it cannot be fixed in time, raise the janitor's age (see "The FAA SWIM
   Jumpstart kit" in `docs/operations.md`) so nothing is dropped meanwhile.

### SwimFilesDroppedUnprocessed (warning)

The janitor deleted messages that were never ingested: the ingest fell behind
its age. Those records are gone; the broker was already told they were handled.
1. Work out when: `nas_swim_files_dropped_unprocessed_total` and the ingest log
   on Ingest & Replay.
2. Fix why ingest fell behind, as for `SwimBacklogNearJanitorAge`, and raise the
   janitor's age if a backlog must be survivable.
3. Tell the watch officer which window has a gap. Feed Health and the alert
   log for that window are incomplete, not clear.

### Watchdog (none)

Not a fault. It fires all the time so you can tell the alerting path is alive:
Alertmanager holds it and sends nothing.
- Present in Alertmanager (https://localhost:8448) = Prometheus is evaluating
  rules and handing them over.
- Missing there = the alerting path is broken; see `AlertsNotReachingAlertmanager`
  and "Alerts are not reaching Slack".

### AlertNotificationsFailing (warning)

Alertmanager has alerts to send and Slack is not accepting them. This one
cannot reach Slack either: it shows at https://localhost:8447/alerts and in
Alertmanager.
- Go to "Alerts are not reaching Slack".

### AlertsNotReachingAlertmanager (critical)

Prometheus fired something and could not hand it over. Nothing is paged while
this is true.
1. `docker ps`: is `nas-alertmanager` up and healthy?
2. `docker logs nas-prometheus --since 15m | grep -i alertmanager`
3. `docker compose up -d alertmanager` starts it again. Silences and
   notification state are in the `alertmanager-data` volume and survive.

## Procedures

### Alerts are not reaching Slack

1. Open Alertmanager (https://localhost:8448). Are alerts listed? If `Watchdog`
   is not, see `AlertsNotReachingAlertmanager`.
2. `docker logs nas-alertmanager --since 30m`. "SLACK_WEBHOOK_URL is not set"
   at the top means no destination is configured. `Notify for alerts failed`
   names the reason (a revoked webhook answers 403 or 404).
3. Put the webhook URL in `instraclustr/.env` (or `nas-feedops-sentinel/.env`)
   as `SLACK_WEBHOOK_URL=`. Create it in the Slack app: Incoming Webhooks,
   activate, Add New Webhook to Workspace, pick the channel. Then
   `docker compose up -d alertmanager` (the variable is read at start). Never
   paste the URL into a chat, an issue or a commit: anyone who has it can post
   to the channel.
4. Send a test alert through the real route:
   `docker exec nas-alertmanager amtool --alertmanager.url=http://localhost:9093 alert add alertname=TestAlert severity=warning --annotation=summary=TestAlert`.
   It arrives after about 30 seconds (10 seconds for `severity=critical`), and
   a "RESOLVED" message follows after 5 minutes. Keep annotation values free of
   spaces: `amtool` warns about unquoted ones, and quoting differs between
   PowerShell and sh. To check delivery without the channel, read
   `alertmanager_notifications_total{integration="slack"}` (and the
   `..._failed_total` series) from `docker exec nas-alertmanager wget -q -O - http://localhost:9093/metrics`.
5. To mute a known noisy alert while you fix it, use Alertmanager's Silences
   (https://localhost:8448/#/silences), with an expiry.

### Readiness says NOT READY or AT RISK

1. Run `docker exec nas-api python -m src.readiness` for the reasons, or
   open Agent Evaluation.
2. For each missed dimension:
   - Its *Source* and description say what was measured.
   - Its detail line says how far off it is and how much evidence there was.
3. An integrity failure ("an investigation record failed its integrity
   check") outranks everything:
   - a sealed record no longer matches its hash;
   - preserve the database and investigate before anything else.
4. Deployment pipelines use `python -m src.readiness --gate`. It exits 0
   only when READY.

### Change the model provider

1. Edit `instraclustr/litellm-config.yaml`: point the `primary-model` alias
   at the new provider (for example `anthropic/claude-…`, `openai/gpt-…`,
   `gemini/…`). The application does not change; it only knows aliases.
   Haiku is the only approved model: also change the allow-list in
   `instraclustr/tests/test_model_provider.py`, which is the review gate.
2. Restart the gateway: `docker compose restart litellm` in `instraclustr/`.
3. On Agent Evaluation, the new provider appears under Reliability →
   Providers, and its model under Cost → By served model. Every metric uses
   the same definitions (OpenTelemetry GenAI conventions), so the before and
   after compare directly.
4. Let readiness reach a verdict on the new provider's own evidence before
   relying on it.

### Change a policy, objective or gate limit

- `policies/model-use.yaml` sets which roles may call a model, and with
  what.
- `policies/tool-use.yaml` sets which tools each agent may use.
- `policies/model-slos.yaml` sets the readiness targets and the
  per-investigation gate limits.

Each is a reviewed code change. `tests/test_governance.py` and
`tests/test_tool_boundary.py` pin every policy decision and must be updated
with it. A policy that fails to load refuses every call; it never allows
everything.

### Policy file refused (`policy_unavailable`)

Every model or tool call is being refused because a policy file did not load.
The log line gives the reason:
- a missing file;
- YAML that is not a mapping;
- an unknown key (for example `rule:` for `rules:`);
- `defaults.action` not `deny`;
- `tool_allowlist` set, which the toolkit's evaluator ignores.

Fix the file, then restart the service. Policies are cached per process.

### Sign-in fails

1. The sign-in screen names a configuration problem it can see, such as an
   `http://` identity URL in `.streamlit/secrets.toml`.
2. Keycloak's page says "Invalid parameter: redirect_uri": the client does
   not list `https://localhost:8443/oauth2callback`. Run
   `scripts/keycloak-rbac.sh` (Setup-Keycloak.ps1 does), which brings a live
   realm to the client settings in `keycloak/realm-nas-sentinel.json`.
3. The browser comes back to the sign-in screen with no error: the code
   exchange failed on the server. `docker logs nas-dashboard` shows why;
   usually the certificate (`[auth.keycloak.client_kwargs] verify` must be
   `/run/tls/edge-ca.crt`) or a `client_secret` that no longer matches the
   realm.
4. The browser warns about the certificate: trust `tls/edge-ca.crt` once in
   the Windows certificate store.
5. `docker ps` shows whether `nas-keycloak` is healthy (its health check runs
   over TLS). `scripts/Collect-Diagnostics.ps1` fetches the discovery
   document from inside the dashboard container, verified the same way.

### Upgrade from HTTP Keycloak

1. Update `.streamlit/secrets.toml` to match `secrets.toml.example`:
   - `redirect_uri = "https://localhost:8443/oauth2callback"`
   - `server_metadata_url = "https://keycloak:8443/realms/nas-sentinel/.well-known/openid-configuration"`
   - `[auth.keycloak.client_kwargs]` with `verify = "/run/tls/edge-ca.crt"`
2. Bring the live realm's client up to date: `scripts/keycloak-rbac.sh`.
3. Recreate the services:
   `docker compose up -d --force-recreate edge-certs keycloak dashboard`
   - `edge-certs` issues Keycloak's certificate from the existing local
     authority, so nothing needs trusting again.
4. Admin console: https://localhost:8081/admin

### Certificates

`edge-certs` renews the edge and Keycloak certificates when they are within
30 days of expiry, on every start. The local authority (`tls/edge-ca.crt`)
lasts 10 years. To move to your own CA:
1. replace `tls/edge/edge.crt` / `edge.key` and `tls/keycloak/keycloak.crt` /
   `keycloak.key` (Keycloak's needs `keycloak` and `localhost` in its names);
2. put your CA in `tls/edge-ca.crt`; `edge-certs` copies it to
   `tls/keycloak/` and `tls/trust/`.

A `tls/` directory from before the per-container layout (certificates beside
`edge-ca.crt`, or empty directories named `keycloak.crt` / `keycloak.key`
that Docker created) is cleared by `edge-certs` on its next run; the
authority is kept, so nothing needs trusting again.

### Send telemetry to a collector

1. Set `OTEL_EXPORTER_OTLP_ENDPOINT` in `.env` (http/protobuf, for example
   `http://otel-collector:4318`), plus `OTEL_EXPORTER_OTLP_HEADERS` if the
   collector needs them.
2. Recreate the application services.
3. Traces (`invoke_workflow` / `invoke_agent` / `chat` / `execute_tool`),
   metrics, and AGT's audit log records all go there. `/metrics` keeps
   working.

### Upgrade the Agent Governance Toolkit

1. Bump `agent-os-kernel` / `agent-sre` in `requirements.txt` and regenerate
   the locks (the commands are at the top of that file).
2. Run the suite. In `tests/test_agt_known_bugs.py`, any strict xfail that
   now passes means AGT fixed that bug, and the suite fails on purpose.
   - Remove the matching AGT-compatible calculation, which makes the metric
     AGT-native again; `docs/agent-metrics.md` names it.
   - Delete the test's xfail marker.
3. Re-read `docs/agent-metrics.md` and move newly correct metrics to AGT.
