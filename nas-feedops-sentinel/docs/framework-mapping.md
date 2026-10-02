# Framework mapping: NIST AI RMF 1.0 and ISO/IEC 42001:2023

<!-- Generated from policies/framework-mapping.yaml by `python -m src.framework_mapping --markdown`. Edit the YAML, not this file: a test fails when they differ. -->

Sentinel's controls mapped to NIST AI RMF 1.0 and ISO/IEC 42001:2023. Mapped does not mean conformant: ISO/IEC 42001 certifies an organization's management system, which a code repository can supply only in part.

Reviewed 2026-10-02. Version 1.0.

## How to read it

- **Direct** references are plain; *related* ones are in italics. Direct means the control does what the subcategory or Annex A control asks, within its scope. Related means it supports it or covers part of it.
- **Status** says how far the control goes: Implemented (it exists, is enforced or measured here, and has evidence), Partial (it stops short, and the note says where), Not covered (nothing here addresses it), Organizational (an organization has to supply it) or Not applicable (with the reason).
- A mapped control is not a conformant system. Every cited file exists, and a test holds that, but whether a control is *enough* is a judgement for an assessor.

## What was checked, and against what

- **NIST AI RMF 1.0 (NIST AI 100-1, January 2023).** Every subcategory identifier, and the meaning each is mapped for, was checked against the published document's Core tables on 2026-10-02.
- **ISO/IEC 42001:2023.** The standard is paywalled. Annex A control numbers and titles were checked against a public listing of the 38 controls (a vendor's summary, not the standard). Management-system clauses are cited at clause level (4 to 10) only, because sub-clause numbering was not verified. Check against a licensed copy before quoting this in an audit, a contract or a certification claim.

## Summary

| Status | Controls |
|---|---|
| Implemented | 16 |
| Partial | 7 |
| Not covered | 4 |
| Organizational | 3 |
| Not applicable | 1 |
| **Total** | **31** |

NIST AI RMF: 45 of 72 subcategories are referenced by at least one control. ISO/IEC 42001 Annex A: 29 of 38 controls are referenced.

| AI RMF function | Subcategories | Referenced | Directly |
|---|---|---|---|
| Govern | 19 | 15 | 7 |
| Map | 18 | 8 | 4 |
| Measure | 22 | 10 | 8 |
| Manage | 13 | 12 | 6 |

## Controls

| Control | NIST AI RMF | ISO/IEC 42001 | Status |
|---|---|---|---|
| [A named person decides; no agent can](#human-decision-gate) | GOVERN 3.2, MAP 3.5 | A.9.2, *A.3.2*, *A.6.2.6* | Implemented |
| [Every agent is registered, and the register is held to the policies](#agent-inventory) | GOVERN 1.6, *MAP 2.1* | *A.4.2*, *A.6.2.7* | Implemented |
| [Each agent has an accountable owner](#accountable-owners) | GOVERN 2.1 | A.3.2 | Partial |
| [Intended use, boundaries and limits are written down](#intended-use-and-limits) | MAP 1.1, MAP 2.2 | A.9.4, *A.6.2.2*, *A.8.2* | Implemented |
| [Operators can tell what a model wrote and how it was produced](#transparency-to-operators) | *MEASURE 2.8*, *MAP 2.2* | *A.8.2*, *A.6.2.8* | Implemented |
| [A deny-by-default policy decides every model call](#model-use-policy) | GOVERN 1.4, *GOVERN 1.2* | A.9.2, *A.9.3*, *A.2.2* | Implemented |
| [Each agent may call only the tools it is granted](#tool-boundary) | *MEASURE 2.7*, *MAP 2.2*, *GOVERN 3.2* | *A.9.2* | Implemented |
| [Identifiers are withheld, pseudonymized and checked before a model sees a prompt](#data-leaving-the-boundary) | MEASURE 2.10, *MAP 4.2*, *MANAGE 1.3* | *A.7.6*, *A.9.2* | Partial |
| [Feed, operator and model text reaches a model as data](#untrusted-input-fencing) | MEASURE 2.7, *MAP 4.2* | *A.6.2.4* | Partial |
| [Operators and services have identities, and roles are bounded in time](#access-and-identity) | *MEASURE 2.7*, *GOVERN 3.2* | none | Implemented |
| [Calls, decisions and investigations are recorded and tamper-evident](#traceability-and-audit) | MEASURE 2.8, *MANAGE 4.1*, *GOVERN 1.4* | A.6.2.8, *A.6.2.7* | Implemented |
| [Data says where it came from, and bad data is stopped at ingest](#data-provenance-and-quality) | *MAP 2.3* | A.7.5, A.7.4 | Implemented |
| [A measured readiness verdict, with objectives set in a file](#readiness-and-evaluation) | MEASURE 1.1, MEASURE 2.3, MANAGE 1.1, *MEASURE 2.5* | A.6.2.4, *A.6.2.6* | Partial |
| [Behaviour in production is monitored and alerts reach a person](#production-monitoring) | MEASURE 2.4, MANAGE 4.1, *MEASURE 3.1* | A.6.2.6 | Implemented |
| [Targeted tests for injection and unsafe output](#adversarial-testing) | MEASURE 2.7, *MAP 5.1* | *A.6.2.4* | Partial |
| [The mission never depends on a model](#fail-safe-degradation) | MANAGE 2.1, *MANAGE 2.2* | *A.6.2.6* | Implemented |
| [The AI layer can be halted at runtime](#disengage-an-agent) | MANAGE 2.4 | *A.6.2.6* | Implemented |
| [One model provider, behind one gateway, held there by a test](#third-party-model-governance) | MANAGE 3.1, *MANAGE 3.2*, *GOVERN 6.1*, *GOVERN 6.2*, *MAP 4.1* | *A.10.3*, *A.10.2* | Partial |
| [Prompts, policies and dependencies change through review and CI](#change-control) | *MANAGE 4.2*, *MAP 4.2* | *A.6.2.3*, *A.4.4*, *A.10.3* | Implemented |
| [Alerts reach people, each with a runbook](#alerts-reach-people) | *MANAGE 4.3*, *MANAGE 2.3*, *GOVERN 4.3* | *A.6.2.6* | Implemented |
| [No outward incident communication, and no channel to report a concern](#incident-communication-and-concerns) | *MANAGE 4.3*, *GOVERN 5.1* | A.8.4, A.3.3, *A.8.3* | Not covered |
| [An operator's override is the appeal, and it is measured](#operator-override-feedback) | MANAGE 4.1, *MEASURE 3.3* | none | Implemented |
| [An operator cannot flag an output as wrong](#problem-reporting-channel) | MEASURE 3.3, *GOVERN 5.2* | A.3.3 | Not covered |
| [The data provider's handling rules are enforced at ingest](#provider-data-handling-rules) | *GOVERN 1.1*, *MAP 1.1* | *A.7.3* | Implemented |
| [Limits and known defects are written down](#residual-risk-documented) | MANAGE 1.4, *MAP 5.1* | none | Partial |
| [No AI system impact assessment](#impact-assessment) | MAP 5.1 | A.5.2, A.5.3, A.5.4, A.5.5 | Not covered |
| [No procedure for retiring an agent or a model](#decommissioning) | GOVERN 1.7 | none | Not covered |
| [Not assessed; the agents do not decide about people](#fairness-and-bias) | MEASURE 2.11 | *A.5.4* | Not applicable |
| [An AI policy, management review and internal audit](#ai-policy-and-management-system) | *GOVERN 1.2*, *GOVERN 1.3*, *GOVERN 1.5* | A.2.2, A.2.4, clause 5, clause 9, clause 10 | Organizational |
| [Competence, awareness and training](#competence-and-training) | GOVERN 2.2 | *A.4.6*, clause 7 | Organizational |
| [A register of the laws and regulations that apply to the AI layer](#legal-requirements-register) | GOVERN 1.1 | *clause 4* | Organizational |

<a id="human-decision-gate"></a>
### A named person decides; no agent can

**Status:** Implemented

Every critical alert is staged by rules. Only a signed-in operator with the right role approves, rejects or overrides, and the execution gate re-checks the role before it writes. An agent's attempt to decide, write or stage is blocked by the tool policy.

**NIST AI RMF:** GOVERN 3.2, MAP 3.5

**ISO/IEC 42001:** A.9.2 Processes for responsible use of AI systems, *A.3.2 AI roles and responsibilities*, *A.6.2.6 AI system operation and monitoring*

**Evidence:**

- `nas-feedops-sentinel/src/execution_gate.py`
- `nas-feedops-sentinel/tests/test_execution_gate.py`
- `nas-feedops-sentinel/tests/test_authorization.py`
- `nas-feedops-sentinel/tests/test_tool_boundary.py`

<a id="agent-inventory"></a>
### Every agent is registered, and the register is held to the policies

**Status:** Implemented

policies/agent-register.yaml lists each agent with its purpose, services, tool grants, data sent, fallback and authority. Tests fail when the register and the two policies or the code disagree.

**NIST AI RMF:** GOVERN 1.6, *MAP 2.1*

**ISO/IEC 42001:** *A.4.2 Resource documentation*, *A.6.2.7 AI system technical documentation*

**Note:** Covers this system's agents. An inventory across an organization's AI systems is outside this repository.

**Evidence:**

- `nas-feedops-sentinel/policies/agent-register.yaml`
- `nas-feedops-sentinel/tests/test_agent_register.py`

<a id="accountable-owners"></a>
### Each agent has an accountable owner

**Status:** Partial

Every register entry names an owner and a last-reviewed date, and the schema refuses a blank owner.

**NIST AI RMF:** GOVERN 2.1

**ISO/IEC 42001:** A.3.2 AI roles and responsibilities

**Note:** Every owner is the role "Sentinel maintainer". A named person, and lines of communication, are not recorded.

**Evidence:**

- `nas-feedops-sentinel/policies/agent-register.yaml`
- `nas-feedops-sentinel/tests/test_agent_register.py`

<a id="intended-use-and-limits"></a>
### Intended use, boundaries and limits are written down

**Status:** Implemented

The README's boundaries section and the governance guide say what the system watches, what a model may and may not do, and what is held back, and the register states each agent's data and fallback.

**NIST AI RMF:** MAP 1.1, MAP 2.2

**ISO/IEC 42001:** A.9.4 Intended use of the AI system, *A.6.2.2 AI system requirements and specification*, *A.8.2 System documentation and information for users*

**Evidence:**

- `README.md`
- `nas-feedops-sentinel/docs/agent-governance.md`
- `nas-feedops-sentinel/docs/playbook.md`
- `nas-feedops-sentinel/docs/walkthrough.md`

<a id="transparency-to-operators"></a>
### Operators can tell what a model wrote and how it was produced

**Status:** Implemented

Briefs are labelled LLM-assisted or deterministic, every model call records its prompt hash, version, input hash and redaction count, and a verdict carries the id of the call behind it.

**NIST AI RMF:** *MEASURE 2.8*, *MAP 2.2*

**ISO/IEC 42001:** *A.8.2 System documentation and information for users*, *A.6.2.8 AI system recording of event logs*

**Evidence:**

- `nas-feedops-sentinel/dashboard/lib/brief_view.py`
- `nas-feedops-sentinel/tests/test_brief_view.py`
- `nas-feedops-sentinel/tests/test_model_call_provenance.py`

<a id="model-use-policy"></a>
### A deny-by-default policy decides every model call

**Status:** Implemented

The Microsoft Agent Governance Toolkit evaluates policies/model-use.yaml before each call: role, gateway alias, prompt and output limits, credentials, protected values. A refusal sends nothing and is recorded.

**NIST AI RMF:** GOVERN 1.4, *GOVERN 1.2*

**ISO/IEC 42001:** A.9.2 Processes for responsible use of AI systems, *A.9.3 Objectives for responsible use of AI systems*, *A.2.2 AI policy*

**Note:** This is the technical enforcement of a policy. The organizational AI policy ISO/IEC 42001 asks for is separate (see ai-policy-and-management-system).

**Evidence:**

- `nas-feedops-sentinel/policies/model-use.yaml`
- `nas-feedops-sentinel/src/governance.py`
- `nas-feedops-sentinel/tests/test_governance.py`

<a id="tool-boundary"></a>
### Each agent may call only the tools it is granted

**Status:** Implemented

policies/tool-use.yaml grants each agent its reads and its one response tool. Deciding, executing and writing are blocked, and a call made outside any named agent is refused.

**NIST AI RMF:** *MEASURE 2.7*, *MAP 2.2*, *GOVERN 3.2*

**ISO/IEC 42001:** *A.9.2 Processes for responsible use of AI systems*

**Evidence:**

- `nas-feedops-sentinel/policies/tool-use.yaml`
- `nas-feedops-sentinel/src/agent_tools.py`
- `nas-feedops-sentinel/tests/test_tool_boundary.py`

<a id="data-leaving-the-boundary"></a>
### Identifiers are withheld, pseudonymized and checked before a model sees a prompt

**Status:** Partial

Models receive a bounded projection, flights as per-prompt tokens, a pattern scrub at one chokepoint, and a check that the values the caller read from its records are not in the finished prompt. A hit blocks the call.

**NIST AI RMF:** MEASURE 2.10, *MAP 4.2*, *MANAGE 1.3*

**ISO/IEC 42001:** *A.7.6 Data preparation*, *A.9.2 Processes for responsible use of AI systems*

**Note:** Pattern- and value-based, not a guarantee: the check knows only the values callers extracted, and short codes are not checked by value. A second, independent detector is the recorded next step. Privacy management proper sits in ISO/IEC 27701, outside ISO/IEC 42001.

**Evidence:**

- `nas-feedops-sentinel/src/redaction.py`
- `nas-feedops-sentinel/tests/test_redaction.py`
- `nas-feedops-sentinel/tests/test_outbound_leak_check.py`

<a id="untrusted-input-fencing"></a>
### Feed, operator and model text reaches a model as data

**Status:** Partial

Free text from outside is cut to a limit and wrapped in untrusted_data tags that it cannot close, and every system prompt says what the fence means. Model output is checked by a grounding gate before it is stored.

**NIST AI RMF:** MEASURE 2.7, *MAP 4.2*

**ISO/IEC 42001:** *A.6.2.4 AI system verification and validation*

**Note:** Held by targeted tests. There is no systematic adversarial corpus run against every role yet.

**Evidence:**

- `nas-feedops-sentinel/src/untrusted.py`
- `nas-feedops-sentinel/tests/test_untrusted.py`
- `nas-feedops-sentinel/tests/test_guardrails_gate_eval.py`

<a id="access-and-identity"></a>
### Operators and services have identities, and roles are bounded in time

**Status:** Implemented

Keycloak signs operators in with realm roles that expire, services use short-lived SPIFFE identities to reach Vault over mutual TLS, and the sign-in and every decision are attributed to a person.

**NIST AI RMF:** *MEASURE 2.7*, *GOVERN 3.2*

**Note:** Security controls sit in ISO/IEC 27001, outside ISO/IEC 42001, so no Annex A control is mapped. An agent has no identity of its own: it is a name inside its service's identity (see the agent register).

**Evidence:**

- `nas-feedops-sentinel/tests/test_keycloak_auth.py`
- `nas-feedops-sentinel/tests/test_session_and_attribution.py`
- `nas-feedops-sentinel/docs/operations.md`

<a id="traceability-and-audit"></a>
### Calls, decisions and investigations are recorded and tamper-evident

**Status:** Implemented

Every model call is a record; every status change goes to an append-only audit log whose table refuses UPDATE and DELETE; every investigation is sealed with SHA-256 and re-verified, and every claim cites evidence.

**NIST AI RMF:** MEASURE 2.8, *MANAGE 4.1*, *GOVERN 1.4*

**ISO/IEC 42001:** A.6.2.8 AI system recording of event logs, *A.6.2.7 AI system technical documentation*

**Evidence:**

- `nas-feedops-sentinel/src/model_telemetry.py`
- `nas-feedops-sentinel/tests/test_model_telemetry.py`
- `nas-feedops-sentinel/tests/test_e2e_proof.py`

<a id="data-provenance-and-quality"></a>
### Data says where it came from, and bad data is stopped at ingest

**Status:** Implemented

Replayed, synthetic and API data is tagged data_origin by a database constraint and flagged on every page; events are validated against schema and business rules at ingest.

**NIST AI RMF:** *MAP 2.3*

**ISO/IEC 42001:** A.7.5 Data provenance, A.7.4 Quality of data for AI systems

**Evidence:**

- `nas-feedops-sentinel/tests/test_data_origin.py`
- `nas-feedops-sentinel/tests/test_data_origin_constraint.py`
- `nas-feedops-sentinel/src/validators.py`
- `nas-feedops-sentinel/tests/test_validators.py`

<a id="readiness-and-evaluation"></a>
### A measured readiness verdict, with objectives set in a file

**Status:** Partial

`python -m src.readiness` grades the last window against model-slos.yaml (grounding, decision quality, latency, cost, override rate, calibration and more) and returns one verdict, usable as a deployment gate.

**NIST AI RMF:** MEASURE 1.1, MEASURE 2.3, MANAGE 1.1, *MEASURE 2.5*

**ISO/IEC 42001:** A.6.2.4 AI system verification and validation, *A.6.2.6 AI system operation and monitoring*

**Note:** The targets are starting points, set from what an operations centre needs, not from a measured baseline.

**Evidence:**

- `nas-feedops-sentinel/src/readiness.py`
- `nas-feedops-sentinel/policies/model-slos.yaml`
- `nas-feedops-sentinel/src/evaluation.py`
- `nas-feedops-sentinel/tests/test_readiness_report.py`
- `nas-feedops-sentinel/tests/test_evaluation.py`

<a id="production-monitoring"></a>
### Behaviour in production is monitored and alerts reach a person

**Status:** Implemented

Prometheus alert rules cover policy refusals, blocked leaks, an agent reaching for the human gate, model error rates and spend, Alertmanager posts to Slack, and the alerting path watches itself.

**NIST AI RMF:** MEASURE 2.4, MANAGE 4.1, *MEASURE 3.1*

**ISO/IEC 42001:** A.6.2.6 AI system operation and monitoring

**Evidence:**

- `nas-feedops-sentinel/monitoring/alert-rules.yml`
- `nas-feedops-sentinel/docs/runbooks.md`
- `nas-feedops-sentinel/tests/test_alertmanager_wiring.py`

<a id="adversarial-testing"></a>
### Targeted tests for injection and unsafe output

**Status:** Partial

Tests cover the fence, the guardrails, dashboard output safety (model and stored text is shown as text) and the refusal of a decide or write tool call.

**NIST AI RMF:** MEASURE 2.7, *MAP 5.1*

**ISO/IEC 42001:** *A.6.2.4 AI system verification and validation*

**Note:** Individual cases, not a systematic corpus run against every agent's input path with pass and fail tracked. That corpus is queued.

**Evidence:**

- `nas-feedops-sentinel/tests/test_untrusted.py`
- `nas-feedops-sentinel/tests/test_guardrails_gate_eval.py`
- `nas-feedops-sentinel/tests/test_dashboard_output_safety.py`

<a id="fail-safe-degradation"></a>
### The mission never depends on a model

**Status:** Implemented

Alerts are staged and the deterministic rules run whether the model answers, fails, refuses or is not configured. A circuit breaker per model alias, a per-pass token budget and one retry layer bound what a failing gateway can cost, and every fallback is recorded.

**NIST AI RMF:** MANAGE 2.1, *MANAGE 2.2*

**ISO/IEC 42001:** *A.6.2.6 AI system operation and monitoring*

**Evidence:**

- `nas-feedops-sentinel/src/llm_guard.py`
- `nas-feedops-sentinel/tests/test_llm_budget.py`
- `nas-feedops-sentinel/tests/test_critic_agent.py`

<a id="disengage-an-agent"></a>
### The AI layer can be halted at runtime

**Status:** Implemented

A supervisor halts every model call from the console, with a reason, and resumes it the same way. The model-use policy refuses every call while it is halted, in every service within a couple of seconds and without a restart; alerts are still staged and the rules still run. The database audits every change, a state that cannot be read halts, a banner shows on every page, and a critical alert fires. An optional automatic trip, off by default, halts after repeated refusals that signal a real fault and is just as visible. Each register entry also records the setting that stops one agent on a restart.

**NIST AI RMF:** MANAGE 2.4

**ISO/IEC 42001:** *A.6.2.6 AI system operation and monitoring*

**Note:** One switch for the whole AI layer; there is no switch for a single agent at runtime. The automatic trip counts refusals per process.

**Evidence:**

- `nas-feedops-sentinel/src/model_halt.py`
- `nas-feedops-sentinel/tests/test_model_halt.py`
- `nas-feedops-sentinel/tests/test_model_halt_page.py`
- `nas-feedops-sentinel/policies/model-use.yaml`
- `nas-feedops-sentinel/policies/agent-register.yaml`

<a id="third-party-model-governance"></a>
### One model provider, behind one gateway, held there by a test

**Status:** Partial

Every call goes through a single gateway by alias; a setting that names a provider directly is refused at start-up; CI fails if a second provider appears. Known defects in the governance toolkit are pinned as strict expected failures.

**NIST AI RMF:** MANAGE 3.1, *MANAGE 3.2*, *GOVERN 6.1*, *GOVERN 6.2*, *MAP 4.1*

**ISO/IEC 42001:** *A.10.3 Suppliers*, *A.10.2 Allocation of responsibilities*

**Note:** No supplier assessment, contract terms or exit plan is recorded. Claude Haiku through the public API is a test configuration, not an authorized boundary for FAA operational data.

**Evidence:**

- `instraclustr/litellm-config.yaml`
- `instraclustr/tests/test_model_provider.py`
- `nas-feedops-sentinel/tests/test_llm_proxy_guard.py`
- `nas-feedops-sentinel/tests/test_agt_known_bugs.py`

<a id="change-control"></a>
### Prompts, policies and dependencies change through review and CI

**Status:** Implemented

CODEOWNERS, CI on every pull request, hash-locked dependencies with pip-audit, an image scan, secret scanning over history, and a register that fails when a prompt's version moves without a review.

**NIST AI RMF:** *MANAGE 4.2*, *MAP 4.2*

**ISO/IEC 42001:** *A.6.2.3 Documentation of AI system design and development*, *A.4.4 Tooling resources*, *A.10.3 Suppliers*

**Note:** Branch protection is not available on the current plan; CODEOWNERS and CI gates stand in for it.

**Evidence:**

- `.github/CODEOWNERS`
- `.github/workflows/pyapp-ci.yml`
- `nas-feedops-sentinel/cve-allowlist.md`
- `nas-feedops-sentinel/tests/test_agent_register.py`

<a id="alerts-reach-people"></a>
### Alerts reach people, each with a runbook

**Status:** Implemented

Prometheus alert rules cover policy refusals, blocked leaks and an agent reaching for the human gate. Alertmanager groups them and posts to Slack, firing and resolved, with a reminder while one keeps firing, and each message names its runbook. The alerting path watches itself, and CI delivers a real alert to a stand-in for Slack. docs/runbooks.md has one runbook per alert rule, and a test holds it.

**NIST AI RMF:** *MANAGE 4.3*, *MANAGE 2.3*, *GOVERN 4.3*

**ISO/IEC 42001:** *A.6.2.6 AI system operation and monitoring*

**Note:** The people told are the operating team, the AI actors this system has. Telling anyone outside it, and a record kept after an incident closes, are listed in incident-communication-and-concerns.

**Evidence:**

- `nas-feedops-sentinel/monitoring/alertmanager.yml`
- `nas-feedops-sentinel/monitoring/alert-rules.yml`
- `nas-feedops-sentinel/docs/runbooks.md`
- `nas-feedops-sentinel/tests/test_alertmanager_wiring.py`
- `.github/scripts/check_alerting.py`

<a id="incident-communication-and-concerns"></a>
### No outward incident communication, and no channel to report a concern

**Status:** Not covered

Slack reaches the operating team. No process tells users of the briefs, or any party outside the team, that an incident happened, and nobody outside it has a way to report a concern about the system.

**NIST AI RMF:** *MANAGE 4.3*, *GOVERN 5.1*

**ISO/IEC 42001:** A.8.4 Communication of incidents, A.3.3 Reporting of concerns, *A.8.3 External reporting*

**Note:** Would take a decision on who the affected parties are (the controllers and managers who read a brief, a customer), a reporting address or form, a rule for who is told what and when, and a short post-incident record. Whether it applies depends on who uses the system beyond its operators.

<a id="operator-override-feedback"></a>
### An operator's override is the appeal, and it is measured

**Status:** Implemented

An operator approves, rejects or overrides what the system staged, with the decision and the reason recorded. The override rate, missed and false escalation rates and calibration error are computed from those decisions, and the readiness verdict uses them.

**NIST AI RMF:** MANAGE 4.1, *MEASURE 3.3*

**Note:** An override appeals a recommendation. It does not report that a brief or a verdict was wrong and why: that is listed in problem-reporting-channel.

**Evidence:**

- `nas-feedops-sentinel/src/evaluation_metrics.py`
- `nas-feedops-sentinel/policies/model-slos.yaml`
- `nas-feedops-sentinel/src/execution_gate.py`
- `nas-feedops-sentinel/tests/test_execution_gate.py`

<a id="problem-reporting-channel"></a>
### An operator cannot flag an output as wrong

**Status:** Not covered

There is no way to mark a brief, a verdict or a planner answer as wrong, with a reason, other than deciding on the staged action.

**NIST AI RMF:** MEASURE 3.3, *GOVERN 5.2*

**ISO/IEC 42001:** A.3.3 Reporting of concerns

**Note:** Would take a "flag this output" control on the console that writes a record linked to the model call, counted in the evaluation metrics, and a place where a maintainer reviews the flags.

<a id="provider-data-handling-rules"></a>
### The data provider's handling rules are enforced at ingest

**Status:** Implemented

Messages the FAA flags blockAtFAA are withheld (nothing stored, shown or sent to a model), and beacon codes from flight plans that are not yet active are neither stored nor shown.

**NIST AI RMF:** *GOVERN 1.1*, *MAP 1.1*

**ISO/IEC 42001:** *A.7.3 Acquisition of data*

**Note:** These are the data provider's terms. Which laws and regulations govern the AI layer itself is listed in legal-requirements-register.

**Evidence:**

- `nas-feedops-sentinel/src/swim_file_ingest.py`
- `nas-feedops-sentinel/tests/test_swim_kit_ingest.py`
- `nas-feedops-sentinel/tests/test_ingest_source_seam.py`
- `README.md`

<a id="residual-risk-documented"></a>
### Limits and known defects are written down

**Status:** Partial

The governance guide and the register state what each control does not do, a whole-codebase review is recorded in results.md with its findings, and defects in the governance toolkit are pinned as tests.

**NIST AI RMF:** MANAGE 1.4, *MAP 5.1*

**Note:** There is no risk register with likelihood, impact and treatment per risk.

**Evidence:**

- `nas-feedops-sentinel/docs/agent-governance.md`
- `results.md`
- `nas-feedops-sentinel/tests/test_agt_known_bugs.py`

<a id="impact-assessment"></a>
### No AI system impact assessment

**Status:** Not covered

No assessment of the system's impact on individuals, groups or society is recorded.

**NIST AI RMF:** MAP 5.1

**ISO/IEC 42001:** A.5.2 AI system impact assessment process, A.5.3 Documentation of AI system impact assessments, A.5.4 Assessing impacts on individuals or groups, A.5.5 Assessing societal impacts of AI systems

**Note:** Would take a documented process and a first assessment of this system: who is affected by a wrong or missing brief (operators, controllers, flight crews downstream), and how.

<a id="decommissioning"></a>
### No procedure for retiring an agent or a model

**Status:** Not covered

Removing an agent means removing its policy lines, prompt and register entry; no procedure says how safely.

**NIST AI RMF:** GOVERN 1.7

**Note:** Would take a short runbook and a register status field (active, suspended, retired).

<a id="fairness-and-bias"></a>
### Not assessed; the agents do not decide about people

**Status:** Not applicable

The agents advise on feed trust and aviation data. No output decides about, ranks or profiles an individual.

**NIST AI RMF:** MEASURE 2.11

**ISO/IEC 42001:** *A.5.4 Assessing impacts on individuals or groups*

**Note:** Revisit if an agent starts to handle personal data or to act on a person.

<a id="ai-policy-and-management-system"></a>
### An AI policy, management review and internal audit

**Status:** Organizational

ISO/IEC 42001 asks an organization for an AI policy, defined scope and objectives, management review, internal audit and continual improvement. None of those is a property of a code repository.

**NIST AI RMF:** *GOVERN 1.2*, *GOVERN 1.3*, *GOVERN 1.5*

**ISO/IEC 42001:** A.2.2 AI policy, A.2.4 Review of the AI policy, clause 5 (Leadership), clause 9 (Performance evaluation), clause 10 (Improvement)

**Note:** The repository can supply evidence to a management system; the system, its owner and its audit cycle are not here.

<a id="competence-and-training"></a>
### Competence, awareness and training

**Status:** Organizational

No training, competence or awareness records for the people who build, run or oversee the system.

**NIST AI RMF:** GOVERN 2.2

**ISO/IEC 42001:** *A.4.6 Human resources*, clause 7 (Support)

**Note:** Operator guidance exists (the playbook), but training and competence are an organization's to record.

<a id="legal-requirements-register"></a>
### A register of the laws and regulations that apply to the AI layer

**Status:** Organizational

No list says which laws, regulations and contract terms govern this system's AI layer, which apply, and which do not. Counsel decides that; the repository can hold the result and test that each entry has an owner.

**NIST AI RMF:** GOVERN 1.1

**ISO/IEC 42001:** *clause 4 (Context of the organization)*

**Note:** The EU AI Act, a federal buyer's AI requirements and any sector rules need a person to decide whether they apply. The register can then record each as applies, does not apply, or confirm with counsel.

## Not referenced by any control

These are not necessarily gaps in the system: many are organizational (workforce, culture, engagement with affected communities) and say nothing about a code repository. They are listed so a reader can see what the mapping does not touch.

- **Govern:** GOVERN 2.3, GOVERN 3.1, GOVERN 4.1, GOVERN 4.2
- **Map:** MAP 1.2, MAP 1.3, MAP 1.4, MAP 1.5, MAP 1.6, MAP 3.1, MAP 3.2, MAP 3.3, MAP 3.4, MAP 5.2
- **Measure:** MEASURE 1.2, MEASURE 1.3, MEASURE 2.1, MEASURE 2.2, MEASURE 2.6, MEASURE 2.9, MEASURE 2.12, MEASURE 2.13, MEASURE 3.2, MEASURE 4.1, MEASURE 4.2, MEASURE 4.3
- **Manage:** MANAGE 1.2
- **ISO/IEC 42001 Annex A:** A.2.3, A.4.3, A.4.5, A.6.1.2, A.6.1.3, A.6.2.5, A.7.2, A.8.5, A.10.4

To regenerate this file: `python -m src.framework_mapping --markdown > docs/framework-mapping.md`.
