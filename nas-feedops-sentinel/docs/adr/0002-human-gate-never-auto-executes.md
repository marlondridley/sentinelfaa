# 0002. A verified person decides every escalation; nothing auto-executes

Status: Accepted (recorded 2026-10-02)

## Context
A CRITICAL alert may lead to an operational action. Whoever decides must be
identifiable, authorized, and accountable after the fact, and no software
component, agent or otherwise, should be able to take that decision for them.

## Decision
A CRITICAL alert is staged as PENDING by a fixed rule and executes nothing
(`stage_critical_alert()`, `src/execution_gate.py`). Only a verified person with
the right Keycloak realm role can decide it (`decide_staged_action()`,
`src/auth.py`). The decision and the actor, with their roles and how they were
verified, are written to the append-only `action_audit_log` in the same
transaction as the change. Both functions call `guard_human_gate()` first, so
code running as an agent is refused by policy whatever credentials it presents.
Identity is Keycloak over verified HTTPS only; an `http://` identity URL is refused.

The gate sits at escalation, not at every model call. Model calls are governed
automatically (see [0001](0001-rules-decide-models-explain.md)).

## Alternatives considered
- **Auto-execute low-risk actions.** Rejected: the first version of "low risk" is
  the first thing to be argued about after an incident.
- **Approval inside the agent framework** (AGT's `EscalationManager`). Rejected:
  it is in-memory, and a decision here must be durable and role-checked.
- **Gate every model invocation.** Rejected: it trains approvers to click through
  and puts the control where the risk is lowest.

## Consequences
- Every decision has a named human, a role and a timestamp that the application
  role cannot change or delete (triggers refuse UPDATE and DELETE).
- Throughput is limited by people. Accepted: this is the point of the gate.

## Evidence
`tests/test_authorization.py`; `tests/test_tool_boundary.py`
(`test_an_agent_cannot_decide_even_with_a_supervisors_credentials`,
`test_an_agent_cannot_stage_an_action`); `tests/test_identity_tls.py`;
`tests/test_e2e_proof.py` (audit log refuses UPDATE and DELETE).

## Limits
Decided out of scope on 2026-09-30, so they are known gaps and not oversights:
a two-person rule for severity downgrades, expiry of old PENDING actions, a
one-time code for approvers, and failing closed on sign-in outside development.
The session is bounded to 4 hours; re-checking Keycloak on each decision needs a
service account in the realm and is not done.
