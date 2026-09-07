# PCT TAIS FeedOps Sentinel

**Agentic AI operational intelligence for live FAA SWIM / STDDS TAIS data across PCT TRACON.**

This is not an aviation map. It is a **trust-and-escalation cockpit** for live aircraft movement data — built around the operational constraints a real federal program would impose, not around the LLM features that are easy to demo.

---

## One-Line Mission

> **Can PCT TRACON trust the current aircraft movement picture across KDCA, KIAD, KBWI, and KADW?**

That single question drives every design decision: what to ingest, when to validate, when to call an LLM, when to escalate, and when to hand control back to a human.

---

## Executive Summary

PCT TAIS FeedOps Sentinel ingests live FAA SWIM / STDDS messages through the **SCDS Jumpstart Kit**, normalizes TAIS flight-plan and track records, converts them into airport-level operational activity, checks feed freshness and data quality, and surfaces the result through a five-layer decision pipeline:

> **The banner announces the condition. The Watch Officer explains it. The ticket preserves it. The human review queue decides it. The audit trail proves it.**

| Layer | Role |
|---|---|
| **PCT LIVE banner** | Fast signal — *what needs attention right now?* |
| **Watch Officer Brief** | Structured explanation of posture, trust, and what changed |
| **Operational Ticket** | Durable internal escalation package linked to a staged action |
| **Human Review Queue** | Decision point — Approve Escalation / Continue Monitoring / Reject |
| **Decision Audit Trail** | Immutable proof of every state transition |

The LLM **explains and drafts**. Deterministic validators, alert rules, the human-in-the-loop gate, the ticket package, and the audit log **control what happens next**. The model is not the control plane.

---

## Why I Built This

I built this because I wanted to do more than talk about FAA modernization, SWIM, STARS-adjacent data, automation, and AI governance in the abstract. I wanted a working system that shows how I think:

1. Start with the mission data.
2. Build the automation layer around it.
3. Make the data trustworthy.
4. Then put AI behind deterministic controls instead of letting the model become the control plane.

This connects directly to my earlier DISA work from **2009–2015**: on-prem Defense data center and middleware automation. Validating connectivity across controlled environments. Testing message and data-feed interoperability. Checking refresh behavior. Monitoring queue and feed health. Producing evidence for ATO-related readiness.

PCT TAIS FeedOps Sentinel is the modern expression of that same operating pattern, using Python, Streamlit, Pydantic validation, LLM reasoning, human-in-the-loop gates, and cloud / AI deployment paths.

The goal was not to prove that an LLM can summarize aviation data. The goal was to prove I can design the system **around** the model: ingestion, normalization, validation, alerting, explainability, escalation, audit, and deployment flexibility.

---

## The LCFE Production Framework

Production-grade agentic systems are not judged by model quality. They are judged by how they hold up against four operational constraints: **Latency, Cost, Failure, Evaluation**. The architecture is built around those constraints.

| LCFE Constraint | Design Response |
|---|---|
| **Latency** | Deterministic ingest and validation first; LLM only used to summarize critical context |
| **Cost** | Avoid LLM calls on every message; sample / replay modes; provider abstraction |
| **Failure** | Degraded modes; deterministic Watch Officer fallback; HITL gate; no auto-execute |
| **Evaluation** | Pytest, replay regression, schema validation on LLM output, audit trail, room for online monitoring |

### L — Latency: can the system produce usable context fast enough?

The end-to-end path:

```
FAA SWIM message arrives at the Solace broker
        ↓  ~milliseconds (network)
Jumpstart Kit writes one JSON-wrapped JMS message file
        ↓  ~milliseconds (disk)
swim-folder scan picks up new files (capped at 500 newest per pass)
        ↓  ~30 s polling interval, sub-second work per file
Parser normalizes JMS headers + JSON body into NASEvent rows
        ↓
Deterministic validation + alert engine + feed-health classification
        ↓  sub-second
LLM reasoning (Critic / Watch Officer) — only on CRITICAL or staged-action paths
        ↓  ~1–3 s when invoked
Banner + dashboard + ticker update on next refresh
```

Deterministic processing happens first. AI is invoked only where reasoning over structured evidence adds genuine operational value — never on every raw message. Per-pass file and Critic caps prevent backlog bursts from pinning the pipeline.

### C — Cost: can the system avoid wasteful AI calls?

- Deterministic validators and alert rules execute before any LLM call
- The AI Critic runs only on CRITICAL alerts, and only `MAX_CRITIC_PER_PASS` (default 5) per ingest cycle
- The Watch Officer Brief is generated from already-summarized structured signals, not raw event streams
- `sample` and `replay` modes exercise the full pipeline without consuming live broker capacity or LLM credits
- The LLM client is provider-agnostic: Anthropic for demo quality, NVIDIA NIM for on-prem federal deployment, Gemini through an OpenAI-compatible endpoint for cost-effective production, Ollama for zero-cost local development
- No agent-loop recursion; the architecture is a directed pipeline, not a free-form ReAct loop

### F — Failure: what happens when feeds, tools, models, or parsers fail?

The system assumes failure and degrades cleanly:

| Failure mode | Behavior |
|---|---|
| Feed goes stale | `feed_health` flags airports as STALE / DEGRADED / UNAVAILABLE; trust assessment becomes DEGRADED or INCOMPLETE |
| Files fail parsing | Moved to `data/failed/`; ingest audit log records the error; the rest of the batch continues |
| LLM key missing | Critic and Planner return structured "unavailable" responses; Watch Officer falls back to deterministic brief |
| LLM call fails or returns malformed JSON | Watch Officer uses deterministic fallback; Critic returns a degraded result with reasoning explaining the failure |
| Broker connectivity drops | Sentinel polling continues against any local files; banner surfaces "no recent messages" |
| Airport data incomplete | Trust assessment becomes INCOMPLETE; dashboard reports affected airports explicitly rather than guessing |
| CRITICAL alert fires | Staged as PENDING in `staged_actions`; **never auto-executes**; only a human in the dashboard can Approve / Reject / Continue Monitoring |
| Internal ticket package | Kept in `operational_tickets` table; **not** auto-submitted to ServiceNow / Remedy / Jira — that integration is intentionally out of scope |

The dashboard always renders. The Streamlit page works without an LLM key. The HITL gate works without an LLM key.

### E — Evaluation: how do we prove it works before and after release?

- **Unit tests** (147 passing as of latest commit) cover validators, alert engine, feed health, execution gate, staged-action workflow, operational tickets, Watch Officer agent (UNKNOWN / NORMAL / ATTENTION / ACTION postures), banner severity priority, and Critic / Planner degradation paths
- **Replay mode** lets captured TAIS files act as a regression dataset against new code
- **Sample mode** synthesizes deterministic operational scenarios including cascade failures and clean states
- **Schema validation** on every LLM output — Watch Officer briefs and Critic verdicts that fail schema fall back to the deterministic path
- **Audit trail** in `action_audit_log` provides full post-event review
- **Future-room**: structured logging is JSON with correlation IDs; metrics module exports Prometheus gauges for HITL queue depth, LLM call latency, gate decisions, and per-airport feed health. Online monitoring for latency, failure rates, model failure rate, and data drift is the natural next step.

Run the full evaluation suite with:

```bash
pytest
```

---

## What the System Does

- Connects to the FAA Solace broker through the SWIFT-authorized **SCDS Jumpstart Kit**
- Parses the Kit's hybrid output: **JMS metadata headers + JSON TAIS payload body**, written as epoch-named files with no extension
- Normalizes TAIS flight-plan and track records into `NASEvent` rows: airport, runway, aircraft callsign, lat / lon, altitude, ground speed, vertical rate, flight phase
- Validates every event against schema and business rules
- Fires structured alerts for low RVR, ISMC degradation, feed staleness, latency spikes, validation failures, and surface-event surges
- Runs an **AI Critic** on every CRITICAL alert: pattern classification, confidence score, escalation recommendation
- Runs a **Watch Officer Agent** that produces a structured operational brief explaining posture, trust assessment, what changed, why it matters, and what a human should review
- Drafts an **internal Operational Ticket** linked to every staged action, carrying the Watch Officer narrative as the ticket body
- Holds CRITICAL actions in a **human-in-the-loop queue** — never auto-executes
- Records every state transition in an **immutable audit trail**
- Provides a natural-language **Operational Planner** for duty-manager-style questions
- Exposes Prometheus metrics for the full pipeline

---

## Mission Questions the Dashboard Answers

| Question | Dashboard section |
|---|---|
| 1. Is the PCT TAIS feed alive right now? | Latency + freshness panel |
| 2. Can we trust the current aircraft picture? | Airspace Map + Watch Officer Brief |
| 3. Which airport is driving the traffic load? | Per-Airport Activity |
| 4. What phase of flight is most active? | Phase counts (departures / arrivals / enroute / ground) |
| 5. Are tracks stale, missing, or abnormal? | Airport Feed Health + altitude / speed distributions |
| 6. Is this isolated or sector-wide? | Per-airport comparison |
| 7. What should the operator do next? | AI Critic + Watch Officer + Human Review Queue |

---

## Core Workflow

```
Live FAA SWIM / STDDS TAIS data
        ↓
Deterministic validation + alerting + feed-health classification
        ↓
AI Critic and Watch Officer reasoning (only when useful)
        ↓
Internal Operational Ticket / Escalation Package drafted (DRAFT)
        ↓
Human Review Queue surfaces the staged action + ticket
        ↓
Operator: Approve Escalation  |  Continue Monitoring  |  Reject
        ↓
SQLite persists the decision
        ↓
Audit Trail records the human transition
```

The LLM contributes language. Humans contribute decisions. The HITL gate enforces that separation.

---

## Dashboard Operating Model

> **The banner announces the condition. The Watch Officer explains it. The ticket preserves it. The human review queue decides it. The audit trail proves it.**

- **PCT LIVE banner** — color-coded fast signal (ACTION / ATTENTION / NORMAL / GOOD), click any headline to jump to the relevant section
- **🧠 Watch Officer Brief** — posture, trust assessment, what changed, why it matters, recommended human review, evidence
- **📦 Operational Ticket** — linked to a staged action; carries the Watch Officer narrative as the ticket body; status DRAFT → APPROVED_FOR_ESCALATION / REJECTED / MONITORING
- **🧑‍✈️ Human Review Queue** — three buttons (Approve Escalation / Continue Monitoring / Reject), decision note text field
- **🗂 Decision Audit Trail** — every transition with actor, note, timestamp

---

## Human-in-the-Loop Execution Gate

The system stages every CRITICAL alert as a PENDING `staged_action`. The action cannot execute until a human decides.

- The AI Critic may flag a CRITICAL alert as `escalate=true` and write rationale.
- The Watch Officer may draft the ticket narrative.
- **Neither has an API path that sets `status=APPROVED_FOR_ESCALATION`.**
- Only a human operator clicking in the dashboard (or in production, calling the approve API with attributed credentials) can move it.
- Every state transition writes to `action_audit_log`: previous_status, new_status, actor, note, timestamp.

**Important — this is a demo-safe gate, not a real FAA operational command channel.** No external system is contacted, no controller is paged, no ATC instruction is issued. The gate proves the architectural pattern. Production deployment would add the integration adapters; the data structures are designed to support them.

---

## Watch Officer Agent and Escalation Packages

The Watch Officer Agent is the **explanation layer** between the banner (signal) and the ticket (record). It runs deterministic signal detection first, then optionally enriches the language with an LLM.

The brief answers four questions in structured form:

- **What changed** — observable signals in the current window
- **Why it matters** — operational consequence in plain English
- **Recommended human review** — what the operator should look at next
- **Evidence** — the underlying numbers and identifiers

That brief becomes the body of the internal **Operational Ticket Package**, persisted in SQLite at `data/feedops.db`. The package carries:

- The original alert and staged action
- Watch Officer posture (GOOD / NORMAL / ATTENTION / ACTION / UNKNOWN)
- Trust assessment (TRUSTWORTHY / DEGRADED / INCOMPLETE / UNKNOWN)
- AI Critic rationale and confidence (when available)
- A unique idempotency key so duplicate staging produces one ticket, not many

When the ticket is approved, rejected, or marked MONITORING, the decision is appended to the same audit log that records staged-action transitions. The integration adapter to a real ticketing system (ServiceNow, Remedy, Jira) is deliberately out of scope. The internal package is the durable artifact a future adapter would read.

---

## System Architecture

```
FAA SWIM / STDDS TAIS subscription
        │
        ▼
FAA Solace broker (ems1.swim.faa.gov:55443)
        │
        │ tcps / JMS authenticated session
        ▼
SCDS Jumpstart Kit (Java consumer provided through SWIFT)
        │
        │ MessageFileOutput, json:true → epoch-named files
        ▼
jumpstart-latest/log/    ──[NTFS junction]──>   nas-feedops-sentinel/data/raw/swim_live/
        │
        │ scanned by swim-folder mode (capped at 500 newest per pass)
        ▼
src/swim_file_ingest.py — parses JMS headers + JSON body
        ▼
TAIS normalization — airport, runway, callsign, lat/lon, altitude, GS, vVert, phase
        ▼
src/validators.py — schema and business-rule checks
        ▼
src/alert_engine.py — structured alert rules
        ▼
src/critic_agent.py — LLM-based pattern + escalation reasoning (CRITICAL only)
        ▼
src/execution_gate.py — stages action; calls Watch Officer; creates Operational Ticket
        ▼
SQLite: data/feedops.db
   • nas_events
   • alert_events
   • critic_results
   • planner_results
   • staged_actions          ← HITL queue
   • action_audit_log        ← immutable transitions
   • watch_officer_briefs    ← brief narratives
   • operational_tickets     ← internal escalation packages
        ▼
Streamlit dashboard + Prometheus /metrics
http://localhost:8501       http://localhost:9108/metrics
```

---

## Live FAA SWIM / STDDS TAIS Integration

The current live integration uses the **FAA SCDS Jumpstart Kit** to subscribe to the **FAA Solace broker** under a SWIFT-authorized account. The Kit writes each JMS message as a separate file into its `log/` folder. Sentinel scans that folder, parses the hybrid header+payload format, normalizes records, and pushes them through the pipeline.

```
data/raw/swim_live/         ← Jumpstart Kit drops files here
data/processed/             ← successfully ingested files move here
data/failed/                ← parse failures land here for inspection
data/processed/ingest_log.jsonl   ← append-only ingest audit
```

Connection details (provider URL, queue name, connection factory, VPN, credentials) come from `.env`. They are obtained from the SWIFT portal's Subscription Details → Connection Details tab. `.env` is gitignored.

The parser is permissive: it accepts files with no extension (Kit default), `.json`, `.xml`, and `.txt`. Schema-validated records reach the alert engine; schema failures are logged and moved to `data/failed/` for review.

---

## Run Modes

```bash
# Synthetic operational scenarios — no broker, no LLM credits
python -m src.main --mode sample --count 100

# Replay captured live messages from data/raw/*.json
python -m src.main --mode replay --source data/raw

# Live ingest of the Jumpstart Kit log folder
python -m src.main --mode swim-folder --source data/raw/swim_live
```

Add `--metrics` to start the Prometheus `/metrics` server on port 9108 alongside any mode.

---

## Quick Start

### 1. Create and activate a virtual environment

```bash
python -m venv .venv
```

Windows PowerShell:
```powershell
.venv\Scripts\activate
```

macOS / Linux:
```bash
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`. The minimum useful set for a local sample-mode demo:

```ini
# LLM provider — pick one
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
CRITIC_MODEL=claude-haiku-4-5-20251001
CRITIC_ENABLED=1
MAX_CRITIC_PER_PASS=5

# Or Gemini through OpenAI-compatible endpoint:
# LLM_PROVIDER=nim
# NIM_ENDPOINT=https://generativelanguage.googleapis.com/v1beta/openai
# NIM_MODEL=gemini-2.5-flash
# NIM_API_KEY=AIzaSy...
```

For live broker access add the SWIM section. **Never commit the real `.env`.**

### 4. Seed the database

```bash
python -m src.main --mode sample --count 100
```

### 5. Start the dashboard

```bash
streamlit run dashboard/app.py
```

Open <http://localhost:8501>.

---

## Start the Dashboard

```bash
streamlit run dashboard/app.py
```

The page renders even when no LLM key is configured. In that case the Critic, Planner, and Watch Officer all fall back to deterministic logic — the dashboard remains operational.

---

## Testing and Evaluation

```bash
pytest -q
```

Coverage areas:

- `tests/test_alert_engine.py` — deterministic alert rules
- `tests/test_feed_health.py` — freshness, worst-wins per-airport rollup
- `tests/test_validators.py` — schema and business-rule edges
- `tests/test_replay.py` — replay loader and per-file fault tolerance
- `tests/test_critic_agent.py` — degraded vs happy-path Critic behavior
- `tests/test_planner_agent.py` — Planner output schema and degradation
- `tests/test_execution_gate.py` — staging idempotency, terminal-state stickiness, audit-row creation, query semantics
- `tests/test_operational_tickets.py` — DRAFT → APPROVED / REJECTED / MONITORING transitions, idempotency, audit integration
- `tests/test_watch_officer_agent.py` — UNKNOWN / NORMAL / ATTENTION / ACTION postures, schema validation, SQLite round-trip, deterministic fallback
- `tests/test_news_agent.py` — banner severity priority and item caps
- `tests/test_guardrails_gate_eval.py` — input/output guardrails and end-to-end gate evaluation against golden scenarios

Replay-based regression:

```bash
python -m src.main --mode replay --source data/raw
```

A golden eval suite runs deterministic-only by default and can include LLM checks with `--llm`:

```bash
python -m src.main --mode eval        # deterministic only
python -m src.main --mode eval --llm  # also exercise Critic + Planner
```

---

## LLM Provider Configuration

`src/llm_client.py` is a thin abstraction over a single `call_with_tool(...)` method. Two implementations ship:

| Provider | When | `.env` shape |
|---|---|---|
| **Anthropic** (native) | Demo quality, prompt-cache friendly | `LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=…`, `CRITIC_MODEL=claude-haiku-4-5-…` |
| **OpenAI-compatible** (NIM / Gemini / Ollama / vLLM / TGI) | On-prem or cost-optimized | `LLM_PROVIDER=nim`, `NIM_ENDPOINT=…`, `NIM_MODEL=…`, `NIM_API_KEY=…` |

The same code runs against either. Swapping providers is an `.env` change, not a code change.

---

## Security and Data Handling

- `.env` is gitignored. Never commit real credentials.
- SQLite database lives at `data/feedops.db` and is local to the host running the dashboard.
- The Jumpstart Kit log folder, `data/processed/`, and `data/failed/` are all gitignored.
- The HITL gate has no auto-execute path: no controller is paged, no ATC system is contacted, no real FAA operational command is issued.
- LLM calls go only to the configured provider endpoint. There is no telemetry, no third-party analytics, no background data exfiltration.
- The system is designed to run inside a federal boundary by swapping the LLM endpoint to an internal NIM / vLLM deployment.

---

## Current Demo Status

**Implemented:**

- Live FAA SWIM / STDDS TAIS ingest through the SCDS Jumpstart Kit
- Solace broker connection (SWIFT-authorized account)
- Hybrid JMS-header + JSON-payload parser
- Pydantic-validated normalization with schema enforcement
- Deterministic alert engine, feed health, validation summary
- AI Critic with confidence + pattern classification + escalation flag
- Operational Planner answering plain-English questions
- PCT Watch Officer Agent with deterministic fallback
- Internal Operational Ticket package (DRAFT / APPROVED_FOR_ESCALATION / REJECTED / MONITORING)
- Human-in-the-loop staged action gate with audit trail
- Streamlit dashboard with Mission Questions Answered, Watch Officer Brief, airspace map, flight phase matrix, altitude / speed distributions, RVR monitor, data quality posture, alert log, HITL queue, audit trail
- CNN-style operational ticker (banner) with click-to-jump and pause behavior
- LLM provider abstraction (Anthropic / NIM / Gemini / Ollama)
- Prometheus `/metrics` endpoint with HITL queue depth, LLM latency, gate decisions, per-airport status
- Sample mode, replay mode, swim-folder live mode, eval mode
- 147+ pytest tests covering deterministic and degraded paths

**Intentionally not implemented:**

- External ticketing integration (ServiceNow / Remedy / Jira) — the internal package is the durable artifact a future adapter would read
- Pager / SMS / phone escalation
- Auto-expire transition for stale staged actions
- Multi-operator (4-eyes) approval workflow
- Cryptographic signing of audit entries
- Any code path that issues a real FAA operational command

---

## Demo Narrative

I built this because FAA modernization is not just about moving data. It is about knowing whether operational data is current, trustworthy, explainable, and safe to act on.

This dashboard answers one operational question: **can PCT TRACON trust the current aircraft movement picture?**

The system ingests live FAA SWIM TAIS flight-plan and track messages through the SCDS Jumpstart Kit, converts them into airport-level activity across KDCA, KIAD, KBWI, and KADW, checks freshness and phase-of-flight distribution, and uses an AI Critic and Watch Officer Agent to decide whether the condition is normal traffic, stale data, missing data, or something that needs human review.

The model is not the control plane. The model explains, scores, and drafts. Deterministic validators, alert rules, human-in-the-loop gates, tickets, and audit trails control what happens next.

---

## Technical Outcomes

> Built **PCT TAIS FeedOps Sentinel**, a Python agentic-AI operational intelligence system for FAA SWIM / STDDS TAIS data covering PCT TRACON (KDCA, KIAD, KBWI, KADW). Implemented live broker ingest through the SCDS Jumpstart Kit, hybrid JMS+JSON parsing, Pydantic-validated TAIS normalization, deterministic alert and feed-health rules, AI Critic and Watch Officer agents with deterministic fallback, an internal escalation-package workflow, a human-in-the-loop execution gate with immutable audit trail, and a Streamlit operational dashboard with airspace map, flight phase matrix, and live-data banner. Designed end-to-end around the LCFE production framework (Latency, Cost, Failure, Evaluation) with provider-agnostic LLM client supporting Anthropic, NVIDIA NIM, Gemini, and local Ollama endpoints. 147+ pytest tests, replay and sample regression modes, Prometheus metrics, Docker Compose with Grafana auto-provisioning.
Do this now

I'd run these three processes:

PowerShell 1 — Jumpstart

cd C:\Users\marlo\source\peratonfaa\jumpstart-latest

Keep your existing SWIM consumer running.

PowerShell 2 — Sentinel

cd C:\Users\marlo\source\peratonfaa\nas-feedops-sentinel
.\.venv\Scripts\Activate.ps1
pip install requirements
python -m src.main --mode swim-folder

PowerShell 3 — Dashboard

cd C:\Users\marlo\source\peratonfaa\nas-feedops-sentinel\dashboard
.\.venv\Scripts\Activate.ps1
python -m streamlit run app.py

Then open:

http://localhost:8501

Streamlit will normally open the browser automatically when launched, and you can also use the localhost URL printed by the server.