import time
from typing import Optional
from pydantic import BaseModel

# --- OpenTelemetry Tracing Setup ---
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

# --- Prometheus Metrics Setup ---
from prometheus_client import Counter, Histogram

# --- CrewAI Event Flow Orchestration ---
from crewai.flow.flow import Flow, start, listen, router
from crewai import Agent, Crew, Process, Task

# --- Local Domain Models & Advisory Services ---
from src.models import AlertEvent, WatchOfficerBrief, StagedAction
from src.watch_officer_agent import build_watch_officer_context, generate_watch_officer_brief
from src.execution_gate import stage_critical_alert
from src.llm_config import get_configured_llm

# =============================================================================
# 1. OPENTELEMETRY & PROMETHEUS INITIALIZATION
# =============================================================================

resource = Resource.create({"service.name": "nas-feedops-sentinel"})
provider = TracerProvider(resource=resource)
otlp_exporter = OTLPSpanExporter(endpoint="http://localhost:4317", insecure=True)
provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("sentinel.flow")

# Prometheus Metrics
FLOW_DURATION = Histogram(
    "sentinel_flow_step_duration_seconds",
    "Execution duration of Sentinel flow steps",
    ["step_name"]
)

STAGED_ACTIONS_TOTAL = Counter(
    "sentinel_staged_actions_total",
    "Total critical actions staged for HITL review",
    ["severity", "airport_code"]
)

# =============================================================================
# 2. STATE DEFINITION & INSTRUMENTED CREWAI FLOW
# =============================================================================

class SentinelState(BaseModel):
    """State container passed down the CrewAI Flow graph."""
    alert: AlertEvent
    analysis_result: str = ""
    watch_brief: Optional[WatchOfficerBrief] = None
    staged_action: Optional[StagedAction] = None


class SentinelFlow(Flow[SentinelState]):
    """
    Main orchestration pipeline merged for OpenTelemetry observability and
    deterministic safety alongside standard CrewAI Agent orchestration.
    """

    @start()
    def analyze_alert_crew(self):
        """
        Step 1: Pipeline Entry Point.
        Uses standard CrewAI Crew/Tasks to evaluate raw alert telemetry.
        """
        start_time = time.time()
        
        with tracer.start_as_current_span("analyze_alert_crew") as span:
            span.set_attribute("sentinel.alert_id", self.state.alert.alert_id)
            span.set_attribute("sentinel.severity", self.state.alert.severity)

            try:
                shared_llm = get_configured_llm()

                critic_agent = Agent(
                    role="Critic Agent",
                    goal="Identify operational anomaly patterns and risk markers.",
                    backstory="Aviation telemetry specialist analyzing ISMC and APDS_RVR feeds.",
                    llm=shared_llm
                )

                planner_agent = Agent(
                    role="Planner Agent",
                    goal="Synthesize mitigation actions based on critic evaluations.",
                    backstory="PCT TRACON advisory planner prioritizing system safety.",
                    llm=shared_llm
                )

                task_evaluate = Task(
                    description=f"Evaluate event payload for alert {self.state.alert.alert_id}: {self.state.alert.model_dump()}",
                    expected_output="Anomalies and pattern risk analysis.",
                    agent=critic_agent
                )

                task_plan = Task(
                    description="Synthesize recommended mitigation actions based on the analysis.",
                    expected_output="Itemized mitigation steps.",
                    agent=planner_agent
                )

                crew = Crew(
                    agents=[critic_agent, planner_agent],
                    tasks=[task_evaluate, task_plan],
                    process=Process.sequential,
                    memory=False,
                )

                result = crew.kickoff()
                self.state.analysis_result = str(result)
                span.set_attribute("sentinel.analysis_complete", True)

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise
            finally:
                FLOW_DURATION.labels(step_name="analyze_alert_crew").observe(time.time() - start_time)

    @router(analyze_alert_crew)
    def route_by_severity(self):
        """
        Step 2: Deterministic Router Branch.
        Routes execution flow based on whether the alert requires immediate HITL staging.
        """
        with tracer.start_as_current_span("route_by_severity") as span:
            severity = self.state.alert.severity
            span.set_attribute("sentinel.routing_decision", severity)
            
            if severity == "CRITICAL":
                return "stage_for_hitl"
            return "generate_advisory_brief"

    @listen("stage_for_hitl")
    async def stage_action_gate(self):
        """
        Step 3a: Control Plane Execution Gate (Critical Path).
        Invokes deterministic stage_critical_alert safety logic and flags HITL requirement.
        """
        start_time = time.time()
        with tracer.start_as_current_span("stage_action_gate") as span:
            span.set_attribute("sentinel.hitl_required", True)
            
            # Deterministic execution gate boundary (awaits async staging)
            critic_res = self.state.analysis_result if isinstance(self.state.analysis_result, CriticResult) else None
            self.state.staged_action = await stage_critical_alert(
                alert=self.state.alert,
                critic_result=critic_res,
            )
            
            STAGED_ACTIONS_TOTAL.labels(
                severity=self.state.alert.severity,
                airport_code=self.state.alert.airport_code
            ).inc()
            
            # Build advisory narrative downstream of staging lock
            context = await build_watch_officer_context(alert=self.state.alert)
            self.state.watch_brief = generate_watch_officer_brief(context, use_llm=True)
            
            FLOW_DURATION.labels(step_name="stage_action_gate").observe(time.time() - start_time)

    @listen("generate_advisory_brief")
    async def build_standard_brief(self):
        """
        Step 3b: Standard Operational Briefing Path (Non-Critical).
        Generates routine advisory briefing without staging execution actions.
        """
        start_time = time.time()
        with tracer.start_as_current_span("build_standard_brief") as span:
            context = await build_watch_officer_context(alert=self.state.alert)
            self.state.watch_brief = generate_watch_officer_brief(context, use_llm=True)
            
            FLOW_DURATION.labels(step_name="build_standard_brief").observe(time.time() - start_time)