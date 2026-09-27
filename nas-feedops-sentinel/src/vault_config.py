from __future__ import annotations

import os
import time
from typing import Any

from pydantic_settings import PydanticBaseSettingsSource

from src.logging_config import get_logger

try:
    import hvac
except ImportError:
    hvac = None

_log = get_logger(__name__)

# Secrets that must be present after the Vault call or the app should refuse
# to start rather than run with a missing/empty credential.
_REQUIRED_AFTER_VAULT = ("LITELLM_API_KEY",)

# A bounded retry, not an unbounded one: this runs at import time (Settings()
# is built at module load, see src/config.py), so an unbounded retry would
# hang the whole process rather than fail loudly. The window (attempts *
# roughly half the max backoff, ~20s at the defaults) covers a rotation
# in progress, a service that raced the Vault-unseal bring-up order documented
# in docs/operations.md, or Vault itself finishing a restart -- transient
# conditions that clear on their own within seconds, not the minutes a
# genuinely wrong credential or policy would need a human to fix. Configurable
# because "how long is a transient condition" is an environment fact, not a
# code fact.
_MAX_ATTEMPTS = int(os.getenv("VAULT_FETCH_MAX_ATTEMPTS", "5"))
_RETRY_BACKOFF_SECONDS = float(os.getenv("VAULT_FETCH_RETRY_SECONDS", "2"))


class VaultConfigError(RuntimeError):
    """Raised when a strictly required secret could not be loaded from Vault."""


def _authenticated_client(vault_url: str, token: str | None, approle_dir: str | None):
    """
    A Vault client authenticated as this service.

    AppRole first: VAULT_APPROLE_DIR holds role_id and secret_id files written
    by the infrastructure bootstrap (instraclustr/vault-unseal-and-seed.bash),
    and the login returns a short-lived token limited to this service's policy
    -- the application can read its own database identity and proxy key and
    nothing else. VAULT_TOKEN is the fallback for local runs; deployed
    services no longer receive one (they used to get the root token).
    """
    if approle_dir:
        from pathlib import Path

        base = Path(approle_dir)
        client = hvac.Client(url=vault_url)
        client.auth.approle.login(
            role_id=(base / "role_id").read_text(encoding="utf-8").strip(),
            secret_id=(base / "secret_id").read_text(encoding="utf-8").strip(),
        )
        _log.info("vault_approle_login", extra={"approle_dir": approle_dir})
        return client
    return hvac.Client(url=vault_url, token=token)


def _fetch_once(
    vault_url: str, vault_token: str | None, approle_dir: str | None,
) -> tuple[dict[str, Any], str | None]:
    """
    One attempt at a fresh login and both reads. Returns (secrets_dict,
    client_error): client_error is set only when _authenticated_client()
    itself failed (couldn't read the credential file, or Vault rejected it) --
    that aborts the whole attempt, since there is no client to read anything
    with. A read that fails on an already-authenticated client is instead
    logged and skipped (vault_secret_empty / vault_secret_fetch_failed,
    unchanged from before): a transient hiccup on ONE path, mid-retry, should
    not be indistinguishable from the client itself being unreachable.
    """
    try:
        client = _authenticated_client(vault_url, vault_token, approle_dir)
    except Exception as exc:  # noqa: BLE001 -- reported to the caller, not swallowed
        return {}, str(exc)

    secrets_dict: dict[str, Any] = {}

    # 1. Fetch PostgreSQL Credentials
    pg_path = os.getenv("VAULT_POSTGRES_SECRET_PATH", "postgres/credentials")
    try:
        # Which database identity this process gets. Application services
        # read postgres/app (DML only); the migrate job reads
        # postgres/credentials (the schema owner).
        pg_resp = client.secrets.kv.v2.read_secret_version(path=pg_path)
        pg_data = pg_resp.get("data", {}).get("data", {})
        if pg_data:
            secrets_dict["POSTGRES_USER"] = pg_data.get("username")
            secrets_dict["POSTGRES_PASSWORD"] = pg_data.get("password")
            # Credentials come from Vault; which database to use is
            # deployment configuration. An explicit POSTGRES_DB wins, so
            # a job aimed at a disposable database (tests, the e2e
            # proof) is not silently redirected to production by the
            # secret's default -- which is exactly what happened before
            # the proof's disposable-database guard caught it.
            if not os.getenv("POSTGRES_DB"):
                secrets_dict["POSTGRES_DB"] = pg_data.get("database")
        else:
            _log.warning("vault_secret_empty", extra={"path": pg_path})
    except Exception as exc:  # noqa: BLE001 -- degrade to missing, not fatal here
        _log.warning(
            f"vault_secret_fetch_failed: {pg_path}: {exc}",
            extra={"path": pg_path, "error": str(exc)},
        )

    # 2. Fetch the application's LiteLLM proxy key (a scoped virtual key)
    try:
        sentinel_resp = client.secrets.kv.v2.read_secret_version(path="sentinel/config")
        sentinel_data = sentinel_resp.get("data", {}).get("data", {})
        if sentinel_data:
            secrets_dict["LITELLM_API_KEY"] = sentinel_data.get("LITELLM_API_KEY")
        else:
            _log.warning("vault_secret_empty", extra={"path": "sentinel/config"})
    except Exception as exc:  # noqa: BLE001 -- degrade to missing, not fatal here
        _log.warning(
            f"vault_secret_fetch_failed: sentinel/config: {exc}",
            extra={"path": "sentinel/config", "error": str(exc)},
        )

    return secrets_dict, None


class VaultSettingsSource(PydanticBaseSettingsSource):
    """Custom Pydantic source interceptor to pull secrets from HashiCorp Vault."""

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        if hvac is None:
            _log.warning(
                "vault_disabled_missing_dependency",
                extra={"reason": "hvac package not installed"},
            )
            return {}

        vault_url = os.getenv("VAULT_ADDR")
        vault_token = os.getenv("VAULT_TOKEN")
        approle_dir = os.getenv("VAULT_APPROLE_DIR")

        if not vault_url or not (vault_token or approle_dir):
            _log.warning(
                "vault_disabled_missing_config",
                extra={
                    "vault_addr_set": bool(vault_url),
                    "vault_token_set": bool(vault_token),
                    "vault_approle_dir_set": bool(approle_dir),
                },
            )
            return {}

        # Every attempt is a fresh login, not a retry of stale client state --
        # if the credential file was mid-rewrite or Vault mid-restart on
        # attempt 1, attempt 2 reads and authenticates from scratch.
        secrets_dict: dict[str, Any] = {}
        missing_required: list[str] = list(_REQUIRED_AFTER_VAULT)
        last_error: str | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            secrets_dict, client_error = _fetch_once(vault_url, vault_token, approle_dir)

            if client_error is not None:
                last_error = client_error
                _log.warning(
                    f"vault_client_init_failed (attempt {attempt}/{_MAX_ATTEMPTS}): {client_error}",
                    extra={"vault_addr": vault_url, "error": client_error, "attempt": attempt},
                )
            else:
                missing_required = [name for name in _REQUIRED_AFTER_VAULT if not secrets_dict.get(name)]
                if not missing_required:
                    if attempt > 1:
                        _log.info("vault_fetch_succeeded_after_retry", extra={"attempt": attempt})
                    return secrets_dict
                last_error = f"missing required secret(s): {missing_required}"
                _log.warning(
                    f"vault_required_secret_missing (attempt {attempt}/{_MAX_ATTEMPTS}): {missing_required}",
                    extra={"missing": missing_required, "attempt": attempt},
                )

            if attempt < _MAX_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)  # linear: 2s, 4s, 6s, 8s at the defaults

        _log.error(
            f"vault_required_secret_missing_after_retries: {last_error}",
            extra={"missing": missing_required, "attempts": _MAX_ATTEMPTS, "last_error": last_error},
        )
        raise VaultConfigError(
            f"Required secret(s) still missing after {_MAX_ATTEMPTS} Vault fetch attempts: "
            f"{missing_required}. Last error: {last_error}. Refusing to start with an unset "
            "credential rather than falling back silently."
        )
