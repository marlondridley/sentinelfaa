"""
What the Watch Officer's model is shown.

The model writes four things: what changed, why it matters, what a person
should review, and what is not known. It is not asked to rediscover the
operating picture from raw telemetry, so it is handed a projection built for
those four questions instead of the whole status snapshot:

  - what changed   alerts rolled up by condition (count, first and last seen,
                   one example), worst first, not one row per reading;
  - to review      the open reviews, each with the Critic's verdict on it;
  - not known      data gaps computed here, deterministically (airports with
                   no data, events that could not be rebuilt, reviews with no
                   AI verdict, and anything left out of this summary).

Every list is capped and every text is trimmed, so the size is bounded by
construction, not by how large the incident is: the model-use policy refuses
any prompt over 120,000 characters (policies/model-use.yaml). The true counts
are always stated and what was cut is named in "omitted". The deterministic
brief (posture, trust, counts, affected airports) is computed from the full
context and never sees this projection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from src import watch_officer_assessment as rules

MAX_ALERT_CONDITIONS = 15
MAX_OPEN_REVIEWS = 10
MAX_AI_VERDICTS = 8
MAX_DRAFT_TICKETS = 5
MAX_EVIDENCE_IDS = 5
TEXT_CHARS = 200

_SEVERITY_RANK = {"CRITICAL": 0, "WARNING": 1}


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def _text(value: Any, limit: int = TEXT_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _alert_conditions(alerts: list[dict], critical: list[dict]) -> list[dict]:
    """One entry per condition, with how often and when, worst and busiest first."""
    seen_ids: set[Any] = set()
    groups: dict[tuple, dict] = {}
    for alert in [*critical, *alerts]:
        alert_id = alert.get("alert_id")
        if alert_id is not None:
            if alert_id in seen_ids:
                continue
            seen_ids.add(alert_id)
        key = (alert.get("alert_type"), alert.get("airport_code"), alert.get("service_name"), alert.get("severity"))
        when = _iso(alert.get("triggered_at"))
        group = groups.get(key)
        if group is None:
            groups[key] = {
                "alert_type": key[0], "airport": key[1], "service": key[2], "severity": key[3],
                "count": 1, "first_seen": when, "last_seen": when, "example": _text(alert.get("description")),
            }
        else:
            group["count"] += 1
            group["first_seen"] = min(group["first_seen"], when)
            group["last_seen"] = max(group["last_seen"], when)
    ordered = sorted(groups.values(), key=lambda g: g["last_seen"], reverse=True)
    return sorted(ordered, key=lambda g: (_SEVERITY_RANK.get(g["severity"], 2), -g["count"]))


def _verdict_summary(row: dict) -> dict:
    return {
        "escalate": bool(row.get("escalate")),
        "confidence": row.get("confidence"),
        "pattern": row.get("pattern"),
        "summary": _text(row.get("bluf") or row.get("reasoning"), 240),
    }


def _open_reviews(pending: list[dict], verdicts: dict[Any, dict]) -> list[dict]:
    reviews = []
    for item in pending:
        verdict = verdicts.get(item.get("alert_id"))
        reviews.append({
            "alert_type": item.get("alert_type"),
            "airport": item.get("airport_code"),
            "readings": item.get("occurrences"),
            "first_seen": _iso(item.get("first_seen_at") or item.get("created_at")),
            "last_seen": _iso(item.get("last_seen_at") or item.get("created_at")),
            "why_staged": _text(item.get("rationale"), 240),
            "proposed": _text(item.get("recommended_action"), 160),
            "ai_verdict": _verdict_summary(verdict) if verdict else None,
        })
    return sorted(reviews, key=lambda r: r["last_seen"], reverse=True)


def _compact(row: dict, fields: int = 6) -> dict:
    """A few scalar fields of a record, each trimmed: enough to name it, not to reproduce it."""
    kept = {k: (_text(v, 120) if isinstance(v, str) else v)
            for k, v in row.items() if isinstance(v, (str, int, float, bool))}
    return dict(list(kept.items())[:fields])


def project_for_model(context: dict[str, Any], signals: dict[str, Any]) -> dict[str, Any]:
    """The bounded, decision-shaped picture the Watch Officer's model is shown."""
    alerts = list(context.get("alerts") or [])
    critical = list(context.get("critical_alerts") or [])
    pending = [dict(p) for p in context.get("pending_actions") or []]
    drafts = [dict(t) for t in context.get("draft_tickets") or []]
    critic_rows = [dict(r) for r in context.get("recent_critic_results") or []]
    verdicts = {r.get("alert_id"): r for r in critic_rows if r.get("alert_id")}

    assessment = rules.assess(context, signals)
    age_limit = (rules._dt(context.get("snapshot_time")) or datetime.now(timezone.utc)) - timedelta(hours=rules.ALERT_MAX_AGE_HOURS)
    conditions = _alert_conditions(alerts, critical)
    for c in conditions:                                       # so a four-day-old alert is never read as current
        last = rules._dt(c["last_seen"])
        c["historical"] = bool(last and last < age_limit)
        c["sensor_artifact"] = rules.is_artifact({"alert_type": f"ALERT_{str(c['alert_type']).upper()}"
                                                  if not str(c["alert_type"]).startswith("ALERT_") else c["alert_type"],
                                                  "description": c["example"]})
    reviews = _open_reviews(pending, verdicts)
    omitted = {
        "alert_conditions": max(0, len(conditions) - MAX_ALERT_CONDITIONS),
        "open_reviews": max(0, len(reviews) - MAX_OPEN_REVIEWS),
        "ai_verdicts": max(0, len(critic_rows) - MAX_AI_VERDICTS),
        "draft_tickets": max(0, len(drafts) - MAX_DRAFT_TICKETS),
    }

    unknowns = [f"No data has been received for {airport}." for airport in signals.get("missing_airports") or []]
    if context.get("failed_reconstruction_count"):
        unknowns.append(f"{context['failed_reconstruction_count']} events could not be rebuilt and were not analysed.")
    if signals.get("no_recent_events"):
        unknowns.append("No events arrived in the analysis window.")
    if signals.get("no_active_aircraft"):
        unknowns.append("No active aircraft positions were reported.")
    unreviewed = sum(1 for r in reviews if r["ai_verdict"] is None)
    if unreviewed:
        unknowns.append(f"{unreviewed} of {len(reviews)} open reviews have no AI verdict.")
    for name, count in omitted.items():
        if count:
            unknowns.append(f"{count} {name.replace('_', ' ')} were left out of this summary to keep it short.")

    projection: dict[str, Any] = {
        "as_of": context.get("snapshot_time"),
        # Settled by rules before you write (src/watch_officer_assessment.py): do not contradict it.
        "assessment": {
            "status": assessment.status, "status_line": assessment.status_line,
            "data_health": {"state": assessment.data_health.state, "scope": assessment.data_health.scope,
                            "reason": assessment.data_health.reason},
            "alerts": {"from_last_6h": assessment.current_alert_count, "critical_from_last_6h": assessment.current_critical_count,
                       "older_history": assessment.historical_alert_count, "sensor_artifacts": assessment.artifact_alert_count},
            "reviews": {"open_from_last_6h": assessment.current_review_count,
                        "older_or_on_artifacts": assessment.historical_review_count},
            "incidents": [{"airports": i.airports, "kinds": i.alert_types, "alerts": i.alert_count, "reviews": i.reviews,
                           "started": i.started_at.isoformat(), "historical": i.historical, "sensor_artifact": i.artifact}
                          for i in assessment.incidents],
            "actions_already_set": [x.action for x in assessment.actions],
            "checks_that_disagree": assessment.consistency_warnings,
        },
        "state": {"airport_status": context.get("airport_status"), "per_airport": context.get("per_airport")},
        "counts": {
            "critical_alerts": context.get("critical_alert_count", 0),
            "open_reviews": context.get("pending_actions_count", len(pending)),
            "draft_tickets": context.get("draft_tickets_count", len(drafts)),
            "events_in_window": context.get("events_in_window", 0),
            "analysed_events": context.get("analyzed_event_count"),
            "active_aircraft": context.get("active_aircraft", 0),
            "avg_latency_ms": context.get("avg_latency_ms"),
        },
        "signals": signals,
        "what_changed": {"alert_conditions": conditions[:MAX_ALERT_CONDITIONS]},
        "open_reviews": reviews[:MAX_OPEN_REVIEWS],
        "recent_ai_verdicts": [_verdict_summary(r) for r in critic_rows[:MAX_AI_VERDICTS]],
        "draft_tickets": [_compact(t) for t in drafts[:MAX_DRAFT_TICKETS]],
        "unknowns": unknowns,
        "omitted": omitted,
        "evidence_ids": list(context.get("provenance_ids") or [])[:MAX_EVIDENCE_IDS],
    }
    if context.get("triggering_alert"):
        projection["triggering_alert"] = _compact(dict(context["triggering_alert"]), fields=8)
    return projection
