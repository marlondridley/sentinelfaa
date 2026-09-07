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
from src.models import AlertEvent, OperationalTicket, WatchOfficerBrief


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NIM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    storage.override_db_path(":memory:")
    yield
    storage.reset_db()


def _critical_alert(**kwargs) -> AlertEvent:
    base = dict(
        alert_type="ALERT_LOW_RVR",
        severity="CRITICAL",
        airport_code="KDCA",
        service_name="APDS_RVR",
        description="Low RVR 700 ft at KDCA runway 19.",
        event_id="evt-1",
    )
    base.update(kwargs)
    return AlertEvent(**base)


# ── Ticket creation tied to staging ──────────────────────────────────────────

class TestTicketCreation:
    def test_critical_alert_creates_ticket(self):
        alert = _critical_alert()
        action = stage_critical_alert(alert)
        ticket = storage.get_ticket_by_action_id(action.action_id)
        assert ticket is not None
        assert ticket.action_id == action.action_id
        assert ticket.alert_id == alert.alert_id
        assert ticket.status == "DRAFT"
        assert ticket.brief_id  # brief was generated
        assert ticket.created_by_agent == "watch_officer"

    def test_duplicate_alert_returns_existing_ticket(self):
        alert = _critical_alert()
        stage_critical_alert(alert)
        ticket_a = storage.get_ticket_by_action_id(
            storage.get_pending_staged_actions()[0].action_id
        )
        # Re-stage same alert
        stage_critical_alert(alert)
        ticket_b = storage.get_ticket_by_action_id(
            storage.get_pending_staged_actions()[0].action_id
        )
        assert ticket_a.ticket_id == ticket_b.ticket_id, "duplicate staging must be idempotent"
        assert len(storage.get_draft_operational_tickets()) == 1

    def test_non_critical_alert_does_not_create_ticket(self):
        warn = _critical_alert(severity="WARNING")
        stage_critical_alert(warn)  # returns None, no ticket
        assert storage.get_draft_operational_tickets() == []

    def test_draft_query_returns_only_drafts(self):
        a1 = stage_critical_alert(_critical_alert(alert_type="ALERT_LOW_RVR"))
        a2 = stage_critical_alert(_critical_alert(alert_type="ALERT_ISMC_LINK_DEGRADED",
                                                 service_name="ISMC"))
        t1 = storage.get_ticket_by_action_id(a1.action_id)
        storage.approve_operational_ticket(t1.ticket_id, actor="x", note="")

        drafts = storage.get_draft_operational_tickets()
        draft_ids = {t.ticket_id for t in drafts}
        # The approved one should be excluded
        assert t1.ticket_id not in draft_ids


# ── Decision transitions ──────────────────────────────────────────────────────

class TestTicketDecisions:
    def test_approve_transitions_draft_to_approved(self):
        action = stage_critical_alert(_critical_alert())
        ticket = storage.get_ticket_by_action_id(action.action_id)
        updated = storage.approve_operational_ticket(
            ticket.ticket_id, actor="supervisor_smith", note="cleared by tower"
        )
        assert updated.status == "APPROVED_FOR_ESCALATION"
        assert updated.decided_by == "supervisor_smith"
        assert updated.decision_note == "cleared by tower"

    def test_reject_transitions_draft_to_rejected(self):
        action = stage_critical_alert(_critical_alert())
        ticket = storage.get_ticket_by_action_id(action.action_id)
        updated = storage.reject_operational_ticket(
            ticket.ticket_id, actor="supervisor_smith", note="false alarm"
        )
        assert updated.status == "REJECTED"
        assert updated.decided_by == "supervisor_smith"

    def test_monitoring_is_non_terminal(self):
        action = stage_critical_alert(_critical_alert())
        ticket = storage.get_ticket_by_action_id(action.action_id)
        # Move to MONITORING
        monitored = storage.mark_operational_ticket_monitoring(
            ticket.ticket_id, actor="supervisor", note="watching weather"
        )
        assert monitored.status == "MONITORING"
        # Then later approve it — should work (MONITORING is non-terminal)
        approved = storage.approve_operational_ticket(
            ticket.ticket_id, actor="supervisor", note="escalating now"
        )
        assert approved.status == "APPROVED_FOR_ESCALATION"

    def test_approved_is_sticky(self):
        action = stage_critical_alert(_critical_alert())
        ticket = storage.get_ticket_by_action_id(action.action_id)
        storage.approve_operational_ticket(ticket.ticket_id, actor="a", note="first")
        again = storage.reject_operational_ticket(ticket.ticket_id, actor="b", note="overrule?")
        # Terminal state must hold
        assert again.status == "APPROVED_FOR_ESCALATION"
        assert again.decided_by == "a"

    def test_rejected_is_sticky(self):
        action = stage_critical_alert(_critical_alert())
        ticket = storage.get_ticket_by_action_id(action.action_id)
        storage.reject_operational_ticket(ticket.ticket_id, actor="a", note="ignore")
        attempted = storage.approve_operational_ticket(ticket.ticket_id, actor="b", note="overrule?")
        assert attempted.status == "REJECTED"
        assert attempted.decided_by == "a"


# ── Audit trail integration ───────────────────────────────────────────────────

class TestAuditIntegration:
    def test_ticket_creation_writes_audit_row(self):
        action = stage_critical_alert(_critical_alert())
        ticket = storage.get_ticket_by_action_id(action.action_id)
        audit = storage.get_action_audit_log(action_id=f"ticket:{ticket.ticket_id}")
        assert any(r["new_status"] == "DRAFT" for r in audit)

    def test_approve_writes_audit_row(self):
        action = stage_critical_alert(_critical_alert())
        ticket = storage.get_ticket_by_action_id(action.action_id)
        storage.approve_operational_ticket(ticket.ticket_id, actor="x", note="ok")
        audit = storage.get_action_audit_log(action_id=f"ticket:{ticket.ticket_id}")
        assert any(
            r["previous_status"] == "DRAFT" and r["new_status"] == "APPROVED_FOR_ESCALATION"
            for r in audit
        )

    def test_monitoring_writes_audit_row(self):
        action = stage_critical_alert(_critical_alert())
        ticket = storage.get_ticket_by_action_id(action.action_id)
        storage.mark_operational_ticket_monitoring(ticket.ticket_id, actor="x", note="watching")
        audit = storage.get_action_audit_log(action_id=f"ticket:{ticket.ticket_id}")
        assert any(r["new_status"] == "MONITORING" for r in audit)
