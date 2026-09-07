"""
Central configuration for NAS FeedOps Sentinel.
All tunables are environment-overridable via .env; no credentials are hardcoded.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Clear empty env vars before loading .env so a stale "FOO=" in the shell
# doesn't silently shadow a real value in .env. Non-empty shell vars still
# win (so explicit overrides and test monkeypatching behave as expected).
for _k in list(os.environ):
    if os.environ[_k] == "":
        del os.environ[_k]
load_dotenv()

# ── Directory layout ──────────────────────────────────────────────────────────
BASE_DIR: Path = Path(__file__).parent.parent
DATA_DIR: Path = BASE_DIR / "data"
RAW_DIR: Path = DATA_DIR / "raw"
PROCESSED_DIR: Path = DATA_DIR / "processed"
DB_PATH: Path = DATA_DIR / "feedops.db"

# Default folder written by the FAA SCDS Jumpstart JMS consumer
SWIM_RAW_FOLDER: Path = Path(os.getenv("SWIM_RAW_FOLDER", str(DATA_DIR / "raw" / "swim_live")))

# ── NAS / SWIM domain constants ───────────────────────────────────────────────
SUPPORTED_AIRPORTS: set[str] = {"KDCA", "KIAD", "KBWI", "KADW"}
SUPPORTED_TRACON: str = "PCT"
SUPPORTED_SERVICES: set[str] = {
    "SMES",           # Surface Movement Event Service
    "APDS_RVR",       # Airport Data / Runway Visual Range
    "ISMC",           # Infrastructure, System Monitoring and Control
    "TOWER_DEPARTURE",
    "TERMINAL_AUTO",  # Terminal Automation Information
}

# ── Alert thresholds (all environment-overridable) ────────────────────────────
LATENCY_ALERT_MS: int = int(os.getenv("LATENCY_ALERT_MS", "30000"))
STALE_FEED_SECONDS: int = int(os.getenv("STALE_FEED_SECONDS", "300"))
SURFACE_SPIKE_COUNT: int = int(os.getenv("SURFACE_SPIKE_COUNT", "10"))
SURFACE_SPIKE_WINDOW_SECONDS: int = int(os.getenv("SURFACE_SPIKE_WINDOW_SECONDS", "60"))
RVR_LOW_THRESHOLD_FT: int = int(os.getenv("RVR_LOW_THRESHOLD_FT", "1200"))

# ── FAA SWIM / SCDS Jumpstart Kit connection values ───────────────────────────
# Obtain from SWIFT portal: Subscription Details > Connection Details tab
# Set in .env only — never commit real values to source control
SWIM_PROVIDER_URL: str = os.getenv("SWIM_PROVIDER_URL", "")
SWIM_QUEUE: str = os.getenv("SWIM_QUEUE", "")
SWIM_CONNECTION_FACTORY: str = os.getenv("SWIM_CONNECTION_FACTORY", "")
SWIM_USERNAME: str = os.getenv("SWIM_USERNAME", "")
SWIM_PASSWORD: str = os.getenv("SWIM_PASSWORD", "")
SWIM_VPN: str = os.getenv("SWIM_VPN", "")

# Legacy names kept for backwards compatibility
SWIM_BROKER_URL: str = os.getenv("SWIM_BROKER_URL", SWIM_PROVIDER_URL)
SWIM_QUEUE_NAME: str = os.getenv("SWIM_QUEUE_NAME", SWIM_QUEUE)

# ── Critic Agent ──────────────────────────────────────────────────────────────
CRITIC_ENABLED: bool = os.getenv("CRITIC_ENABLED", "1").strip().lower() not in ("0", "false", "no")
CRITIC_MODEL: str = os.getenv("CRITIC_MODEL", "claude-haiku-4-5-20251001")
