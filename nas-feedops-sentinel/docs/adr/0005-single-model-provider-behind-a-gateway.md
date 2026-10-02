# 0005. One model (Claude Haiku) behind a LiteLLM gateway, no fallback

Status: Accepted (decided 2026-09-30)

## Context
The advisory agents need a model. A second provider or a free-tier fallback adds
a data-handling boundary, a second set of terms, and behaviour that differs by
provider, all to protect a layer that is advisory by design (see
[0001](0001-rules-decide-models-explain.md)).

## Decision
Claude Haiku 4.5 is the only model, reached only through the LiteLLM proxy by
alias (`primary-model`). `src/config.proxy_model()` refuses any direct provider
route, and the application holds a scoped virtual key with a budget and a rate
limit. Retries happen in one layer. There is no fallback provider: if the model is
down or out of quota, agents report themselves unavailable, the circuit breaker
says so, and every alert is still staged for a person. Swapping the model is a
change to `instraclustr/litellm-config.yaml`, not to application code. A test
(`instraclustr/tests/test_model_provider.py`) holds the configuration to Haiku
only, so adding a provider is a reviewed change to that test as well.

## Alternatives considered
- **Multi-provider fallback.** Rejected: more boundaries to govern for an advisory
  layer that already degrades safely.
- **Direct SDK calls from agents.** Rejected: no single place to apply policy,
  redaction, budget and audit (see `src/llm_client.py`).
- **Self-hosted model.** Not chosen for the beta; remains an option behind the
  same alias.

## Consequences
- A provider outage removes AI explanations, not alerts.
- One provider's behaviour and one set of data-handling terms to assess.
- A single point of failure for the AI layer, accepted because of the fallbacks.

## Evidence
`instraclustr/litellm-config.yaml`; `instraclustr/tests/test_model_provider.py`;
`tests/test_governance.py` (a refused call sends nothing and is recorded as costing
nothing).

## Limits
The configuration is a test configuration, not an authorized boundary for FAA
operational data. Before real feed data is sent, `primary-model` must move to an
approved endpoint (Claude on Bedrock GovCloud or Vertex Assured Workloads, Azure
OpenAI Government, or on-prem inference). Backup and disaster recovery for the
stack were decided out of scope.
