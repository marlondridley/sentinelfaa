"""
What needs the watch officer now: the items of the Overview's attention strip.

Each item names its subject and says where it is: a dashboard page and the
filters that open that page on the subject itself -- one review, one
airport's RVR, one airport's feed status, the CRITICAL alerts -- so one click
lands on the thing, not on a page to search. Deterministic, from the review
queue, feed status, RVR readings and alerts; no model is involved.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src import config, storage

ACTION, ATTENTION, GOOD = "ACTION", "ATTENTION", "GOOD"
_RANK = {ACTION: 0, ATTENTION: 1, GOOD: 2}


def _item(text: str, level: str, page: str, **params: Any) -> dict:
    """`params` are the page's own filters -- the Alert Log's include `severity`."""
    return {"text": text, "severity": level, "page": page,
            "params": {k: v for k, v in params.items() if v is not None}}


def _lowest_rvr(row: dict) -> tuple[int, str] | None:
    """
    (feet, position) of the lowest RVR position on a live, current reading, or
    None. The alert rule raises CRITICAL for any of the three positions, so the
    strip reads all three; a replayed reading, or one older than the stale-feed
    limit, is not the picture right now (a silent feed shows as a feed item).
    """
    if row.get("data_origin", "live") != "live":
        return None
    seen = row.get("event_time_utc")
    if isinstance(seen, datetime):
        seen = seen if seen.tzinfo else seen.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - seen).total_seconds() > config.STALE_FEED_SECONDS:
            return None
    positions = [(row[k], name) for k, name in
                 (("rvr_touchdown", "touchdown"), ("rvr_midpoint", "midpoint"), ("rvr_rollout", "rollout"))
                 if row.get(k) is not None]
    return min(positions) if positions else None


async def build_attention_items(
    airport_status: dict[str, str],
    alerts: list[dict],
    rvr_rows: list[dict],
) -> list[dict]:
    """Most urgent first: {"text", "severity", "page", "params"}; one all-clear item when nothing needs anyone."""
    items: list[dict] = []

    pending = await storage.get_pending_staged_actions()
    if len(pending) == 1:
        [review] = pending
        items.append(_item(
            f"Review awaiting a decision: {(review.alert_type or '').removeprefix('ALERT_')} "
            f"at {review.airport_code or 'PCT'} ({review.occurrences:,} reading(s))",
            ACTION, "critic", review=review.action_id))
    elif pending:
        items.append(_item(f"{len(pending)} reviews awaiting a decision", ACTION, "critic"))

    threshold = config.RVR_LOW_THRESHOLD_FT
    for row in rvr_rows:
        lowest = _lowest_rvr(row)
        if lowest and lowest[0] < threshold:
            feet, position = lowest
            airport, runway = row.get("airport_code"), row.get("runway")
            items.append(_item(f"Low RVR {airport} runway {runway}: {feet:,} ft at {position} "
                               f"(minimum {threshold:,})", ACTION, "health", airport=airport, focus="rvr"))

    for airport, status in sorted(airport_status.items()):
        if status in ("UNAVAILABLE", "DISCONNECTED"):
            items.append(_item(f"{airport} feed {status.lower()}: escalate to the SWIM service desk",
                               ACTION, "health", airport=airport, focus="status"))
        elif status == "STALE":
            items.append(_item(f"{airport} feed stale: messages overdue", ATTENTION, "health",
                               airport=airport, focus="status"))
        elif status == "DEGRADED":
            items.append(_item(f"{airport} feed degraded: check the ISMC link", ATTENTION, "health",
                               airport=airport, focus="status"))

    # Every CRITICAL alert is covered by a review, open or decided; one that no
    # review covers is a gap in the escalation path (the reconciler repairs it).
    critical_ids = [a["alert_id"] for a in alerts if a.get("severity") == "CRITICAL" and a.get("alert_id")]
    uncovered = set(critical_ids) - set(await storage.get_covering_actions(critical_ids))
    if uncovered:
        items.append(_item(f"{len(uncovered)} CRITICAL alert(s) not covered by any review", ATTENTION,
                           "alerts", severity="CRITICAL"))

    if not airport_status:
        items.append(_item("No SWIM data: check the Jumpstart Kit is publishing", ATTENTION, "ingest"))
    if not items:
        items.append(_item("All clear: feeds current, nothing awaiting review", GOOD, "overview"))
    return sorted(items, key=lambda i: _RANK[i["severity"]])
