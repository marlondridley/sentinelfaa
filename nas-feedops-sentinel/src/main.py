"""
NAS FeedOps Sentinel -- CLI entry point.
"""
from __future__ import annotations

import argparse
import os
import signal
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
from src.swim_file_ingest import request_shutdown, scan_swim_folder, watch_swim_folder
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
    max_critic_per_pass = int(os.getenv("MAX_CRITIC_PER_PASS", "5"))
    
    if critical_alerts:
        to_evaluate = critical_alerts[:max_critic_per_pass]
        skipped_count = len(critical_alerts) - len(to_evaluate)
        _log.info(
            "critic_evaluation_start",
            extra={
                "evaluating_count": len(to_evaluate),
                "skipped_count": skipped_count,
                "total_critical": len(critical_alerts),
            },
        )
        
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
        _log.info(
                "hitl_actions_staged",
                extra={"staged_count": staged_count},
            )
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


# ── Graceful shutdown ─────────────────────────────────────────────────────────
#
# On docker stop / ECS / Kubernetes termination the process receives SIGTERM.
# Left unhandled, Python's default action is immediate exit, which can drop an
# in-flight SWIM file mid-parse/mid-move. Installing a handler here lets the
# orchestrator's grace period (typically 30s) be used to finish the file
# currently being ingested -- see swim_file_ingest.request_shutdown(), which
# the scan loop polls between files -- rather than dropping it.

def _install_shutdown_handler() -> None:
    def _handle_shutdown_signal(signum: int, frame) -> None:
        _log.warning(
            "shutdown_signal_received",
            extra={"signal": signal.Signals(signum).name},
        )
        request_shutdown()

    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m src.main")
    parser.add_argument("--mode", choices=["sample", "replay", "demo", "eval"], default="demo")
    parser.add_argument("--path", type=str, default=None, help="Custom path for replay or folder scan")
    parser.add_argument(
        "--watch",
        action="store_true",
        default=False,
        help=(
            "replay mode only: keep running and watch the live SWIM folder for "
            "new files instead of scanning once and exiting. Reacts to "
            "filesystem events plus a periodic reconciliation scan "
            "(--reconcile-interval)."
        ),
    )
    parser.add_argument(
        "--reconcile-interval",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="Safety-net rescan interval in --watch mode (default 300s).",
    )
    args = parser.parse_args()

    configure_logging()
    _install_shutdown_handler()
    start_metrics_server()
    
    if args.mode == "demo":
        run_demo()
    elif args.mode == "sample":
        print("[SAMPLE] Generating sample event batch...")
        events = generate_sample_batch()
        _run_pipeline(events, "SAMPLE")
    elif args.mode == "replay" and args.watch:
        target_path = Path(args.path) if args.path else Path(config.SWIM_RAW_FOLDER) if hasattr(config, "SWIM_RAW_FOLDER") else Path("/app/data/raw")
        _log.info(
            "swim_folder_watch_start",
            extra={"target_path": str(target_path), "reconcile_interval": args.reconcile_interval},
        )

        def _on_events(events, stats) -> None:
            _log.info(
                "swim_watch_pipeline_dispatch",
                extra={"event_count": len(events), "files_processed": stats.files_processed},
            )
            _run_pipeline(events, "REPLAY")

        watch_swim_folder(
            target_path, reconcile_interval=args.reconcile_interval, on_events=_on_events
        )
    elif args.mode == "replay":
        target_path = Path(args.path) if args.path else Path(config.SWIM_RAW_FOLDER) if hasattr(config, "SWIM_RAW_FOLDER") else Path("/app/data/raw")
        _log.info("replay_scan_start", extra={"target_path": str(target_path)})
        events = scan_swim_folder(target_path)
        if not events:
            _log.info("replay_scan_empty_directory_fallback", extra={"target_path": str(target_path)})
            events, parse_errors = replay_from_directory(target_path)
            if parse_errors:
                _log.warning(
                    "replay_directory_parse_errors",
                    extra={"error_count": len(parse_errors), "target_path": str(target_path)},
                )
        if events:
            _run_pipeline(events, "REPLAY")
        else:
            _log.warning("replay_scan_no_events_found", extra={"target_path": str(target_path)})
    elif args.mode == "eval":
        evaluator = AgentEvaluator(run_llm=True)
        report = evaluator.run()
        print_eval_report(report)


if __name__ == "__main__":
    main()