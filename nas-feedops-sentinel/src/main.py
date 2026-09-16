"""
NAS FeedOps Sentinel -- CLI entry point.
"""
from __future__ import annotations

import os
import argparse
import sys
from pathlib import Path

from crewai import Crew, Task

from src import config
from src.async_bridge import run_async
from src.agent_evaluator import AgentEvaluator, print_eval_report
from src.alert_engine import check_alerts
from src.critic_agent import get_critic_agent
from src.demo_scenario import generate_cascade_failure_scenario
from src.execution_gate import stage_critical_alert
from src.feed_health import compute_feed_health
from src.llm_config import get_configured_llm
from src.logging_config import configure_logging, get_logger, new_correlation_id
from src.metrics import (
    ALERTS_TRIGGERED,
    EVENTS_INGESTED,
    EVENTS_VALIDATED,
    set_airport_status,
    start_metrics_server,
)
from src.models import CriticResult, NASEvent, PlannerResult
from src.planner_agent import get_planner_agent
from src.replay import replay_from_directory, write_sample_replay_file
from src.sample_data import generate_sample_batch
from src.storage import (
    save_alerts,
    save_critic_result,
    save_events,
    save_planner_result,
)
from src.swim_file_ingest import scan_swim_folder
from src.validators import validate_event

_log = get_logger(__name__)


def _run_pipeline(events: list[NASEvent], label: str = "PIPELINE") -> None:
    cid = new_correlation_id()
    _log.info("pipeline_start", extra={"label": label, "event_count": len(events)})

    for event in events:
        result = validate_event(event)
        event.validation_status = "PASSED" if result.passed else "FAILED"
        event.validation_errors = result.errors
        EVENTS_INGESTED.labels(
            source=label.lower(),
            airport=event.airport_code or "unknown",
            service=event.service_name or "unknown",
        ).inc()
        EVENTS_VALIDATED.labels(status=event.validation_status).inc()

    run_async(save_events(events))

    alerts = check_alerts(events)
    run_async(save_alerts(alerts))

    for a in alerts:
        ALERTS_TRIGGERED.labels(
            alert_type=a.alert_type,
            severity=a.severity,
            airport=a.airport_code or "unknown",
        ).inc()

    # Instantiate configured CrewAI LLM & Agent
    llm = get_configured_llm()
    critic_agent = get_critic_agent(llm=llm)
    critical_alerts = [a for a in alerts if a.severity == "CRITICAL"]
    critic_results = {}
    max_critic_per_pass = int(__import__("os").getenv("MAX_CRITIC_PER_PASS", "5"))
    
    if critical_alerts:
        to_evaluate = critical_alerts[:max_critic_per_pass]
        suffix = f" (skipped {len(critical_alerts) - len(to_evaluate)})" if len(critical_alerts) > len(to_evaluate) else ""
        print(f"  [CRITIC] Evaluating {len(to_evaluate)} CRITICAL alert(s){suffix} ...")
        
        for alert in to_evaluate:
            task = Task(
                description=f"Evaluate the following operational alert and determine if escalation is required: {alert.model_dump_json()}",
                expected_output="A structured evaluation with confidence, pattern, and escalation boolean.",
                agent=critic_agent,
                output_pydantic=CriticResult
            )
            crew = Crew(agents=[critic_agent], tasks=[task], verbose=False, memory=False)
            crew_output = crew.kickoff()
            result = crew_output.pydantic
            
            run_async(save_critic_result(result))
            critic_results[alert.alert_id] = result

    staged_count = 0
    for alert in critical_alerts:
        critic_for_alert = critic_results.get(alert.alert_id)
        action = run_async(stage_critical_alert(alert, critic_for_alert))
        if action is not None:
            staged_count += 1
    if staged_count:
        print(f"  [HITL] Staged {staged_count} CRITICAL action(s) for human review.")

    health = compute_feed_health(events)
    for airport, status in health["airport_status"].items():
        set_airport_status(airport, status)


def run_demo() -> None:
    print("[DEMO] Generating cascade failure scenario ...")
    events = generate_cascade_failure_scenario()
    for event in events:
        result = validate_event(event)
        event.validation_status = "PASSED" if result.passed else "FAILED"
        event.validation_errors = result.errors

    _run_pipeline(events, "DEMO")

    question = "What is the current operational status of PCT TRACON and what should the duty manager do right now?"
    llm = get_configured_llm()
    planner_agent = get_planner_agent(llm=llm)
    task = Task(
        description=question,
        expected_output="A structured operational assessment including answer, confidence, recommended actions, and sub-queries.",
        agent=planner_agent,
        output_pydantic=PlannerResult
    )
    crew = Crew(agents=[planner_agent], tasks=[task], verbose=False, memory=False)
    crew_output = crew.kickoff()
    result = crew_output.pydantic
    
    run_async(save_planner_result(result))
    print(f"  Confidence : {result.confidence:.0%}" if result.confidence else "N/A")
    print(f"  Answer     : {result.answer}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m src.main")
    parser.add_argument("--mode", choices=["sample", "replay", "demo", "eval"], default="demo")
    parser.add_argument("--path", type=str, default=None, help="Custom path for replay or folder scan")
    args = parser.parse_args()

    configure_logging()
    
    if args.mode == "demo":
        run_demo()
    elif args.mode == "sample":
        print("[SAMPLE] Generating sample event batch...")
        events = generate_sample_batch()
        _run_pipeline(events, "SAMPLE")
    elif args.mode == "replay":
        target_path = Path(args.path) if args.path else Path(config.SWIM_RAW_FOLDER) if hasattr(config, "SWIM_RAW_FOLDER") else Path("/app/data/raw")
        print(f"[REPLAY] Scanning SWIM folder at {target_path}...")
        events = scan_swim_folder(target_path)
        if not events:
            print(f"  [REPLAY] No events found via folder scan, attempting directory replay...")
            events = replay_from_directory(target_path)
        if events:
            _run_pipeline(events, "REPLAY")
        else:
            print(f"  [REPLAY] Warning: No events ingested from {target_path}. Check volume mounts or file formats.")
    elif args.mode == "eval":
        evaluator = AgentEvaluator(run_llm=True)
        report = evaluator.run()
        print_eval_report(report)


if __name__ == "__main__":
    main()