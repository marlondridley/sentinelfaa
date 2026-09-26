"""
Agent execution telemetry, defined once in OpenTelemetry's GenAI semantic
conventions.

Sentinel is model-agnostic: DeepSeek today behind the LiteLLM gateway,
Gemini, OpenAI or Claude tomorrow. So what a model call *is*, as telemetry,
cannot depend on the provider. It is defined here, in the vocabulary of
opentelemetry-semantic-conventions, and every provider is measured by that
one definition:

  spans    invoke_workflow investigation   one per investigation
             invoke_agent <specialist>     one per specialist step
               chat <model alias>          one per model call
           invoke_agent critic             the per-alert Critic
             chat <model alias>

  metrics  gen_ai.client.operation.duration   s; retries and the repair included
           gen_ai.client.token.usage          {token}; gen_ai.token.type input|output
           and the Agent Governance Toolkit's own instruments (agent-sre):
           agent.tasks.total, agent.task.duration   per agent invocation
           agent.llm.latency                        per model call
           agent.tool_calls.total, agent.tool.latency  per governed tool call
           agent.policy.violations                  refused model and tool calls
           agent.sre.cost.total_usd                 gateway-reported spend

  logs     every model-use policy decision, through the Microsoft Agent
           Governance Toolkit's own GovernanceAuditLogger and OTelLogsBackend
           (src/governance.py), under AGT's agt.* attribute names

A chat span carries gen_ai.* (provider, requested and served model, token
usage, error.type), the policy decision under AGT's agt.* names, and
sentinel.* for what the conventions have no name for: the outcome class,
attempts, the schema repair, cost, and the subject the call was about.

Export uses the standard OpenTelemetry environment variables. Set
OTEL_EXPORTER_OTLP_ENDPOINT (or a per-signal OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT)
and traces, metrics and logs go to any OTLP collector: Jaeger, Tempo, Azure
Monitor, Datadog, Splunk. Without it nothing leaves the process. The metrics
are always on /metrics, through OpenTelemetry's Prometheus reader, next to
the existing prometheus_client series.

The durable record is a projection of the same attributes: model_telemetry.
record() builds the model_calls row from the chat span's attribute set, so
the table readiness reads and the traces a collector receives cannot define
a call differently. The table exists because a readiness verdict must not
depend on an optional collector's retention, and because it is append-only.
"""
from __future__ import annotations

import os
import threading
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import baggage, context, metrics, trace
from opentelemetry._logs import get_logger_provider, set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as gen_ai
from opentelemetry.semconv._incubating.metrics import gen_ai_metrics
from opentelemetry.semconv.attributes import error_attributes

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from agent_os.otel_audit_backend import ATTR_ACTION, ATTR_AGENT_ID, ATTR_DECISION, ATTR_REASON
    from agent_sre.integrations.otel.metrics import MetricsExporter
    from agent_sre.tracing import conventions as sre
    from agent_sre.tracing.metrics import create_agent_metrics

SERVICE_NAME = "nas-feedops-sentinel"
INSTRUMENTATION = "sentinel"

# ── Attribute names ──────────────────────────────────────────────────────────
# OpenTelemetry GenAI conventions.
OPERATION = gen_ai.GEN_AI_OPERATION_NAME
PROVIDER = gen_ai.GEN_AI_PROVIDER_NAME
REQUEST_MODEL = gen_ai.GEN_AI_REQUEST_MODEL
RESPONSE_MODEL = gen_ai.GEN_AI_RESPONSE_MODEL
REQUEST_MAX_TOKENS = gen_ai.GEN_AI_REQUEST_MAX_TOKENS
REQUEST_TEMPERATURE = gen_ai.GEN_AI_REQUEST_TEMPERATURE
INPUT_TOKENS = gen_ai.GEN_AI_USAGE_INPUT_TOKENS
OUTPUT_TOKENS = gen_ai.GEN_AI_USAGE_OUTPUT_TOKENS
AGENT_NAME = gen_ai.GEN_AI_AGENT_NAME
WORKFLOW_NAME = gen_ai.GEN_AI_WORKFLOW_NAME
TOKEN_TYPE = gen_ai.GEN_AI_TOKEN_TYPE
ERROR_TYPE = error_attributes.ERROR_TYPE
EXCEPTION_TYPE = "exception.type"
CHAT = gen_ai.GenAiOperationNameValues.CHAT.value
INVOKE_AGENT = gen_ai.GenAiOperationNameValues.INVOKE_AGENT.value
INVOKE_WORKFLOW = gen_ai.GenAiOperationNameValues.INVOKE_WORKFLOW.value
EXECUTE_TOOL = gen_ai.GenAiOperationNameValues.EXECUTE_TOOL.value
TOOL_NAME = gen_ai.GEN_AI_TOOL_NAME
OTHER_PROVIDER = "_OTHER"

# The Agent Governance Toolkit's names (agent_os.otel_audit_backend), so a
# policy decision reads the same on a span as in AGT's own audit log records.
AGT_AGENT_ID = ATTR_AGENT_ID
AGT_ACTION = ATTR_ACTION
AGT_DECISION = ATTR_DECISION
AGT_REASON = ATTR_REASON
AGT_RULE = "agt.audit.meta.rule"
AGT_POLICY = "agt.audit.meta.policy"

# Sentinel's own, where the conventions have no name.
CALL_ID = "sentinel.call_id"
SUBJECT = "sentinel.subject"
OUTCOME = "sentinel.outcome"
ATTEMPTS = "sentinel.attempts"
REPAIRED = "sentinel.repaired"
COST_USD = "sentinel.cost_usd"
INVESTIGATION_ID = "sentinel.investigation.id"
ACTION_ID = "sentinel.action.id"
ALERT_ID = "sentinel.alert.id"
AGENT_METHOD = "sentinel.agent.method"          # llm | deterministic
AGENT_FALLBACK = "sentinel.agent.fallback_reason"

_SUBJECT_BAGGAGE = "sentinel.subject"

# Bucket boundaries the GenAI conventions recommend for these two histograms.
_DURATION_BUCKETS = (0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92)
_MS_BUCKETS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 20000, 40000, 80000, 160000)
_TOKEN_BUCKETS = (1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864)

# The served model's family -> gen_ai.provider.name. Behind the gateway the
# requested alias names only the wire protocol ("openai/primary-model"), so
# the provider is known once a response names the model that served it.
_PROVIDER_PREFIXES = (
    ("deepseek", "deepseek"),
    ("gemini", "gcp.gemini"),
    ("models/gemini", "gcp.gemini"),
    ("claude", "anthropic"),
    ("anthropic", "anthropic"),
    ("gpt-", "openai"),
    ("chatgpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("mistral", "mistral_ai"),
    ("mixtral", "mistral_ai"),
    ("codestral", "mistral_ai"),
    ("command", "cohere"),
    ("grok", "x_ai"),
)
_PROVIDER_ROUTES = {
    "deepseek": "deepseek", "gemini": "gcp.gemini", "vertex_ai": "gcp.vertex_ai",
    "anthropic": "anthropic", "bedrock": "aws.bedrock", "azure": "azure.ai.openai",
    "azure_ai": "azure.ai.inference", "mistral": "mistral_ai", "groq": "groq",
    "cohere": "cohere", "xai": "x_ai", "perplexity": "perplexity", "watsonx": "ibm.watsonx.ai",
}

_lock = threading.Lock()
_instruments: dict[str, Any] = {}


def provider_name(served_model: str | None) -> str:
    """gen_ai.provider.name for a served model; _OTHER when it is unknown or unrecognised."""
    model = (served_model or "").strip().lower()
    if not model:
        return OTHER_PROVIDER
    route, _, rest = model.partition("/")
    if rest and route in _PROVIDER_ROUTES:
        return _PROVIDER_ROUTES[route]
    name = rest or model
    for prefix, provider in _PROVIDER_PREFIXES:
        if name.startswith(prefix):
            return provider
    return OTHER_PROVIDER


# ── Providers ────────────────────────────────────────────────────────────────

def _otlp(signal: str) -> bool:
    return bool(os.environ.get(f"OTEL_EXPORTER_OTLP_{signal}_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))


def setup() -> None:
    """
    Install the tracer, meter and logger providers, once per process.

    Providers someone else already installed (opentelemetry-instrument, a
    host application) are kept, and Sentinel's telemetry goes through them.
    """
    if _instruments:
        return
    with _lock:
        if _instruments:
            return
        resource = Resource.create({} if os.environ.get("OTEL_SERVICE_NAME") else {"service.name": SERVICE_NAME})

        if not hasattr(trace.get_tracer_provider(), "add_span_processor"):
            tracer_provider = TracerProvider(resource=resource)
            if _otlp("TRACES"):
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            trace.set_tracer_provider(tracer_provider)

        if not isinstance(metrics.get_meter_provider(), MeterProvider):
            from opentelemetry.exporter.prometheus import PrometheusMetricReader

            readers: list[Any] = [PrometheusMetricReader()]
            if _otlp("METRICS"):
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

                readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
            metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers, views=[
                # AGT's latency instruments are in milliseconds with OTel's
                # default buckets, which stop at 10 s; model calls run longer.
                View(instrument_name=name, aggregation=ExplicitBucketHistogramAggregation(_MS_BUCKETS))
                for name in ("agent.llm.latency", "agent.task.duration", "agent.tool.latency")
            ]))

        if not isinstance(get_logger_provider(), LoggerProvider):
            logger_provider = LoggerProvider(resource=resource)
            if _otlp("LOGS"):
                from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

                logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
            set_logger_provider(logger_provider)

        meter = metrics.get_meter(INSTRUMENTATION)
        _instruments.update(
            duration=meter.create_histogram(
                gen_ai_metrics.GEN_AI_CLIENT_OPERATION_DURATION, unit="s",
                description="GenAI operation duration.",
                explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
            ),
            tokens=meter.create_histogram(
                gen_ai_metrics.GEN_AI_CLIENT_TOKEN_USAGE, unit="{token}",
                description="Number of input and output tokens used.",
                explicit_bucket_boundaries_advisory=_TOKEN_BUCKETS,
            ),
            # AGT computes and names the agent metrics (agent-sre); OTel carries them.
            agt=create_agent_metrics(metrics.get_meter("agent_sre.tracing")),
            agt_exporter=MetricsExporter(service_name=SERVICE_NAME),
        )


def tracer() -> trace.Tracer:
    setup()
    return trace.get_tracer(INSTRUMENTATION)


def add_span_processor(processor: SpanProcessor) -> None:
    """Attach a span processor to the installed provider (tests attach an in-memory exporter)."""
    setup()
    trace.get_tracer_provider().add_span_processor(processor)


# ── Subject: what a call is about ────────────────────────────────────────────

@contextmanager
def subject(value: str | None) -> Iterator[None]:
    """
    Everything called inside is about `value`: an investigation_id, or an
    alert_id for the Critic. Carried as OpenTelemetry baggage, so it reaches
    every model call below without being passed through each function.
    """
    if not value:
        yield
        return
    token = context.attach(baggage.set_baggage(_SUBJECT_BAGGAGE, value))
    try:
        yield
    finally:
        context.detach(token)


def current_subject() -> str | None:
    value = baggage.get_baggage(_SUBJECT_BAGGAGE)
    return str(value) if value else None


# ── Metrics, from a chat span's attributes ───────────────────────────────────

def record_call_metrics(attributes: dict[str, Any], duration_s: float) -> None:
    """One model call's metrics, from the same attributes its chat span carries."""
    setup()
    labels = {k: attributes[k] for k in (OPERATION, PROVIDER, REQUEST_MODEL, RESPONSE_MODEL, ERROR_TYPE, AGENT_NAME)
              if attributes.get(k) is not None}
    _instruments["duration"].record(duration_s, labels)
    for key, kind in ((INPUT_TOKENS, "input"), (OUTPUT_TOKENS, "output")):
        if attributes.get(key) is not None:
            _instruments["tokens"].record(attributes[key], {**labels, TOKEN_TYPE: kind})
    role = str(attributes.get(AGENT_NAME) or "unregistered")
    _instruments["agt"].llm_latency.record(duration_s * 1000, {
        sre.AGENT_TASK_NAME: role, sre.AGENT_MODEL_PROVIDER: str(attributes.get(PROVIDER) or OTHER_PROVIDER),
        OUTCOME: str(attributes.get(OUTCOME)),
    })
    if attributes.get(COST_USD):
        _instruments["agt_exporter"].record_cost(agent_id=role, cost_usd=float(attributes[COST_USD]))


def record_agent_task(agent: str, duration_s: float, outcome: str) -> None:
    """One agent invocation (an invoke_agent span): AGT's task counter and duration."""
    setup()
    _instruments["agt"].tasks_total.add(1, {sre.AGENT_TASK_NAME: agent, OUTCOME: outcome})
    _instruments["agt"].task_duration.record(duration_s * 1000, {sre.AGENT_TASK_NAME: agent})


def record_tool_call(tool: str, agent: str | None, duration_s: float, decision: str) -> None:
    """One governed tool call (an execute_tool span): AGT's tool counter and latency."""
    setup()
    labels = {sre.AGENT_TOOL_NAME: tool, sre.AGENT_TASK_NAME: agent or "unregistered",
              sre.AGENT_POLICY_DECISION: decision}
    _instruments["agt"].tool_calls_total.add(1, labels)
    _instruments["agt"].tool_latency.record(duration_s * 1000, labels)


def record_policy_decision(role: str, decision: str, rule: str | None, *, allowed: bool, action: str) -> None:
    """A refused model or tool call counts on AGT's agent.policy.violations."""
    if allowed:
        return
    setup()
    _instruments["agt"].policy_violations.add(1, {
        AGT_AGENT_ID: role, sre.AGENT_POLICY_NAME: rule or "(default)", sre.AGENT_POLICY_DECISION: decision,
        AGT_ACTION: action,
    })


def span_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """OpenTelemetry attributes cannot be None; unknown is left out."""
    return {k: v for k, v in attributes.items() if v is not None}
