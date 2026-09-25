"""
The escalation-integrity reconciler, Vault AppRole login, and the edge TLS
certificate.
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import storage
from src.config import settings
from src.models import AlertEvent


@pytest.fixture
def db():
    storage.override_db_path(":memory:")
    yield storage
    storage.override_db_path(":memory:")


def _critical(minutes_ago: float, **kw) -> AlertEvent:
    return AlertEvent(alert_type=kw.pop("alert_type", "ALERT_FEED_DISCONNECTED"), severity="CRITICAL",
                      airport_code="KDCA", service_name="ISMC", description="reconcile test",
                      triggered_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago), **kw)


# ── Reconciler ────────────────────────────────────────────────────────────────

async def test_reconciler_stages_recent_and_records_old_unstaged_alerts(db, monkeypatch):
    """The state the live database was in: CRITICAL alerts saved, never staged."""
    from src.metrics import CRITICAL_ALERTS_UNSTAGED
    from src.reconcile import reconcile_unstaged

    monkeypatch.setattr(settings, "RECONCILE_STAGE_WINDOW_MINUTES", 60)
    recent, old = _critical(5), _critical(60 * 24 * 30, alert_type="ALERT_LOW_RVR")
    await db.save_alerts([recent, old])          # saved like the old dashboard path did: no staging

    summary = await reconcile_unstaged()
    assert summary == {"staged_pending": 1, "recorded_expired": 1, "still_unstaged": 0}
    assert CRITICAL_ALERTS_UNSTAGED._value.get() == 0

    pending = await db.get_pending_staged_actions()
    assert [p.alert_id for p in pending] == [recent.alert_id]
    assert await db.get_ticket_by_action_id(pending[0].action_id) is not None

    by_alert = {a.alert_id: a for a in await db.get_recent_staged_actions(limit=10)}
    expired = by_alert[old.alert_id]
    assert expired.status == "EXPIRED"
    audit = await db.get_action_audit_log(action_id=expired.action_id)
    assert audit[0]["new_status"] == "EXPIRED" and "never staged" in audit[0]["note"]


async def test_reconciler_is_idempotent(db):
    from src.reconcile import reconcile_unstaged

    await db.save_alerts([_critical(5), _critical(60 * 48)])
    await reconcile_unstaged()
    assert await reconcile_unstaged() == {"staged_pending": 0, "recorded_expired": 0, "still_unstaged": 0}


async def test_reconciler_leaves_already_staged_alerts_alone(db):
    from src.execution_gate import stage_critical_alert
    from src.reconcile import reconcile_unstaged

    alert = _critical(5)
    await db.save_alerts([alert])
    await stage_critical_alert(alert)
    assert (await reconcile_unstaged())["staged_pending"] == 0


# ── Vault AppRole ─────────────────────────────────────────────────────────────

def test_services_log_in_with_approle_not_a_token(tmp_path: Path, monkeypatch):
    import src.vault_config as vault_config

    (tmp_path / "role_id").write_text("role-123\n")
    (tmp_path / "secret_id").write_text("secret-456\n")
    logins, clients = [], []

    class FakeClient:
        def __init__(self, url, token=None):
            clients.append(token)
            self.auth = types.SimpleNamespace(approle=types.SimpleNamespace(
                login=lambda role_id, secret_id: logins.append((role_id, secret_id))))
            self.secrets = types.SimpleNamespace(kv=types.SimpleNamespace(v2=types.SimpleNamespace(
                read_secret_version=lambda path: {"data": {"data": {
                    "postgres/app": {"username": "sentinel_app", "password": "p", "database": "sentinel_db"},
                    "sentinel/config": {"LITELLM_API_KEY": "sk-sentinel-virtual"},
                }.get(path, {})}})))

    monkeypatch.setattr(vault_config, "hvac", types.SimpleNamespace(Client=FakeClient))
    monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    monkeypatch.setenv("VAULT_APPROLE_DIR", str(tmp_path))
    monkeypatch.setenv("VAULT_POSTGRES_SECRET_PATH", "postgres/app")
    monkeypatch.delenv("POSTGRES_DB", raising=False)

    import src.config
    values = vault_config.VaultSettingsSource(src.config.Settings)()
    assert logins == [("role-123", "secret-456")]
    assert clients == [None], "an AppRole service must not carry a token of its own"
    assert values["POSTGRES_USER"] == "sentinel_app"
    assert values["LITELLM_API_KEY"] == "sk-sentinel-virtual"
    assert "GEMINI_API_KEY" not in values and "LITELLM_MASTER_KEY" not in values


# ── Edge TLS certificate ──────────────────────────────────────────────────────

def test_edge_certificate_covers_localhost_and_is_kept_across_restarts(tmp_path: Path):
    from cryptography import x509

    from src.devcert import CERT_NAME, ensure_certificate

    assert ensure_certificate(tmp_path) is True
    cert = x509.load_pem_x509_certificate((tmp_path / CERT_NAME).read_bytes())
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in sans.get_values_for_type(x509.DNSName)
    first = (tmp_path / CERT_NAME).read_bytes()
    assert ensure_certificate(tmp_path) is False
    assert (tmp_path / CERT_NAME).read_bytes() == first
