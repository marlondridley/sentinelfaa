"""
Tests for the Human-in-the-Loop execution gate.

Verifies the behavior contracts the README claims:
  - CRITICAL alerts create a staged PENDING action.
  - Non-CRITICAL alerts do NOT stage.
  - Duplicate CRITICAL alerts are idempotent (one action, not many).
  - approve / reject transition only from PENDING.
  - Terminal states are sticky — re-approving an APPROVED action is a no-op.
  - Every state transition writes an immutable audit row.

The storage layer and every gate function here are async (asyncpg). These
tests used to call them without awaiting, so each call returned an
un-awaited coroutine, the first attribute access on it raised, and none of
the contracts above was ever actually checked. They are async now.

Alerts are persisted before they are staged because production does exactly
that (src/pipeline.py calls save_alerts() before stage_critical_alert()), and
the schema enforces it: staged_actions.alert_id is a foreign key to
alert_events. Staging an alert that was never saved exercises a state the
pipeline cannot produce, so _persist_and_stage() below does both, in order.
"""
from __future__ import annotations

import pytest

from src import storage
from src.execution_gate import (
    _idempotency_key,
    approve_staged_action,
    get_action_audit_log,
    get_pending_staged_actions,
    get_recent_staged_actions,
    reject_staged_action,
    stage_critical_alert,
)
from src.models import AlertEvent, CriticResult

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NIM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def _critical_alert(**kwargs) -> AlertEvent:
    base = dict(
        alert_type="ALERT_LOW_RVR",
        severity="CRITICAL",
        airport_code="KDCA",
        service_name="APDS_RVR",
        description="Low RVR 700 ft at touchdown on runway 19 at KDCA.",
    )
    base.update(kwargs)
    return AlertEvent(**base)


def _warning_alert(**kwargs) -> AlertEvent:
    base = dict(
        alert_type="ALERT_HIGH_LATENCY",
        severity="WARNING",
        airport_code="KIAD",
        service_name="ISMC",
        description="Latency 45 s on ISMC at KIAD.",
    )
    base.update(kwargs)
    return AlertEvent(**base)


def _critic(**kwargs) -> CriticResult:
    base = dict(
        alert_id="placeholder",
        confidence=0.88,
        reasoning="ISMC degraded concurrent with RVR drop; classic weather-correlated pattern.",
        pattern="WEATHER_CORRELATED",
        escalate=True,
    )
    base.update(kwargs)
    return CriticResult(**base)


async def _persist_and_stage(alert: AlertEvent, critic_result: CriticResult | None = None):
    """Save the alert, then stage it -- the same order src/pipeline.py uses."""
    await storage.save_alert(alert)
    return await stage_critical_alert(alert, critic_result)


# ── Staging behavior ──────────────────────────────────────────────────────────

class TestStaging:
    async def test_critical_alert_creates_staged_action(self):
        alert = _critical_alert()
        action = await _persist_and_stage(alert)

        assert action is not None
        assert action.status == "PENDING"
        assert action.alert_id == alert.alert_id
        assert action.airport_code == "KDCA"
        assert action.severity == "CRITICAL"
        assert action.recommended_action  # non-empty
        assert action.rationale            # non-empty
        assert action.idempotency_key      # non-empty

    async def test_non_critical_alert_does_not_stage(self):
        warn = _warning_alert()
        action = await _persist_and_stage(warn)
        assert action is None
        assert await get_pending_staged_actions() == []

    async def test_staged_action_starts_pending(self):
        alert = _critical_alert()
        action = await _persist_and_stage(alert)
        assert action.status == "PENDING"
        assert action.decided_at is None
        assert action.decided_by is None

    async def test_duplicate_critical_alert_does_not_create_duplicate(self):
        alert = _critical_alert()
        first  = await _persist_and_stage(alert)
        second = await _persist_and_stage(alert)  # the same alert again: nothing new is opened

        assert first is not None and second is None, \
            "Re-staging the same alert must open nothing: its review already covers it"
        assert [a.action_id for a in await get_pending_staged_actions()] == [first.action_id]

    async def test_different_critical_alerts_create_different_actions(self):
        a1 = _critical_alert(alert_type="ALERT_LOW_RVR")
        a2 = _critical_alert(alert_type="ALERT_ISMC_LINK_DEGRADED", service_name="ISMC")
        s1 = await _persist_and_stage(a1)
        s2 = await _persist_and_stage(a2)
        assert s1.action_id != s2.action_id
        assert len(await get_pending_staged_actions()) == 2

    async def test_critic_result_enriches_rationale_and_confidence(self):
        alert = _critical_alert()
        critic = _critic(alert_id=alert.alert_id, confidence=0.92)
        action = await _persist_and_stage(alert, critic)

        assert action.critic_confidence == 0.92
        assert action.critic_pattern == "WEATHER_CORRELATED"
        # Rationale should come from the critic's reasoning
        assert "weather-correlated" in action.rationale.lower()
        # Recommended action should reference the critic confidence
        assert "0.92" in action.recommended_action or "92%" in action.recommended_action

    async def test_no_critic_uses_deterministic_rationale(self):
        alert = _critical_alert()
        action = await _persist_and_stage(alert, critic_result=None)

        assert action.critic_confidence is None
        assert action.critic_pattern is None
        # Deterministic rationale references the alert type
        assert action.alert_type in action.rationale


# ── Decision workflow (approve / reject) ─────────────────────────────────────

class TestDecisionWorkflow:
    async def test_approve_transitions_pending_to_approved(self):
        alert = _critical_alert()
        action = await _persist_and_stage(alert)
        updated = await approve_staged_action(action.action_id, actor="supervisor_smith", note="verified")

        assert updated is not None
        assert updated.status == "APPROVED"
        assert updated.decided_by == "supervisor_smith"
        assert updated.decision_note == "verified"
        assert updated.decided_at is not None

    async def test_reject_transitions_pending_to_rejected(self):
        alert = _critical_alert()
        action = await _persist_and_stage(alert)
        updated = await reject_staged_action(action.action_id, actor="supervisor_smith", note="false alarm")

        assert updated is not None
        assert updated.status == "REJECTED"
        assert updated.decided_by == "supervisor_smith"
        assert updated.decision_note == "false alarm"

    async def test_approve_writes_audit_row(self):
        alert = _critical_alert()
        action = await _persist_and_stage(alert)
        before_count = len(await get_action_audit_log(action_id=action.action_id))
        await approve_staged_action(action.action_id, actor="supervisor_smith", note="ok")

        audit = await get_action_audit_log(action_id=action.action_id)
        assert len(audit) == before_count + 1
        latest = audit[0]
        assert latest["previous_status"] == "PENDING"
        assert latest["new_status"] == "APPROVED"
        assert latest["actor"] == "supervisor_smith"
        assert latest["note"] == "ok"

    async def test_reject_writes_audit_row(self):
        alert = _critical_alert()
        action = await _persist_and_stage(alert)
        await reject_staged_action(action.action_id, actor="supervisor_smith", note="not real")

        audit = await get_action_audit_log(action_id=action.action_id)
        latest = audit[0]
        assert latest["previous_status"] == "PENDING"
        assert latest["new_status"] == "REJECTED"
        assert latest["actor"] == "supervisor_smith"

    async def test_duplicate_approval_is_idempotent(self):
        alert = _critical_alert()
        action = await _persist_and_stage(alert)
        await approve_staged_action(action.action_id, actor="a", note="first")
        second = await approve_staged_action(action.action_id, actor="b", note="second-attempt")

        # State must not change on re-approval
        assert second.status == "APPROVED"
        assert second.decided_by == "a", \
            "Terminal state must be sticky; second approval must not overwrite the first"
        assert second.decision_note == "first"

        # No second audit row for the duplicate
        audit = await get_action_audit_log(action_id=action.action_id)
        # 1 from staging + 1 from first approval = 2; the duplicate must not add a third
        assert len(audit) == 2

    async def test_rejected_action_cannot_later_be_approved(self):
        alert = _critical_alert()
        action = await _persist_and_stage(alert)
        await reject_staged_action(action.action_id, actor="supervisor", note="ignore")
        attempted = await approve_staged_action(action.action_id, actor="supervisor2", note="overrule?")

        # Terminal — must remain REJECTED.
        assert attempted.status == "REJECTED"
        assert attempted.decided_by == "supervisor"

    async def test_approving_unknown_action_returns_none(self):
        result = await approve_staged_action("nonexistent-id-12345", actor="x", note="")
        assert result is None


# ── Query semantics ───────────────────────────────────────────────────────────

class TestQuerySemantics:
    async def test_pending_query_returns_only_pending(self):
        a1 = await _persist_and_stage(_critical_alert(alert_type="ALERT_LOW_RVR"))
        a2 = await _persist_and_stage(_critical_alert(alert_type="ALERT_ISMC_LINK_DEGRADED",
                                                 service_name="ISMC"))
        a3 = await _persist_and_stage(_critical_alert(alert_type="ALERT_FEED_DISCONNECTED",
                                                 service_name="SMES"))

        await approve_staged_action(a1.action_id, actor="x", note="")
        await reject_staged_action(a2.action_id, actor="x", note="")
        # a3 stays PENDING

        pending = await get_pending_staged_actions()
        assert len(pending) == 1
        assert pending[0].action_id == a3.action_id

    async def test_recent_query_returns_all_statuses(self):
        a1 = await _persist_and_stage(_critical_alert(alert_type="ALERT_LOW_RVR"))
        await _persist_and_stage(_critical_alert(alert_type="ALERT_ISMC_LINK_DEGRADED",
                                                 service_name="ISMC"))
        await approve_staged_action(a1.action_id, actor="x", note="")

        recent = await get_recent_staged_actions(limit=10)
        statuses = {r.status for r in recent}
        assert "APPROVED" in statuses
        assert "PENDING" in statuses
        assert len(recent) == 2

    async def test_audit_log_global_query_returns_all_entries(self):
        a1 = await _persist_and_stage(_critical_alert(alert_type="ALERT_LOW_RVR"))
        a2 = await _persist_and_stage(_critical_alert(alert_type="ALERT_ISMC_LINK_DEGRADED",
                                                 service_name="ISMC"))
        await approve_staged_action(a1.action_id, actor="x", note="")
        await reject_staged_action(a2.action_id, actor="x", note="")

        global_audit = await get_action_audit_log(action_id=None, limit=50)
        # 2 stagings (action) + 2 ticket DRAFT rows + 1 approve + 1 reject = 6
        # (Each staging also creates an OperationalTicket whose initial draft
        # transition is recorded in the shared action_audit_log table.)
        assert len(global_audit) == 6


# ── Event-loop responsiveness ────────────────────────────────────────────────

async def test_staging_never_waits_on_a_model(monkeypatch):
    """
    Staging a CRITICAL alert must not call a model. It used to write the
    ticket's Watch Officer brief with an LLM, once per alert, serially, inside
    staging -- a burst of alerts became a burst of unbudgeted model calls, and
    the pass waited on every one. The model narrative now comes from the
    investigation, which runs after staging under a per-pass budget.
    """
    import time

    import src.watch_officer_agent as watch_officer

    calls = []

    class Model:
        async def acall_with_tool(self, **kwargs):
            calls.append(kwargs)
            raise AssertionError("staging called the model")

    monkeypatch.setattr(watch_officer.settings, "CRITIC_ENABLED", True)
    monkeypatch.setattr(watch_officer, "build_llm_client", lambda *a, **k: Model())

    started = time.perf_counter()
    action = await _persist_and_stage(_critical_alert())
    elapsed = time.perf_counter() - started

    assert calls == []
    ticket = await storage.get_ticket_by_action_id(action.action_id)
    assert ticket is not None, "the ticket is still built, from the rule-based brief"
    assert elapsed < 5, f"staging took {elapsed:.1f}s"


# ── Idempotency key behavior ──────────────────────────────────────────────────

class TestIdempotencyKey:
    def test_same_alert_yields_same_key(self):
        a = _critical_alert()
        assert _idempotency_key(a) == _idempotency_key(a)

    def test_different_alerts_yield_different_keys(self):
        a1 = _critical_alert(alert_type="ALERT_LOW_RVR")
        a2 = _critical_alert(alert_type="ALERT_ISMC_LINK_DEGRADED")
        assert _idempotency_key(a1) != _idempotency_key(a2)
