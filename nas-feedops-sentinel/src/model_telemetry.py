"""
One record per model call: the policy decision, how the call ended, wall
time, tokens and cost.

LiteLLMClient.acall_with_tool() opens an OpenTelemetry chat span and a
ModelCall for every logical call -- the policy check, every retry and the
one schema repair belong to the same call -- and passes it to record() when
the call ends, however it ends.

record() builds the call's attributes once, in the OpenTelemetry GenAI
vocabulary (call_attributes(), names in src/telemetry.py), and everything
else is derived from that one set: the span's attributes, the standard
gen_ai.client.* metrics, and the model_calls row readiness measures
(row_from_attributes()). A new provider behind the gateway is measured by
the same definition as the old one. record() never raises: losing a
measurement must not lose the call's result or change what the caller falls
back to. No prompt or response text is recorded, on the span or in the row
-- not even an exception message, which can quote the model's reply.

Outcome classes -- the failure modes readiness reports on:

  ok               the first answer had every required key
  repaired         it had them after the one repair request
  incomplete       it still lacked required keys after the repair; the
                   caller's guardrail decides what that means
  invalid_output   not JSON, not a JSON object, empty, or malformed envelope
  timeout          no answer within LLM_TIMEOUT_SECONDS
  rate_limited     refused as too frequent, after retries
  quota_exhausted  a daily or billing quota; never retried
  auth_error       the gateway refused the key
  bad_request      the request itself was rejected (400/404/422, context window)
  provider_error   5xx, connection failures, anything else upstream
  policy_denied    refused by the model-use policy; nothing was sent
  circuit_open     the alias's circuit breaker was open (src/llm_guard.py); nothing was sent
  budget_exhausted the pipeline pass's token budget was spent; nothing was sent
  cancelled        the caller cancelled it (an activity timeout, a shutdown) before an answer came back; the
                   request may already have been sent and billed
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from opentelemetry.trace import Status, StatusCode

from src import storage
from src import telemetry as otel
from src.logging_config import get_logger

_log = get_logger(__name__)

SUCCESS_OUTCOMES = ("ok", "repaired")
# Outcomes where the call returned to its caller; every other one raised.
RETURNED_OUTCOMES = ("ok", "repaired", "incomplete")
OUTCOMES = (
    "ok", "repaired", "incomplete", "invalid_output", "timeout", "rate_limited",
    "quota_exhausted", "auth_error", "bad_request", "provider_error", "policy_denied", "cancelled",
    "circuit_open", "budget_exhausted",
)
# Outcomes of calls this system refused itself: nothing was sent, so they say nothing about the model.
NOT_SENT_OUTCOMES = ("policy_denied", "circuit_open", "budget_exhausted")

# Messages src/llm_client.py raises itself when the model's reply is unusable.
_INVALID_OUTPUT_MARKERS = (
    "non-JSON content", "not an object", "Empty completion content", "Unexpected LiteLLM response shape",
)


@dataclass
class ModelCall:
    """Accumulates one logical call's measurements across its requests."""

    role: str
    model_alias: str
    correlation_id: str | None = None
    subject: str | None = None   # what the call was about: an investigation_id, or an alert_id
    max_tokens: int | None = None
    temperature: float | None = None
    span: Any = None             # the call's OpenTelemetry chat span
    call_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started: float = field(default_factory=time.perf_counter)
    attempts: int = 0            # completion requests sent: retries and the repair included
    responses: int = 0           # completions that came back (usage and cost known for these)
    repaired: bool = False
    incomplete: bool = False
    served_model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = 0.0  # None once any completion's cost is unknown
    policy_action: str = "unevaluated"
    policy_rule: str | None = None
    policy_name: str | None = None

    def add_response(self, response: Any) -> None:
        """Fold in one completion's served model, token usage and cost."""
        self.responses += 1
        self.served_model = getattr(response, "model", None) or self.served_model
        usage = getattr(response, "usage", None)
        for attr in ("prompt_tokens", "completion_tokens"):
            value = getattr(usage, attr, None) if usage is not None else None
            if isinstance(value, int):
                setattr(self, attr, (getattr(self, attr) or 0) + value)
        cost = response_cost(response)
        if cost is None or self.cost_usd is None:
            self.cost_usd = None
        else:
            self.cost_usd += cost


def response_cost(response: Any) -> float | None:
    """
    The gateway's own figure (the x-litellm-response-cost header LiteLLM
    surfaces in _hidden_params), else litellm's price table for the served
    model, else None. Unknown stays unknown: never reported as zero.
    """
    hidden = getattr(response, "_hidden_params", None) or {}
    try:
        # Prices a reply already received; no model is called (ruff TID251).
        from litellm.cost_calculator import get_response_cost_from_hidden_params  # noqa: TID251

        cost = get_response_cost_from_hidden_params(hidden)
        if cost is None and isinstance(hidden, dict):
            cost = hidden.get("response_cost")
        if cost is None:
            import litellm  # noqa: TID251

            cost = litellm.completion_cost(completion_response=response)
        cost = float(cost)
    except Exception:  # noqa: BLE001 -- no price available is an answer, not an error
        return None
    return cost if cost >= 0 else None


def classify_outcome(exc: BaseException) -> str:
    """Map a failed call's exception onto an outcome class."""
    from src.llm_client import LLMBudgetExceeded, LLMCircuitOpen, LLMPolicyDenied

    if isinstance(exc, LLMPolicyDenied):
        return "policy_denied"
    if isinstance(exc, LLMCircuitOpen):
        return "circuit_open"
    if isinstance(exc, LLMBudgetExceeded):
        return "budget_exhausted"
    text = str(exc)
    if any(marker in text for marker in _INVALID_OUTPUT_MARKERS):
        return "invalid_output"
    cause = exc.__cause__ or exc
    names = {cls.__name__ for cls in type(cause).__mro__}
    status = getattr(cause, "status_code", None)
    if isinstance(cause, (asyncio.TimeoutError, TimeoutError)) or any("Timeout" in n for n in names):
        return "timeout"
    if "PerDay" in text or "quota" in text.lower():
        return "quota_exhausted"
    if "RateLimitError" in names or status == 429:
        return "rate_limited"
    if names & {"AuthenticationError", "PermissionDeniedError"} or status in (401, 403):
        return "auth_error"
    if names & {"BadRequestError", "NotFoundError", "ContextWindowExceededError",
                "UnprocessableEntityError"} or status in (400, 404, 413, 422):
        return "bad_request"
    return "provider_error"


def _recorded_cost(call: ModelCall) -> float | None:
    """Never sent: nothing spent. Sent but nothing came back: unknown, not zero."""
    if call.attempts == 0:
        return 0.0
    if call.responses == 0:
        return None
    return call.cost_usd


def call_attributes(call: ModelCall, outcome: str, error: BaseException | None = None) -> dict[str, Any]:
    """
    The one definition of a model call: the attributes its chat span carries.
    None means unknown and is left off the span.
    """
    return {
        otel.OPERATION: otel.CHAT,
        otel.PROVIDER: otel.provider_name(call.served_model),
        otel.REQUEST_MODEL: call.model_alias,
        otel.RESPONSE_MODEL: call.served_model,
        otel.REQUEST_MAX_TOKENS: call.max_tokens,
        otel.REQUEST_TEMPERATURE: call.temperature,
        otel.INPUT_TOKENS: call.prompt_tokens,
        otel.OUTPUT_TOKENS: call.completion_tokens,
        otel.AGENT_NAME: call.role,
        otel.ERROR_TYPE: None if outcome in RETURNED_OUTCOMES else outcome,
        otel.EXCEPTION_TYPE: type(error.__cause__ or error).__name__ if error is not None else None,
        otel.AGT_AGENT_ID: call.role,
        otel.AGT_ACTION: "model_call",
        otel.AGT_DECISION: call.policy_action,
        otel.AGT_RULE: call.policy_rule,
        otel.AGT_POLICY: call.policy_name,
        otel.CALL_ID: call.call_id,
        otel.SUBJECT: call.subject,
        otel.OUTCOME: outcome,
        otel.ATTEMPTS: call.attempts,
        otel.REPAIRED: call.repaired,
        otel.COST_USD: _recorded_cost(call),
    }


def row_from_attributes(
    attributes: dict[str, Any], *, created_at: datetime, latency_ms: float,
    trace_id: str | None = None, correlation_id: str | None = None,
) -> dict[str, Any]:
    """The model_calls row: a projection of the chat span's attributes, nothing more."""
    a = attributes
    return {
        "call_id": a[otel.CALL_ID],
        "created_at": created_at,
        "correlation_id": correlation_id if correlation_id not in (None, "-") else None,
        "trace_id": trace_id,
        "subject": a.get(otel.SUBJECT),
        "role": a[otel.AGENT_NAME],
        "model_alias": a[otel.REQUEST_MODEL],
        "served_model": a.get(otel.RESPONSE_MODEL),
        "provider": a[otel.PROVIDER],
        "outcome": a[otel.OUTCOME],
        "attempts": a[otel.ATTEMPTS],
        "repaired": a[otel.REPAIRED],
        "latency_ms": latency_ms,
        "prompt_tokens": a.get(otel.INPUT_TOKENS),
        "completion_tokens": a.get(otel.OUTPUT_TOKENS),
        "cost_usd": a.get(otel.COST_USD),
        "policy_action": a[otel.AGT_DECISION],
        "policy_rule": a.get(otel.AGT_RULE),
        "error_type": a.get(otel.EXCEPTION_TYPE),
    }


async def record(call: ModelCall, outcome: str, error: BaseException | None = None) -> None:
    """Annotate the span, count the call, and store its row. Never raises."""
    latency_s = time.perf_counter() - call.started
    attributes = call_attributes(call, outcome, error)
    trace_id = None
    try:
        span = call.span
        if span is not None:
            span.set_attributes(otel.span_attributes(attributes))
            if attributes[otel.ERROR_TYPE] is not None:
                # The outcome class only: an exception message can quote the reply.
                span.set_status(Status(StatusCode.ERROR, outcome))
            span_context = span.get_span_context()
            if span_context.is_valid:
                trace_id = format(span_context.trace_id, "032x")
        otel.record_call_metrics(attributes, latency_s)
    except Exception as exc:  # noqa: BLE001
        _log.warning("model_call_telemetry_failed", extra={"error": str(exc)})

    row = row_from_attributes(
        attributes, created_at=call.created_at, latency_ms=round(latency_s * 1000, 1),
        trace_id=trace_id, correlation_id=call.correlation_id,
    )
    try:
        await storage.record_model_call(row)
    except Exception as exc:  # noqa: BLE001 -- a lost measurement must not fail the call
        _log.warning("model_call_record_failed", extra={"role": call.role, "error": str(exc)})
