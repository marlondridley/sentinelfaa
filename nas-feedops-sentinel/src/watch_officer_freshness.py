"""
The Watch Officer brief as it should be shown now, not as it was when it was written.

A brief is stored when an operator asks for one, on the first visit, and when a CRITICAL alert is staged. If the feed
dies after a GREEN brief, the alerts that follow are WARNINGs, nothing stages, and the dashboard went on showing
"ALL CLEAR -- data is current" with only a generated-at time to say otherwise: the rule that checks our own
pipeline first never ran. The rules are cheap and need no model, so the dashboard recomputes the assessment from the
current data on every load and shows that; the stored brief supplies the narrative under "Details", and a notice
says so when the brief is older than the pipeline-down threshold or its status no longer matches.

Pure: it takes the context the Watch Officer already builds (build_watch_officer_context) and touches nothing.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src import watch_officer_assessment as rules
from src.models import WatchOfficerBrief
from src.timeutil import as_utc


def current_view(
    stored: WatchOfficerBrief, context: dict, *, now: datetime | None = None,
) -> tuple[WatchOfficerBrief, str | None]:
    """
    (the brief to render, a notice to put above it or None).

    The brief to render is `stored` with its assessment replaced by one computed from `context` now. The notice is
    set when the stored brief is older than WATCH_PIPELINE_DOWN_MINUTES, or when the status it carried is not the
    status now.
    """
    now = now or datetime.now(timezone.utc)
    fresh = rules.assess(context, now=now)
    age_minutes = (now - as_utc(stored.generated_at)).total_seconds() / 60
    was = stored.assessment.status if stored.assessment else None
    changed = was is not None and was != fresh.status

    notice = None
    if changed:
        notice = (f"The status has changed since this brief was written {rules.age_text(age_minutes)} ago: "
                  f"it was {was}, it is now {fresh.status}. The status and actions below are current; "
                  "the details are from when it was written. Refresh the brief to update them.")
    elif age_minutes >= rules.DOWN_AFTER_MINUTES:
        notice = (f"This brief was written {rules.age_text(age_minutes)} ago. The status and actions below are "
                  "recomputed from the current data; the details are from when it was written.")
    return stored.model_copy(update={"assessment": fresh}), notice
