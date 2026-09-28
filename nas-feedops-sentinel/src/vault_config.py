"""
Secrets for the application process: read from the file this container's
Vault Agent sidecar renders. The process itself never authenticates to Vault,
constructs a Vault client, holds a Vault token, or has any credential on
disk -- see the header of docker-compose.yml.

How the file gets there (instraclustr/README.md, "Workload identity"):
spire-agent attests the service's spiffe-helper sidecar and issues it the
service's own X.509-SVID; the Vault Agent sidecar logs in to Vault over
mutual TLS with it and renders what the service's policy allows
(instraclustr/vault-agent/*.hcl) to a memory-only volume this container
mounts read-only, at VAULT_AGENT_ENV_FILE.

Unset or empty VAULT_AGENT_ENV_FILE (tests, local runs) turns the source
off: settings then come from the environment and .env alone.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from pydantic_settings import PydanticBaseSettingsSource

from src.logging_config import get_logger

_log = get_logger(__name__)

# Secrets that must be present in the rendered file, or the process refuses to
# start rather than run with a missing/empty credential.
_REQUIRED_AFTER_VAULT = ("LITELLM_API_KEY",)

# What the rendered file may set: credentials, nothing else. This source
# outranks the environment (src/config.py), so a key outside this list -- a
# template edit, or a secret written to Vault by mistake or on purpose --
# could otherwise redirect a service: POSTGRES_DB from Vault once moved the
# end-to-end proof off its disposable database onto the live one, and a host
# setting would send the credentials themselves elsewhere. Which database and
# which host are deployment configuration, from compose.
_ALLOWED_KEYS = frozenset({
    "POSTGRES_USER", "POSTGRES_PASSWORD",      # this service's database identity
    "APP_DB_USER", "APP_DB_PASSWORD",          # the role the migrate job creates
    "LITELLM_API_KEY",                         # the scoped LiteLLM virtual key
})

# Bounded, because this runs at import time (Settings() is built at module
# load, src/config.py): an unbounded wait would hang the process instead of
# failing loudly. Only the file-not-rendered-yet race at container start needs
# it -- compose already starts the service after its Vault Agent is healthy,
# and Agent owns retrying the actual Vault login.
_AGENT_FILE_MAX_ATTEMPTS = int(os.getenv("VAULT_AGENT_FILE_MAX_ATTEMPTS", "10"))
_AGENT_FILE_POLL_SECONDS = float(os.getenv("VAULT_AGENT_FILE_POLL_SECONDS", "0.5"))


class VaultConfigError(RuntimeError):
    """Raised when a strictly required secret could not be loaded."""


def _parse_agent_env_file(path: Path) -> dict[str, str]:
    """
    Parse Agent's rendered KEY=VALUE file -- see instraclustr/vault-agent/*.hcl
    for the exact template that writes it. Deliberately not python-dotenv (a
    transitive dependency already, via pydantic-settings): that format is
    ours end to end, so a few lines here behave exactly as specified instead
    of inheriting a general-purpose parser's quoting/export/multiline rules
    for a format we never asked it to support.
    """
    values: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _credentials_only(values: dict[str, str], path: str) -> dict[str, str]:
    ignored = sorted(set(values) - _ALLOWED_KEYS)
    if ignored:
        _log.warning(
            f"vault_agent_file_keys_ignored: {ignored} (only credentials are taken from {path})",
            extra={"ignored": ignored, "path": path},
        )
    return {k: v for k, v in values.items() if k in _ALLOWED_KEYS}


class VaultAgentFileSettingsSource(PydanticBaseSettingsSource):
    """
    Reads the secrets Vault Agent rendered for this service. Performs no Vault
    authentication of its own. The only retry is for the file-not-rendered-yet
    race at container start.
    """

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        env_file = os.getenv("VAULT_AGENT_ENV_FILE")
        if not env_file:
            _log.warning(
                "vault_agent_disabled_missing_config",
                extra={"vault_agent_env_file_set": False},
            )
            return {}

        path = Path(env_file)
        last_error: str | None = None
        values: dict[str, str] = {}

        for attempt in range(1, _AGENT_FILE_MAX_ATTEMPTS + 1):
            if path.exists():
                values = _credentials_only(_parse_agent_env_file(path), env_file)
                missing_required = [name for name in _REQUIRED_AFTER_VAULT if not values.get(name)]
                if not missing_required:
                    if attempt > 1:
                        _log.info("vault_agent_file_ready_after_wait", extra={"attempt": attempt})
                    return values
                last_error = f"missing required secret(s): {missing_required}"
                _log.warning(
                    f"vault_agent_file_incomplete (attempt {attempt}/{_AGENT_FILE_MAX_ATTEMPTS}): {missing_required}",
                    extra={"missing": missing_required, "attempt": attempt},
                )
            else:
                last_error = "file not yet rendered"
                _log.warning(
                    f"vault_agent_file_not_found (attempt {attempt}/{_AGENT_FILE_MAX_ATTEMPTS}): {env_file}",
                    extra={"path": env_file, "attempt": attempt},
                )

            if attempt < _AGENT_FILE_MAX_ATTEMPTS:
                time.sleep(_AGENT_FILE_POLL_SECONDS)

        missing_required = [name for name in _REQUIRED_AFTER_VAULT if not values.get(name)]
        _log.error(
            f"vault_agent_file_not_ready_after_wait: {last_error}",
            extra={"missing": missing_required, "attempts": _AGENT_FILE_MAX_ATTEMPTS, "last_error": last_error},
        )
        raise VaultConfigError(
            f"Vault Agent's rendered secrets file ({env_file}) was not ready after "
            f"{_AGENT_FILE_MAX_ATTEMPTS} attempts ({_AGENT_FILE_MAX_ATTEMPTS * _AGENT_FILE_POLL_SECONDS:.1f}s). "
            f"Last state: {last_error}. Refusing to start with an unset credential rather than "
            "falling back silently."
        )
