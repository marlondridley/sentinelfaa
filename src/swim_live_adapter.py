"""
FAA SWIM Live Adapter — placeholder for future JMS/STOMP queue integration.

Implementation roadmap:
  1. Obtain FAA SWIM publisher access at https://www.faa.gov/air_traffic/technology/swim/
  2. Set SWIM_BROKER_URL, SWIM_QUEUE_NAME, SWIM_USERNAME, SWIM_PASSWORD in .env
  3. Install a JMS-compatible client, e.g.:
       pip install stomp.py       # for ActiveMQ / STOMP protocol
       pip install jpype1         # for raw JMS if the broker requires it
  4. Replace connect_and_consume() with a real subscription loop that:
       a. Connects to the ActiveMQ/Solace broker with the SWIM credentials.
       b. Subscribes to the STDDS queues (SMES, APDS_RVR, ISMC, etc.).
       c. Parses incoming XML/JSON payloads into NASEvent objects.
       d. Feeds events into validators.validate_event() and alert_engine.check_alerts().
       e. Persists events and alerts via storage.save_events() / save_alerts().
  5. For XML parsing, FAA SWIM publishes FIXM/AIXM-compliant payloads; use lxml or
     the standard xml.etree library to extract fields into NASEvent attributes.
"""
from __future__ import annotations

from src import config


def connect_and_consume() -> None:
    """Entry point for live mode. Currently a descriptive placeholder."""
    _banner("NAS FeedOps Sentinel — LIVE MODE (Placeholder)")
    print("  Live SWIM adapter is not yet connected.")
    print()
    print("  To enable live data ingestion you need:")
    print("    1. FAA SWIM publisher credentials (ActiveMQ broker URL, queue")
    print("       name, username, password).")
    print("    2. A JMS-compatible Python client library (e.g., stomp.py).")
    print("    3. Replacement of this stub with a real subscription loop.")
    print("       See the module docstring for a detailed implementation guide.")
    print()
    print("  Configure credentials without hardcoding — set these in .env:")
    print(f"    SWIM_BROKER_URL  = {config.SWIM_BROKER_URL or '(not set)'}")
    print(f"    SWIM_QUEUE_NAME  = {config.SWIM_QUEUE_NAME or '(not set)'}")
    print(f"    SWIM_USERNAME    = {'*' * len(config.SWIM_USERNAME) or '(not set)'}")
    print(f"    SWIM_PASSWORD    = {'*' * len(config.SWIM_PASSWORD) or '(not set)'}")
    print()
    print("  FAA SWIM program information:")
    print("    https://www.faa.gov/air_traffic/technology/swim/")
    print()
    print("  Run sample or replay mode to exercise the framework without")
    print("  a live feed connection:")
    print("    python -m src.main --mode sample --count 100")
    print("    python -m src.main --mode replay --source data/raw")
    _banner("Exiting live mode stub")


def _banner(text: str) -> None:
    width = 70
    print("=" * width)
    print(f"  {text}")
    print("=" * width)
