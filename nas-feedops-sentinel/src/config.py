"""
Central configuration for NAS FeedOps Sentinel[cite: 4].

All tunables are environment-overridable via a Pydantic Settings object[cite: 4].

The application uses LiteLLM as its single LLM gateway[cite: 4]. Provider-specific
models and failover are managed by LiteLLM, not by the NAS application[cite: 4].
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from src.vault_config import VaultSettingsSource
from pydantic_settings import BaseSettings, SettingsConfigDict, PydanticBaseSettingsSource
from pydantic import Field, field_validator, model_validator

BASE_DIR: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = BASE_DIR / "data"
RAW_DIR: Path = DATA_DIR / "raw"
PROCESSED_DIR: Path = DATA_DIR / "processed"
FAILED_DIR: Path = DATA_DIR / "failed"


# Clear empty environment variables before Settings loads .env[cite: 4].
# This prevents a stale FOO="" in the shell from shadowing .env values[cite: 4].
for _k in list(os.environ):
    if os.environ[_k] == "":
        del os.environ[_k]


class Settings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Credentials (Will be populated by Vault if active, else .env) ──[cite: 4]
    LITELLM_API_KEY: str | None = None
    LITELLM_MASTER_KEY: str | None = None
    OPENAI_API_KEY: str | None = None
    GEMINI_API_KEY: str | None = None
    DEEPSEEK_API_KEY: str | None = None

    # ── Postgres and DB Configuration ──────────────────────────────────────[cite: 4]
    POSTGRES_USER: str | None = None
    POSTGRES_PASSWORD: str | None = None
    POSTGRES_DB: str | None = None
    DB_USER: str | None = None
    DB_PASSWORD: str | None = None
    DB_NAME: str | None = None

    # ── Paths ────────────────────────────────────────────────────────────────[cite: 4]
    INGEST_DIR: Path = RAW_DIR / "ingest"
    SWIM_RAW_FOLDER: Path = RAW_DIR / "swim_live"

    # ── Database / PgBouncer ────────────────────────────────────────────────[cite: 4]
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 6432
    DATABASE_URL: str = ""

    # ── NAS domain constants ────────────────────────────────────────────────[cite: 4]
    SUPPORTED_AIRPORTS: set[str] = Field(
        default_factory=lambda: {
            "KDCA",
            "KIAD",
            "KBWI",
            "KADW",
        }
    )

    SUPPORTED_TRACON: str = "PCT"

    SUPPORTED_SERVICES: set[str] = Field(
        default_factory=lambda: {
            "SMES",
            "APDS_RVR",
            "ISMC",
            "TOWER_DEPARTURE",
            "TERMINAL_AUTO",
        }
    )

    # ── Alert thresholds ─────────────────────────────────────────────────────[cite: 4]
    LATENCY_ALERT_MS: int = 30000
    STALE_FEED_SECONDS: int = 300
    SURFACE_SPIKE_COUNT: int = 10
    SURFACE_SPIKE_WINDOW_SECONDS: int = 60
    RVR_LOW_THRESHOLD_FT: int = 1200

    # ── Agents / LLM ─────────────────────────────────────────────────────────[cite: 4]
    CRITIC_ENABLED: bool = True
    AGENT_VERBOSE: bool = os.getenv("AGENT_VERBOSE", "0") == "1"
    # This is an APPLICATION-FACING LiteLLM alias[cite: 4].
    LLM_MODEL: str = "openai/primary-model"

    # Optional role-specific aliases[cite: 4].
    CRITIC_MODEL: str = ""
    PLANNER_MODEL: str = ""
    WATCH_OFFICER_MODEL: str = ""

    MAX_CRITIC_PER_PASS: int = 5

    # ── LiteLLM Proxy ────────────────────────────────────────────────────────[cite: 4]
    LITELLM_PROXY_URL: str = "http://localhost:4000/v1"

    # ── Temporal ─────────────────────────────────────────────────────────────[cite: 4]
    TEMPORAL_HOST: str = "localhost:7233"
    TEMPORAL_NAMESPACE: str = "default"
    TEMPORAL_TASK_QUEUE: str = "nas-feedops-queue"

    TEMPORAL_WORKER_MAX_CONCURRENT_ACTIVITIES: int = 20
    TEMPORAL_WORKER_MAX_CONCURRENT_WORKFLOWS: int = 100

    # ── Observability ────────────────────────────────────────────────────────[cite: 4]
    LOG_LEVEL: str = "INFO"
    METRICS_PORT: int = 9108

    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://localhost:4317"

    # ── Security & API ───────────────────────────────────────────────────────
    API_KEY: str | None = None
    CORS_ALLOWED_ORIGINS: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:8501",
            "http://localhost:3000",
            "http://127.0.0.1:8501",
            "http://127.0.0.1:3000",
        ]
    )

    # ── Vault ────────────────────────────────────────────────────────────────[cite: 4]
    VAULT_ADDR: str = "http://localhost:8200"
    VAULT_TOKEN: str = ""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Priority: Init args > Vault > Environment Variables > .env file
        return (
            init_settings,
            VaultSettingsSource(settings_cls),
            env_settings,
            dotenv_settings,
        )

    @field_validator("CRITIC_ENABLED", mode="before")
    @classmethod
    def _parse_bool(cls, value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
                "",
            }
        return bool(value)

    @model_validator(mode="after")
    def _defaults(self) -> Settings:
        if not self.DATABASE_URL:
            self.DATABASE_URL = (
                f"postgresql://"
                f"{self.POSTGRES_USER}:"
                f"{self.POSTGRES_PASSWORD}@"
                f"{self.POSTGRES_HOST}:"
                f"{self.POSTGRES_PORT}/"
                f"{self.POSTGRES_DB}"
            )

        if not self.CRITIC_MODEL:
            self.CRITIC_MODEL = self.LLM_MODEL

        if not self.PLANNER_MODEL:
            self.PLANNER_MODEL = self.LLM_MODEL

        if not self.WATCH_OFFICER_MODEL:
            self.WATCH_OFFICER_MODEL = self.LLM_MODEL

        return self


settings = Settings()


def __getattr__(name: str) -> Any:
    """
    Backwards-compatible access such as:

        config.CRITIC_MODEL

    while the canonical pattern remains:

        config.settings.CRITIC_MODEL
    """
    return getattr(settings, name)