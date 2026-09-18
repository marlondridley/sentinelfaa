"""
Unit and regression tests for async stage_critical_alert in execution_gate.py.
Verifies that storage operations (create_staged_action, get_pending_staged_actions,
get_ticket_by_action_id, save_watch_officer_brief, create_operational_ticket)
are properly awaited and persisted.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
import pytest

from src.execution_gate import stage_critical_alert, _build_escalation_package
from src.models import AlertEvent, CriticResult, OperationalTicket, StagedAction, WatchOfficerBrief


@pytest.mark.asyncio
async def test_stage_critical_alert_awaits_create_staged_action():
    alert = AlertEvent(
        alert_id="ALT-CRIT-001",
        alert_type="ALERT_LOW_RVR",
        severity="CRITICAL",
        airport_code="KDCA",
        service_name="APDS_RVR",
        description="Low RVR 600ft",
    )

    fake_staged = StagedAction(
        action_id="act-test-123",
        alert_id="ALT-CRIT-001",
        severity="CRITICAL",
        status="PENDING",
        idempotency_key="key-123",
    )

    with patch("src.storage.create_staged_action", new_callable=AsyncMock) as mock_create, \
         patch("src.storage.get_pending_staged_actions", new_callable=AsyncMock) as mock_pending, \
         patch("src.execution_gate._build_escalation_package", new_callable=AsyncMock) as mock_esc:

        mock_create.return_value = fake_staged
        mock_pending.return_value = [fake_staged]
        mock_esc.return_value = None

        result = await stage_critical_alert(alert)

        # Verify create_staged_action was awaited with a StagedAction instance
        mock_create.assert_awaited_once()
        created_arg = mock_create.await_args[0][0]
        assert isinstance(created_arg, StagedAction)
        assert created_arg.alert_id == "ALT-CRIT-001"
        assert created_arg.status == "PENDING"

        # Verify result is the StagedAction returned by storage, not a coroutine
        assert isinstance(result, StagedAction)
        assert result.action_id == "act-test-123"

        # Verify escalation package and metric refresh were awaited
        mock_pending.assert_awaited_once()
        mock_esc.assert_awaited_once_with(fake_staged, alert, None)


@pytest.mark.asyncio
async def test_stage_non_critical_alert_returns_none_without_storage_call():
    alert = AlertEvent(
        alert_id="ALT-WARN-001",
        alert_type="ALERT_HIGH_LATENCY",
        severity="WARNING",
        airport_code="KIAD",
        service_name="ISMC",
        description="High latency",
    )

    with patch("src.storage.create_staged_action", new_callable=AsyncMock) as mock_create:
        result = await stage_critical_alert(alert)
        assert result is None
        mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_build_escalation_package_awaits_storage():
    alert = AlertEvent(
        alert_id="ALT-CRIT-002",
        alert_type="ALERT_LOW_RVR",
        severity="CRITICAL",
        airport_code="KDCA",
        service_name="APDS_RVR",
        description="Low RVR",
    )
    staged = StagedAction(
        action_id="act-test-456",
        alert_id="ALT-CRIT-002",
        severity="CRITICAL",
        status="PENDING",
        idempotency_key="key-456",
    )
    brief = WatchOfficerBrief(
        brief_id="brf-001",
        headline="Test Headline",
        posture="ACTION",
        trust_assessment="DEGRADED",
    )

    with patch("src.storage.get_ticket_by_action_id", new_callable=AsyncMock) as mock_get_ticket, \
         patch("src.watch_officer_agent.build_watch_officer_context", new_callable=AsyncMock) as mock_ctx, \
         patch("src.watch_officer_agent.generate_watch_officer_brief", return_value=brief) as mock_brief, \
         patch("src.storage.save_watch_officer_brief", new_callable=AsyncMock) as mock_save_brief, \
         patch("src.storage.create_operational_ticket", new_callable=AsyncMock) as mock_create_ticket:

        mock_get_ticket.return_value = None
        mock_ctx.return_value = {"airport_status": {}}
        mock_create_ticket.side_effect = lambda t: t

        ticket = await _build_escalation_package(staged, alert, None)

        mock_get_ticket.assert_awaited_once_with("act-test-456")
        mock_ctx.assert_awaited_once_with(alert=alert)
        mock_save_brief.assert_awaited_once_with(brief)
        mock_create_ticket.assert_awaited_once()
        assert ticket is not None
        assert ticket.action_id == "act-test-456"
