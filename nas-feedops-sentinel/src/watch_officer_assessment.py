"""
What the Watch Officer's brief says, worked out by rules before any model writes a word.

A brief is read in ten seconds by someone deciding what to do. So it has a fixed
shape: a status, a one-sentence bottom line, at most three actions each with an
owner and a "done when", and what to keep an eye on. The rules that shape it:

  1. Lead with a status and a bottom line (red / amber / green, and what it means).
  2. Check our own pipeline first. If it is down or catching up, say so and
     suppress operational conclusions: every alert raised in that time is suspect.
  3. Cluster alerts by cause. Alerts that begin together across airports are one
     incident, not five reviews.
  4. Stale alerts are history. Older than ALERT_MAX_AGE_HOURS with no newer evidence,
     they are reported as historical, never as current. A sensor's not-reporting code
     (RVR 0 ft) is an artifact, not a reading.
  5. Every action has an owner and a "done when", and there are at most three.
  6. A hard length cap above the fold (ABOVE_FOLD_MAX_WORDS words).
  7. Check the brief against itself before it is published; what disagrees is
     flagged, never published as if it agreed.

This module is pure: it takes the context the Watch Officer already builds and
returns a BriefAssessment. A model may then write prose inside that frame
(src/watch_officer_agent.py), and cannot change the status, the counts or the
actions. The text is written to fit the length cap, not trimmed to it afterwards.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config import settings
from src.models import BriefAction, BriefAssessment, BriefIncident, DataHealth
from src.timeutil import as_utc

ALERT_MAX_AGE_HOURS = settings.WATCH_ALERT_MAX_AGE_HOURS          # older than this, with no newer evidence: history
DOWN_AFTER_MINUTES = settings.WATCH_PIPELINE_DOWN_MINUTES         # nothing received for this long: the pipeline is down
BACKLOG_LAG_SECONDS = settings.WATCH_BACKLOG_LAG_SECONDS          # recent average lag above this: catching up, not live
CLUSTER_GAP_MINUTES = settings.WATCH_CLUSTER_GAP_MINUTES          # alerts closer together than this are one incident
LATENCY_WINDOW_MINUTES = 15      # lag is judged on what arrived recently, never on the average of everything stored
MAX_ACTIONS = 3
MAX_WATCH = 3
MAX_INCIDENTS = 5
ABOVE_FOLD_MAX_WORDS = 120
MODEL_BLUF_MAX_WORDS = 40

PIPELINE_ONCALL = "Pipeline on-call"
WATCH_OFFICER = "Watch officer"
DUTY_MANAGER = "Duty manager"

_DOWN_LIKE = frozenset({"STALE", "UNAVAILABLE", "DISCONNECTED"})
# The RVR reading is the first thing in the alert's wording ("Low RVR 0 ft at touchdown on runway 10 at KBWI").
# Anchored there because the same sentence goes on to name a runway, which is free text from the message: a
# runway of "10L 0 ft" made a real low-RVR alert read as a sensor code.
_ARTIFACT_RVR = re.compile(r"^\s*Low RVR 0 ft at\b")
# A bottom line about our data must not reach for consequences in the airspace, or imply that this console
# feeds controllers: it does not. Whole words, and the sense that matters ("reaching controllers", not "the
# Kafka controller").
_OVERREACH = re.compile(
    r"\bair traffic control(?:lers?)?\b|\batc\b"
    r"|\b(?:to|reach(?:es|ing)?|for|by)\s+(?:the\s+)?controllers?\b"
    r"|\bunsafe\b|\bcollisions?\b|\bloss of separation\b|\btactical (?:information|data|picture)\b"
    r"|\bcontrollers?\b(?:\W+\w+){0,4}?\W+(?:affected|impacted|misled|unaware)\b"
    r"|\bground (?:stops?|delays?)\b|\b(?:affect|impact)(?:s|ed|ing)?\s+(?:air\s+)?traffic\b",
    re.IGNORECASE)
# A claim the console is denying is the statement the prompt asks for ("not used by controllers"), not an
# overreach. Negation counts if it comes within a few words before the phrase, in the same sentence.
_NEGATIONS = frozenset({"not", "no", "never", "nor", "neither", "without", "cannot", "nothing", "none", "nobody"})
_NEGATION_WORDS = 4


def _negated(text: str, start: int) -> bool:
    before = re.split(r"[.;:!?]", text[:start])[-1]
    return any(w.lower() in _NEGATIONS or w.lower().endswith("n't")
               for w in re.findall(r"[\w']+", before)[-_NEGATION_WORDS:])


def overreach(text: str) -> str | None:
    """The phrase in `text` that claims something about the airspace or controllers this console cannot know, if any."""
    text = text or ""
    for match in _OVERREACH.finditer(text):
        if not _negated(text, match.start()):
            return match.group(0).strip().lower()
    return None


# -- small helpers ----------------------------------------------------------------------------------

def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return as_utc(value)
    if isinstance(value, str) and value:
        try:
            return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def words(text: str) -> int:
    return len(text.split())


def age_text(minutes: float | None) -> str:
    if minutes is None:
        return "an unknown time"
    if minutes < 90:
        return f"{minutes:.0f} min"
    hours = minutes / 60
    if hours < 48:
        return f"about {hours:.0f} h"
    return f"about {hours / 24:.0f} days"


def _short(alert_type: str | None) -> str:
    return (alert_type or "alert").removeprefix("ALERT_").replace("_", " ").lower()


def is_artifact(alert: dict[str, Any]) -> bool:
    """A sensor not-reporting code stored as a reading: RVR 0 ft is 'not reporting', not zero visibility."""
    return alert.get("alert_type") == "ALERT_LOW_RVR" and bool(_ARTIFACT_RVR.search(alert.get("description") or ""))


# -- 2. our own pipeline, first ---------------------------------------------------------------------

def assess_data_health(context: dict[str, Any], now: datetime) -> DataHealth:
    statuses: dict[str, str] = dict(context.get("airport_status") or {})
    reporting = {a: s for a, s in statuses.items() if s != "NO DATA"}          # an airport that never reports is not "down"
    stale = sorted(a for a, s in reporting.items() if s in _DOWN_LIKE)
    latest = _dt(context.get("latest_event_at"))
    age = round(max((now - latest).total_seconds() / 60, 0.0), 1) if latest else None
    lag_s = (context.get("avg_latency_ms") or 0) / 1000
    all_stale = bool(reporting) and len(stale) == len(reporting) and len(reporting) >= 2

    if not context.get("events_in_window") and latest is None:
        return DataHealth(state="UNKNOWN", scope="none", reason="No events have been ingested.")
    if (age is not None and age >= DOWN_AFTER_MINUTES) or all_stale:
        return DataHealth(
            state="DOWN", scope="pipeline", minutes_since_last_event=age, stale_airports=stale,
            reason=(f"No data has arrived for {age_text(age)}" if age is not None and age >= DOWN_AFTER_MINUTES
                    else "Every reporting airport is stale at the same time")
                   + (f"; all {len(reporting)} reporting airports went stale together." if all_stale else "."))
    degraded = sorted({*stale, *(a for a, s in reporting.items() if s == "DEGRADED")})
    if degraded:
        return DataHealth(state="DEGRADED", scope="airport", minutes_since_last_event=age, stale_airports=degraded,
                          reason=f"Data is stale or degraded at {', '.join(degraded)}.")
    if lag_s >= BACKLOG_LAG_SECONDS:
        return DataHealth(state="DEGRADED", scope="backlog", minutes_since_last_event=age,
                          reason=f"Data is arriving {age_text(lag_s / 60)} after it happened: a backlog, not live.")
    return DataHealth(state="OK", scope="none", minutes_since_last_event=age,
                      reason="Data is current at every reporting airport.")


# -- 3 and 4. alerts: current or history, one incident or five -------------------------------------------

def _alerts(context: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[Any] = set()
    out = []
    for alert in [*(context.get("critical_alerts") or []), *(context.get("alerts") or [])]:
        key = alert.get("alert_id") or id(alert)
        if key in seen:
            continue
        seen.add(key)
        when = _dt(alert.get("triggered_at"))
        if when is not None:
            out.append({**alert, "_at": when})
    return sorted(out, key=lambda a: a["_at"])


def _review_time(review: dict[str, Any]) -> datetime | None:
    return _dt(review.get("last_seen_at")) or _dt(review.get("created_at")) or _dt(review.get("first_seen_at"))


def cluster_incidents(alerts: list[dict[str, Any]], reviews: list[dict[str, Any]], now: datetime) -> list[BriefIncident]:
    """Alerts that began within CLUSTER_GAP_MINUTES of each other, across airports, as one incident each."""
    limit = now - timedelta(hours=ALERT_MAX_AGE_HOURS)
    gap = timedelta(minutes=CLUSTER_GAP_MINUTES)
    known_alert_ids = {a.get("alert_id") for a in alerts}
    groups: list[list[dict[str, Any]]] = []
    for alert in alerts:
        if groups and (alert["_at"] - groups[-1][-1]["_at"]) <= gap:
            groups[-1].append(alert)
        else:
            groups.append([alert])

    incidents = []
    for group in groups:
        start, end = group[0]["_at"], group[-1]["_at"]
        ids = {a.get("alert_id") for a in group}
        attached = sum(
            1 for r in reviews
            if r.get("alert_id") in ids
            or (r.get("alert_id") not in known_alert_ids          # its alert is older than the ones fetched: place it by time
                and (rt := _review_time(r)) is not None and start - gap <= rt <= end + gap))
        incidents.append(BriefIncident(
            started_at=start, ended_at=end,
            airports=sorted({a["airport_code"] for a in group if a.get("airport_code")}),
            alert_types=sorted({_short(a.get("alert_type")) for a in group}),
            alert_count=len(group), reviews=attached,
            historical=end < limit, artifact=all(is_artifact(a) for a in group)))
    return sorted(incidents, key=lambda i: i.ended_at, reverse=True)[:MAX_INCIDENTS]


# -- the assessment -----------------------------------------------------------------------------------

def assess(context: dict[str, Any], signals: dict[str, Any] | None = None, *, now: datetime | None = None) -> BriefAssessment:
    now = now or _dt(context.get("snapshot_time")) or datetime.now(timezone.utc)
    limit = now - timedelta(hours=ALERT_MAX_AGE_HOURS)
    health = assess_data_health(context, now)

    alerts = _alerts(context)
    reviews = [dict(r) for r in context.get("pending_actions") or []]
    artifact_ids = {a.get("alert_id") for a in alerts if is_artifact(a)}
    genuine = [a for a in alerts if not is_artifact(a)]
    current = [a for a in genuine if a["_at"] >= limit]
    historical = [a for a in genuine if a["_at"] < limit]
    current_critical = [a for a in current if a.get("severity") == "CRITICAL"]

    def is_recent(review: dict[str, Any]) -> bool:
        seen = _review_time(review)
        return seen is None or seen >= limit                        # no timestamp: cannot call it old

    current_reviews = [r for r in reviews if is_recent(r) and r.get("alert_id") not in artifact_ids]
    artifact_reviews = [r for r in reviews if is_recent(r) and r.get("alert_id") in artifact_ids]
    stale_reviews = [r for r in reviews if not is_recent(r)] + artifact_reviews     # closed as history either way

    result = BriefAssessment(
        data_health=health, incidents=cluster_incidents(alerts, reviews, now),
        current_alert_count=len(current), current_critical_count=len(current_critical),
        historical_alert_count=len(historical), artifact_alert_count=len(artifact_ids),
        current_review_count=len(current_reviews), historical_review_count=len(stale_reviews))
    result.affected_airports = sorted({*health.stale_airports, *(a["airport_code"] for a in current_critical if a.get("airport_code"))})
    n_reporting = len([s for s in (context.get("airport_status") or {}).values() if s != "NO DATA"])
    _status_and_bluf(result, health, current, current_critical, current_reviews, stale_reviews, historical)
    result.actions = _actions(health, current_critical, current_reviews, stale_reviews, len(artifact_reviews), n_reporting)
    result.watch = _watch(context, health, historical)
    result.confidence_label, result.confidence_reason = _confidence(health)
    result.consistency_warnings = check_consistency(result, context, alerts, reviews, limit)
    return fit_above_the_fold(result)


def _open_work(critical: list, reviews: list, at: str = "") -> tuple[str, str]:
    """
    The headline and the sentence for open CRITICAL work, in the review queue's unit (src/review_queue.py):
    reviews awaiting a decision, not readings -- one condition firing forty times is one decision. A review
    someone chose to keep monitoring is open and asks nobody. With no review on record, the alerts are what waits.
    """
    span = f"from the last {ALERT_MAX_AGE_HOURS} h"
    awaiting = [r for r in reviews if not r.get("monitoring")]
    watched = len(reviews) - len(awaiting)
    if awaiting:
        return (f"{len(awaiting)} REVIEW(S) AWAIT A DECISION",
                f"{len(awaiting)} review(s){at} of critical alerts {span} await a person"
                + (f"; {watched} more being monitored." if watched else "."))
    if watched:
        return (f"{watched} CRITICAL CONDITION(S) BEING MONITORED",
                f"{watched} critical condition(s){at} {span} are open and being monitored; none awaits a decision.")
    return (f"{len(critical)} CRITICAL ALERT(S) NEED A DECISION",
            f"{len(critical)} critical alert(s){at} {span} await a person.")


def _status_and_bluf(result, health, current, current_critical, current_reviews, stale_reviews, historical) -> None:
    if health.state == "DOWN":
        result.status, result.status_line = "RED", "DATA PICTURE: DOWN — our feed, not the airports"
        together = f"; all {len(health.stale_airports)} airports went stale together" if len(health.stale_airports) >= 2 else ""
        result.bluf = (f"Our feed stopped {age_text(health.minutes_since_last_event)} ago{together}. This is our pipeline, "
                       "not the airports. Don't use this data for operational awareness until it is green.")
    elif health.state == "UNKNOWN":
        result.status, result.status_line = "GREY", "DATA PICTURE: UNKNOWN — nothing ingested"
        result.bluf = "No data has been ingested, so the picture cannot be assessed. Confirm the feed is connected."
    elif health.state == "DEGRADED":
        # A CRITICAL alert somewhere the data is trustworthy still needs a person, whatever is wrong elsewhere:
        # the same alert is RED when every feed is healthy, and must not turn AMBER because one airport is stale.
        stale = set(health.stale_airports)
        reliable = [a for a in current_critical if a.get("airport_code") not in stale]
        unverified = [a for a in current_critical if a.get("airport_code") in stale]
        where = "catching up" if health.scope == "backlog" else f"degraded at {', '.join(health.stale_airports)}"
        if reliable:
            airports = sorted({a["airport_code"] for a in reliable if a.get("airport_code")})
            headline, sentence = _open_work(
                reliable, [r for r in current_reviews if r.get("airport_code") not in stale],
                at=f" at {', '.join(airports)}" if airports else "")
            result.status = "RED"
            result.status_line = f"{headline} — feed {where}"
            result.bluf = f"{sentence} {health.reason} Treat the degraded picture as unverified until it recovers."
        else:
            result.status = "AMBER"
            result.status_line = ("DATA PICTURE: CATCHING UP — a backlog, not live" if health.scope == "backlog"
                                  else f"DATA PICTURE: DEGRADED — {', '.join(health.stale_airports)}")
            result.bluf = health.reason + " Treat that picture as unverified until it recovers."
        if unverified:
            result.bluf += (f" {len(unverified)} critical alert(s) at {', '.join(sorted({a.get('airport_code') or '?' for a in unverified}))}"
                            " cannot be verified while that feed is degraded.")
    else:
        if current_critical:
            result.status = "RED"
            result.status_line, sentence = _open_work(current_critical, current_reviews)
            result.bluf = f"Data is current. {sentence}"
        elif current or current_reviews:
            result.status, result.status_line = "AMBER", "OPEN ITEMS — data is current"
            result.bluf = (f"Data is current. {len(current)} alert(s) and {len(current_reviews)} review(s) from the "
                           f"last {ALERT_MAX_AGE_HOURS} h are open.")
        else:
            result.status, result.status_line = "GREEN", "ALL CLEAR — data is current, nothing open"
            result.bluf = f"Data is current at every reporting airport and nothing from the last {ALERT_MAX_AGE_HOURS} h needs a decision."
        old = len(stale_reviews) or len(historical)
        if old:
            result.bluf += f" {old} older item(s) are history and need closing."


def _actions(health, current_critical, current_reviews, stale_reviews, fresh_artifacts, n_reporting) -> list[BriefAction]:
    acts: list[BriefAction] = []
    if health.state in ("DOWN", "UNKNOWN"):
        acts.append(BriefAction(
            action="Confirm the feed is live; restart it if not.", owner=PIPELINE_ONCALL,
            done_when=f"All {n_reporting or 'reporting'} airports show OK on Feed Health for 5 min."))
    elif health.state == "DEGRADED":
        airport_scope = health.scope == "airport"
        acts.append(BriefAction(
            action=(f"Restore the feed at {', '.join(health.stale_airports)}." if airport_scope
                    else "Let the backlog drain; confirm lag is back to normal."),
            owner=PIPELINE_ONCALL,
            done_when=(f"{', '.join(health.stale_airports)} show OK on Feed Health." if airport_scope
                       else "Average lag is under 30 s.")))
    if stale_reviews:
        why = "as feed outage" if health.state in ("DOWN", "DEGRADED") else "as history"
        what = (f"{len(stale_reviews)} review(s) ({fresh_artifacts} on RVR 0 ft sensor codes)" if fresh_artifacts
                else f"{len(stale_reviews)} stale review(s)")
        acts.append(BriefAction(
            action=f"Close {what} {why}.", owner=WATCH_OFFICER,
            done_when=f"None in the queue is older than {ALERT_MAX_AGE_HOURS} h."))
    # What still asks for a person: a review nobody has answered. One being monitored already has its answer.
    awaiting = [r for r in current_reviews if not r.get("monitoring")]
    if awaiting or (current_critical and not current_reviews):
        n = len(awaiting) or len(current_critical)
        acts.append(BriefAction(
            action=(f"Decide the {n} review(s) awaiting a decision"
                    + (" once the feed is current." if health.state == "DOWN" else ".")),
            owner=DUTY_MANAGER, done_when="Each has Approve, Reject or Monitor recorded."))
    return acts[:MAX_ACTIONS]


def _watch(context: dict[str, Any], health: DataHealth, historical: list[dict[str, Any]]) -> list[str]:
    items: list[str] = []
    real_rvr = [a for a in historical if a.get("alert_type") == "ALERT_LOW_RVR"]
    if real_rvr:
        a = real_rvr[-1]
        value = re.search(r"(\d+) ft", a.get("description") or "")
        items.append(f"{a.get('airport_code') or 'An airport'} RVR{f' {value.group(1)} ft' if value else ''}: "
                     "re-check once live; may be real.")
    lag = (context.get("avg_latency_ms") or 0) / 1000
    if lag >= 30 and health.state != "OK":
        items.append(f"Lag {lag:,.0f} s: should fall under 30 s after reconnect.")
    return items[:MAX_WATCH]


def _confidence(health: DataHealth) -> tuple[str, str]:
    """How sure we are of the diagnosis about our own data (not a claim about the airspace)."""
    if health.state == "DOWN" and len(health.stale_airports) >= 2:
        return "HIGH", f"all airports stale together; newest event {age_text(health.minutes_since_last_event)} old."
    if health.state == "DOWN":
        return "MEDIUM", f"nothing has arrived for {age_text(health.minutes_since_last_event)}."
    if health.state == "DEGRADED":
        return "MEDIUM", "only part of the feed is affected."
    if health.state == "OK":
        return "HIGH", "data is current at every reporting airport."
    return "LOW", "nothing has been ingested to judge from."


# -- 7. the brief checked against itself ------------------------------------------------------------------------

def check_consistency(result: BriefAssessment, context: dict[str, Any], alerts: list[dict[str, Any]],
                      reviews: list[dict[str, Any]], limit: datetime) -> list[str]:
    warnings: list[str] = []
    if result.current_review_count + result.historical_review_count != len(reviews):
        warnings.append(f"Review counts disagree: {result.current_review_count} current + "
                        f"{result.historical_review_count} historical is not the {len(reviews)} pending.")
    old_ids = {a.get("alert_id") for a in alerts if a["_at"] < limit}
    stale_escalations = [c for c in context.get("recent_critic_results") or []
                         if c.get("escalate") and c.get("alert_id") in old_ids]
    if stale_escalations:
        warnings.append(f"{len(stale_escalations)} AI escalation(s) concern alerts older than {ALERT_MAX_AGE_HOURS} h "
                        "and are not current.")
    if result.data_health.state == "DOWN":
        # Alerts raised while data was still arriving are what they were; those raised after it stopped are
        # consequences of the outage (our own staleness checks firing), not evidence about the airports.
        latest = _dt(context.get("latest_event_at"))
        after = [a for a in alerts if latest and a["_at"] > latest and a["_at"] >= limit]
        if after:
            warnings.append(f"{len(after)} alert(s) were raised after the last data arrived: they describe the outage, "
                            "not the airports.")
    return warnings


# -- 6. length --------------------------------------------------------------------------------------------------------

def above_the_fold_words(a: BriefAssessment) -> int:
    parts = [a.status_line, a.bluf, f"Confidence: {a.confidence_label} {a.confidence_reason}", *a.watch]
    for act in a.actions:
        parts += [act.action, "Owner:", act.owner, "Done when:", act.done_when]
    return sum(words(p) for p in parts)


def fit_above_the_fold(a: BriefAssessment) -> BriefAssessment:
    """A backstop: the text is written to fit, but if a long airport list or count pushes it over, trim least important first."""
    a.above_fold_words = above_the_fold_words(a)
    while a.above_fold_words > ABOVE_FOLD_MAX_WORDS and a.watch:
        a.watch.pop()
        a.above_fold_words = above_the_fold_words(a)
    if a.above_fold_words > ABOVE_FOLD_MAX_WORDS:
        a.confidence_reason = ""
        a.above_fold_words = above_the_fold_words(a)
    sentences = re.split(r"(?<=[.!?])\s+", a.bluf)
    while a.above_fold_words > ABOVE_FOLD_MAX_WORDS and len(sentences) > 1:
        sentences.pop()
        a.bluf = " ".join(sentences)
        a.above_fold_words = above_the_fold_words(a)
    return a


# -- a model's bottom line, accepted only if it fits the frame ----------------------------------------------------------

_ALL_CLEAR = re.compile(
    r"\ball clear\b|\ball (?:normal|good|well|ok(?:ay)?)\b|\bnormal operations\b|\beverything is (?:normal|fine|ok)\b"
    r"|\bno (?:issues?|problems?|concerns?|alerts?|anomal\w+|action(?: is)?(?: needed| required)?)\b"
    r"|\bnothing (?:to report|needs|is open|open)\b",
    re.IGNORECASE)


def accept_model_bluf(a: BriefAssessment, text: str) -> tuple[str, str | None]:
    """
    (bottom line to use, reason the model's was refused). The status, counts and actions are not the
    model's to change; its sentence replaces the rule-written one only when the data is not down, it fits,
    and it says nothing the console cannot know.
    """
    text = " ".join((text or "").split())
    if a.data_health.state in ("DOWN", "UNKNOWN"):
        return a.bluf, "the pipeline state decides the bottom line, not a model"
    if not text:
        return a.bluf, "the model gave no bottom line"
    if words(text) > MODEL_BLUF_MAX_WORDS:
        return a.bluf, f"longer than {MODEL_BLUF_MAX_WORDS} words"
    lowered = f" {text.lower()} "
    claim = overreach(text)
    if claim:
        return a.bluf, f"claims something about {claim} that this console cannot know"
    if a.data_health.state == "DEGRADED" and not re.search(r"\b(data|feed|pipeline|stale|backlog)\b", lowered):
        return a.bluf, "does not say the data is degraded"
    if a.status != "GREEN" and _ALL_CLEAR.search(text):
        # The status is the rules', and it is not green. A headline that says the opposite would sit above it.
        return a.bluf, f"says all is well while the status is {a.status}"
    return text, None
