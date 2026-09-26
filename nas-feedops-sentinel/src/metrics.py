"""
Prometheus metrics for NAS FeedOps Sentinel.

Exposes operational metrics at /metrics (Prometheus text format) so the
service plugs into existing NOC observability stacks (Prometheus + Grafana
or equivalent).

Metrics families:
  Pipeline activity — events ingested, alerts triggered, validation failures
  LLM activity      — critic/planner call latency, error counts
  Gate activity     — decisions by risk tier, HITL queue depth
  Feed health       — per-airport overall status gauge
"""
from __future__ import annotations

from threading import Thread
from wsgiref.simple_server import WSGIRequestHandler, make_server

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
)

# ── Service info ──────────────────────────────────────────────────────────────

SERVICE_INFO = Info("nas_sentinel", "NAS FeedOps Sentinel service info")
SERVICE_INFO.info({"service": "nas-feedops-sentinel", "version": "1.0.0"})


# ── Pipeline metrics ──────────────────────────────────────────────────────────

EVENTS_INGESTED = Counter(
    "nas_events_ingested_total",
    "Total NAS events ingested through the pipeline.",
    ["source", "airport", "service"],
)

SWIM_MESSAGES_WITHHELD = Counter(
    "nas_swim_messages_withheld_total",
    "SWIM messages not ingested because the FAA flagged them blockAtFAA=true.",
)

EVENTS_VALIDATED = Counter(
    "nas_events_validated_total",
    "Total NAS events that passed/failed validation.",
    ["status"],  # PASSED | FAILED
)

ALERTS_TRIGGERED = Counter(
    "nas_alerts_triggered_total",
    "Total alerts triggered by the deterministic alert engine.",
    ["alert_type", "severity", "airport"],
)


# ── LLM metrics ───────────────────────────────────────────────────────────────

LLM_CALL_LATENCY = Histogram(
    "nas_llm_call_duration_seconds",
    "Latency of LLM calls in seconds.",
    ["agent"],  # critic | planner
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

LLM_CALLS = Counter(
    "nas_llm_calls_total",
    "Total LLM calls by outcome.",
    ["agent", "outcome"],  # outcome: success | error | disabled | guardrail_rejected
)


# Agent metrics are computed by the Agent Governance Toolkit and carried by
# OpenTelemetry (src/telemetry.py): gen_ai.client.operation.duration and
# gen_ai.client.token.usage per model call, and AGT's agent.tasks.total,
# agent.task.duration, agent.llm.latency, agent.tool_calls.total,
# agent.tool.latency, agent.policy.violations and agent.sre.cost.total_usd --
# served on this same /metrics endpoint by OpenTelemetry's Prometheus reader.


# ── Gate metrics ──────────────────────────────────────────────────────────────

GATE_DECISIONS = Counter(
    "nas_gate_decisions_total",
    "Execution gate decisions by risk tier.",
    ["risk_tier", "hitl_required"],
)

HITL_QUEUE_DEPTH = Gauge(
    "nas_hitl_queue_depth",
    "Current number of STAGED HITL tickets awaiting human approval.",
)


# ── Feed-health gauges ────────────────────────────────────────────────────────

# The headline invariant, measured: CRITICAL alerts inside the staging window
# with no staged action. Anything but 0 for more than a reconcile interval is
# the failure that left 84 CRITICAL alerts unreviewed (alert rule
# CriticalAlertsUnstaged in monitoring/alert-rules.yml).
CRITICAL_ALERTS_UNSTAGED = Gauge(
    "nas_critical_alerts_unstaged",
    "CRITICAL alerts inside the staging window with no staged action.",
)
HITL_OLDEST_PENDING_SECONDS = Gauge(
    "nas_hitl_oldest_pending_age_seconds",
    "Age of the oldest PENDING staged action (0 when the queue is empty).",
)
RECONCILED_ALERTS = Counter(
    "nas_reconciled_alerts_total",
    "CRITICAL alerts the reconciler found unstaged, by what it did with them.",
    ["outcome"],  # staged_pending | recorded_expired
)

AIRPORT_OVERALL_STATUS = Gauge(
    "nas_airport_overall_status",
    "Worst-wins feed status per airport (0=OK, 1=STALE, 2=DEGRADED, 3=UNAVAILABLE, 4=DISCONNECTED).",
    ["airport"],
)

_STATUS_LEVEL = {
    "OK": 0, "STALE": 1, "DEGRADED": 2, "UNAVAILABLE": 3, "DISCONNECTED": 4,
}


def set_airport_status(airport: str, status: str) -> None:
    AIRPORT_OVERALL_STATUS.labels(airport=airport).set(_STATUS_LEVEL.get(status, 0))


# ── /metrics HTTP server ──────────────────────────────────────────────────────

class _SilentHandler(WSGIRequestHandler):
    def log_message(self, format, *args):  # silence default per-request logs
        pass


def _metrics_app(environ, start_response):
    if environ.get("PATH_INFO") == "/metrics":
        data = generate_latest()
        start_response("200 OK", [("Content-Type", CONTENT_TYPE_LATEST)])
        return [data]
    if environ.get("PATH_INFO") in ("/health", "/healthz"):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]
    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"not found"]


_server_thread: Thread | None = None


def start_metrics_server(host: str = "0.0.0.0", port: int = 9108) -> None:
    """Start Prometheus /metrics endpoint in a background daemon thread."""
    global _server_thread
    if _server_thread and _server_thread.is_alive():
        return

    def _serve():
        server = make_server(host, port, _metrics_app, handler_class=_SilentHandler)
        server.serve_forever()

    _server_thread = Thread(target=_serve, daemon=True, name="metrics-server")
    _server_thread.start()