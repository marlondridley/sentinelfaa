"""
The agent layer's tool boundary.

Every tool an advisory agent uses is named in TOOLS, and every invocation is
put to the tool-use policy (policies/tool-use.yaml, evaluated by the
Microsoft Agent Governance Toolkit in src/governance.py) before it runs:

  respond   the structured-output tool an agent hands a model; checked by
            LiteLLMClient.acall_with_tool() before anything is sent
  read      each query that builds an agent's context or evidence; run
            through invoke(), which checks, then runs it inside an
            OpenTelemetry execute_tool span
  escalate  staging an action for a human; decide: approving, rejecting,
            overriding or monitoring one. Agents are refused both:
            src/execution_gate.py calls guard_human_gate() before either,
            and it refuses whenever the code is running as an agent.

Which agent is invoking is carried as OpenTelemetry baggage (acting_as()), so
it reaches every call beneath an agent's entry point without being threaded
through each function, and a tool invoked outside any agent is evaluated as
unregistered, which the policy refuses.

The human gate is unchanged and stays where it was: at escalation, where a
person with the right Keycloak role decides on each staged action. This
boundary is automatic and applies to every invocation; it is what stops an
agent from reaching that gate from the other side, whatever its prompt or
output says.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

from opentelemetry import baggage, context
from opentelemetry.trace import SpanKind, Status, StatusCode

from src import governance, telemetry

T = TypeVar("T")

_AGENT_BAGGAGE = "sentinel.agent"


@dataclass(frozen=True)
class Tool:
    name: str
    access: str          # read | respond | escalate | decide | write
    description: str


TOOLS: dict[str, Tool] = {tool.name: tool for tool in (
    # Context and evidence reads
    Tool("read_alert_context", "read", "Recent events and alerts around one alert (src/context_router.py)"),
    Tool("read_recent_activity", "read", "Recent events, alerts and Critic results (src/planner_agent.py)"),
    Tool("read_operating_picture", "read", "Events, alerts, traffic, latency and the review queue "
                                           "(src/watch_officer_agent.py)"),
    Tool("collect_evidence", "read", "The investigation snapshot (src/investigation/evidence.py)"),
    # Structured-output tools handed to a model (prompts/*.yaml)
    Tool("submit_critique", "respond", "The Critic's or Red-Team Critic's assessment"),
    Tool("submit_operational_brief", "respond", "The Planner's answer"),
    Tool("submit_watch_officer_brief", "respond", "The Watch Officer's overview brief"),
    Tool("submit_investigation_brief", "respond", "The Watch Officer's investigation brief"),
    Tool("submit_feed_finding", "respond", "The Feed Diagnostician's finding"),
    Tool("submit_correlation_finding", "respond", "The Correlation Analyst's finding"),
    # The human gate's side (src/execution_gate.py): never an agent's
    Tool("stage_for_human_review", "escalate", "Stage an action for a person to decide"),
    Tool("decide_staged_action", "decide", "Approve, reject, override or monitor a staged action"),
)}


class ToolCallDenied(PermissionError):
    """The tool-use policy refused this invocation; the tool did not run."""

    def __init__(self, tool: str, decision: governance.GovernanceDecision):
        self.tool, self.decision = tool, decision
        super().__init__(
            f"Tool {tool!r} refused by the tool-use policy (rule: {decision.rule or 'default'}): {decision.reason}"
        )


# ── Which agent is acting ────────────────────────────────────────────────────

@contextmanager
def acting_as(agent: str) -> Iterator[None]:
    """Everything called inside is on behalf of `agent`."""
    token = context.attach(baggage.set_baggage(_AGENT_BAGGAGE, agent))
    try:
        yield
    finally:
        context.detach(token)


def current_agent() -> str | None:
    value = baggage.get_baggage(_AGENT_BAGGAGE)
    return str(value) if value else None


# ── The boundary ─────────────────────────────────────────────────────────────

def authorize(tool: str, *, role: str | None = None) -> governance.GovernanceDecision:
    """Put one invocation to the tool-use policy. Raises ToolCallDenied when refused."""
    spec = TOOLS.get(tool)
    decision = governance.evaluate_tool_call(
        role=role or current_agent(), tool=tool, access=spec.access if spec else "unknown",
    )
    if not decision.allowed:
        raise ToolCallDenied(tool, decision)
    return decision


async def invoke(tool: str, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    """Run a read tool for the current agent: the policy first, then the tool, in an execute_tool span."""
    agent = current_agent()
    started = time.perf_counter()
    with telemetry.tracer().start_as_current_span(
        f"execute_tool {tool}", kind=SpanKind.INTERNAL,
        attributes=telemetry.span_attributes({
            telemetry.OPERATION: telemetry.EXECUTE_TOOL, telemetry.TOOL_NAME: tool,
            telemetry.AGENT_NAME: agent,
        }),
    ) as span:
        try:
            decision = authorize(tool)
        except ToolCallDenied as exc:
            span.set_attributes(telemetry.span_attributes({
                telemetry.AGT_DECISION: exc.decision.action, telemetry.AGT_RULE: exc.decision.rule,
                telemetry.ERROR_TYPE: "policy_denied",
            }))
            span.set_status(Status(StatusCode.ERROR, "policy_denied"))
            telemetry.record_tool_call(tool, agent, time.perf_counter() - started, exc.decision.action)
            raise
        span.set_attributes(telemetry.span_attributes(
            {telemetry.AGT_DECISION: decision.action, telemetry.AGT_RULE: decision.rule}))
        try:
            return await fn(*args, **kwargs)
        finally:
            telemetry.record_tool_call(tool, agent, time.perf_counter() - started, decision.action)


def guard_human_gate(tool: str) -> None:
    """
    Called by the human gate's own functions (staging, deciding). Code running
    as an agent is put to the policy, which refuses it; anyone else -- the
    alert rules staging an action, a person deciding through the dashboard or
    API -- passes through to the gate's own checks (Keycloak roles).
    """
    if current_agent() is not None:
        authorize(tool)
