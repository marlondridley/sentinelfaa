# Agent metrics: where each one comes from

The Microsoft Agent Governance Toolkit (AGT) computes and governs. OpenTelemetry
carries the data to Prometheus or any OTLP collector.

A metric is not adopted because an AGT metric has a similar name. Each one is
checked against:
- its **semantic definition**: what it means, including which direction is good;
- its **lifecycle**: which window it covers;
- its **population**: what is counted, and what an empty population reads as;
- its **aggregation**: how individual observations combine;
- the **installed AGT implementation**: what version 3.7.0 actually returns.

Each metric then falls into one of three categories, and every value and gate
on the Agent Evaluation page names its category first in its **Source** column
(`src/agt_metrics.py` `CATEGORIES`):

- **AGT-native.** AGT defines the semantic and implements it correctly. The
  value comes from the AGT component named, read only through the paths the
  tests show to be correct.
- **AGT-compatible.** AGT defines the semantic, but the installed version has
  a known implementation defect, so Sentinel supplies a validated calculation.
  A test in `tests/test_agt_known_bugs.py` shows each defect. Those tests are
  strict xfails: if an AGT release fixes the bug, the test passes, the suite
  fails, and the compatibility calculation can go.
- **Sentinel-specific.** The metric is a mission or workflow outcome that AGT
  does not define.

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

| Group | Metric | Category and source |
|---|---|---|
| Decision quality | Decision quality score | AGT-native: `EvaluationEngine` mean score over each investigation's evaluation suite |
| | Evidence grounding | AGT-native: `EvaluationEngine` hallucination-criterion score, judged by the grounding gate |
| | Recommendation accuracy | AGT-native: `EvaluationEngine` correctness-criterion score, judged against the operator's decision |
| | False-escalation rate | Sentinel-specific |
| | Missed-escalation rate | Sentinel-specific |
| | Confidence calibration (ECE) | AGT-compatible: ECE, while AGT `CalibrationDeltaSLI` has bug G |
| | Contradiction rate | Sentinel-specific |
| | Unsupported-claim rate | AGT-native: `HallucinationRate` via `collect()`, one evaluation per model claim, judged by the grounding gate |
| | Human override rate | Sentinel-specific |
| | Evidence improvement | Sentinel-specific |
| Latency | Per agent, P50/P95/P99 | AGT-native: `ResponseLatency`; live AGT `agent.task.duration` |
| | Per investigation (end-to-end) | AGT-native: `ResponseLatency`; live OTel `invoke_workflow` span |
| | Model time | AGT-native: `ResponseLatency`; live OTel `gen_ai.client.operation.duration` and AGT `agent.llm.latency` |
| | Tool / evidence-retrieval time | AGT-native: `ResponseLatency` over evidence collection; live AGT `agent.tool.latency` |
| | Handoff time | Sentinel-specific |
| | Queue time (alert to investigation) | AGT-native: `ResponseLatency` |
| | Time to human decision | AGT-native: `ResponseLatency` |
| | Timeout rate | Sentinel-specific (AGT has no provider error taxonomy) |
| | Retry rate | Sentinel-specific |
| Cost | Tokens in / out / total | AGT-native: `BudgetTracker` per investigation, over the gateway's counts; live OTel `gen_ai.client.token.usage` |
| | Per invocation, by role | AGT-native: `CostGuard` |
| | Per investigation | AGT-native: `CostGuard` |
| | Per alert | AGT-native: `CostGuard` |
| | Per resolved investigation | Sentinel-specific (all spend divided by the resolved subset) |
| | Per useful evidence item | Sentinel-specific |
| | By model | AGT-native: `CostGuard`, keyed by served model |
| | Retry cost, failed-call cost | Sentinel-specific |
| Reliability | Success and failure rate, per agent and overall | AGT-native: `FleetManager` |
| | Provider availability | AGT-native: `FleetManager`, keyed by provider |
| | Timeout, retry and fallback rates | Sentinel-specific |
| | Schema / validation failures, malformed output | Sentinel-specific (outcome classes in `src/model_telemetry.py`) |
| | Investigation completion | AGT-native: `TaskSuccessRate` via `collect()` |
| Orchestration | Agents invoked | AGT-native: `FleetManager`; live AGT `agent.tasks.total` |
| | Tool calls | AGT-native: `BudgetTracker`; live AGT `agent.tool_calls.total` |
| | Handoffs | Sentinel-specific |
| | Dependency failures | Sentinel-specific |
| | Duplicate work | Sentinel-specific |
| | Dead-end investigations | Sentinel-specific |
| | Context size and growth | Sentinel-specific |
| | Max depth | AGT-native: `DelegationChainDepth` |
| | Termination reason | Sentinel-specific |
| Governance gates | Quality | AGT-native: `EvaluationEngine` report with grounding as a required criterion, plus a Sentinel-specific evidence-id traceability check |
| | Safety | AGT-native: `PolicyEvaluator` decisions, audited through `GovernanceAuditLogger`, plus a Sentinel-specific check that only a verified human decided (AGT's `EscalationManager` is in-memory, not a durable RBAC-verified decision) |
| | Cost | AGT-native: `BudgetTracker` (`max_cost_usd`, `max_tokens`, `max_tool_calls`) |
| | Latency | AGT-native: `BudgetTracker` (`max_duration_seconds`) |
| | Audit | Sentinel-specific (sealed-record SHA-256, provenance hashes, complete call records) |
| | Policy compliance rate | AGT-native: `GovernanceMetrics` |
| Readiness | Error budget, verdict | AGT-compatible: `src/readiness.py`, while AGT `ErrorBudget` and `SLO` have bugs A and B |

## AGT bugs, with the test that shows each

All against 3.7.0. Each is a strict xfail in `tests/test_agt_known_bugs.py`.

| | Component | Bug | Fails on | Test | Sentinel's answer |
|---|---|---|---|---|---|
| A | agent-sre `ErrorBudget.record_event()` | adds 1.0 per failure against a budget of `1 - target` | aggregation | `test_agt_error_budget_one_failure_in_a_hundred_is_not_exhausted` | AGT-compatible: `src/readiness.py` |
| B | agent-sre `ErrorBudget.burn_rate()` | divides the budget by the window in seconds | aggregation | `test_agt_error_budget_burn_rate_is_error_rate_over_allowed_rate` | AGT-compatible: `src/readiness.py` |
| — | agent-sre `SLO.evaluate()` | EXHAUSTED at 99% against 95%, because of A | aggregation | `test_agt_slo_at_99_percent_against_95_is_not_exhausted` | AGT-compatible: `src/readiness.py` |
| C | agent-sre `TaskSuccessRate`, `ToolCallAccuracy`, `PolicyCompliance`, `HallucinationRate` | `current_value()` averages running rates: 99% reads as 94.8%, 1% as 5.2% | aggregation | `test_agt_rate_sli_current_value_is_the_rate[...]`, `test_agt_hallucination_rate_current_value_is_the_rate` | a fresh SLI per window, read through `collect()` |
| D | agent-sre `CostPerTask` | `current_value()` averages running averages | aggregation | `test_agt_cost_per_task_current_value_is_the_mean_cost` | `CostGuard` |
| E | agent-sre `ResponseLatency` | `current_value()` ignores its window | lifecycle | `test_agt_latency_outside_the_window_is_not_current` | a fresh instance per window |
| F | agent-sre `ResponseLatency`, `CostPerTask` | `compliance()` treats lower-is-better values as higher-is-better | semantic definition | `test_agt_latency_under_its_target_is_compliant`, `test_agt_cost_under_its_target_is_compliant` | `compliance()` never read; grading in `src/readiness.py` |
| G | agent-sre `CalibrationDeltaSLI` | over- and under-confidence cancel | aggregation | `test_agt_calibration_delta_detects_offsetting_miscalibration` | AGT-compatible: ECE (`src/agt_metrics.py`) |
| H | agent-sre `EvaluationEngine` | `pass_rate()` and `average_score()` are 0.0 with no evaluations | population | `test_agt_evaluation_engine_with_no_evaluations_has_no_score` | "no data" when the history is empty |
| I | agent-sre `EvaluationEngine.get_stats()` | leaves out a criterion that always scores 0 | population | `test_agt_evaluation_stats_keep_a_criterion_that_always_fails` | `get_stats()` never read |
| J | agent-sre `EvaluationEngine.run()` | a judge error on a required criterion does not fail the report | semantic definition | `test_agt_a_required_criterion_the_judge_could_not_evaluate_fails_the_report` | a judge error on a required criterion fails the report |
| K | agent-os-kernel `PolicyDocument` | ignores unknown keys (`rule:` for `rules:` loads with no rules) | implementation (policy loading) | `test_agt_policy_document_rejects_an_unknown_key` | `src/governance.py` refuses the policy |
| L | agent-os-kernel `PolicyDocument` | defaults to allow when `defaults` is absent | implementation (policy default) | `test_agt_policy_document_without_defaults_does_not_allow_everything` | `src/governance.py` refuses the policy |
| M | agent-os-kernel `PolicyEvaluator` | `PolicyDocument.tool_allowlist` is documented as enforced by the evaluator, and never read | implementation (documented control never enforced) | `test_agt_policy_evaluator_enforces_the_tool_allowlist` | tool access is written as rules (`policies/tool-use.yaml`); `src/governance.py` refuses a policy that sets `tool_allowlist` |
