"""
News Agent — standard CrewAI agent factory & breaking-news operational ticker generator.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src import config, storage

Severity = str

ANCHOR_AIRPORT_HEALTH = "airport-health"
ANCHOR_AIRSPACE_OPS   = "airspace-ops"
ANCHOR_ALERT_LOG      = "alert-log"
ANCHOR_RVR            = "rvr-section"
ANCHOR_CRITIC         = "critic-intel"
ANCHOR_LATENCY        = "latency-section"
ANCHOR_HITL           = "hitl-queue"
ANCHOR_TOP            = "page-top"
ANCHOR_WATCH_OFFICER  = "watch-officer"


async def _hitl_items() -> list[dict]:
    pending = await storage.get_pending_staged_actions()
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


def _airport_status_items(airport_status: dict[str, str]) -> list[dict]:
    items: list[dict] = []
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


def _rvr_items(rvr_rows: list[dict]) -> list[dict]:
    items: list[dict] = []
    threshold = getattr(config, "RVR_LOW_THRESHOLD_FT", 1200)
    for row in rvr_rows:
        td = row.get("rvr_touchdown")
        if td is not None and td < threshold:
            items.append({
                "text": (
                    f"RVR LOW at {row.get('airport_code', '?')} rwy {row.get('runway', '?')}: "
                    f"{td} ft touchdown — below {threshold} ft minimum"
                ),
                "severity": "ACTION",
                "anchor": ANCHOR_RVR,
            })
    return items


async def _critic_items() -> list[dict]:
    items: list[dict] = []
    recent = await storage.get_recent_critic_results(limit=10)
    escalations = []
    for r in recent:
        esc = r.get("escalate")
        if isinstance(esc, str):
            esc = esc.strip().lower() in ("true", "1", "yes")
        if bool(esc):
            escalations.append(r)

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


async def build_fast_signal_items(
    airport_status: dict[str, str],
    alerts: list[dict],
    rvr_rows: list[dict],
) -> list[dict]:
    items: list[dict] = []

    pending = await storage.get_pending_staged_actions()
    drafts  = await storage.get_draft_operational_tickets()

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

    items.extend(_rvr_items(rvr_rows))

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

    crit = [a for a in alerts if a.get("severity") == "CRITICAL"]
    if crit and not pending:
        items.append({
            "text": f"{len(crit)} CRITICAL alert(s) detected — no escalation package yet",
            "severity": "ATTENTION",
            "anchor": ANCHOR_ALERT_LOG,
        })

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


async def build_ticker_items(
    airport_status: dict[str, str],
    alerts: list[dict],
    rvr_rows: list[dict],
    phase_counts: dict,
    active_aircraft_total: int,
    avg_latency_ms: float | None,
) -> list[dict]:
    items: list[dict] = await build_fast_signal_items(
        airport_status=airport_status,
        alerts=alerts,
        rvr_rows=rvr_rows,
    )

    rank = {"ACTION": 0, "ATTENTION": 1, "NORMAL": 2, "GOOD": 3}
    items.sort(key=lambda x: rank.get(x["severity"], 99))

    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    items.insert(0, {
        "text": f"PCT TRACON BREAKING · {ts}",
        "severity": "NORMAL",
        "anchor": ANCHOR_TOP,
    })

    return items