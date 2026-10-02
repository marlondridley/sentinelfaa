"""
The context router's summaries (src/context_router.py): how events are rolled up by service, how a feed
status becomes a service status, what an airport snapshot says, and the status context the Watch Officer reads.
Storage is replaced.
"""
from __future__ import annotations

import pytest

from src import context_router as cr


# ── Rolling events up by service ─────────────────────────────────────────────────────────────────────

def test_events_are_summarised_per_service_with_latency_only_where_it_was_measured():
    summary = cr._summarise_events([
        {"service_name": "ISMC", "latency_ms": 100.0, "feed_status": "ok", "event_id": "a"},
        {"service_name": "ISMC", "latency_ms": 300.0, "feed_status": "degraded", "event_id": "b"},
        {"service_name": "SMES", "latency_ms": None, "feed_status": "", "id": "row-7"},
        {"latency_ms": 5.0},                                    # no service name
    ])

    assert summary["ISMC"] == {"count": 2, "feed_statuses": ["degraded", "ok"], "sample_event_ids": ["a", "b"],
                               "avg_latency_ms": 200.0, "max_latency_ms": 300.0}
    assert summary["SMES"] == {"count": 1, "feed_statuses": [], "sample_event_ids": ["row-7"]}
    assert summary["unknown"]["count"] == 1 and summary["unknown"]["sample_event_ids"] == ["UNKNOWN"]


def test_only_the_first_five_event_ids_are_kept_as_samples():
    summary = cr._summarise_events([{"service_name": "APDS", "event_id": f"e{i}"} for i in range(9)])

    assert summary["APDS"]["count"] == 9
    assert summary["APDS"]["sample_event_ids"] == ["e0", "e1", "e2", "e3", "e4"]


# ── Statuses ─────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("statuses, worst", [
    ([], "OK"),
    (["OK", "STALE"], "STALE"),
    (["degraded", "UNAVAILABLE", "OK"], "UNAVAILABLE"),
    (["OK", "DISCONNECTED", "STALE"], "DISCONNECTED"),
    (["something-new"], "something-new"),
])
def test_the_worst_status_wins(statuses, worst):
    assert cr._worst_status(statuses) == worst


@pytest.mark.parametrize("feed_statuses, status", [
    (["Disconnected", "degraded"], "DISCONNECTED"),
    (["unavailable", "degraded"], "UNAVAILABLE"),
    (["DEGRADED"], "DEGRADED"),
    (["ok", "connected"], "OK"),
    ([], "OK"),
])
def test_a_services_status_is_its_worst_reported_feed_status(feed_statuses, status):
    assert cr._service_status_from_feed_statuses(feed_statuses) == status


# ── Snapshots ────────────────────────────────────────────────────────────────────────────────────────

def test_rvr_rows_are_indexed_by_airport_and_runway():
    index = cr._index_rvr_by_airport([
        {"airport_code": "KDCA", "runway": "01", "rvr_touchdown": 800, "rvr_midpoint": 900, "rvr_rollout": 1000,
         "event_time_utc": "t1"},
        {"airport_code": "KDCA", "runway": None, "rvr_touchdown": 1500},
        {"airport_code": "KIAD", "runway": "19L", "rvr_touchdown": 2000},
    ])

    assert index["KDCA"]["01"] == {"touchdown_ft": 800, "midpoint_ft": 900, "rollout_ft": 1000, "time_utc": "t1"}
    assert index["KDCA"]["unknown"]["touchdown_ft"] == 1500
    assert set(index) == {"KDCA", "KIAD"}


def test_an_airport_with_no_events_says_there_is_no_data():
    snapshot = cr._build_airport_snapshot("KDCA", [], {}, [], {})

    assert snapshot["overall_status"] == "NO DATA"
    assert snapshot["event_count_30m"] == 0 and snapshot["services"] == {}
    assert snapshot["flight_phases"] == {"DEPARTURE": 0, "ARRIVAL": 0, "ENROUTE": 0, "GROUND": 0, "UNKNOWN": 0}


def test_an_airport_snapshot_carries_service_status_rvr_and_a_few_flights():
    flights = [{"aircraft_id": f"AAL{i}", "flight_phase": "ARRIVAL", "altitude_ft": 3000, "runway": "01"}
               for i in range(12)] + [{}]
    snapshot = cr._build_airport_snapshot(
        "KDCA",
        [{"service_name": "ISMC", "feed_status": "degraded", "event_id": "e1", "latency_ms": 10.0}],
        {"ARRIVAL": 4, "GROUND": 1},
        flights,
        {"01": {"touchdown_ft": 800}},
    )

    assert snapshot["overall_status"] == "DEGRADED"
    assert snapshot["services"]["ISMC"]["status"] == "DEGRADED"
    assert snapshot["services"]["ISMC"]["sample_event_ids"] == ["e1"]
    assert snapshot["rvr"] == {"01": {"touchdown_ft": 800}}
    assert snapshot["active_aircraft_count"] == 13
    assert snapshot["flight_phases"]["ARRIVAL"] == 4 and snapshot["flight_phases"]["GROUND"] == 1
    assert len(snapshot["sample_flights"]) == 8
    assert snapshot["sample_flights"][0] == "AAL0 (ARRIVAL, alt=3000ft, rwy=01)"


def test_a_flight_with_no_details_is_still_listed():
    snapshot = cr._build_airport_snapshot("KDCA", [], {}, [{}], {})
    assert snapshot["sample_flights"] == ["UNKNOWN (?, alt=Noneft, rwy=-)"]


def test_alerts_are_enriched_with_the_critics_verdict_where_there_is_one():
    alerts = [
        {"alert_id": "a1", "alert_type": "LOW_RVR", "severity": "CRITICAL", "airport_code": "KDCA",
         "service_name": "APDS", "description": "d", "triggered_at": "t"},
        {"alert_id": "a2", "alert_type": "STALE", "severity": "WARNING", "airport_code": "KIAD",
         "service_name": "SMES", "description": "d", "triggered_at": "t"},
    ]
    verdicts = {"a1": {"confidence": 0.8, "pattern": "p", "escalate": "true", "reasoning": "r"}}

    first, second = cr._enrich_alerts_with_critic(alerts, verdicts)

    assert first["critic"] == {"confidence": 0.8, "pattern": "p", "escalate": True, "reasoning": "r"}
    assert "critic" not in second


# ── The status context ───────────────────────────────────────────────────────────────────────────────

async def test_the_status_context_gathers_everything_the_watch_officer_reads(monkeypatch):
    import src.config as config

    async def _events(**kw):
        return [{"service_name": "ISMC", "airport_code": "KDCA", "feed_status": "ok", "event_id": "e1"}]

    async def _alerts(**kw):
        return [{"alert_id": "a1", "alert_type": "T", "severity": "CRITICAL", "airport_code": "KDCA",
                 "service_name": "ISMC", "description": "d", "triggered_at": "t"},
                {"alert_id": "a2", "alert_type": "T", "severity": "WARNING", "airport_code": "KIAD",
                 "service_name": "ISMC", "description": "d", "triggered_at": "t"}]

    async def _critic(**kw):
        return [{"alert_id": "a1", "confidence": 0.9, "pattern": "p", "escalate": True, "reasoning": "r"}]

    async def _rvr():
        return [{"airport_code": "KDCA", "runway": "01", "rvr_touchdown": 800}]

    async def _phases(**kw):
        return {"KDCA": {"ARRIVAL": 2}}

    async def _aircraft(**kw):
        return [{"airport_code": "KDCA", "aircraft_id": "AAL1"}, {"airport_code": None, "aircraft_id": "AAL2"}]

    for name, fn in [("get_events_for_critic", _events), ("get_alerts_for_critic", _alerts),
                     ("get_recent_critic_results", _critic), ("get_latest_rvr", _rvr),
                     ("get_flight_phase_counts", _phases), ("get_active_aircraft", _aircraft)]:
        monkeypatch.setattr(cr.storage, name, fn)

    context = await cr.ContextRouter().build_status_context()

    assert set(context["airports"]) == set(config.SUPPORTED_AIRPORTS)
    assert context["airports"]["KDCA"]["active_aircraft_count"] == 1, "an aircraft with no airport is not placed"
    assert context["summary"] == {"total_events_30m": 1, "critical_alerts": 1, "warning_alerts": 1,
                                  "escalation_required": True}
    assert context["active_alerts"][0]["critic"]["escalate"] is True
