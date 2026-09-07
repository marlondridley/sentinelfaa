"""
News Agent — assembles a Breaking-News-style ticker of operational headlines
from the current SQLite state. Rule-based (no LLM dependency) so it always
renders, even when the Critic/Planner are disabled.

Severity scheme (drives ticker color in the dashboard):
  GOOD       — green, things going well / normal flow
  NORMAL     — grey, baseline operational facts
  ATTENTION  — yellow, something worth watching but not urgent
  ACTION     — red, supervisor should act now

Items are returned as a list of dicts:
  {"text": str, "severity": str, "anchor": str}

The `anchor` is a DOM id the dashboard places on the relevant section so
clicking the headline scroll-jumps to it.
"""
from __future__ import annotations

from datetime import datetime
from typing import List

from src import config, storage


Severity = str  # one of: GOOD | NORMAL | ATTENTION | ACTION


# DOM anchors the dashboard exposes (kept in sync with dashboard/app.py)
ANCHOR_AIRPORT_HEALTH = "airport-health"
ANCHOR_AIRSPACE_OPS   = "airspace-ops"
ANCHOR_ALERT_LOG      = "alert-log"
ANCHOR_RVR            = "rvr-section"
ANCHOR_CRITIC         = "critic-intel"
ANCHOR_LATENCY        = "latency-section"
ANCHOR_HITL           = "hitl-queue"
ANCHOR_TOP            = "page-top"
ANCHOR_WATCH_OFFICER  = "watch-officer"


def _hitl_items() -> List[dict]:
    """Surface the HITL pending queue depth (or its absence)."""
    pending = storage.get_pending_staged_actions()
    if pending:
        return [{
            "text": f"{len(pending)} staged action(s) pending human review",
            "severity": "ACTION",
            "anchor": ANCHOR_HITL,
        }]
    return [{
        "text": "No staged critical actions pending",
        "severity": "GOOD",
        "anchor": ANCHOR_HITL,
    }]


def _airport_status_items(airport_status: dict[str, str]) -> List[dict]:
    """Per-airport feed health headlines."""
    items: List[dict] = []
    for ap in sorted(config.SUPPORTED_AIRPORTS):
        st = airport_status.get(ap, "NO DATA")
        if st == "OK":
            items.append({"text": f"{ap} feed healthy", "severity": "GOOD", "anchor": ANCHOR_AIRPORT_HEALTH})
        elif st == "STALE":
            items.append({"text": f"{ap} feed STALE — recent messages overdue", "severity": "ATTENTION", "anchor": ANCHOR_AIRPORT_HEALTH})
        elif st == "DEGRADED":
            items.append({"text": f"{ap} feed DEGRADED — investigate ISMC link", "severity": "ATTENTION", "anchor": ANCHOR_AIRPORT_HEALTH})
        elif st in ("UNAVAILABLE", "DISCONNECTED"):
            items.append({"text": f"{ap} feed {st} — escalate to SWIM service desk", "severity": "ACTION", "anchor": ANCHOR_AIRPORT_HEALTH})
        elif st == "NO DATA":
            items.append({"text": f"{ap} no data in window", "severity": "NORMAL", "anchor": ANCHOR_AIRPORT_HEALTH})
    return items


def _airspace_activity_items(phase_counts: dict, active_total: int) -> List[dict]:
    """Live airspace volume + per-airport flight phase headlines."""
    items: List[dict] = []
    if active_total > 0:
        items.append({
            "text": f"{active_total} aircraft active in PCT sector",
            "severity": "NORMAL",
            "anchor": ANCHOR_AIRSPACE_OPS,
        })
    for ap in sorted(config.SUPPORTED_AIRPORTS):
        pc = phase_counts.get(ap, {})
        dep = pc.get("DEPARTURE", 0)
        arr = pc.get("ARRIVAL", 0)
        if dep + arr == 0:
            continue
        if arr > 15 or dep > 15:
            sev = "ATTENTION"
            note = " (high flow)"
        else:
            sev = "GOOD"
            note = ""
        items.append({
            "text": f"{ap}: {dep} departing, {arr} arriving{note}",
            "severity": sev,
            "anchor": ANCHOR_AIRSPACE_OPS,
        })
    return items


def _alert_items(alerts: List[dict]) -> List[dict]:
    """Operational alert headlines, deduped by alert_type per airport."""
    items: List[dict] = []
    crit = [a for a in alerts if a.get("severity") == "CRITICAL"]
    warn = [a for a in alerts if a.get("severity") == "WARNING"]

    if crit:
        by_type: dict[str, int] = {}
        for a in crit:
            by_type[a.get("alert_type", "?")] = by_type.get(a.get("alert_type", "?"), 0) + 1
        for atype, n in by_type.items():
            label = atype.replace("ALERT_", "").replace("_", " ").title()
            items.append({
                "text": f"{n} CRITICAL · {label} — supervisor action required",
                "severity": "ACTION",
                "anchor": ANCHOR_ALERT_LOG,
            })
    elif warn:
        items.append({
            "text": f"{len(warn)} WARNING alerts open — review in alert log",
            "severity": "ATTENTION",
            "anchor": ANCHOR_ALERT_LOG,
        })
    else:
        items.append({"text": "No active alerts", "severity": "GOOD", "anchor": ANCHOR_ALERT_LOG})

    return items


def _rvr_items(rvr_rows: List[dict]) -> List[dict]:
    """RVR safety headlines — surface only sub-threshold readings."""
    items: List[dict] = []
    for row in rvr_rows:
        td = row.get("rvr_touchdown")
        if td is not None and td < config.RVR_LOW_THRESHOLD_FT:
            items.append({
                "text": (
                    f"RVR LOW at {row['airport_code']} rwy {row.get('runway','?')}: "
                    f"{td} ft touchdown — below {config.RVR_LOW_THRESHOLD_FT} ft minimum"
                ),
                "severity": "ACTION",
                "anchor": ANCHOR_RVR,
            })
    return items


def _critic_items() -> List[dict]:
    """Surface Critic escalation recommendations (AI advisory)."""
    items: List[dict] = []
    recent = storage.get_recent_critic_results(limit=10)
    escalations = [r for r in recent if r.get("escalate")]
    if not escalations:
        return items
    items.append({
        "text": (
            f"AI Critic recommends ESCALATION on {len(escalations)} alert(s) "
            "— see Critic Intelligence panel"
        ),
        "severity": "ACTION",
        "anchor": ANCHOR_CRITIC,
    })
    return items


def _latency_item(avg_latency_ms: float | None) -> List[dict]:
    """Data freshness headline."""
    if avg_latency_ms is None:
        return []
    if avg_latency_ms < 5_000:
        sev, note = "GOOD", "live"
    elif avg_latency_ms < 30_000:
        sev, note = "NORMAL", "within acceptable range"
    elif avg_latency_ms < 300_000:
        sev, note = "ATTENTION", "DEGRADED"
    else:
        return [{
            "text": f"Data freshness {avg_latency_ms/60_000:.1f}min — backlog processing, not live",
            "severity": "ATTENTION",
            "anchor": ANCHOR_LATENCY,
        }]
    return [{
        "text": f"Data freshness {avg_latency_ms/1000:.1f}s — {note}",
        "severity": sev,
        "anchor": ANCHOR_LATENCY,
    }]


def build_fast_signal_items(
    airport_status: dict[str, str],
    alerts: List[dict],
    rvr_rows: List[dict],
) -> List[dict]:
    """
    The banner's NEW responsibility: answer "What needs attention right now?"
    Strict priority — show at most a handful of items, ordered most-urgent first.

    Priority:
      1. ACTION (red): pending staged actions OR draft escalation packages
      2. ACTION (red): sub-1200ft RVR safety condition
      3. ATTENTION (yellow): stale / degraded / missing airport feeds
      4. ATTENTION (yellow): CRITICAL alerts without staging (degraded mode)
      5. GOOD / NORMAL (green / grey): all clear
    """
    items: List[dict] = []

    pending = storage.get_pending_staged_actions()
    drafts  = storage.get_draft_operational_tickets()

    # 1. Pending review queue (highest priority)
    if pending or drafts:
        n_pending = len(pending)
        n_drafts  = len(drafts)
        if n_pending and n_drafts:
            text = (
                f"{n_drafts} escalation package(s) pending human review · "
                f"{n_pending} staged action(s) PENDING"
            )
        elif n_pending:
            text = f"{n_pending} staged action(s) pending human review"
        else:
            text = f"{n_drafts} escalation package(s) drafted — open Review Queue"
        items.append({"text": text, "severity": "ACTION", "anchor": ANCHOR_HITL})

    # 2. RVR safety (always elevate)
    items.extend(_rvr_items(rvr_rows))

    # 3. Feed-health concerns — only surface when no pending items present so
    #    operator's eye isn't pulled to second-tier signals during active reviews
    if not (pending or drafts):
        bad = [
            (ap, st) for ap, st in airport_status.items()
            if st in ("STALE", "DEGRADED", "UNAVAILABLE", "DISCONNECTED")
        ]
        if bad:
            for ap, st in bad[:3]:
                items.append({
                    "text": f"{ap} TAIS picture {st.lower()} — Watch Officer brief drafted",
                    "severity": "ATTENTION",
                    "anchor": ANCHOR_WATCH_OFFICER,
                })

    # 4. CRITICAL alerts with no staging (degraded-mode flag)
    crit = [a for a in alerts if a.get("severity") == "CRITICAL"]
    if crit and not pending:
        items.append({
            "text": f"{len(crit)} CRITICAL alert(s) detected — no escalation package yet",
            "severity": "ATTENTION",
            "anchor": ANCHOR_ALERT_LOG,
        })

    # 5. All clear
    if not items:
        if airport_status:
            healthy = all(st == "OK" for st in airport_status.values())
            if healthy:
                items.append({
                    "text": "All tracked airports current — no pending human review",
                    "severity": "GOOD",
                    "anchor": ANCHOR_AIRPORT_HEALTH,
                })
            else:
                items.append({
                    "text": "PCT aircraft movement picture current — no staged actions",
                    "severity": "NORMAL",
                    "anchor": ANCHOR_TOP,
                })
        else:
            items.append({
                "text": "No data — verify Jumpstart Kit is publishing",
                "severity": "ATTENTION",
                "anchor": ANCHOR_LATENCY,
            })

    return items


def build_ticker_items(
    airport_status: dict[str, str],
    alerts: List[dict],
    rvr_rows: List[dict],
    phase_counts: dict,
    active_aircraft_total: int,
    avg_latency_ms: float | None,
) -> List[dict]:
    """
    Assemble the full ticker, ordered by severity (most urgent first).

    Returns a list of {"text": str, "severity": "GOOD" | "NORMAL" | "ATTENTION" | "ACTION"}.
    """
    # The banner is FAST SIGNAL only — it answers "What needs attention right now?"
    # We do NOT enumerate every metric here. Detailed explanation lives in the
    # Watch Officer Brief and the Operational Ticket package.
    items: List[dict] = build_fast_signal_items(
        airport_status=airport_status,
        alerts=alerts,
        rvr_rows=rvr_rows,
    )

    # Stable ordering: ACTION first, then ATTENTION, NORMAL, GOOD
    rank = {"ACTION": 0, "ATTENTION": 1, "NORMAL": 2, "GOOD": 3}
    items.sort(key=lambda x: rank.get(x["severity"], 99))

    # Prepend a timestamp so the ticker visibly updates per refresh
    ts = datetime.utcnow().strftime("%H:%M:%S UTC")
    items.insert(0, {
        "text": f"PCT TRACON BREAKING · {ts}",
        "severity": "NORMAL",
        "anchor": ANCHOR_TOP,
    })

    return items
