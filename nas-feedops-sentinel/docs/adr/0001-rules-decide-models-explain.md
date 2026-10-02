# 0001. Deterministic rules decide; models explain and challenge

Status: Accepted (recorded 2026-10-02)

## Context
Sentinel watches the FAA SWIM feed and raises alerts an operator acts on. A
wrong alert has operational cost, and the people who assess this system need to
be able to say why any alert fired. Language models are useful for explaining and
challenging, but they are non-deterministic, can be wrong with confidence, and
can be steered by text inside the data they read.

## Decision
What the operator acts on is decided by deterministic rules over measured facts
(`src/alert_engine.py`, `src/feed_health.py`, `src/validators.py`). A model can
explain an alert, summarize a situation, and challenge a finding. It cannot
raise, lower, suppress or stage an alert. The investigation workflow's order is
code, not model output (`src/investigation/workflow.py`). Every agent has a
deterministic fallback, so a failed or refused model call degrades the advice
and never the mission.

## Alternatives considered
- **Model classifies severity.** Rejected: not reproducible, not auditable, and a
  prompt-injection path into escalation.
- **Model proposes, rules confirm.** Rejected for now: it keeps a model on the
  critical path for little gain, since the rules already see the same facts.
- **Agent framework chooses the flow.** Rejected: the order of an investigation
  must not depend on model output.

## Consequences
- The system works with the AI layer off. `CRITIC_ENABLED=0` or a halt changes
  the quality of the explanations, not which alerts exist.
- The AI layer can be switched off at runtime for every model call without a
  restart (`src/model_halt.py`), and the cost of doing so is low because the
  mission never depended on it.
- Rules need maintenance when the feed changes; a model would adapt without edits.
  This is accepted as the price of being able to explain every alert.

## Evidence
`policies/tool-use.yaml` (`agents-never-decide-or-write`,
`escalation-belongs-to-the-rules`); `tests/test_alert_engine.py`;
`tests/test_tool_boundary.py`; `tests/test_agent_register.py` (no agent has
`decide`, `stage`, `write` or `execute`). See
[agent-governance.md](../agent-governance.md) boundaries 3 and 5.

## Limits
- An agent's identity is a string carried with the call, not a credential, so code
  inside the same service could claim another agent's name. Per-agent workload
  identity would close this and is not built.
- Rules are only as good as their thresholds, which are the maintainer's
  judgement.
