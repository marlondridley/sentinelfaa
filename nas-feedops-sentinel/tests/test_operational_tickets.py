"""
Tests for OperationalTicket — the internal escalation package.

Verifies:
  - Ticket is created when a CRITICAL alert is staged
  - Duplicate staging of the same alert returns the existing ticket (idempotent)
  - get_draft_operational_tickets() lists DRAFT only
  - Approve transitions DRAFT -> APPROVED_FOR_ESCALATION (sticky)
  - Reject transitions DRAFT -> REJECTED (sticky)
  - MONITORING is non-terminal (can later be re-decided)
  - All ticket transitions write an action_audit_log row
"""
from __future__ import annotations

import pytest

from src import storage
from src.execution_gate import stage_critical_alert
from src.models import AlertEvent


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
        description="Low RVR 700 ft at KDCA runway 19.",
    )
    base.update(kwargs)
    return AlertEvent(**base)


async def _persist_and_stage(alert: AlertEvent, critic_result=None):
    """Save the alert, then stage it -- the same order src/pipeline.py uses
    (staged_actions.alert_id is a foreign key to alert_events)."""
    await storage.save_alert(alert)
    return await stage_critical_alert(alert, critic_result)


# ── Ticket creation tied to staging ──────────────────────────────────────────

class TestTicketCreation:
    async def test_critical_alert_creates_ticket(self):
        alert = _critical_alert()
        action = await _persist_and_stage(alert)
        ticket = await storage.get_ticket_by_action_id(action.action_id)
        assert ticket is not None
        assert ticket.action_id == action.action_id
        assert ticket.alert_id == alert.alert_id
        assert ticket.status == "DRAFT"
        assert ticket.brief_id  # brief was generated
        assert ticket.created_by_agent == "watch_officer"

    async def test_duplicate_alert_returns_existing_ticket(self):
        alert = _critical_alert()
        await _persist_and_stage(alert)
        ticket_a = await storage.get_ticket_by_action_id(
            (await storage.get_pending_staged_actions())[0].action_id
        )
        # Re-stage same alert
        await _persist_and_stage(alert)
        ticket_b = await storage.get_ticket_by_action_id(
            (await storage.get_pending_staged_actions())[0].action_id
        )
        assert ticket_a.ticket_id == ticket_b.ticket_id, "duplicate staging must be idempotent"
        assert len(await storage.get_draft_operational_tickets()) == 1

    async def test_non_critical_alert_does_not_create_ticket(self):
        warn = _critical_alert(severity="WARNING")
        await _persist_and_stage(warn)  # returns None, no ticket
        assert await storage.get_draft_operational_tickets() == []

    async def test_draft_query_returns_only_drafts(self):
        a1 = await _persist_and_stage(_critical_alert(alert_type="ALERT_LOW_RVR"))
        await _persist_and_stage(_critical_alert(alert_type="ALERT_ISMC_LINK_DEGRADED",
                                                 service_name="ISMC"))
        t1 = await storage.get_ticket_by_action_id(a1.action_id)
        await storage.approve_operational_ticket(t1.ticket_id, actor="x", note="")

        drafts = await storage.get_draft_operational_tickets()
        draft_ids = {t.ticket_id for t in drafts}
        # The approved one should be excluded
        assert t1.ticket_id not in draft_ids


# ── Decision transitions ──────────────────────────────────────────────────────

class TestTicketDecisions:
    async def test_approve_transitions_draft_to_approved(self):
        action = await _persist_and_stage(_critical_alert())
        ticket = await storage.get_ticket_by_action_id(action.action_id)
        updated = await storage.approve_operational_ticket(
            ticket.ticket_id, actor="supervisor_smith", note="cleared by tower"
        )
        assert updated.status == "APPROVED_FOR_ESCALATION"
        assert updated.decided_by == "supervisor_smith"
        assert updated.decision_note == "cleared by tower"

    async def test_reject_transitions_draft_to_rejected(self):
        action = await _persist_and_stage(_critical_alert())
        ticket = await storage.get_ticket_by_action_id(action.action_id)
        updated = await storage.reject_operational_ticket(
            ticket.ticket_id, actor="supervisor_smith", note="false alarm"
        )
        assert updated.status == "REJECTED"
        assert updated.decided_by == "supervisor_smith"

    async def test_monitoring_is_non_terminal(self):
        action = await _persist_and_stage(_critical_alert())
        ticket = await storage.get_ticket_by_action_id(action.action_id)
        # Move to MONITORING
        monitored = await storage.mark_operational_ticket_monitoring(
            ticket.ticket_id, actor="supervisor", note="watching weather"
        )
        assert monitored.status == "MONITORING"
        # Then later approve it — should work (MONITORING is non-terminal)
        approved = await storage.approve_operational_ticket(
            ticket.ticket_id, actor="supervisor", note="escalating now"
        )
        assert approved.status == "APPROVED_FOR_ESCALATION"

    async def test_approved_is_sticky(self):
        action = await _persist_and_stage(_critical_alert())
        ticket = await storage.get_ticket_by_action_id(action.action_id)
        await storage.approve_operational_ticket(ticket.ticket_id, actor="a", note="first")
        again = await storage.reject_operational_ticket(ticket.ticket_id, actor="b", note="overrule?")
        # Terminal state must hold
        assert again.status == "APPROVED_FOR_ESCALATION"
        assert again.decided_by == "a"

    async def test_rejected_is_sticky(self):
        action = await _persist_and_stage(_critical_alert())
        ticket = await storage.get_ticket_by_action_id(action.action_id)
        await storage.reject_operational_ticket(ticket.ticket_id, actor="a", note="ignore")
        attempted = await storage.approve_operational_ticket(ticket.ticket_id, actor="b", note="overrule?")
        assert attempted.status == "REJECTED"
        assert attempted.decided_by == "a"


# ── Audit trail integration ───────────────────────────────────────────────────

class TestAuditIntegration:
    async def test_ticket_creation_writes_audit_row(self):
        action = await _persist_and_stage(_critical_alert())
        ticket = await storage.get_ticket_by_action_id(action.action_id)
        audit = await storage.get_action_audit_log(action_id=f"ticket:{ticket.ticket_id}")
        assert any(r["new_status"] == "DRAFT" for r in audit)

    async def test_approve_writes_audit_row(self):
        action = await _persist_and_stage(_critical_alert())
        ticket = await storage.get_ticket_by_action_id(action.action_id)
        await storage.approve_operational_ticket(ticket.ticket_id, actor="x", note="ok")
        audit = await storage.get_action_audit_log(action_id=f"ticket:{ticket.ticket_id}")
        assert any(
            r["previous_status"] == "DRAFT" and r["new_status"] == "APPROVED_FOR_ESCALATION"
            for r in audit
        )

    async def test_monitoring_writes_audit_row(self):
        action = await _persist_and_stage(_critical_alert())
        ticket = await storage.get_ticket_by_action_id(action.action_id)
        await storage.mark_operational_ticket_monitoring(ticket.ticket_id, actor="x", note="watching")
        audit = await storage.get_action_audit_log(action_id=f"ticket:{ticket.ticket_id}")
        assert any(r["new_status"] == "MONITORING" for r in audit)
