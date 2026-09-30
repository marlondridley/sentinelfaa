# PCT TAIS FeedOps Sentinel

[![Sentinel CI](https://github.com/marlondridley/sentinelfaa/actions/workflows/pyapp-ci.yml/badge.svg?branch=main)](https://github.com/marlondridley/sentinelfaa/actions/workflows/pyapp-ci.yml)

**Feed-trust monitoring and human-in-the-loop escalation for live FAA SWIM data across Potomac TRACON (PCT): KDCA, KIAD, KBWI and KADW.**

Sentinel subscribes to the FAA's terminal SWIM feeds, checks continuously whether that data is fresh, well-formed and complete, and turns anything critical into a staged action that a named, authorized person must decide. Language models explain and argue the other side; rules and people decide.

> **Can we trust the aircraft-movement data SWIM is publishing for PCT right now — and if not, who needs to look at what?**

That question sets every design choice below: what to ingest, when to call a model, when to escalate, and what gets written down.

- [Boundaries](#boundaries) — the contract the rest of the repository serves
- [How it works](#how-it-works) · [The stack](#the-stack) · [Run it](#run-it) · [Prove it](#prove-it)
- [Five-minute walkthrough](nas-feedops-sentinel/docs/walkthrough.md) · [Operations guide](nas-feedops-sentinel/docs/operations.md) · [Operator playbook](nas-feedops-sentinel/docs/playbook.md) · [Runbooks](nas-feedops-sentinel/docs/runbooks.md)
- [How AI is governed](nas-feedops-sentinel/docs/agent-governance.md): eight boundaries, the Sentinel ↔ AGT crosswalk, custom vs off the shelf · [Where every agent metric comes from](nas-feedops-sentinel/docs/agent-metrics.md)

---

## Boundaries

**What it watches.** Sentinel is a *downstream consumer* of SWIM, the FAA's publication of National Airspace System data to outside users, received through the SCDS Jumpstart Kit. It can tell you whether that published picture is fresh, well-formed and complete, service by service and airport by airport. It cannot see a controller's STARS display and does not claim to: a gap in SWIM is evidence worth investigating, not proof of what is on a scope.

**What a model may do.** Explain, summarize, and challenge. Every model call is budgeted per pass, time-limited, redacted first (callsigns, beacon codes, positions and raw payloads are stripped from prompts), and routed through one gateway. A model outage degrades to deterministic rules, and the record says so.

**What a model may not do.** Stage, approve, reject or execute anything. Every CRITICAL alert is staged for human review by rules, whether the model answers, fails, returns garbage or is not configured, and no code path lets model output change an action's status.

**How AI is governed.** The Microsoft Agent Governance Toolkit (AGT) decides every model call (`policies/model-use.yaml`) and every tool an agent uses (`policies/tool-use.yaml`) before it happens: deny by default, fail closed, every decision in AGT's audit log. No agent can decide, write or stage, whatever credentials it presents; the human gate stays at escalation. Execution is carried as OpenTelemetry (GenAI conventions, one definition for every model provider), and AGT computes the agent metrics wherever it computes them correctly. Agents never grade themselves: quality is judged from the grounding gate, the operator's recorded decision, the gateway's usage and cost, and the sealed record. [docs/agent-governance.md](nas-feedops-sentinel/docs/agent-governance.md) walks through it boundary by boundary.

**Who decides.** A signed-in operator with a Keycloak realm role: `watch_officer` (approve, reject, continue monitoring), `supervisor` (also override), `auditor` (read only). Buttons an operator may not use are disabled with the reason, and the execution gate re-checks the role before it writes anything.

**What gets written down.** Every status change goes to `action_audit_log` with the operator's identity, roles and sign-in method. The database keeps that table append-only: a trigger refuses UPDATE and DELETE, and the application's own database role can only read and insert. Every staged action also gets an investigation record whose claims must cite collected evidence, sealed with SHA-256 and re-verified when displayed.

**What it holds back.**
- Messages the FAA flags `blockAtFAA=true` are withheld: nothing from them is stored, shown or sent to a model.
- Beacon codes from TAIS flight plans that are not yet active are neither stored nor shown, per the FAA's STDDS beacon-code notice.
- Replayed, synthetic and API data is tagged `data_origin` at ingest, and every page warns while non-live data is present, so a demo is never read as the live picture.

**What it will never do.** Page anyone, contact an FAA system, or issue anything resembling an ATC instruction. "Approve escalation" records a human decision on an internal ticket; connecting that ticket to ServiceNow, Remedy or Jira is deliberately out of scope.

---

## How it works

```
FAA SWIM (Solace) ─► SCDS Jumpstart Kit, on the host ─► data/raw/swim_live/log/
                                                                 │
sentinel (watcher) ◄─────────────────────────────────────────────┘
  claim the file (atomic rename) ─► parse JMS headers + JSON ─► honor blockAtFAA
  ─► extract TAIS · TDES/TDLS · APDS · SMES ─► stable ids, data_origin=live
                                     │
src/pipeline.py (one pipeline for every entry point)
  validate ─► save events ─► alert rules ─► save alerts
  ─► Critic on CRITICAL alerts (bounded, advisory)
  ─► stage EVERY CRITICAL alert: PENDING action + DRAFT ticket + audit row
  ─► investigate each staged action: collect evidence ─► Feed Diagnostician ‖
     Correlation Analyst ─► Red-Team Critic ─► Watch Officer brief ─► seal
                                     │
dashboard review queue ─► role-checked decision ─► action_audit_log (append-only)

reconciler, every 60 s: stage any CRITICAL alert that slipped through, or
record it EXPIRED if it is older than the staging window
```

The live watcher, a Temporal workflow started through the API, the dashboard's sample and replay buttons, and the CLI all run the same `src/pipeline.py`. Events and alerts carry identities derived from the SWIM message itself, so a re-dropped file or a retried workflow updates rows instead of duplicating them.

**What the live subscription carries:** TAIS (terminal automation flight plans and tracks from STARS), TDES (tower departure events, including TDLS departure clearances), APDS (runway visual range) and SMES (ASDE-X surface movement and safety-logic hold bars). All ten messages of a real 2026-08-27 capture, committed under `nas-feedops-sentinel/tests/fixtures/swim_kit/`, ingest with none quarantined.

**Alert rules** (`src/alert_engine.py`; thresholds and where each comes from are documented in `src/config.py`):

| Rule | Severity | Fires when |
|---|---|---|
| `ALERT_FEED_DISCONNECTED` | CRITICAL | a message reports its feed disconnected |
| `ALERT_LOW_RVR` | CRITICAL | runway visual range below 1,200 ft |
| `ALERT_ISMC_LINK_DEGRADED` | CRITICAL | ISMC reports a degraded or unavailable link |
| `ALERT_FEED_STALE` | WARNING | a service has sent nothing for 300 s |
| `ALERT_HIGH_LATENCY` | WARNING | delivery lag above 30 s |
| `ALERT_SCHEMA_VALIDATION_FAILED` | WARNING | an event fails schema or business rules |
| `ALERT_SURFACE_EVENT_SPIKE` | WARNING | 10 or more surface events at one airport within 60 s |

**Where the data lives:** PostgreSQL, through PgBouncer: `nas_events`, `alert_events`, `critic_results`, `planner_results`, `staged_actions`, `operational_tickets`, `action_audit_log`, `investigations`, `watch_officer_briefs`, `ingest_log`.

**The operator console** (Streamlit) opens on a GREEN/AMBER/RED verdict computed by fixed rules from schema conformance, ingest lag and open CRITICAL alerts; every number on it is a link to the page that explains it.

| Question | Where it is answered |
|---|---|
| Is the PCT feed alive right now? | Overview verdict; Feed Health: ingest volume and delivery lag |
| Can we trust the current aircraft picture? | Overview verdict; Live Airspace: stale and no-data status |
| Which airport is driving the traffic load? | Live Airspace: per-airport counts |
| What phase of flight is most active? | Live Airspace: phase breakdown |
| Are tracks stale, missing, or abnormal? | Feed Health: schema conformance; Live Airspace: movement profile |
| Is this isolated or sector-wide? | Feed Health: service-by-facility matrix; Alert Log: spread across facilities |
| What should the operator do next? | AI Critic & Review: the staged action, its investigation, and the decision |

---

## The stack

| Stack | Service | Role |
|---|---|---|
| `instraclustr/` | `postgres`, `pgbouncer` | the database, pooled |
| | `vault` (+ `vault-unseal` job) | secrets; TLS only; unseal key split 2-of-3; no stored root token; policy as code (Terraform), applied in a root ceremony |
| | `spire-server`, `spire-agent` | workload identity: each application service gets its own short-lived X.509-SVID, chained to an offline root, and uses it to log in to Vault over mutual TLS |
| | `litellm` (+ `litellm-db-init`, `litellm-bootstrap` jobs) | the model gateway; the application holds a scoped, budgeted key |
| `nas-feedops-sentinel/` | `migrate` | one-shot schema owner job; everything else runs as a restricted role |
| | `sentinel` | the live SWIM folder watcher and the reconciler |
| | `api` | FastAPI: `/health`, `/v1/ready`, `/v1/ingest` |
| | `temporal`, `temporal-worker`, `temporal-ui` | ingest and pipeline workflows started through the API |
| | `dashboard` | the Streamlit operator console |
| | `keycloak` (+ its own Postgres) | operator identity and realm roles; serves HTTPS only |
| | `prometheus`, `grafana` | metrics and alert rules, including the GenAI and AGT agent metrics (OTLP export optional) |
| | `edge` | nginx: the only published entry point, HTTPS on 127.0.0.1 |

Every port is bound to `127.0.0.1`. How identities, secrets and certificates fit together is in [docs/operations.md](nas-feedops-sentinel/docs/operations.md). To let invited guests use the dashboard from the internet (a Cloudflare tunnel, no port opened, read-only guest accounts), see [docs/public-access.md](nas-feedops-sentinel/docs/public-access.md).

---

## Run it

Requires Windows with Docker Desktop and PowerShell. The live feed also needs Java 25 and a SWIFT-authorized SCDS subscription; everything else runs without one.

1. Copy `instraclustr/.env.example` to `instraclustr/.env` and `nas-feedops-sentinel/.env.example` to `nas-feedops-sentinel/.env`, and fill them in. Model provider keys and the Sentinel database's passwords (`POSTGRES_*`, `APP_DB_*`) belong in `instraclustr/.env`: the application gets its database identity only from Vault. The application stack's own service secrets (`API_KEY`, Grafana, Keycloak, Temporal) may live in either file; the launcher reads both, the application's winning. First time only: the certificate ceremony, Vault's workload auth and SPIRE's first boot, in the order in [instraclustr/README.md](instraclustr/README.md#first-time-setup).
2. From the repository root:
   ```powershell
   .\launch_nas_sentinel.ps1                # infrastructure, then the application
   .\launch_nas_sentinel.ps1 -WithLiveFeed  # also start the Jumpstart Kit
   ```
3. Create an operator (granted `watch_officer` unless `APP_ROLES` says otherwise):
   ```powershell
   .\nas-feedops-sentinel\scripts\Setup-Keycloak.ps1
   ```
4. Trust `nas-feedops-sentinel\tls\edge-ca.crt` once in the Windows certificate store, then sign in:

| URL | What |
|---|---|
| https://localhost:8443 | Dashboard |
| https://localhost:8444/health | API |
| https://localhost:8445 | Grafana |
| https://localhost:8446 | Temporal UI |
| https://localhost:8447/alerts | Prometheus and its alert rules |
| https://localhost:8448 | Alertmanager: what was sent to Slack, and silences |
| https://localhost:8081/admin | Keycloak admin (loopback only; Keycloak serves HTTPS only) |

---

## Prove it

One command runs the whole path against a throwaway database:

```bash
cd nas-feedops-sentinel
docker compose --profile proof run --rm proof
```

It ingests the ten real captured SWIM messages, injects a labelled synthetic cascade failure, checks that every CRITICAL alert is staged and investigated with a sealed, traceable record, has an `auditor` try to approve (refused, nothing written), has a `watch_officer` approve (recorded with their roles), and confirms the audit log refuses UPDATE and DELETE. It prints one line per check and exits non-zero if any fails; CI runs it on every push. Output from a run without a model:

```
== 1. Real FAA SWIM capture (2026-08-27, 10 messages)
  [PASS] all ten captured files ingested, none quarantined -- processed=10 failed=0 events=14
...
== 2. Injected fault (SYNTHETIC cascade-failure scenario)
  [PASS] every CRITICAL alert staged PENDING for human review -- 15 CRITICAL, 15 pending
...
  [PASS] every investigation is sealed and every claim traces to evidence
...
== 3. Authorization: an auditor may not decide
  [PASS] auditor approval refused -- approve requires role supervisor or watch_officer; e2e-proof-auditor (synthetic) has auditor.
  [PASS] nothing written by the refused attempt
...
== 5. The audit log is append-only
  [PASS] refused: UPDATE -- action_audit_log is append-only; UPDATE is not permitted on this table
  [PASS] refused: DELETE -- action_audit_log is append-only; DELETE is not permitted on this table

PROOF PASSED
```

---

## Tests and CI

The suite (about 490 tests at this writing, 18 of them strict xfails that document bugs in the Agent Governance Toolkit) lives in `nas-feedops-sentinel/tests/` and runs against a real Postgres. It refuses to touch any database whose name is not marked disposable; see [nas-feedops-sentinel/README.md](nas-feedops-sentinel/README.md) for how to run it.

[CI](.github/workflows/pyapp-ci.yml) runs on every push and pull request:

- `ruff` lint, then the full suite and the end-to-end proof in the test image against Postgres 16
- `pip-audit` over the hash-locked runtime dependencies; accepted exceptions must be listed in [cve-allowlist.md](nas-feedops-sentinel/cve-allowlist.md), and there are none
- both compose files render with the example environment files
- Trivy over the shipped runtime image; any HIGH or CRITICAL finding with a published fix fails the build

---

## Models

Every model call goes through one LiteLLM gateway (`instraclustr/litellm-config.yaml`). The application names aliases only (`primary-model`), and settings that name a provider route directly (`gemini/...`) are refused at startup, so changing providers is an edit to that file, not to the application. Today `primary-model` is DeepSeek with Gemini as fallback: a low-cost test configuration that can be pointed at Claude, OpenAI or Gemini the same way. Real FAA operational data needs an authorized endpoint, for example Claude on Bedrock GovCloud, Azure OpenAI Government, or an on-premises vLLM or NIM deployment.

---

## AI Governance and Evaluation

Sentinel separates mission execution from AI governance.

The deterministic mission pipeline remains responsible for ingesting SWIM data, validating messages, determining alert conditions, managing escalation state, and enforcing human-controlled execution gates.

Model-backed investigation roles operate through the common LLM client and are instrumented with provider-neutral telemetry. OpenTelemetry provides the common telemetry layer across model providers.

The Microsoft Agent Governance Toolkit (AGT) provides standardized governance, evaluation, and SRE primitives for model-backed agent execution. Sentinel does not assume that an AGT metric with a similar name automatically represents the corresponding operational metric. Each metric is validated against its semantic definition, lifecycle, population, aggregation, and installed AGT implementation before it is adopted.

Metrics therefore fall into three categories:

- **AGT-native:** AGT defines and correctly implements the required semantic.
- **AGT-compatible:** AGT defines the required semantic, but Sentinel temporarily supplies a validated compatibility calculation when the installed AGT version contains a known implementation defect.
- **Sentinel-specific:** the metric represents a mission or workflow outcome that AGT does not define.

AGT evaluates agent behavior; it does not provide the operational ground truth for that behavior. Agent outputs are evaluated against independently available evidence, deterministic results, or recorded human outcomes.

Agents remain advisory. They cannot approve an escalation, stage an operational action, or override a human decision. The execution gate remains the authority for role-checked operational decisions.

This separation allows Sentinel to standardize AI governance across different model providers without allowing the governance framework to redefine mission-specific operational semantics.

Every metric and gate check on the Agent Evaluation page names its category in its **Source** column. Each metric's category, and the AGT defect behind each AGT-compatible one, is in [docs/agent-metrics.md](nas-feedops-sentinel/docs/agent-metrics.md); how each boundary is enforced, and which test proves it, is in [docs/agent-governance.md](nas-feedops-sentinel/docs/agent-governance.md).

---

## Latency, Cost, Failure, Evaluation

Production agentic systems are judged less by model quality than by how they hold up against four operational constraints.

| | How the design answers it |
|---|---|
| **Latency** | Ingest, validation and alerting are deterministic and come first; the watcher polls every 2 s; a model runs only for CRITICAL alerts or when an operator asks the planner, and every call has a 90 s timeout. |
| **Cost** | No model call per message. The Critic sees at most `MAX_CRITIC_PER_PASS` (default 5) CRITICAL alerts per pass and investigations use the model for at most `MAX_LLM_INVESTIGATIONS_PER_PASS` (default 3); the rest run on rules. The gateway key has a budget and a rate limit, and every investigation is held to a cost, token and tool-call budget (AGT `BudgetTracker`). Spend is measured per call, investigation, alert and model. |
| **Failure** | Every step degrades to rules and records that it did. Unreadable files go to `data/failed/` with a reason, one bad record fails its own file and never the scan, unstaged CRITICAL alerts are reconciled every minute, and nothing auto-executes. |
| **Evaluation** | An evaluation layer measures every investigation from the record: decision quality, grounding, unsupported claims, accuracy against operator decisions, missed and false escalations, calibration, latency by stage, cost, reliability by agent and provider, and quality, safety, cost, latency and audit gates. A readiness verdict (`python -m src.readiness --gate`) turns it into a deployment gate. The dashboard shows it in three layers: five indicators on the Overview, one investigation's gates on the Review page, and everything on Agent Evaluation & Governance. Plus the test suite, a one-command end-to-end proof, and alert rules. |

---

## Known limits

- **RVR encoding.** APDS reports runway visual range as a coded value. The ×100-to-feet scale is corroborated against KBWI weather observations but not confirmed against the non-public APDS interface document; implausible readings are refused rather than stored. `ALERT_LOW_RVR` depends on it.
- **Transport.** JMS is the only authorized SCDS API, so a JVM (the Jumpstart Kit) is the edge. It writes one durable record per message and stops rather than acknowledge one it could not write; Python reads the folder through a small source interface. That is fine for PCT volumes on one host (a janitor keeps the folder scannable). A broker such as Kafka would be a second implementation of that interface, and is deferred until a second consumer or a second feed (TFMS, TBFM) appears. What the broker does after the JVM halts has not been observed against a real broker here.
- **Paging.** Prometheus alert rules are defined, but no Alertmanager is configured, so nothing pages anyone.
- **Decisions.** One approver per action; there is no four-eyes workflow.
- **Audit integrity.** The audit log is append-only in the database, but it is not hash-chained or signed.
- **Sign-in.** Operators sign in on Keycloak's own page (OIDC, authorization code with PKCE), so a second factor or a federated identity provider is a realm setting, not a code change. The certificates come from a local authority; a shared deployment needs an internal PKI.
- **Evaluation.** Model behaviour is measured from real runs, but the readiness targets are starting points, not baselines, and a quiet feed produces too little evidence to judge ("insufficient data" is the honest answer). Alert precision and detection latency are not yet measured against labelled replays.
- **AGT.** Thirteen bugs in agent-sre and agent-os-kernel 3.7.0 are worked around, each with a failing test ([docs/agent-metrics.md](nas-feedops-sentinel/docs/agent-metrics.md)); a draft upstream issue is in `docs/upstream/`. The metrics they affect are AGT-compatible: Sentinel computes them, including the error-budget verdict, until the installed AGT is fixed.
- **Telemetry.** OpenTelemetry export to a collector is configured, not bundled: no collector ships in the stack.

---

## Why I Built This

I built this because I wanted to do more than talk about FAA modernization, SWIM, STARS-adjacent data, automation, and AI governance in the abstract. I wanted a working system that shows how I think:

1. Start with the mission data.
2. Build the automation layer around it.
3. Make the data trustworthy.
4. Then put AI behind deterministic controls instead of letting the model become the control plane.

This connects directly to my work earlier in my career from **2009–2015**: on-prem data center and middleware automation. Validating connectivity across controlled environments. Testing message and data-feed interoperability. Checking refresh behavior. Monitoring queue and feed health. Producing evidence for ATO-related readiness.

PCT TAIS FeedOps Sentinel is the modern expression of that same operating pattern, using Python, Streamlit, Pydantic validation, LLM reasoning, human-in-the-loop gates, and cloud / AI deployment paths.

The goal was not to prove that an LLM can summarize aviation data. The goal was to prove I can design the system **around** the model: ingestion, normalization, validation, alerting, explainability, escalation, audit, and deployment flexibility.

---

## Technical Outcomes

> Built **PCT TAIS FeedOps Sentinel**, a Python system that monitors the trustworthiness of live FAA SWIM / STDDS data (TAIS, TDES, APDS, SMES) for PCT TRACON (KDCA, KIAD, KBWI, KADW). Implemented live broker ingest through the SCDS Jumpstart Kit with single-consumer file claims and idempotent event identities; Pydantic-validated normalization; deterministic alert and feed-health rules; FAA data-handling rules (blockAtFAA withholding, beacon codes released only for active flight plans); a four-specialist investigation workflow whose claims must cite evidence and whose records are sealed; a human-in-the-loop gate with Keycloak role-based authorization (HTTPS only) and a database-enforced append-only audit log; Microsoft Agent Governance Toolkit policies over every model call and every agent tool call, with agents barred from deciding; OpenTelemetry GenAI telemetry; an evaluation layer and readiness gate that measure the AI from the record rather than from its own account; and a Streamlit operator console. Designed around Latency, Cost, Failure and Evaluation constraints, with every model call budgeted, redacted and routed through a LiteLLM gateway under a scoped key, and secrets held in Vault behind per-service AppRoles. Backed by a test suite of about 490 tests and a one-command end-to-end proof, run in CI alongside a dependency CVE audit and a blocking container image scan.

---

## Repository layout

```
launch_nas_sentinel.ps1   one-command local launcher (Windows / PowerShell)
instraclustr/             infrastructure: Postgres, PgBouncer, Vault, LiteLLM
nas-feedops-sentinel/     the application
  src/                    ingest, pipeline, alert rules, gate, investigation, storage, API, Temporal
  dashboard/              the Streamlit console: Overview and six pages, including Agent Evaluation
  policies/               AGT model-use and tool-use policies; readiness objectives and gate limits
  tests/                  the pytest suite, with real SWIM captures in fixtures/swim_kit/
  scripts/                end-to-end proof, Keycloak setup, diagnostics
  docs/                   operations, playbook, runbooks, walkthrough, agent governance and metrics
  jumpstart/              the FAA SCDS Jumpstart Kit (L3Harris/swim-jumpstart), a live-run
                          script and one custom output class
  keycloak/  monitoring/  prompts/  docker/
reference/                Temporal's docker-compose quickstart, kept for reference
```
