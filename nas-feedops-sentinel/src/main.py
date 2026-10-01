"""
NAS FeedOps Sentinel -- CLI entry point.
"""
from __future__ import annotations

import argparse
import signal
from pathlib import Path

from src import config
from src.async_bridge import run_async
from src.agent_evaluator import AgentEvaluator, print_eval_report
from src.logging_config import configure_logging, get_logger, new_correlation_id
from src.metrics import start_metrics_server
from src.models import NASEvent
from src.pipeline import run_pipeline
from src.replay import replay_from_directory
from src.swim_file_ingest import (
    commit_scan,
    isolate_poison,
    request_shutdown,
    scan_swim_folder,
    watch_swim_folder,
)

_log = get_logger(__name__)


def _run_pipeline(events: list[NASEvent], label: str = "PIPELINE") -> dict:
    """
    Run the one shared pipeline (src/pipeline.py) from synchronous CLI code.

    This used to be a second, divergent copy of the pipeline built on CrewAI.
    It skipped the Critic's guardrail and redaction, and any LLM error raised
    straight out of crew.kickoff() -- after events and alerts were committed
    but before staging -- so CRITICAL alerts from the live watcher were never
    staged for human review. Every entry point now goes through
    run_pipeline(), whose staging does not depend on the LLM succeeding.
    """
    return run_async(run_pipeline(events, label=label, correlation_id=new_correlation_id()))


# ── Graceful shutdown ─────────────────────────────────────────────────────────
#
# On docker stop / ECS / Kubernetes termination the process receives SIGTERM.
# Left unhandled, Python's default action is immediate exit, which can drop an
# in-flight SWIM file mid-parse/mid-move. Installing a handler here lets the
# orchestrator's grace period (typically 30s) be used to finish the file
# currently being ingested -- see swim_file_ingest.request_shutdown(), which
# the scan loop polls between files -- rather than dropping it.

def _start_reconciler() -> None:
    """
    Run the escalation-integrity reconciler (src/reconcile.py) every
    RECONCILE_INTERVAL_SECONDS in a daemon thread, alongside the watcher.
    run_async serializes it with the watcher's own storage calls.
    """
    import threading
    import time

    from src.reconcile import reconcile_unstaged
    from src.swim_file_ingest import shutdown_requested

    def _loop() -> None:
        while not shutdown_requested():
            try:
                run_async(reconcile_unstaged())
            except Exception as exc:  # noqa: BLE001 -- keep reconciling; the metric shows failure
                _log.error("reconcile_failed", extra={"error": str(exc)})
            time.sleep(config.RECONCILE_INTERVAL_SECONDS)

    threading.Thread(target=_loop, daemon=True, name="reconciler").start()


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
    # Real data only: replay captured SWIM files or watch the live feed. The
    # generators that used to be modes here wrote synthetic events into the
    # operational database; they now exist only as test fixtures.
    parser.add_argument("--mode", choices=["replay", "eval"], default="replay")
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
    
    if args.mode == "replay" and args.watch:
        _start_reconciler()
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
            try:
                _run_pipeline(events, "REPLAY")
            except Exception as exc:  # noqa: BLE001
                # About the data: store file by file and quarantine the offender. About the database: the claims
                # are released and this raises, and the watcher tries the folder again.
                isolate_poison(events, stats, lambda file_events: _run_pipeline(file_events, "REPLAY"), exc)

        watch_swim_folder(
            target_path, reconcile_interval=args.reconcile_interval, on_events=_on_events
        )
    elif args.mode == "replay":
        target_path = Path(args.path) if args.path else Path(config.SWIM_RAW_FOLDER) if hasattr(config, "SWIM_RAW_FOLDER") else Path("/app/data/raw")
        _log.info("replay_scan_start", extra={"target_path": str(target_path)})
        # scan_swim_folder returns (events, stats); this used to bind the whole
        # tuple to `events`, which is never falsy, so the directory fallback
        # below could not run and the pipeline was handed a 2-tuple.
        # Captured messages replayed once are a replay, not the live feed: stored as "live" they refreshed each
        # feed's last-seen time and could hide a feed that had really gone quiet. --watch stays live.
        events, stats = scan_swim_folder(target_path, defer_archive=True, data_origin="replay")
        if not events:
            _log.info("replay_scan_empty_directory_fallback", extra={"target_path": str(target_path)})
            events, parse_errors = replay_from_directory(target_path)
            if parse_errors:
                _log.warning(
                    "replay_directory_parse_errors",
                    extra={"error_count": len(parse_errors), "target_path": str(target_path)},
                )
        if events:
            try:
                _run_pipeline(events, "REPLAY")
            except Exception as exc:  # noqa: BLE001
                isolate_poison(events, stats, lambda file_events: _run_pipeline(file_events, "REPLAY"), exc)
        else:
            _log.warning("replay_scan_no_events_found", extra={"target_path": str(target_path)})
        # Archived and logged only now that the events are stored. Also when there were none: messages that
        # parsed to nothing are finished, and left claimed they were never logged and sat in .inflight/.
        commit_scan(stats)
    elif args.mode == "eval":
        evaluator = AgentEvaluator(run_llm=True)
        report = evaluator.run()
        print_eval_report(report)


if __name__ == "__main__":
    main()