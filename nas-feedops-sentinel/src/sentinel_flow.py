"""
Sentinel Flow execution setup.
"""
import time
from typing import Optional
from pydantic import BaseModel

from crewai import Crew, Process, Task
from crewai.flow.flow import Flow, listen, router, start

from src.critic_agent import get_critic_agent
from src.execution_gate import stage_critical_alert
from src.llm_config import get_configured_llm
from src.models import AlertEvent, StagedAction, WatchOfficerBrief
from src.planner_agent import get_planner_agent
from src.watch_officer_agent import build_watch_officer_context, generate_watch_officer_brief


class SentinelState(BaseModel):
    alert: AlertEvent
    analysis_result: str = ""
    watch_brief: Optional[WatchOfficerBrief] = None
    staged_action: Optional[StagedAction] = None


class SentinelFlow(Flow[SentinelState]):

    @start()
    def analyze_alert_crew(self):
        shared_llm = get_configured_llm()

        critic_agent = get_critic_agent(llm=shared_llm)
        planner_agent = get_planner_agent(llm=shared_llm)

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
            process=Process.sequential
        )

        result = crew.kickoff()
        self.state.analysis_result = str(result)

    @router(analyze_alert_crew)
    def route_by_severity(self):
        if self.state.alert.severity == "CRITICAL":
            return "stage_for_hitl"
        return "generate_advisory_brief"

    @listen("stage_for_hitl")
    def stage_action_gate(self):
        self.state.staged_action = stage_critical_alert(
            alert=self.state.alert,
            critic_result=self.state.analysis_result
        )
        context = build_watch_officer_context(alert=self.state.alert)
        self.state.watch_brief = generate_watch_officer_brief(context, use_llm=True)

    @listen("generate_advisory_brief")
    def build_standard_brief(self):
        context = build_watch_officer_context(alert=self.state.alert)
        self.state.watch_brief = generate_watch_officer_brief(context, use_llm=True)