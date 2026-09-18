"""
NAS FeedOps Sentinel -- CLI entry point.

Usage:
    python -m src.main --mode sample --count 100
    python -m src.main --mode replay --source data/raw
    python -m src.main --mode swim-folder --source data/raw/swim_live
    python -m src.main --mode swim-folder --watch --reconcile-interval 300
    python -m src.main --mode live
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from src import config
from src.agent_evaluator import AgentEvaluator, print_eval_report
from src.alert_engine import check_alerts
from src.critic_agent import CriticAgent
from src.execution_gate import stage_critical_alert
from src.demo_scenario import generate_cascade_failure_scenario
from src.feed_health import compute_feed_health
from src.logging_config import configure_logging, get_logger, new_correlation_id
from src.metrics import (
    ALERTS_TRIGGERED,
    EVENTS_INGESTED,
    EVENTS_VALIDATED,
    GATE_DECISIONS,
    HITL_QUEUE_DEPTH,
    set_airport_status,
    start_metrics_server,
)
from src.models import NASEvent
from src.planner_agent import PlannerAgent
from src.replay import replay_from_directory, write_sample_replay_file
from src.sample_data import generate_sample_batch
from src.storage import (
    save_alerts,
    save_critic_result,
    save_events,
    save_planner_result,
)
from src.swim_live_adapter import connect_and_consume
from src.validators import validate_event


_log = get_logger(__name__)


# ── Shared pipeline ───────────────────────────────────────────────────────────


def _run_pipeline(events: list[NASEvent], label: str = "PIPELINE") -> None:
    """Validate, persist, alert, and summarise a batch of NASEvents."""
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

    save_events(events)

    alerts = check_alerts(events)
    save_alerts(alerts)

    for a in alerts:
        ALERTS_TRIGGERED.labels(
            alert_type=a.alert_type,
            severity=a.severity,
            airport=a.airport_code or "unknown",
        ).inc()

    # Critic evaluation — runs on CRITICAL alerts only; degrades gracefully if API key absent.
    # Capped to MAX_CRITIC_PER_PASS to keep latency + cost bounded under backlog.
    critic = CriticAgent()
    critical_alerts = [a for a in alerts if a.severity == "CRITICAL"]
    critic_results = {}
    max_critic_per_pass = int(os.getenv("MAX_CRITIC_PER_PASS", "5"))
    if critical_alerts:
        to_evaluate = critical_alerts[:max_critic_per_pass]
        skipped = len(critical_alerts) - len(to_evaluate)
        _log.info(
            "critic_evaluation_start",
            extra={
                "evaluating_count": len(to_evaluate),
                "skipped_count": skipped,
                "total_critical": len(critical_alerts),
            },
        )
        for alert in to_evaluate:
            result = critic.critique(alert)
            save_critic_result(result)
            critic_results[alert.alert_id] = result

    # HITL execution gate — every CRITICAL alert is staged for human approval.
    # The Critic verdict (if any) informs the rationale; deterministic text used otherwise.
    # The LLM never sees the approval/reject API; staging is enforced here.
    staged_count = 0
    for alert in critical_alerts:
        critic_for_alert = critic_results.get(
            alert.alert_id
        )  # may be None if skipped or disabled
        action = stage_critical_alert(alert, critic_for_alert)
        if action is not None:
            staged_count += 1
    if staged_count:
        _log.info("hitl_actions_staged", extra={"staged_count": staged_count})

    health = compute_feed_health(events)
    for airport, status in health["airport_status"].items():
        set_airport_status(airport, status)

    passed = sum(1 for e in events if e.validation_status == "PASSED")
    failed = len(events) - passed

    sep = "-" * 50
    print()
    print(f"[{label}] {sep}")
    print(f"  Events processed : {len(events)}")
    print(f"  Validation passed: {passed}")
    print(f"  Validation failed: {failed}")
    print(f"  Alerts triggered : {len(alerts)}")
    print(f"  Airport status   : {health['airport_status']}")
    print(f"  SMES counts      : {health['smes_counts']}")

    if alerts:
        print(f"\n  Top alerts (showing up to 10 of {len(alerts)}):")
        for alert in alerts[:10]:
            print(f"    [{alert.severity:8s}] {alert.alert_type}: {alert.description}")
            cr = critic_results.get(alert.alert_id)
            if cr:
                conf_str = (
                    f"{cr.confidence:.2f}" if cr.confidence is not None else "N/A"
                )
                escalate_flag = " [ESCALATE]" if cr.escalate else ""
                print(
                    f"             Critic: conf={conf_str} pattern={cr.pattern}{escalate_flag}"
                )
                print(f"             {cr.reasoning}")
        if len(alerts) > 10:
            print(
                f"    ... and {len(alerts) - 10} more. Run the dashboard for full list."
            )

    print()
    print("  Dashboard: streamlit run dashboard/app.py")
    print(f"[{label}] Done. {sep}")


# ── Mode handlers ─────────────────────────────────────────────────────────────


def run_sample(count: int) -> None:
    print(f"[SAMPLE] Generating {count} synthetic NAS events ...")
    events = generate_sample_batch(count)
    replay_file = write_sample_replay_file(events)
    print(f"[SAMPLE] Replay file saved: {replay_file}")
    _run_pipeline(events, "SAMPLE")


def run_replay(source: Path) -> None:
    if not source.exists():
        _log.error("replay_source_not_found", extra={"source": str(source)})
        print(f"[REPLAY] Source directory not found: {source}")
        print("[REPLAY] Generate data first:  python -m src.main --mode sample")
        sys.exit(1)

    _log.info("replay_scan_start", extra={"source": str(source)})
    events, errors = replay_from_directory(source)

    if errors:
        _log.warning(
            "replay_directory_parse_errors",
            extra={
                "error_count": len(errors),
                "source": str(source),
                "sample_errors": errors[:5],
            },
        )
        print(f"[REPLAY] {len(errors)} parse error(s):")
        for err in errors[:5]:
            print(f"  {err}")
        if len(errors) > 5:
            print(f"  ... and {len(errors) - 5} more.")

    if not events:
        _log.warning("replay_scan_no_events_found", extra={"source": str(source)})
        print("[REPLAY] No events could be loaded. Aborting.")
        sys.exit(1)

    _run_pipeline(events, "REPLAY")


def run_swim_folder(source: Path) -> None:
    """
    Scan a local folder written by the FAA SCDS Jumpstart JMS consumer,
    parse new files into NASEvents, and run the full validation + alert pipeline.

    Architecture:
      FAA SWIFT/SCDS Jumpstart Kit writes raw messages to --source.
      This mode scans that folder, ingests new files, moves them to
      data/processed/ or data/failed/, and updates the ingest audit log.
    """
    from src.swim_file_ingest import scan_swim_folder

    source.mkdir(parents=True, exist_ok=True)
    _log.info("swim_folder_scan_start", extra={"source": str(source)})
    print(f"[SWIM-FOLDER] Scanning {source} for new STDDS messages ...")
    events, stats = scan_swim_folder(source)

    print(f"[SWIM-FOLDER] Files detected  : {stats.files_detected}")
    print(f"[SWIM-FOLDER] Files processed : {stats.files_processed}")
    print(f"[SWIM-FOLDER] Files failed    : {stats.files_failed}")
    print(f"[SWIM-FOLDER] Events parsed   : {stats.events_parsed}")

    if not events:
        print("[SWIM-FOLDER] No new events to process.")
        print(f"[SWIM-FOLDER] Drop .json / .xml / .txt files into {source}")
        print("[SWIM-FOLDER] The FAA SCDS Jumpstart Kit will write files here")
        print("              when connected to your STDDS subscription.")
        return

    _run_pipeline(events, "SWIM-FOLDER")


def run_swim_folder_watch(source: Path, reconcile_interval: float) -> None:
    """
    Long-running counterpart to run_swim_folder(): watches --source for new
    files (filesystem events) and re-scans it on a reconcile_interval-second
    timer regardless, as a safety net. Runs until SIGTERM/SIGINT.

    Each scan pass's events are fed into the same _run_pipeline() used by the
    one-shot path, so validation/alerting/critic/HITL staging all run exactly
    as they do today -- this only changes how often scan_swim_folder() runs.
    """
    from src.swim_file_ingest import watch_swim_folder

    source.mkdir(parents=True, exist_ok=True)
    _log.info(
        "swim_folder_watch_start",
        extra={"source": str(source), "reconcile_interval": reconcile_interval},
    )
    print(
        f"[SWIM-FOLDER] Watching {source} for new STDDS messages "
        f"(reconcile every {reconcile_interval:.0f}s) ... Ctrl+C to stop."
    )

    def _on_events(events, stats) -> None:
        print(
            f"[SWIM-FOLDER] {stats.events_parsed} event(s) from "
            f"{stats.files_processed} file(s); running pipeline ..."
        )
        _run_pipeline(events, "SWIM-FOLDER")

    watch_swim_folder(
        source, reconcile_interval=reconcile_interval, on_events=_on_events
    )
    print("[SWIM-FOLDER] Watch stopped.")


def run_demo() -> None:
    """
    Cascade failure demo: reproducible KDCA multi-service degradation
    followed by a Planner natural language query over the results.
    """
    sep = "=" * 60
    print()
    print(sep)
    print("  NAS FeedOps Sentinel -- DEMO MODE")
    print("  Scenario: CASCADE FAILURE at KDCA (PCT TRACON)")
    print(sep)
    print()

    print("[DEMO] Generating cascade failure scenario ...")
    events = generate_cascade_failure_scenario()
    for event in events:
        result = validate_event(event)
        event.validation_status = "PASSED" if result.passed else "FAILED"
        event.validation_errors = result.errors

    _run_pipeline(events, "DEMO")

    # Planner query over the scenario
    print()
    print(sep)
    print("  PLANNER AGENT QUERY")
    print(sep)
    question = (
        "What is the current operational status of PCT TRACON "
        "and what should the duty manager do right now?"
    )
    print(f"  Q: {question}")
    print()

    planner = PlannerAgent()
    result = planner.query(question)
    save_planner_result(result)

    conf_str = f"{result.confidence:.0%}" if result.confidence is not None else "N/A"
    print(f"  Confidence : {conf_str}")
    print(f"  Answer     : {result.answer}")

    if result.recommended_actions:
        print()
        print("  Recommended Actions:")
        for i, action in enumerate(result.recommended_actions, 1):
            print(f"    {i}. {action}")

    if result.sub_queries:
        print()
        print("  Sub-queries analyzed:")
        for q in result.sub_queries:
            print(f"    - {q}")

    print()
    print(sep)
    print("  Dashboard: streamlit run dashboard/app.py")
    print(sep)


def run_eval(llm: bool = False) -> None:
    """Run the agent evaluation suite against the golden scenario dataset."""
    sep = "=" * 60
    print()
    print(sep)
    print("  NAS FeedOps Sentinel -- EVAL MODE")
    llm_note = (
        "LLM evals ENABLED" if llm else "deterministic only (add --llm for LLM evals)"
    )
    print(f"  {llm_note}")
    print(sep)
    evaluator = AgentEvaluator(run_llm=llm)
    report = evaluator.run()
    print_eval_report(report)


def run_live() -> None:
    connect_and_consume()


# ── Argument parsing ──────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="NAS FeedOps Sentinel -- FAA SWIM/STDDS feed automation framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.main --mode sample --count 200
  python -m src.main --mode replay --source data/raw
  python -m src.main --mode swim-folder --source data/raw/swim_live
  python -m src.main --mode live
  streamlit run dashboard/app.py
""",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        default=False,
        help="Enable LLM-based eval checks (requires ANTHROPIC_API_KEY). Used with --mode eval.",
    )
    parser.add_argument(
        "--metrics",
        action="store_true",
        default=False,
        help="Start Prometheus /metrics HTTP server on --metrics-port.",
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=9108,
        metavar="PORT",
        help="Port for Prometheus /metrics endpoint (default 9108).",
    )
    parser.add_argument(
        "--mode",
        choices=["sample", "replay", "swim-folder", "live", "demo", "eval"],
        default="sample",
        help=(
            "sample: generated data | replay: from data/raw JSON | "
            "swim-folder: scan FAA Jumpstart output folder | live: SWIM stub | "
            "demo: CASCADE_FAILURE scenario + Planner query"
        ),
    )
    parser.add_argument(
        "--count",
        type=int,
        default=100,
        metavar="N",
        help="Number of events to generate (sample mode only, default 100)",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Source directory. Defaults to data/raw for replay, "
            "data/raw/swim_live for swim-folder."
        ),
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        default=False,
        help=(
            "swim-folder mode only: keep running and watch --source for new "
            "files instead of scanning once and exiting. Reacts to filesystem "
            "events plus a periodic reconciliation scan (--reconcile-interval)."
        ),
    )
    parser.add_argument(
        "--reconcile-interval",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="Safety-net rescan interval in --watch mode (default 300s).",
    )
    return parser


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
        from src.swim_file_ingest import request_shutdown

        _log.warning(
            "shutdown_signal_received",
            extra={"signal": signal.Signals(signum).name},
        )
        request_shutdown()

    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    configure_logging()
    _install_shutdown_handler()
    if args.metrics:
        start_metrics_server(port=args.metrics_port)
        _log.info("metrics_server_started", extra={"port": args.metrics_port})

    if args.mode == "sample":
        run_sample(args.count)
    elif args.mode == "replay":
        source = args.source or config.RAW_DIR
        run_replay(source)
    elif args.mode == "swim-folder":
        source = args.source or (config.DATA_DIR / "raw" / "swim_live")
        if args.watch:
            run_swim_folder_watch(source, reconcile_interval=args.reconcile_interval)
        else:
            run_swim_folder(source)
    elif args.mode == "live":
        run_live()
    elif args.mode == "demo":
        run_demo()
    elif args.mode == "eval":
        run_eval(llm=args.llm)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
