from __future__ import annotations

import os
from typing import Any
from pydantic import SecretStr

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

try:
    import hvac
except ImportError:
    hvac = None

class VaultSettingsSource(PydanticBaseSettingsSource):
    """Custom Pydantic source interceptor to pull secrets from HashiCorp Vault[cite: 5]."""

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        if hvac is None:
            return {}

        vault_url = os.getenv("VAULT_ADDR")
        vault_token = os.getenv("VAULT_TOKEN")

        if not vault_url or not vault_token:
            return {}

        secrets_dict: dict[str, Any] = {}

        try:
            client = hvac.Client(url=vault_url, token=vault_token)

            # 1. Fetch PostgreSQL Credentials
            try:
                pg_resp = client.secrets.kv.v2.read_secret_version(path="postgres/credentials")
                pg_data = pg_resp.get("data", {}).get("data", {})
                if pg_data:
                    secrets_dict["POSTGRES_USER"] = pg_data.get("username")
                    secrets_dict["POSTGRES_PASSWORD"] = pg_data.get("password")
                    secrets_dict["POSTGRES_DB"] = pg_data.get("database")
            except Exception:
                pass

            # 2. Fetch Global LLM Provider Keys
            try:
                llm_resp = client.secrets.kv.v2.read_secret_version(path="litellm")
                llm_data = llm_resp.get("data", {}).get("data", {})
                if llm_data:
                    secrets_dict["DEEPSEEK_API_KEY"] = llm_data.get("DEEPSEEK_API_KEY")
                    secrets_dict["GEMINI_API_KEY"] = llm_data.get("GEMINI_API_KEY")
                    secrets_dict["LITELLM_MASTER_KEY"] = llm_data.get("LITELLM_MASTER_KEY")
            except Exception:
                pass

            # 3. Fetch Sentinel Application Proxy Key
            try:
                sentinel_resp = client.secrets.kv.v2.read_secret_version(path="sentinel/config")
                sentinel_data = sentinel_resp.get("data", {}).get("data", {})
                if sentinel_data:
                    secrets_dict["LITELLM_API_KEY"] = sentinel_data.get("LITELLM_API_KEY")
            except Exception:
                pass

        except Exception:
            pass

        return secrets_dict


class Settings(BaseSettings):
    # --- General App Settings (Pulled from .env) ---
    log_level: str
    critic_enabled: int
    critic_model: str
    swim_raw_folder: str

    # --- Vault Connection (Pulled from .env) ---
    vault_addr: str
    vault_token: str

    # --- Secure Credentials (Pulled dynamically from Vault) ---
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    litellm_api_key: str  # The proxy key needed to talk to your models

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            VaultSettingsSource(settings_cls), # Injects Vault secrets here
            env_settings,                      # Grabs .env variables here
            dotenv_settings,
        )

# Instantiate this once. The rest of your app will import this specific object.


if __name__ == "__main__":
    settings = Settings()
