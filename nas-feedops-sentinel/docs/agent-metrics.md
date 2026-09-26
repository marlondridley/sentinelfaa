# Agent metrics: where each one comes from

The Microsoft Agent Governance Toolkit (AGT) computes and governs. OpenTelemetry
carries the data to Prometheus or any OTLP collector. A metric is built in this
repository only for one of two reasons:

- **AGT bug.** A test in `tests/test_agt_known_bugs.py` shows AGT returning
  the wrong value. Those tests are strict xfails. If an AGT release fixes the
  bug, the test passes, the suite fails, and the workaround can go.
- **No equivalent.** AGT has no metric that measures this.

Agents never grade themselves. The flow is: execution telemetry, then the
evaluation layer, then quality metrics, then the dashboard. Every judgment
comes from evidence the agents do not control:
- the grounding gate's evidence-id check;
- the operator's recorded decision;
- the gateway's usage and cost figures;
- the sealed record's hash.

Versions audited: `agent-os-kernel` 3.7.0 and `agent-sre` 3.7.0, the newest
releases on PyPI. Both are pure-Python wheels (`py3-none-any`) and install on
the arm64 Docker host. `agent-control-specification` ships an x86_64-only
wheel and is not used.

## AGT components used

| Package | Component | Used for | How it is used safely |
|---|---|---|---|
| agent-os-kernel | `policies.PolicyEvaluator`, `PolicyDocument` | model-use and tool-call policy decisions | `src/governance.py` rejects unknown keys, any policy that is not deny-by-default, and `tool_allowlist` (bugs K, L, M) |
| agent-os-kernel | `credential_redactor.CredentialRedactor` | credentials in prompts | as is |
| agent-os-kernel | `audit_logger.GovernanceAuditLogger` + `otel_audit_backend.OTelLogsBackend` | every policy decision, as an OTel log record under `agt.*` | as is |
| agent-os-kernel | `metrics.GovernanceMetrics` | policy compliance: checks, approvals, violations, blocked | one instance per window, fed from the durable record |
| agent-os-kernel | `policies.budget.BudgetPolicy` / `BudgetTracker` | per-investigation cost, token, tool-call and duration gates | as is |
| agent-sre | `fleet.FleetManager` | per-agent and per-provider success rate, mean latency, spend, invocation count | one fleet per window |
| agent-sre | `slo.indicators.ResponseLatency` | p50 / p95 / p99 of every latency | one instance per window and percentile; `compliance()` is never read (bugs E, F) |
| agent-sre | `slo.indicators.TaskSuccessRate`, `HallucinationRate` | investigation completion, unsupported-claim rate | one instance per window, read through `collect()`; `current_value()` is never read (bug C) |
| agent-sre | `slo.indicators.DelegationChainDepth` | orchestration depth against its maximum | as is (its `compliance()` is correctly lower-is-better) |
| agent-sre | `cost.guard.CostGuard` | spend and mean cost per invocation, investigation, alert and model | one guard per window and dimension |
| agent-sre | `evals.EvaluationEngine`, `EvalSuite` | decision quality score, evidence grounding, recommendation accuracy, the quality gate | Sentinel supplies the judge (grounding gate, operator decision); empty history reads as no data, a judge error on a required criterion fails the report, `get_stats()` is never read (bugs H, I, J) |
| agent-sre | `tracing.metrics.create_agent_metrics` | live `agent.tasks.total`, `agent.tool_calls.total`, `agent.policy.violations`, `agent.task.duration`, `agent.llm.latency`, `agent.tool.latency` | exported through OTel |
| agent-sre | `integrations.otel.MetricsExporter` | live `agent.sre.sli.value`, `agent.sre.cost.total_usd`, `agent.sre.latency_ms` | `record_sli`, `record_cost` and `record_latency` only; `record_slo_from_object()` exports the buggy values and is never called |

OpenTelemetry supplies the GenAI semantic conventions:
- `chat`, `invoke_agent` and `invoke_workflow` spans;
- `gen_ai.client.operation.duration`;
- `gen_ai.client.token.usage`.

These are the provider-neutral definition of a model call. DeepSeek, Gemini,
OpenAI and Claude are measured the same way.

## AGT components audited and not used

| Component | Why not |
|---|---|
| agent-sre `slo.objectives.ErrorBudget`, `SLO` | Bugs A, B and SLO: one failure in 100 against a 95% objective reads as an exhausted budget with a burn rate of 518,400. The readiness verdict uses `src/readiness.py`. |
| agent-sre `CalibrationDeltaSLI` | Bug G: over- and under-confidence cancel. See the ECE row below. |
| agent-sre `CostPerTask` | Bugs D, F. `CostGuard` computes the same mean correctly. |
| agent-sre `PolicyCompliance`, `ToolCallAccuracy` | Bug C. `GovernanceMetrics` counts policy checks correctly. |
| agent-sre `evals.RulesJudge` built-in rules | Its hallucination rule scores word overlap and hedging. Sentinel's grounding gate checks each cited evidence id exactly. |
| agent-os-kernel `context_budget.ContextScheduler` | It allocates token budgets with a fixed 90/10 lookup/reasoning split. It does not measure prompt size or its growth. |
| agent-os-kernel `task_outcome.TaskOutcomeRecorder` | It is a trust-score heuristic, not a measurement in this design. |
| agent-os-kernel `shift_left_metrics` | It tracks the development-lifecycle stage where a violation was caught. Sentinel's violations are all at runtime. |
| agent-os-kernel `_mcp_metrics` | It is for an MCP gateway. Sentinel's agents call no MCP tools. |
| agent-os-kernel `circuit_breaker`, `retry`, `health` | They are behaviours (breaking, retrying, health checks), not metrics. Retries are tenacity's, counted per call in the durable record. |
| agent-sre incidents, alerts, chaos, delivery, replay, k8s, certification, benchmarks | These are operational tooling, not measurements in this design. |

## Metric by metric

| Group | Metric | Source |
|---|---|---|
| Decision quality | Decision quality score | AGT `EvaluationEngine` mean score over each investigation's evaluation suite |
| | Evidence grounding | AGT `EvaluationEngine` hallucination-criterion score, judged by the grounding gate |
| | Recommendation accuracy | AGT `EvaluationEngine` correctness-criterion score, judged against the operator's decision |
| | False-escalation rate | in-repo: no equivalent |
| | Missed-escalation rate | in-repo: no equivalent |
| | Confidence calibration (ECE) | in-repo: AGT bug `test_agt_calibration_delta_detects_offsetting_miscalibration` |
| | Contradiction rate | in-repo: no equivalent |
| | Unsupported-claim rate | AGT `HallucinationRate` via `collect()`, one evaluation per model claim, judged by the grounding gate |
| | Human override rate | in-repo: no equivalent |
| | Evidence improvement | in-repo: no equivalent |
| Latency | Per agent, P50/P95/P99 | AGT `ResponseLatency`; live AGT `agent.task.duration` |
| | Per investigation (end-to-end) | AGT `ResponseLatency`; live OTel `invoke_workflow` span |
| | Model time | AGT `ResponseLatency`; live OTel `gen_ai.client.operation.duration` and AGT `agent.llm.latency` |
| | Tool / evidence-retrieval time | AGT `ResponseLatency` over evidence collection; live AGT `agent.tool.latency` |
| | Handoff time | in-repo: no equivalent |
| | Queue time (alert to investigation) | AGT `ResponseLatency` |
| | Time to human decision | AGT `ResponseLatency` |
| | Timeout rate | in-repo: no equivalent (AGT has no provider error taxonomy) |
| | Retry rate | in-repo: no equivalent |
| Cost | Tokens in / out / total | measured by the gateway; live OTel `gen_ai.client.token.usage`; per investigation AGT `BudgetTracker` |
| | Per invocation, by role | AGT `CostGuard` |
| | Per investigation | AGT `CostGuard` |
| | Per alert | AGT `CostGuard` |
| | Per resolved investigation | in-repo: no equivalent (all spend divided by the resolved subset) |
| | Per useful evidence item | in-repo: no equivalent |
| | By model | AGT `CostGuard`, keyed by served model |
| | Retry cost, failed-call cost | in-repo: no equivalent |
| Reliability | Success and failure rate, per agent and overall | AGT `FleetManager` |
| | Provider availability | AGT `FleetManager`, keyed by provider |
| | Timeout, retry and fallback rates | in-repo: no equivalent |
| | Schema / validation failures, malformed output | in-repo: no equivalent (outcome classes in `src/model_telemetry.py`) |
| | Investigation completion | AGT `TaskSuccessRate` via `collect()` |
| Orchestration | Agents invoked | AGT `FleetManager`; live AGT `agent.tasks.total` |
| | Tool calls | AGT `BudgetTracker`; live AGT `agent.tool_calls.total` |
| | Handoffs | in-repo: no equivalent |
| | Dependency failures | in-repo: no equivalent |
| | Duplicate work | in-repo: no equivalent |
| | Dead-end investigations | in-repo: no equivalent |
| | Context size and growth | in-repo: no equivalent |
| | Max depth | AGT `DelegationChainDepth` |
| | Termination reason | in-repo: no equivalent |
| Governance gates | Quality | AGT `EvaluationEngine` report with grounding as a required criterion, plus the in-repo traceability check (no equivalent) |
| | Safety | AGT `PolicyEvaluator` decisions, audited through `GovernanceAuditLogger`, plus an in-repo check that only a verified human decided (no equivalent: AGT's `EscalationManager` is in-memory, not a durable RBAC-verified decision) |
| | Cost | AGT `BudgetTracker` (`max_cost_usd`, `max_tokens`, `max_tool_calls`) |
| | Latency | AGT `BudgetTracker` (`max_duration_seconds`) |
| | Audit | in-repo: no equivalent (sealed-record SHA-256, provenance hashes, complete call records) |
| | Policy compliance rate | AGT `GovernanceMetrics` |
| Readiness | Error budget, verdict | in-repo `src/readiness.py`: AGT bugs `test_agt_error_budget_one_failure_in_a_hundred_is_not_exhausted`, `test_agt_error_budget_burn_rate_is_error_rate_over_allowed_rate`, `test_agt_slo_at_99_percent_against_95_is_not_exhausted` |

## AGT bugs, with the test that shows each

All against 3.7.0. Each is a strict xfail in `tests/test_agt_known_bugs.py`.

| | Component | Bug | Test | Sentinel's answer |
|---|---|---|---|---|
| A | agent-sre `ErrorBudget.record_event()` | adds 1.0 per failure against a budget of `1 - target` | `test_agt_error_budget_one_failure_in_a_hundred_is_not_exhausted` | in-repo readiness evaluator |
| B | agent-sre `ErrorBudget.burn_rate()` | divides the budget by the window in seconds | `test_agt_error_budget_burn_rate_is_error_rate_over_allowed_rate` | in-repo readiness evaluator |
| — | agent-sre `SLO.evaluate()` | EXHAUSTED at 99% against 95%, because of A | `test_agt_slo_at_99_percent_against_95_is_not_exhausted` | in-repo readiness evaluator |
| C | agent-sre `TaskSuccessRate`, `ToolCallAccuracy`, `PolicyCompliance`, `HallucinationRate` | `current_value()` averages running rates: 99% reads as 94.8%, 1% as 5.2% | `test_agt_rate_sli_current_value_is_the_rate[...]`, `test_agt_hallucination_rate_current_value_is_the_rate` | a fresh SLI per window, read through `collect()` |
| D | agent-sre `CostPerTask` | `current_value()` averages running averages | `test_agt_cost_per_task_current_value_is_the_mean_cost` | `CostGuard` |
| E | agent-sre `ResponseLatency` | `current_value()` ignores its window | `test_agt_latency_outside_the_window_is_not_current` | a fresh instance per window |
| F | agent-sre `ResponseLatency`, `CostPerTask` | `compliance()` treats lower-is-better values as higher-is-better | `test_agt_latency_under_its_target_is_compliant`, `test_agt_cost_under_its_target_is_compliant` | `compliance()` never read; grading in `src/readiness.py` |
| G | agent-sre `CalibrationDeltaSLI` | over- and under-confidence cancel | `test_agt_calibration_delta_detects_offsetting_miscalibration` | in-repo ECE (`src/agt_metrics.py`) |
| H | agent-sre `EvaluationEngine` | `pass_rate()` and `average_score()` are 0.0 with no evaluations | `test_agt_evaluation_engine_with_no_evaluations_has_no_score` | "no data" when the history is empty |
| I | agent-sre `EvaluationEngine.get_stats()` | leaves out a criterion that always scores 0 | `test_agt_evaluation_stats_keep_a_criterion_that_always_fails` | `get_stats()` never read |
| J | agent-sre `EvaluationEngine.run()` | a judge error on a required criterion does not fail the report | `test_agt_a_required_criterion_the_judge_could_not_evaluate_fails_the_report` | a judge error on a required criterion fails the report |
| K | agent-os-kernel `PolicyDocument` | ignores unknown keys (`rule:` for `rules:` loads with no rules) | `test_agt_policy_document_rejects_an_unknown_key` | `src/governance.py` refuses the policy |
| L | agent-os-kernel `PolicyDocument` | defaults to allow when `defaults` is absent | `test_agt_policy_document_without_defaults_does_not_allow_everything` | `src/governance.py` refuses the policy |
| M | agent-os-kernel `PolicyEvaluator` | `PolicyDocument.tool_allowlist` is documented as enforced by the evaluator, and never read | `test_agt_policy_evaluator_enforces_the_tool_allowlist` | tool access is written as rules (`policies/tool-use.yaml`); `src/governance.py` refuses a policy that sets `tool_allowlist` |
