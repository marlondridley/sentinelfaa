"""
The review queue, defined once: which open reviews await a decision, which a
person has chosen to keep monitoring, and what the AI suggests for each.

A review (a staged action, src/execution_gate.py) is open while it is PENDING.
"Continue monitoring" is a person's answer that leaves it open: the gate keeps
the action PENDING and marks its ticket MONITORING, with who and when. So an
open review is in exactly one of two states:

  awaiting    nobody has answered it yet: the work queue
  monitoring  someone answered "keep watching": acknowledged, still open, and
              still decidable (approve, override, reject) from its detail

The Review Queue page, the Dashboard and the attention strip all read these
from here, so a count on one screen is the same rows on another. The unit is
always the review (one per condition), never the reading: a condition that
fires two hundred times is one row, and one item of work.

What the AI suggests is the Critic's newest verdict on any reading the review
covers -- not only its first reading, which is all the review's own alert_id
names.

Every open review is listed whatever its data came from: a replayed fault's
review is real work. `live` says which ones count toward the Dashboard's
verdict, which is about the live feed alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src import storage
from src.models import OperationalTicket, StagedAction

ESCALATE, NO_ESCALATION, NO_AI_REVIEW = "Escalate", "Don't escalate", "No AI review"
MONITORING_TICKET = "MONITORING"


@dataclass(frozen=True)
class OpenReview:
    action: StagedAction
    ticket: OperationalTicket | None
    verdict: dict | None          # storage.get_latest_critic_verdicts_for_actions
    live: bool = True             # not known to come from a replay, a sample or an API post

    @property
    def monitoring(self) -> bool:
        return self.ticket is not None and self.ticket.status == MONITORING_TICKET

    @property
    def confidence(self) -> float | None:
        """None for no verdict, and for a degraded one (Critic disabled, errored or rejected by its guardrail)."""
        return (self.verdict or {}).get("confidence")

    @property
    def suggests(self) -> str:
        if self.confidence is None:
            return NO_AI_REVIEW
        return ESCALATE if self.verdict.get("escalate") else NO_ESCALATION

    @property
    def ai_line(self) -> str:
        """In words, for a line of text: 'AI suggests escalate · 86%', or that it gave no verdict."""
        if self.confidence is None:
            return "Not reviewed by the AI"
        return f"AI suggests {self.suggests.lower()} · {self.confidence:.0%}"

    @property
    def waiting_since(self) -> datetime:
        return self.action.first_seen_at or self.action.created_at


async def open_reviews() -> list[OpenReview]:
    """Every open review, newest first, with its ticket and the AI's newest verdict."""
    actions = await storage.get_pending_staged_actions()
    ids = [a.action_id for a in actions]
    tickets = await storage.get_tickets_by_action_ids(ids)
    verdicts = await storage.get_latest_critic_verdicts_for_actions(ids)
    live = {a.action_id for a in await storage.get_pending_staged_actions(data_origin="live")}
    return [OpenReview(a, tickets.get(a.action_id), verdicts.get(a.action_id), a.action_id in live)
            for a in actions]


def awaiting(reviews: list[OpenReview]) -> list[OpenReview]:
    return [r for r in reviews if not r.monitoring]


def monitored(reviews: list[OpenReview]) -> list[OpenReview]:
    return [r for r in reviews if r.monitoring]


def escalations(reviews: list[OpenReview]) -> list[OpenReview]:
    """The reviews awaiting a decision that the AI suggests escalating."""
    return [r for r in awaiting(reviews) if r.suggests == ESCALATE]
