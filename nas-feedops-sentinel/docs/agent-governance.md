# How AGT governs AI inside Sentinel

This document explains how AI is governed inside Sentinel, not how the
Microsoft Agent Governance Toolkit (AGT) works in general. For each boundary
it answers four questions:
- what is guaranteed;
- which code enforces it;
- what AGT does, and what Sentinel does itself;
- which test proves it.

Everything here is in the repository and runs in CI.

The short version: **agents advise, rules escalate, people decide.**
- Every model call and every agent tool call passes an AGT policy first.
- Every call is carried as OpenTelemetry.
- Every metric is AGT-native, AGT-compatible or Sentinel-specific, and says
  which; none is adopted because an AGT metric has a similar name
  ([agent-metrics.md](agent-metrics.md)).
- Nothing an agent says about itself is used to grade it.

```
 SWIM feed ─► deterministic rules ─► CRITICAL alert ─► staged for a person ─► human decision
                 (5 mission)            │                  (6 human decision)      │
                                        ▼                                          │
                              investigation workflow (7 workflow)                  │
                                        │                                          │
                 ┌──────────────────────┼──────────────────────┐                   │
                 ▼                      ▼                      ▼                   │
          agent tool call        model call (1 execution)   evidence               │
                 │                      │                                          │
                 └─── AGT policy ───────┘  (3 governance)                           │
                              │                                                    │
                              ▼                                                    ▼
              OpenTelemetry spans, metrics, AGT audit logs (2 telemetry)    audit log
                              │                                                    │
                              ▼                                                    ▼
                    durable record: model_calls, sealed investigations, action_audit_log (8 audit)
                              │
                              ▼
          evaluation: AGT metrics over independently sourced evidence (4 evaluation)
                              │
                              ▼
               dashboard layers 1-3, readiness verdict, deployment gate
```

## The agent register: `policies/agent-register.yaml`

Seven principals act in the advisory layer: the Critic, the Planner, the Watch
Officer, the investigation orchestrator (no model) and the three investigation
specialists. The register is one file that says, for each, what it is for, who
owns it, its risk tier and why, which services it runs in, the tools it is
granted, what data it is sent, what it falls back to, and how to stop it.
`python -m src.agent_register` prints it.

It is held to the rest of the system by `tests/test_agent_register.py`:
- the agents that call a model are exactly the roles `model-use.yaml` allows,
  and the register's tool lists are exactly `tool-use.yaml`'s grants, so a role
  or a grant cannot change in one place;
- every prompt belongs to an agent, and a prompt whose version moved since it was
  reviewed fails until the register is updated with it;
- every setting, service and test the register names exists, and every
  `acting_as()` role in the code is registered;
- no agent has any authority: `decide`, `stage`, `write` and `execute` are
  false, and the schema refuses a register that says otherwise.

The controls in this guide are mapped to NIST AI RMF 1.0 and ISO/IEC 42001:2023 in
[framework-mapping.md](framework-mapping.md), which says what is implemented, partial,
not covered and organizational, with the evidence for each.

**Limits, stated plainly.**
- **Identity.** A service has its own SPIFFE identity; an agent runs inside a
  service and is a string in OpenTelemetry baggage. It is enforced by the two
  policies and recorded on every call, but carries no credential of its own, so
  code in the same service could claim another agent's name. A per-agent
  workload identity would close that, and is not built.
- **One runtime switch, for the whole layer.** A supervisor's halt (below) stops
  every model call at runtime. There is no switch for a single agent: settings are
  built once at import (`src/config.py`), so every entry's `disable` takes effect when
  the service restarts.
- **Owners and tiers are judgements.** Each owner is the role "Sentinel
  maintainer" until a named person takes it, and the tiers are the author's
  reading of the rubric at the top of the file.

## The halt: `src/model_halt.py`

One switch makes every model call refuse, in every service, without a restart. A
supervisor halts it, or resumes it, on the Agent Evaluation page; both need a reason.
While it is halted the first rule of `model-use.yaml` (`model-halted`) refuses every
call, nothing is sent, and each agent falls back to its deterministic rules exactly as
it does when a model fails. Alerts are still staged and the human gate is untouched,
because the mission never depended on a model. That is why halting is cheap here.

- **The state is one row in the database,** read before every model call and kept for
  `MODEL_HALT_CACHE_SECONDS` (2 s), so a halt reaches the other services within that long.
- **The audit is the database's.** A trigger writes every change to the append-only
  `action_audit_log` (`model_halt:<id>`, with the actor and the reason), a second trigger
  refuses DELETE, and the application role may only read and update the row. No code
  path, and nobody holding that role, can change the state unrecorded.
- **A state that cannot be read halts.** An unknown state is not "running". A database
  the migrate job has not reached has no table at all, which is not a halt, so a rolling
  deploy keeps working.
- **It is visible.** A banner on every page says who halted it, when and why;
  `ModelLayerHalted` is a critical alert; the runbook says what to do.
- **An optional automatic trip** (`MODEL_AUTO_HALT_ENABLED`, off by default) halts after
  `MODEL_AUTO_HALT_THRESHOLD` refusals within `MODEL_AUTO_HALT_WINDOW_SECONDS` for a
  credential or a protected value found in a prompt. It counts only those: a
  misconfigured role or an oversized prompt is a bug to fix, not an attack to halt for.
  A trip is as visible as a human's halt, and only a supervisor resumes it. It is
  optional because it can be provoked on purpose to switch the AI layer off. The count
  is per process: each service counts the refusals it saw.

Proof: `tests/test_model_halt.py`, `tests/test_model_halt_page.py`.

## 1. AI execution boundary: `src/llm_client.py`

**Guarantee.** No model is reached except through one function, and that
function cannot be used carelessly.

**Enforced in.** `LiteLLMClient.acall_with_tool()` is the only path to a model:
the Critic, Planner, Watch Officer and all four investigation specialists go
through it. On every call it:
1. redacts the prompt (`src/redaction.py`: tail numbers, squawk codes,
   positions, callsigns), with no opt-out;
2. asks the model-use policy, then checks that the output tool the role hands
   the model is that role's own (see boundary 3);
3. sends to the LiteLLM gateway only, by alias (`primary-model`),
   under a scoped virtual key with a budget and a rate
   limit;
4. is bounded: `LLM_TIMEOUT_SECONDS`, three attempts for transient errors,
   and one schema-repair request, never more;
5. leaves the answer untrusted: the Critic's output guardrail and the
   investigation grounding gate judge it before anything is stored;
6. on any failure or refusal, lets the caller fall back to its deterministic
   rules. The AI layer degrades; the mission does not.

**AGT's part.** AGT's `PolicyEvaluator` makes the allow/refuse decision, and
its `CredentialRedactor` finds credentials in the outgoing prompt.

**Sentinel's part.** The chokepoint, redaction, the gateway, the retry and
repair limits, and the fallbacks.

**Proof.**
- `tests/test_governance.py::test_a_refused_call_sends_nothing_and_is_recorded_as_costing_nothing`
- `tests/test_model_telemetry.py`
- `tests/test_redaction.py`
- `tests/test_outbound_leak_check.py`

**What keeps an identifier from reaching a model, in layers.** No single layer
is a guarantee; the design is that a miss in one is not enough.
1. **Choose the fields.** Each agent is shown a bounded projection built field
   by field (the Watch Officer's `project_for_model`, the Critic's SQL column
   list), not a record. A field that was never selected cannot leak.
2. **Pseudonymize identities, drop measurements.** `pseudonymize_structured`
   gives a flight a token (`AC-1`) that means nothing outside one prompt, so a
   model can tell two flights apart without learning which; positions,
   altitudes, codes and raw payloads are dropped. The Planner puts the tokens
   back in its answer, locally (`Pseudonymizer.reveal`); nothing is sent.
3. **Scrub by pattern.** `redact_text` runs on every prompt at the chokepoint,
   with no opt-out. It is a guess about what an identifier looks like.
4. **Look for the values.** Callers pass `protected_values`: what they read
   from their own records under a sensitive key (`collect_sensitive_values`).
   `count_leaks` searches the finished prompt for those values themselves, so
   it finds a registration written `G-ABCD` that no pattern anticipated. A hit
   is the `no-protected-values-in-prompts` rule: the call is blocked, recorded
   with the rule that refused it, and the agent falls back to its rules. Only
   the count is logged, never the values.
5. **Watch it.** `ProtectedValueBlockedFromPrompt` (critical) fires on any
   block; a test fails if a model call site stops passing `protected_values`,
   and a corpus of identifier shapes must satisfy "whatever the scrub leaves
   in, the value check finds".

**Limits, stated plainly.**
- The value check knows only the values callers extracted. An identifier that
  appears only inside free text nobody parsed is not on its list.
- A 4-digit code (a beacon code) cannot be told from a number in prose, so
  values under 5 digits are not checked by value; the keyed and pattern layers
  cover them.
- `redacted` in the policy says the scrub is stable (it asks the same patterns
  that ran it). It is not an independent check; layer 4 is.
- Detection is still pattern- and value-based. Names, e-mail addresses and
  free-text personal data outside the aviation identifiers are not covered.

**Next hardening step for an enterprise deployment.** Add a second, independent
detector over the finished prompt, for example Microsoft Presidio with
recognizers for N-numbers, callsigns, beacon codes and coordinates, plus its
general personal-data recognizers, or Azure AI Language PII detection at the
gateway. Its errors would not be correlated with the patterns here. It is left
out on purpose: it adds a dependency and needs customization and tuning per
deployment, and its result could feed the same policy field
(`protected_values_found`) so the rule does not change.

## 2. Telemetry boundary: OpenTelemetry

**Guarantee.** Every agent execution is observable in one provider-neutral
vocabulary. Nothing that leaves the process carries prompt or response text.

**Enforced in.** `src/telemetry.py`, with the names from
`opentelemetry-semantic-conventions`.
- **Spans:**
  - `invoke_workflow investigation`
  - `invoke_agent <specialist>` and `invoke_agent critic`
  - `chat <alias>` for each model call
  - `execute_tool <tool>` for each governed tool call
- **What a chat span carries:**
  - GenAI attributes: `gen_ai.provider.name`, `gen_ai.request.model`,
    `gen_ai.response.model`, `gen_ai.usage.*` and `error.type`;
  - AGT's policy decision under AGT's own names (`agt.*`);
  - Sentinel's outcome class and cost.
- **Metrics:**
  - the GenAI standard `gen_ai.client.operation.duration` and
    `gen_ai.client.token.usage`;
  - AGT's instruments (boundary 4);
  - all on `/metrics` through OpenTelemetry's Prometheus reader.
- **Export:** OTLP to any collector through the standard
  `OTEL_EXPORTER_OTLP_*` variables. Nothing is exported unless one is set.
- **Subject:** which investigation or alert a call belongs to travels as
  OpenTelemetry baggage, so cost and time are attributed without threading
  ids through every function.

**AGT's part.** AGT's `OTelLogsBackend` writes its audit records through the
same pipeline, and its metric instruments are exported through it.

**Sentinel's part.** Provider detection from the served model: DeepSeek,
Gemini, OpenAI and Claude are all measured by one definition.

**Proof.**
- `tests/test_tool_boundary.py::test_a_governed_read_runs_inside_an_execute_tool_span`
- `tests/test_evaluation.py::test_an_investigation_is_evaluated_from_the_record_not_from_the_agents`,
  which shows four model calls attributed through baggage

## 3. Governance boundary: AGT

**Guarantee.** Every model call and every agent tool call is decided by a
written policy before it happens. Decisions are deny by default and fail
closed, and every decision is audited.

**Enforced in.**

- `policies/model-use.yaml` decides every model call:
  - which roles may call a model;
  - which gateway aliases they may use;
  - prompt size and output budget;
  - no credentials in a prompt (a block).
- `policies/tool-use.yaml` decides every tool call; it is the agent layer's
  tool boundary:
  - each agent may use only its own output tool and its own context or
    evidence reads;
  - `agents-never-decide-or-write` blocks decide, execute and write for
    every agent;
  - `escalation-belongs-to-the-rules` denies staging;
  - `agents-must-identify-themselves` denies anonymous calls.
- `src/governance.py` evaluates both, through agent-os-kernel's
  `PolicyEvaluator`.
- `src/agent_tools.py`:
  - is the tool registry;
  - provides `acting_as()`, which carries the calling agent as baggage;
  - provides `invoke()`, for governed reads;
  - provides `guard_human_gate()`, which the human gate calls (boundary 6).
- Every decision goes to AGT's `GovernanceAuditLogger` with its
  `OTelLogsBackend` (`agt.audit.*` log records). Every refusal counts on
  AGT's `agent.policy.violations`.

**AGT's part.**
- policy language and evaluation;
- credential detection;
- the audit logger and its OTel backend;
- the violation counter.

**Sentinel's part.** Three guarantees AGT leaves to its host:
- A policy that cannot be loaded refuses everything.
- A policy with an unknown key, without deny-by-default, or relying on
  `tool_allowlist` is refused. AGT would load the first two as
  allow-everything, and the evaluator never reads `tool_allowlist` (bugs K,
  L and M).
- Every context field is always present, because AGT's matcher treats a
  missing field as "no match".

**Proof.**
- `tests/test_governance.py` (26 tests)
- `tests/test_tool_boundary.py`: every grant, every refusal, an agent
  holding a supervisor's credentials still refused, and AGT audit records
  read back from OTel
- `tests/test_agt_known_bugs.py`: bugs K, L and M

## 4. Evaluation boundary: AGT plus independently sourced Sentinel evidence

**Guarantee.** Agents never grade themselves. Every quality, cost, latency
and reliability figure is computed from evidence the agents do not control:
- the grounding gate's check of every cited evidence id;
- the operator's recorded decision;
- the gateway's token and cost figures;
- the sealed record's hash;
- the append-only audit log.

A figure that cannot be computed honestly reads "no data".

**Enforced in.**
- `src/evaluation.py`: per-investigation measures, loading, and the window
  evaluation. The gates are in `src/evaluation_gates.py` (one function per
  gate), the window metric groups in `src/evaluation_metrics.py`, and the
  records and status names in `src/evaluation_types.py`.
- `src/agt_metrics.py`: the AGT adapters. They build fresh AGT objects per
  window and read them only through paths shown to be correct.
- `src/readiness.py`: the objectives in `policies/model-slos.yaml` and one
  verdict. `--gate` turns it into a deployment gate.

**AGT's part.** AGT is the source wherever it computes correctly:

| AGT component | Metrics |
|---|---|
| agent-sre `EvaluationEngine`, with Sentinel's evidence judge | decision quality, evidence grounding, recommendation accuracy |
| `ResponseLatency` | every percentile |
| `CostGuard` | spend per invocation, investigation, alert and model |
| `FleetManager` | per-agent and per-provider reliability |
| `TaskSuccessRate` | investigation completion |
| `HallucinationRate` | unsupported claims |
| `DelegationChainDepth` | orchestration depth |
| agent-os-kernel `GovernanceMetrics` | policy compliance |
| `BudgetTracker` | the per-investigation cost and latency gates |

**Sentinel's part.** Only what AGT gets wrong or does not have:
- error-budget arithmetic and the verdict (bugs A and B);
- ECE calibration (bug G);
- false and missed escalations, overrides and contradictions;
- handoffs, retry and failure cost;
- the audit gate.

The full map is `docs/agent-metrics.md`.

**Proof.**
- `tests/test_evaluation.py`: real workflow, scripted model, verified
  decision, exact values
- `tests/test_agt_known_bugs.py`: 13 AGT bugs as strict xfails. If AGT
  fixes one, the suite fails and the workaround is retired.

## 5. Mission outcome boundary: Sentinel

**Guarantee.** What the operator acts on is decided by deterministic rules
over measured facts. A model can explain and challenge; it cannot raise,
lower, suppress or stage an alert.

**Enforced in.**
- `src/alert_engine.py`: alert rules.
- `src/feed_health.py`: feed status.
- `src/validators.py`: schema conformance.
- `src/swim_parse.py` and `src/swim_feed_tais.py`: FAA data handling,
  including `blockAtFAA` withholding and beacon-code release only for active
  flight plans.
- The Dashboard verdict (GREEN/AMBER/RED) is computed from conformance, lag
  and open CRITICAL alerts, with no model consulted.
- The investigation workflow's order never depends on model output.

**AGT's part.** None. Mission logic is Sentinel's, and it is deliberately not
an agent.

**Sentinel's part.** All of it.

**Proof.**
- `tests/test_alert_engine.py`
- `tests/test_swim_kit_ingest.py`: the ten real FAA captures, ten for ten
- `tests/test_e2e_proof.py`

## 6. Human decision boundary: `src/execution_gate.py`

**Guarantee.** Only a verified person with the right role decides on an
escalation, and no agent can reach that decision from any direction. The
human gate sits at escalation, not at every AI invocation: invocations are
governed automatically (boundary 3).

**Enforced in.**
- `stage_critical_alert()` is the only way an action is staged. The rule is
  fixed (CRITICAL → PENDING) and never executes anything.
- `decide_staged_action()` is the only way it is decided. It checks the
  verified sign-in and Keycloak realm role (`src/auth.py`). It records the
  actor with their roles and how they were verified, atomically with the
  change.
- Both call `guard_human_gate()` first. Code running as an agent is refused
  by the tool-use policy, whatever principal it presents.
- Identity is Keycloak over HTTPS only: TLS end to end, certificate
  verified, an `http://` identity URL refused.

**AGT's part.** The policy decision that refuses an agent at the gate
(`agents-never-decide-or-write`, `escalation-belongs-to-the-rules`).

**Sentinel's part.** The gate itself, role-based authorization, verified
identity, and the atomic audit write.

**Proof.**
- `tests/test_authorization.py`
- `tests/test_tool_boundary.py::test_an_agent_cannot_decide_even_with_a_supervisors_credentials`
- `tests/test_tool_boundary.py::test_an_agent_cannot_stage_an_action`
- `tests/test_identity_tls.py`

## 7. Workflow boundary: Temporal

**Guarantee.** Work started through the API runs durably, retries only what
can succeed on a retry, and runs once per request.

**Enforced in.**
- `src/temporal_workflows.py`:
  - pipeline and ingest workflows;
  - at most five attempts with backoff;
  - validation errors (`ValueError`, `TypeError`) are non-retryable, so a
    malformed payload fails fast instead of retrying forever.
- `src/api.py` seeds the workflow id from an idempotency key, so a client
  retry attaches to the running workflow instead of starting a second one.
- Inside a pipeline run, the investigation is Sentinel's own deterministic
  orchestration (`src/investigation/workflow.py`):
  - a fixed order;
  - two specialists in parallel, each isolated on its own evidence;
  - the record written only by the orchestrator;
  - a failed specialist falls back to its rules;
  - a failed investigation is sealed as FAILED, never raised into the
    pipeline.

**AGT's part.** None. Agent Framework-style orchestration is not used; the
order is code, not model output.

**Sentinel's part.** Temporal configuration, idempotency and the
orchestrator.

**Proof.**
- `tests/test_investigation.py`
- `tests/test_pipeline.py`

## 8. Audit boundary: Sentinel persistence plus telemetry

**Guarantee.** What happened can be reconstructed later and cannot be
quietly edited afterwards.

**Enforced in.**
- Append-only tables, enforced by database triggers and by the application
  role's grants (`src/storage/schema.py`, `src/storage/migrate.py`):
  - `action_audit_log`: every staging and every decision, with its actor;
  - `investigations`: each record sealed with a SHA-256 and verified on
    every read;
  - `model_calls`: one row per model call, projected from its chat span's
    attributes, with `trace_id` linking back to the trace. Each row also
    carries the prompt's hash and version, the hash of the redacted input
    that was sent, and the redaction count; no prompt or input text is
    kept. A Critic verdict (`critic_results.call_id`) and a Watch Officer
    brief (`watch_officer_briefs.call_id`) carry the id of the call that
    produced them.
- AGT's audit log records every policy decision, including tool calls,
  through OpenTelemetry.
- Provenance on every specialist output: model, prompt version, prompt and
  input hashes. The evaluation's audit gate checks all of it per
  investigation.

**AGT's part.** The governance audit trail.

**Sentinel's part.** Durable, tamper-evident storage. A readiness verdict
must not depend on an optional collector's retention.

**Proof.**
- `tests/test_e2e_proof.py`: the audit log refuses UPDATE and DELETE
- `tests/test_investigation.py`
- `tests/test_evaluation.py`: the audit gate

## Controls crosswalk

| Sentinel control | Where | AGT control | Metric or evidence it produces |
|---|---|---|---|
| One governed path to any model | `src/llm_client.py` | `PolicyEvaluator` on `policies/model-use.yaml` | `model_calls` row and chat span per call; `agent.policy.violations` on refusal |
| No credentials in prompts | `src/governance.py` | `CredentialRedactor` + block rule | `agt.audit.decision=block` log record |
| Prompt redaction | `src/redaction.py` | none (Sentinel-specific NAS identifiers) | the `redacted` flag in every policy context |
| Agent tool boundary | `src/agent_tools.py`, `policies/tool-use.yaml` | `PolicyEvaluator` | `execute_tool` span; `agent.tool_calls.total`; `agt.audit.action=tool_call` |
| Agents never decide or stage | `src/execution_gate.py` `guard_human_gate()` | `PolicyEvaluator` block/deny rules | `AgentBlockedFromDecidingOrWriting` alert |
| Fail closed on a bad policy | `src/governance.py` `_load()` | hardens `PolicyDocument` (bugs K, L, M) | every call refused, `policy=None` in the record |
| Governance audit trail | `src/governance.py` `_audit()` | `GovernanceAuditLogger` + `OTelLogsBackend` | `agt.audit.*` OTel log records |
| Per-investigation cost and latency gates | `src/evaluation_gates.py` | `BudgetTracker` | Cost and Latency gates; gate pass rates |
| Decision quality | `src/evaluation_metrics.py`, `src/agt_metrics.py` | `EvaluationEngine` with Sentinel's evidence judge | decision quality, grounding and accuracy scores |
| Unsupported claims (hallucination rate) | grounding gate in `src/investigation/specialists.py` | `HallucinationRate` via `collect()` | unsupported-claim rate |
| Latency objectives | `src/evaluation_metrics.py` | `ResponseLatency` | P50/P95/P99 per agent, stage and investigation |
| Cost accounting | `src/evaluation_metrics.py` | `CostGuard` | cost per invocation, investigation, alert and model |
| Reliability | `src/evaluation_metrics.py` | `FleetManager`, `TaskSuccessRate` | success per agent and provider; completion |
| Orchestration depth | `src/evaluation_metrics.py` | `DelegationChainDepth` | depth within limit |
| Policy compliance | `src/evaluation_metrics.py` | `GovernanceMetrics` | policy-compliance objective |
| Deployment readiness | `src/readiness.py` | none (`ErrorBudget` bugs A, B) | READY / AT RISK / NOT READY / INSUFFICIENT DATA; `--gate` exit code |
| Human decision by a verified person | `src/execution_gate.py`, `src/auth.py`, Keycloak | none | Safety gate; `action_audit_log` actor with roles and verification |
| Sealed, verifiable investigations | `src/investigation/record.py`, `investigations_repo.py` | none | Audit gate; integrity failure forces NOT READY |
| Append-only records | `src/storage/schema.py`, `migrate.py` | none | database refuses UPDATE/DELETE |

## Custom and off the shelf

| Capability | Off the shelf | Custom in Sentinel | Why custom |
|---|---|---|---|
| Policy language and evaluation | AGT agent-os-kernel `PolicyEvaluator`, `PolicyDocument` | the two policy files, loader hardening | AGT loads malformed policies as allow-all (bugs K, L); `tool_allowlist` is not enforced (bug M) |
| Credential detection | AGT `CredentialRedactor` | none | none |
| NAS data redaction | none | `src/redaction.py` | AGT has no NAS identifiers (tail numbers, squawks, positions) |
| Governance audit log | AGT `GovernanceAuditLogger` + `OTelLogsBackend` | none | none |
| Tool registry and agent identity | none | `src/agent_tools.py` | AGT's MCP gateway is for MCP tools; Sentinel's tools are in-process |
| Tracing and metrics transport | OpenTelemetry SDK 1.45, OTLP exporter, Prometheus reader | provider detection, subject baggage | the gateway alias hides the provider until the response names it |
| GenAI vocabulary | OpenTelemetry semantic conventions 0.66b0 | `sentinel.*` attributes for the outcome class and cost | the conventions have no cost or outcome-class attribute |
| Agent metric instruments | AGT agent-sre `create_agent_metrics`, `MetricsExporter` | none | none (`record_slo_from_object` is not used: it exports bug-A values) |
| Percentiles, cost, reliability, completion, hallucination rate, depth, compliance | AGT agent-sre and agent-os-kernel (boundary 4) | window adapters | fresh objects per window; `collect()` rather than `current_value()` (bugs C, E) |
| Decision-quality scoring | AGT `EvaluationEngine` | `EvidenceJudge`, "no data" and fail-closed guards | the judge must use independent evidence; bugs H, I, J |
| Error budget and readiness verdict | none used (AGT `ErrorBudget`, `SLO` are wrong) | `src/readiness.py` (AGT-compatible) | bugs A, B |
| Calibration | none used (AGT `CalibrationDeltaSLI` is wrong) | ECE in `src/agt_metrics.py` (AGT-compatible) | bug G |
| Escalation, override, contradiction, handoff, retry-cost metrics | none | `src/evaluation_metrics.py` (Sentinel-specific) | AGT defines no such metric |
| Human approval | Keycloak (identity), Temporal (durable runs) | `src/execution_gate.py`, `src/auth.py` | AGT's `EscalationManager` is in-memory; decisions here must be durable and role-checked |
| Model gateway | LiteLLM proxy | alias-only settings, scoped key | none |
| Orchestration | Temporal for API runs | `src/investigation/workflow.py` | a fixed, deterministic order; no model decides the flow |
| Durable, tamper-evident record | PostgreSQL | append-only triggers, sealed SHA-256 records | the evidence a deployment gate relies on |

Versions: agent-os-kernel 3.7.0, agent-sre 3.7.0, OpenTelemetry 1.45.0,
semantic conventions 0.66b0. All are pure Python and run on the arm64
Docker host. `agent-control-specification` ships an x86_64-only wheel and is
not used.
