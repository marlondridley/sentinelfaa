"""
Authorization: who may decide a staged action, and what the database lets
the application role do.

Identity (src/auth.py Principal) was already recorded honestly. What was
missing was authority: any signed-in user -- or anyone typing a callsign --
could approve, override or reject, and the application connected to Postgres
as the superuser that owned the audit log.
"""
from __future__ import annotations

import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

from src import storage
from src.auth import (
    AuthorizationError,
    Decision,
    Principal,
    PrincipalSource,
    authorize,
    format_actor,
)
from src.config import settings
from src.keycloak_auth import roles_from_claims


def _kc(*roles: str, source=PrincipalSource.PASSWORD_GRANT) -> Principal:
    return Principal(principal_id="jsmith", role="Watch Officer", source=source, roles=frozenset(roles))


# ── The role matrix ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("decision", [Decision.APPROVE, Decision.REJECT, Decision.MONITOR])
def test_watch_officer_may_make_ordinary_dispositions(decision):
    authorize(_kc("watch_officer"), decision)


def test_override_takes_the_supervisor_role():
    with pytest.raises(AuthorizationError, match="supervisor"):
        authorize(_kc("watch_officer"), Decision.OVERRIDE)
    authorize(_kc("supervisor"), Decision.OVERRIDE)


@pytest.mark.parametrize("decision", list(Decision))
def test_auditor_decides_nothing(decision):
    with pytest.raises(AuthorizationError):
        authorize(_kc("auditor"), decision)


def test_signed_in_user_without_a_role_is_read_only():
    with pytest.raises(AuthorizationError, match="no roles"):
        authorize(_kc(), Decision.APPROVE)


def test_self_declared_identity_cannot_decide_unless_explicitly_allowed():
    typed = Principal(principal_id="anyone", role="Watch Officer", source=PrincipalSource.SELF_DECLARED)
    with pytest.raises(AuthorizationError, match="verified operator sign-in"):
        authorize(typed, Decision.APPROVE)
    authorize(typed, Decision.APPROVE, allow_unverified=True)


def test_an_api_key_is_not_an_operator():
    service = Principal(principal_id="ingest-service", role="service", source=PrincipalSource.API_KEY)
    with pytest.raises(AuthorizationError):
        authorize(service, Decision.APPROVE)


def test_roles_come_only_from_the_token_and_only_known_ones():
    claims = {"realm_access": {"roles": ["watch_officer", "offline_access", "default-roles-nas-sentinel"]}}
    assert roles_from_claims(claims) == frozenset({"watch_officer"})
    assert roles_from_claims({}) == frozenset()


def test_audit_actor_records_the_roles_held_at_decision_time():
    assert "roles=supervisor,watch_officer" in format_actor(_kc("watch_officer", "supervisor"))


# ── The gate enforces it, not just the UI ────────────────────────────────────

@pytest.fixture
def db():
    storage.override_db_path(":memory:")
    yield storage
    storage.override_db_path(":memory:")


async def _staged(db):
    from src.execution_gate import stage_critical_alert
    from src.models import AlertEvent

    alert = AlertEvent(alert_type="ALERT_FEED_DISCONNECTED", severity="CRITICAL", airport_code="KDCA",
                       service_name="ISMC", description="test")
    await db.save_alerts([alert])
    return await stage_critical_alert(alert)


async def test_gate_refuses_an_unauthorized_decision_and_writes_nothing(db):
    from src.execution_gate import decide_staged_action

    action = await _staged(db)
    audit_before = await db.get_action_audit_log(action_id=action.action_id)
    with pytest.raises(AuthorizationError):
        await decide_staged_action(action.action_id, Decision.APPROVE, _kc("auditor"), "trying")
    assert [a.action_id for a in await db.get_pending_staged_actions()] == [action.action_id]
    assert await db.get_action_audit_log(action_id=action.action_id) == audit_before


async def test_gate_records_an_authorized_decision_with_roles_and_moves_the_ticket(db):
    from src.execution_gate import decide_staged_action

    action = await _staged(db)
    result = await decide_staged_action(action.action_id, Decision.APPROVE, _kc("watch_officer"), "verified")
    assert result.status == "APPROVED"
    assert "roles=watch_officer" in result.decided_by
    ticket = await db.get_ticket_by_action_id(action.action_id)
    assert ticket.status == "APPROVED_FOR_ESCALATION"


# ── API: fail closed, no LiteLLM-key fallback, no client paths ───────────────

@pytest.fixture
def api(monkeypatch):
    import src.api as api_module

    started = []

    async def fake_ingest(**kw):
        started.append(kw)
        return "wf-1"

    monkeypatch.setattr(api_module, "start_ingest_workflow", fake_ingest)
    monkeypatch.setattr(settings, "API_KEYS", {})
    return TestClient(api_module.app), started


def test_api_refuses_every_call_when_no_key_is_configured(api, monkeypatch):
    client, started = api
    monkeypatch.setattr(settings, "API_KEY", None)
    monkeypatch.setattr(settings, "LITELLM_API_KEY", "proxy-master-key")
    resp = client.post("/v1/ingest", json={}, headers={"X-API-Key": "proxy-master-key"})
    assert resp.status_code == 503
    assert started == []


def test_the_llm_proxy_key_is_not_an_api_credential(api, monkeypatch):
    client, started = api
    monkeypatch.setattr(settings, "API_KEY", "the-real-api-key")
    monkeypatch.setattr(settings, "LITELLM_API_KEY", "proxy-master-key")
    assert client.post("/v1/ingest", json={}, headers={"X-API-Key": "proxy-master-key"}).status_code == 401
    assert client.post("/v1/ingest", json={}, headers={"X-API-Key": "the-real-api-key"}).status_code == 200
    assert len(started) == 1


def test_api_rejects_a_client_supplied_source_dir(api, monkeypatch):
    client, started = api
    monkeypatch.setattr(settings, "API_KEY", "k")
    resp = client.post("/v1/ingest", json={"source_dir": "/app/src"}, headers={"X-API-Key": "k"})
    assert resp.status_code == 422
    assert started == []


async def test_ingest_activity_refuses_paths_outside_the_raw_data_root():
    from src.temporal_activities import ingest_folder_activity

    with pytest.raises(ValueError, match="inside"):
        await ingest_folder_activity(source_dir="/app/src")


# ── Database: the application role cannot rewrite evidence ───────────────────

async def test_app_role_can_work_but_cannot_rewrite_evidence_or_change_schema(db):
    from src.storage.migrate import apply_grants, ensure_app_role
    from src.storage.pool import get_db_pool

    await _staged(db)
    role, password = f"sentinel_app_t{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    pool = await get_db_pool()
    try:
        async with pool.acquire() as conn:
            await ensure_app_role(conn, role, password)
            await apply_grants(conn, role)
        app = await asyncpg.connect(host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
                                    user=role, password=password, database=settings.POSTGRES_DB)
        try:
            assert await app.fetchval("SELECT count(*) FROM action_audit_log") > 0
            await app.execute("UPDATE staged_actions SET decision_note = 'ok'")    # ordinary write: allowed
            for statement in ("UPDATE action_audit_log SET note = 'x'",
                              "DELETE FROM action_audit_log",
                              "TRUNCATE action_audit_log",
                              "DROP TRIGGER trg_action_audit_log_append_only ON action_audit_log",
                              "CREATE TABLE sneaky (id int)"):
                with pytest.raises(asyncpg.PostgresError):
                    await app.execute(statement)
        finally:
            await app.close()
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f"DROP OWNED BY {role}")
            await conn.execute(f"DROP ROLE {role}")
