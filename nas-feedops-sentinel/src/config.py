"""
Central configuration for NAS FeedOps Sentinel.

All tunables are environment-overridable via a Pydantic Settings object.

The application uses LiteLLM as its single LLM gateway. Provider-specific
models and failover are managed by LiteLLM, not by the NAS application.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

from src.vault_config import VaultSettingsSource
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict, PydanticBaseSettingsSource
from pydantic import AliasChoices, Field, ValidationInfo, field_validator, model_validator

BASE_DIR: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = BASE_DIR / "data"
RAW_DIR: Path = DATA_DIR / "raw"
PROCESSED_DIR: Path = DATA_DIR / "processed"
FAILED_DIR: Path = DATA_DIR / "failed"


# NOTE: this module deliberately does NOT strip empty environment variables.
#
# It used to. Five lines here deleted every os.environ entry whose value was
# the empty string, on the theory that a stale FOO="" in a shell should not
# shadow .env. That trade was wrong three ways:
#
#   1. An empty value is the only way to say "ignore .env, use the computed
#      default" from a container's environment, and deleting it made every
#      such override do the exact opposite of what was asked -- silently.
#      `docker run -e DATABASE_URL= -e POSTGRES_DB=sentinel_test ...`, the
#      documented way to aim the suite at a throwaway database, resolved to
#      .env's nas-pgbouncer:6432/sentinel_db instead: the live database.
#      Only the current_database() check in src/storage/test_support.py kept
#      a full-suite run from TRUNCATE-ing production data, audit trail
#      included.
#   2. For the same reason `-e VAULT_ADDR= -e VAULT_TOKEN=` could not turn
#      the Vault settings source off. VaultSettingsSource short-circuits on
#      an empty address or token (src/vault_config.py), but both variables
#      were deleted before it ever ran, so .env's values were used and Vault
#      kept injecting POSTGRES_* above the environment.
#   3. It mutated global process state as an import side effect, across every
#      variable in the environment -- including ones this application does
#      not own (NO_PROXY="", PYTHONWARNINGS="", ...) and that other libraries
#      in the same process read.
#
# So: empty means empty. Both places where that matters already handle it --
# VaultSettingsSource returns {} on an empty address or token, and
# _defaults() below rebuilds DATABASE_URL from the POSTGRES_* settings
# whenever DATABASE_URL is empty.


def proxy_model(value: str, *, setting: str = "model") -> str:
    """
    Normalize a model setting to the LiteLLM proxy's OpenAI-compatible route.

    Model settings name a proxy alias -- a model_name in
    instraclustr/litellm-config.yaml such as "primary-model" -- and the client
    always reaches it as "openai/<alias>" against LITELLM_PROXY_URL.

    A bare alias ("gemini-2.5-flash") gets the prefix. Before this, that exact
    value (the compose default for CRITIC_MODEL) made litellm pick Google
    Vertex from the name and call it directly, bypassing the proxy -- and its
    provider allowlist, keys and logging -- and failing on missing google.auth.
    Any other provider prefix ("gemini/...", "anthropic/...") is refused for the
    same reason: it would route around the gateway.
    """
    text = (value or "").strip()
    if not text:
        return text
    if text.startswith("openai/"):
        return text
    if "/" in text:
        raise ValueError(
            f"{setting}={text!r} names a provider route. Model settings must be a "
            "LiteLLM proxy alias (e.g. 'primary-model'); every call goes through "
            "the proxy, never directly to a provider."
        )
    return f"openai/{text}"


class Settings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Credentials (Will be populated by Vault if active, else .env) ──
    # Only the proxy's scoped virtual key. Provider keys and the proxy master key
    # belong to the LiteLLM container, never to this application.
    LITELLM_API_KEY: str | None = None

    # ── Postgres and DB Configuration ──────────────────────────────────────
    POSTGRES_USER: str | None = None
    POSTGRES_PASSWORD: str | None = None
    POSTGRES_DB: str | None = None
    DB_USER: str | None = None
    DB_PASSWORD: str | None = None
    DB_NAME: str | None = None

    # ── Paths ────────────────────────────────────────────────────────────────
    INGEST_DIR: Path = RAW_DIR / "ingest"
    SWIM_RAW_FOLDER: Path = RAW_DIR / "swim_live"
    # Poll the SWIM folder instead of relying on native file events, which do
    # not reach a container through a Windows/macOS host bind mount.
    SWIM_WATCH_POLLING: bool = False
    SWIM_WATCH_POLL_SECONDS: float = 2.0

    # ── Database / PgBouncer ────────────────────────────────────────────────
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 6432
    DATABASE_URL: str = ""

    # ── NAS domain constants ────────────────────────────────────────────────
    SUPPORTED_AIRPORTS: Annotated[set[str], NoDecode] = Field(
        default_factory=lambda: {
            "KDCA",
            "KIAD",
            "KBWI",
            "KADW",
        }
    )

    SUPPORTED_TRACON: str = "PCT"

    SUPPORTED_SERVICES: Annotated[set[str], NoDecode] = Field(
        default_factory=lambda: {
            "SMES",
            "APDS_RVR",
            "ISMC",
            "TOWER_DEPARTURE",
            "TERMINAL_AUTO",
        }
    )

    # ── Alert thresholds ─────────────────────────────────────────────────────
    #
    # PROVENANCE. These values decide what the system calls an operational
    # fact, so where each one comes from matters as much as what it is.
    # Two different kinds are mixed here and they should not be defended
    # the same way:
    #
    #   DOMAIN-DERIVED -- traceable to an external aviation authority.
    #   Must not be changed casually, and must carry a citation.
    #
    #   ENGINEERING-CHOSEN -- our own SLO/heuristic for feed health.
    #   Legitimate to tune, but must be defended as an engineering
    #   judgement rather than presented as though an authority set it.
    #
    # RVR_LOW_THRESHOLD_FT = 1200
    #   DOMAIN-DERIVED -- CITATION STILL REQUIRED.
    #   1200 ft is one of the standard reported RVR steps and sits in the
    #   low-visibility band where approach/landing minima begin to bind.
    #   It is NOT a universal constant: the figure that actually governs
    #   is the minimum published for the specific approach in use on the
    #   specific runway. Before this is presented as authoritative it must
    #   be reconciled against the approach minima in effect for the
    #   runways in scope (KDCA / KIAD / KBWI / KADW) and the citation
    #   recorded here. Until then it is a defensible screening threshold
    #   for 'visibility low enough that a human should look' -- not a
    #   regulatory determination, and it should not be described as one.
    #
    # STALE_FEED_SECONDS = 300
    #   ENGINEERING-CHOSEN. Five minutes of silence is treated as possible
    #   silent feed failure. Known weakness: applied uniformly to every
    #   service in SWIM_SERVICES, but those feeds do not share a
    #   publication cadence -- a steady high-rate surface feed (SMES) and
    #   a low-rate status feed (ISMC) cannot both be well served by one
    #   number. The defensible version is per-service, derived from each
    #   feed's observed inter-message interval. Treat this single global
    #   value as a known simplification, not a measured threshold.
    #
    # LATENCY_ALERT_MS = 30000
    #   ENGINEERING-CHOSEN. Delivery-latency SLO for the ingest path.
    #   Same per-service caveat as STALE_FEED_SECONDS.
    #
    # SURFACE_SPIKE_COUNT = 10 / SURFACE_SPIKE_WINDOW_SECONDS = 60
    #   ENGINEERING-CHOSEN heuristic for a data loop or a genuine surface
    #   incident: >10 SMES events in 60 s. Not calibrated against a
    #   measured baseline of normal PCT surface-event rates; calibrating
    #   it against real traffic is what would make it defensible.
    #
    LATENCY_ALERT_MS: int = 30000
    STALE_FEED_SECONDS: int = 300
    SURFACE_SPIKE_COUNT: int = 10
    SURFACE_SPIKE_WINDOW_SECONDS: int = 60
    RVR_LOW_THRESHOLD_FT: int = 1200

    # SMES_POSITION_SAMPLE_SECONDS -- ENGINEERING-CHOSEN.
    #   ASDE-X positionReports arrive once per target per second, on whole
    #   seconds (measured 2026-09-25: ~86 reports/s across KIAD/KDCA/KBWI,
    #   ~7M rows/day unsampled). A report is kept when its time is a multiple
    #   of this many seconds, so every track is sampled at that interval and
    #   re-ingesting a message yields the same events. 10 kept 10% of reports
    #   and 95% of tracks in the measured sample. 1 keeps every report.
    SMES_POSITION_SAMPLE_SECONDS: int = 10

    # APDS_RVR_SCALE_FT -- multiplier converting a wire RVR reading to feet.
    #
    # CORROBORATED, NOT YET CONFIRMED AGAINST THE ICD. The wire format is
    # defined in the STDDS APDS service description (WSDD NAS-JMSDD-4307-001),
    # which is not public, so it is called out here, not buried in code.
    #
    # Evidence: the one captured APDS message
    # (tests/fixtures/swim_kit/apds_sample, KBWI, 2026-08-27T20:42Z) reports
    # touchdown/midpoint/rollout = 60 on all six runway ends. KBWI's ASOS
    # reported 10.00 statute miles visibility at 19:54, 20:54 and 21:54 UTC
    # that day (Iowa Environmental Mesonet ASOS archive, station BWI). At
    # 10 SM, RVR sits at the top of its reportable range, which in the US is
    # 6000 ft. A true reading of 60 ft would need dense fog -- well under
    # 1/16 SM -- on every runway at once. So the wire value is not plain
    # feet, and 60 x 100 = 6000 ft matches the observed weather exactly.
    #
    # What that does NOT prove: that low readings also scale by 100 (a coded
    # "at maximum" value would look the same). A capture taken in genuinely
    # low visibility, or the ICD, settles it; then drop this caveat.
    #
    # Both error directions are costly:
    #   too high -- a genuine low-RVR event reads as ~100x too far and
    #               ALERT_LOW_RVR never fires. A missed safety signal.
    #   too low  -- every routine message reads below RVR_LOW_THRESHOLD_FT
    #               and ALERT_LOW_RVR fires constantly; alarm fatigue.
    # The first is the silent one, so it is also guarded at runtime: see
    # APDS_RVR_MAX_PLAUSIBLE_FT below.
    APDS_RVR_SCALE_FT: int = 100

    # APDS_RVR_MAX_PLAUSIBLE_FT -- runtime check on APDS_RVR_SCALE_FT.
    #
    # ENGINEERING-CHOSEN. No US RVR system reports above 6000 ft. If the
    # wire value were already in feet, APDS_RVR_SCALE_FT = 100 would turn a
    # real 1800 ft reading into 180,000 ft -- and, being far above the
    # low-RVR threshold, it would never alert. Any scaled reading above this
    # bound therefore means the scale is wrong, and the message is refused
    # into data/failed/ with that diagnosis instead of being stored. The
    # bound is deliberately loose: a wrong scale is off by 100x, so exactly
    # where it sits between 6000 and ~100,000 ft does not matter.
    APDS_RVR_MAX_PLAUSIBLE_FT: int = 10_000

    # ── Agents / LLM ─────────────────────────────────────────────────────────
    CRITIC_ENABLED: bool = True
    AGENT_VERBOSE: bool = os.getenv("AGENT_VERBOSE", "0") == "1"
    # This is an APPLICATION-FACING LiteLLM alias.
    LLM_MODEL: str = "openai/primary-model"

    # Optional role-specific aliases.
    CRITIC_MODEL: str = ""
    PLANNER_MODEL: str = ""
    WATCH_OFFICER_MODEL: str = ""

    MAX_CRITIC_PER_PASS: int = 5
    LLM_TIMEOUT_SECONDS: float = 90.0

    # ── Model governance and readiness ──────────────────────────────────────
    # MODEL_POLICY_PATH: the model-use policy (Microsoft Agent Governance
    # Toolkit) consulted before every model call -- src/governance.py.
    # MODEL_SLO_PATH: the objectives `python -m src.readiness` measures model
    # behavior against -- src/readiness.py.
    MODEL_POLICY_PATH: Path = BASE_DIR / "policies" / "model-use.yaml"
    # TOOL_POLICY_PATH: which tools each advisory agent may invoke (the agent
    # layer's tool boundary; src/agent_tools.py). Refused when unloadable.
    TOOL_POLICY_PATH: Path = BASE_DIR / "policies" / "tool-use.yaml"
    MODEL_SLO_PATH: Path = BASE_DIR / "policies" / "model-slos.yaml"

    # ── Investigations (src/investigation/) ─────────────────────────────────
    # Every staged action gets an investigation. The first
    # MAX_LLM_INVESTIGATIONS_PER_PASS in a pipeline pass run the four
    # specialists with the model (4 calls each); the rest run the same flow
    # deterministically, so a burst of CRITICAL alerts cannot become an
    # unbounded burst of LLM calls, and no staged action goes uninvestigated.
    # Escalation-integrity reconciler (src/reconcile.py), run by the live
    # watcher: unstaged CRITICAL alerts younger than the window are staged
    # PENDING; older ones are recorded EXPIRED.
    RECONCILE_INTERVAL_SECONDS: float = 60.0
    RECONCILE_STAGE_WINDOW_MINUTES: int = 60

    INVESTIGATIONS_ENABLED: bool = True
    MAX_LLM_INVESTIGATIONS_PER_PASS: int = 3
    INVESTIGATION_WINDOW_MINUTES: int = 60
    INVESTIGATION_MODEL: str = ""

    # ── LiteLLM Proxy ────────────────────────────────────────────────────────
    # Every model call goes through this gateway; nothing talks to a provider
    # directly. Accepts LLM_BASE_URL as well: docker-compose.yml and .env set
    # that name, and when only LITELLM_PROXY_URL was read, every container
    # silently fell back to localhost:4000 -- nothing listening -- so the
    # Critic and Watch Officer never reached a model and degraded quietly.
    LITELLM_PROXY_URL: str = Field(
        default="http://localhost:4000/v1",
        validation_alias=AliasChoices("LITELLM_PROXY_URL", "LLM_BASE_URL"),
    )

    # ── Temporal ─────────────────────────────────────────────────────────────
    TEMPORAL_HOST: str = "localhost:7233"
    TEMPORAL_NAMESPACE: str = "default"
    TEMPORAL_TASK_QUEUE: str = "nas-feedops-queue"

    TEMPORAL_WORKER_MAX_CONCURRENT_ACTIVITIES: int = 20
    TEMPORAL_WORKER_MAX_CONCURRENT_WORKFLOWS: int = 100

    # ── Observability ────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    METRICS_PORT: int = 9108

    # Disabled by default: no OTel collector is wired into docker-compose.yml
    # yet (Grafana only has a Prometheus datasource provisioned, no Tempo/Jaeger),
    # so leaving this True with nothing listening on OTEL_EXPORTER_OTLP_ENDPOINT
    # just means every crewai/Flow span export batch times out against a dead
    # address on every run. Flip to True once a real trace backend is added.
    OTEL_TRACING_ENABLED: bool = False
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://localhost:4317"

    # ── Security & API ───────────────────────────────────────────────────────
    # API_KEY is the legacy single shared secret, kept for backwards compatibility:
    # any caller presenting it authenticates as the "legacy_api_key" principal.
    # API_KEYS (optional) maps distinct secrets to named principals -- e.g.
    #   API_KEYS={"abc123...": "ingest-service", "def456...": "ops-cron"}
    # -- so the audit trail can say *which* caller ingested/acted, not just "someone
    # with the key". Set as a JSON object string in the environment/.env.
    API_KEY: str | None = None
    API_KEYS: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)
    # With no API key configured, /v1/ingest refuses every call (503) unless
    # this is false. It used to fail open -- and in practice the API was only
    # protected because it silently accepted the LiteLLM proxy key instead.
    API_AUTH_REQUIRED: bool = True
    API_DOCS_ENABLED: bool = False

    # Self-declared (typed-callsign) operators may approve/override/reject only
    # when this is true: a single-user lab with no identity provider. Off, a
    # decision needs a verified Keycloak sign-in holding the right realm role.
    ALLOW_UNVERIFIED_DECISIONS: bool = False

    # Run schema bootstrap + migrations whenever a process opens its pool.
    # True for tests and local runs. The compose services set it false and run
    # `python -m src.storage.migrate` once, as the schema owner, so the
    # application role never needs DDL rights.
    RUN_MIGRATIONS_ON_CONNECT: bool = True
    CORS_ALLOWED_ORIGINS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "http://localhost:8501",
            "http://localhost:3000",
            "http://127.0.0.1:8501",
            "http://127.0.0.1:3000",
        ]
    )

    # ── Vault ────────────────────────────────────────────────────────────────
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

    @field_validator(
        "API_KEYS", "CORS_ALLOWED_ORIGINS", "SUPPORTED_AIRPORTS", "SUPPORTED_SERVICES",
        mode="before",
    )
    @classmethod
    def _parse_collection(cls, value: Any, info: ValidationInfo) -> Any:
        """
        Parse list/set/dict settings from their environment string.

        These fields are marked NoDecode so the raw string reaches this
        validator. Otherwise pydantic-settings JSON-decodes it first, and an
        empty value -- which is exactly what docker-compose's
        `- API_KEYS=${API_KEYS:-}` passes when the variable is unset --
        raised SettingsError at import and crash-looped nas-api.

          ""                  -> the field's default. An empty string is how
                                 compose spells "not configured"; it cannot
                                 mean an empty collection, and an empty
                                 SUPPORTED_AIRPORTS would reject every event.
          '["a","b"]' / '{}'  -> JSON.
          "a, b"              -> comma-separated (lists and sets only).

        API_KEYS must be a JSON object; anything else is an error rather than
        a silently empty key map.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return cls.model_fields[info.field_name].get_default(call_default_factory=True)

        import json as _json

        if info.field_name == "API_KEYS":
            parsed = _json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError("API_KEYS must be a JSON object mapping key -> principal name")
            return parsed
        if text[0] == "[":
            return _json.loads(text)
        return [part.strip() for part in text.split(",") if part.strip()]

    @field_validator("CRITIC_ENABLED", "INVESTIGATIONS_ENABLED", mode="before")
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

        if not self.INVESTIGATION_MODEL:
            self.INVESTIGATION_MODEL = self.LLM_MODEL

        for name in ("LLM_MODEL", "CRITIC_MODEL", "PLANNER_MODEL", "WATCH_OFFICER_MODEL", "INVESTIGATION_MODEL"):
            setattr(self, name, proxy_model(getattr(self, name), setting=name))

        if self.CRITIC_ENABLED and not self.CRITIC_MODEL and not self.LLM_MODEL:
            raise ValueError(
                "CRITIC_ENABLED is true but no model is configured: both "
                "CRITIC_MODEL and LLM_MODEL resolve to an empty string. Set "
                "LLM_MODEL (or CRITIC_MODEL specifically) in the environment, "
                "or set CRITIC_ENABLED=false to disable the critic agent."
            )

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