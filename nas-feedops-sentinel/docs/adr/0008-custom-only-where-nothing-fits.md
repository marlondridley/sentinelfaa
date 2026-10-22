# 0008. Custom code is the SWIM layer, the human gate and the AI evaluation; the rest is off the shelf

Status: Proposed (drafted 2026-10-21; the owner accepts it by changing this line)

## Context
The question was put plainly: could Sentinel be replaced by an open-source or
off-the-shelf tool? Integrators who run general monitoring and ticketing
platforms will ask it, and an assessor will ask it of any system that carries
this much of its own code.

Sentinel is about 18,800 lines in `src/`, 5,300 in `dashboard/` and 108 files in
files. Most of what it stands on is already open source: Keycloak, SPIRE and
Vault, PostgreSQL behind PgBouncer, Temporal, Prometheus, Alertmanager, Grafana,
OpenTelemetry, LiteLLM and the Microsoft Agent Governance Toolkit. What is
custom falls into two halves:

- **Is the feed alive, fresh and well-formed?** Once messages are decoded this is
  generic monitoring: freshness, volume, lag, schema conformance and thresholds.
  Mature tools do it.
- **The AI explains, a person decides, and the evidence is independent and
  auditable.** We found no product that does this as a whole. Parts of it exist
  separately (approvals in ticketing tools, traces in LLM-observability tools).

## Decision
Custom code is limited to five things. Everything else is a component we
configure, not rewrite.

1. **Decoding FAA SWIM payloads** into events and checking they are well-formed:
   `src/swim_*.py`, `src/raw_message.py`, `src/airports.py` (2,399 lines). We know
   of no ready-made decoder with these checks.
2. **The seven deterministic rules and the verdict**, run in the same pass that
   stages every CRITICAL alert (`src/alert_engine.py`, `src/pipeline.py`,
   `src/execution_gate.py`), with the reconciler behind them (`src/reconcile.py`).
3. **The human gate**: a role-checked decision and an append-only log the database
   itself enforces (`src/execution_gate.py`, `src/auth.py`, `src/storage/schema.py`;
   [0002](0002-human-gate-never-auto-executes.md)).
4. **The AI layer's NAS-specific parts**: prompts, evidence rules and redaction of
   tail numbers, squawks and positions ([0001](0001-rules-decide-models-explain.md),
   [0007](0007-structured-retrieval-not-vector-search.md)).
5. **Evaluation from independent evidence** and the readiness gate
   ([0006](0006-verification-from-independent-evidence.md)).

New work follows one rule: before writing plumbing (transport, charting, alert
routing, workflow, model gateway, tracing), name the off-the-shelf option and why
it was not used. A change that adds custom plumbing says so in its description.

Two custom parts look like commodity and are kept on purpose, for now:

- **The alert rules.** A CRITICAL alert and its staged review must come from one
  pass, so an alert cannot exist without a review. The reconciler is there because
  that once failed: "the code path does it" left 84 CRITICAL alerts with no staged
  action. A separate alerting engine polling the database would reopen that gap.
  The rules that need only the clock and the last stored message (stale feed, lag)
  are the candidates to hand to an external engine.
- **The console pages.** Each count links to the rows it counts, and the review
  window carries the decision and its attribution in one place. The look is
  approved. The pages that only draw charts (Feed Health, Live Airspace) are the
  most replaceable part of the system.

## Alternatives considered
- **Grafana or Kibana for every page.** Rejected for now: they show state well but
  do not carry a role-checked decision, a count that equals its rows, or sign-off
  in one window, and we would run two interfaces. Likely home of the chart-only
  pages if they are ever moved.
- **Elastic or Splunk detection plus ServiceNow or Jira approvals.** Rejected as
  the base: the decision record would live in a system we do not control, rules
  would sit outside the staging pass, and nothing evaluates the AI from
  independent evidence. It may be where a customer already is (see Consequences).
- **A workflow product (Camunda, Flowable) for the human gate.** Rejected: the wait
  for a person lives in database state (the review queue, the reconciler and the
  stale-review alert), not in a workflow engine. A second engine would serve one
  human step, and the database-enforced append-only log would still be ours to build.
- **Agent frameworks (LangGraph, CrewAI) for orchestration.** Rejected: the order of
  an investigation is fixed and no model decides the flow. Plain code gives us that
  (`src/investigation/workflow.py`); Temporal is not involved in it.
- **An LLM-observability platform (Langfuse, Phoenix) as the evaluation layer.**
  Rejected as the record of quality: the rule is that evidence comes from our own
  database, not from the agents' telemetry. It could still serve as a trace viewer;
  that is not decided.
- **NiFi, Camel or Kafka Connect for ingest.** Handled by
  [0004](0004-jms-edge-kafka-deferred.md). Decoding stays custom whichever carries
  the messages.
- **A commercial SWIM or aviation-data monitor.** None known that decodes these
  payloads and judges whether the feed is trustworthy. Not searched exhaustively.

## Consequences
- Temporal's role is narrow. It runs the two jobs the HTTP API can start (ingest a
  folder, process a batch of events) with retries and a job id that de-duplicates
  client retries. The live feed does not use it, and neither does the investigation.
  Nothing in this repository calls that endpoint. Whether the component earns its
  six containers is open; see Revisit when.
- The custom surface an assessor must read is five named parts, each tied to a
  record above, instead of "the whole app".
- We maintain 2,399 lines of FAA decoding and a charting interface that a
  platform could host. That is the price of the decision.
- Grafana and the Streamlit pages overlap in what they can draw. The overlap is
  accepted until the pages cost more to keep than a board would.
- Where an integrator already runs a monitoring or ticketing platform, Sentinel's
  job is to feed it, not to compete with it. Sentinel exposes Prometheus metrics
  and an HTTP API today. Whether it should also hand events and alerts to a
  customer's platform is not decided.

## Revisit when
- A second feed (TFMS, TBFM) adds chart pages faster than they can be kept up.
- A customer requires events or alerts in its own platform, or decisions recorded
  in its ticketing system. That needs a second gate and would extend or supersede
  [0002](0002-human-gate-never-auto-executes.md).
- It is confirmed that nothing calls the API endpoint that starts a Temporal run,
  or a use for durable waiting appears (a timed reminder or escalation tied to a
  review). Then decide whether to keep Temporal or replace it with a job table in
  PostgreSQL.
- An off-the-shelf monitor that understands SWIM payloads appears, or an
  integrator names one it already holds.

## Evidence
`src/swim_parse.py`, `src/swim_feed_*.py` with `tests/test_swim_parse_golden.py`
(the real captures); `src/alert_engine.py` with `tests/test_alert_engine.py`;
`src/pipeline.py` for rules and staging in one pass; `src/reconcile.py` for the
invariant and why it exists, and the `CriticalAlertsUnstaged` rule in
`monitoring/alert-rules.yml`; `tests/test_execution_gate.py` and
`tests/test_execution_gate_async.py` for the gate; the "Custom and off the shelf"
table in [agent-governance.md](../agent-governance.md) for the AI layer in detail;
`docker-compose.yml` and `instraclustr/` for the components we run as shipped.

## Limits
This is a desk comparison. No alternative was run against SWIM data. What each
product can and cannot do comes from its documentation as we understand it, and
versions, licences and prices were not checked, so re-check any claim here before
putting it in front of a customer. "None known" means none found, not none
exist. Line counts are from 2026-10-21 and will drift.
