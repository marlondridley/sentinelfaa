"""
Which airports and TRACONs are in scope is configuration (SUPPORTED_AIRPORTS, SUPPORTED_TRACON),
not code. The default is the four Potomac TRACON airports the system was verified on; widening it to
another airport or TRACON must not need an edit anywhere but the environment.

No second-TRACON feed has ever been captured here. The "widened" ingest tests below rewrite the airport
and TRACON of a real KBWI capture, so they prove the scope is read from config, not that a KJFK feed
parses: what a real one looks like is not known until one is captured.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import airports
from src.config import settings
from src.models import NASEvent
from src.swim_file_ingest import (
    _derive_flight_phase,
    _jms_headers_only_record,
    _normalise_icao,
    _normalise_record,
    _parse_file,
    _primary_airport_from,
)
from src.validators import validate_event

APDS = Path(__file__).parent / "fixtures" / "swim_kit" / "apds_sample"     # KBWI, tracon PCT
DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"


def _event(airport: str, tracon: str) -> NASEvent:
    now = datetime.now(timezone.utc)
    return NASEvent(
        airport_code=airport, tracon=tracon, service_name="SMES", message_type="SURFACE_MOVEMENT",
        event_time_utc=now - timedelta(seconds=1), received_time_utc=now, latency_ms=1000.0, track_id="TRK1",
    )


@pytest.fixture
def new_york(monkeypatch):
    """A deployment pointed at New York TRACON instead: nothing but config changed."""
    monkeypatch.setattr(settings, "SUPPORTED_AIRPORTS", {"KJFK", "KLGA", "KEWR"})
    monkeypatch.setattr(settings, "SUPPORTED_TRACON", "N90")


# ── the default is unchanged ──────────────────────────────────────────────────

def test_the_default_scope_is_the_four_pct_airports():
    assert settings.SUPPORTED_AIRPORTS == {"KDCA", "KIAD", "KBWI", "KADW"}
    assert settings.SUPPORTED_TRACON == "PCT" and settings.SUPPORTED_TRACONS == {"PCT"}


def test_the_pct_airports_are_all_in_the_registry_under_pct():
    assert {airports.tracon_of(a) for a in settings.SUPPORTED_AIRPORTS} == {"PCT"}


@pytest.mark.parametrize("airport, tracon", [("KJFK", "PCT"), ("KDCA", "N90"), ("KDCA", "ZZZ")])
def test_out_of_scope_events_are_refused_where_they_are_built(airport, tracon):
    with pytest.raises(ValueError, match="not one of the supported"):
        _event(airport, tracon)


# ── widening the scope is configuration ───────────────────────────────────────

def test_a_deployment_pointed_at_another_tracon_accepts_its_airports(new_york):
    event = _event("KJFK", "N90")
    assert validate_event(event).passed


def test_and_refuses_the_old_ones(new_york):
    with pytest.raises(ValueError, match="not one of the supported"):
        _event("KDCA", "PCT")


def test_the_tracons_in_scope_follow_the_airports(monkeypatch):
    monkeypatch.setattr(settings, "SUPPORTED_AIRPORTS", {"KJFK", "KATL"})
    monkeypatch.setattr(settings, "SUPPORTED_TRACON", "N90")
    assert settings.SUPPORTED_TRACONS == {"N90", "A80"}
    assert validate_event(_event("KATL", "A80")).passed       # an airport of the second TRACON, from its own feed


def test_an_airport_the_registry_does_not_know_still_works_under_the_deployments_tracon(monkeypatch):
    monkeypatch.setattr(settings, "SUPPORTED_AIRPORTS", {"KXYZ"})
    assert airports.get("KXYZ") is None and settings.SUPPORTED_TRACONS == {"PCT"}
    assert validate_event(_event("KXYZ", "PCT")).passed


def test_validation_says_what_is_in_scope(new_york):
    event = _event("KJFK", "N90").model_copy(update={"airport_code": "KBWI"})   # copied unvalidated: a mutated event
    result = validate_event(event)
    assert not result.passed
    assert any("KBWI" in e and "KEWR" in e for e in result.errors)     # names the airport, and what would have been fine


# ── ingest reads the same scope ───────────────────────────────────────────────

def test_the_extractors_keep_only_airports_in_scope():
    assert _primary_airport_from("HGR", "KBWI") == "KBWI"           # any candidate may be the in-scope one
    assert _primary_airport_from("HGR", "KJFK") is None


def test_the_extractors_follow_the_scope_when_it_is_widened(new_york):
    assert _primary_airport_from("HGR", "KJFK") == "KJFK"
    assert _primary_airport_from("KBWI") is None


def test_a_real_message_for_an_airport_out_of_scope_yields_nothing_and_is_not_an_error(new_york):
    records, error = _parse_file(APDS)      # a real KBWI RVR message, read by a deployment that is not PCT

    assert records == [] and error is None


def test_the_same_message_read_for_the_new_scope_is_extracted_and_valid(new_york, tmp_path):
    """A KBWI capture with its airport and TRACON rewritten. It shows scope is config, not that a KJFK feed parses."""
    text = APDS.read_text(encoding="utf-8-sig").replace("KBWI", "KJFK").replace("value = PCT", "value = N90")
    derived = tmp_path / "1787863340481"
    derived.write_text(text, encoding="utf-8")

    records, error = _parse_file(derived)

    assert error is None and records
    for raw in records:
        event, why = _normalise_record(raw, str(derived))
        assert event is not None, why
        assert (event.airport_code, event.tracon) == ("KJFK", "N90")
        assert validate_event(event).passed


def test_a_record_that_names_no_tracon_gets_the_deployments(new_york):
    assert _jms_headers_only_record({"mex": "TDES"}, Path("x"))["tracon"] == "N90"
    assert _jms_headers_only_record({"mex": "TDES", "tracon": "A80"}, Path("x"))["tracon"] == "A80"


def test_no_airport_named_reads_as_the_deployments_tracon(new_york):
    assert _normalise_icao(None) == "N90" and _normalise_icao("jfk") == "KJFK"


def test_a_flight_plan_that_names_the_tracon_is_read_against_the_scope(new_york):
    # The flight plan's departure is the TRACON itself when no airport was filed.
    assert _derive_flight_phase(3000, 800, "KJFK", "N90", "KBOS") == "DEPARTURE"
    assert _derive_flight_phase(3000, 800, "KJFK", "PCT", "KBOS") == "DEPARTURE"      # still climbing fast: the >500 fpm rule
    assert _derive_flight_phase(3000, 350, "KJFK", "PCT", "KBOS") == "ENROUTE"        # PCT is nobody's here


# ── the registry and the dashboard ────────────────────────────────────────────

def test_registry_rows_are_consistent():
    for code, airport in airports.REGISTRY.items():
        assert code == airport.code and len(code) == 4 and code.startswith("K")
        assert -90 <= airport.lat <= 90 and -180 <= airport.lon <= 180
        assert airport.tracon and airport.name


@pytest.fixture
def dashboard_data(monkeypatch):
    pytest.importorskip("streamlit", reason="the dashboard package imports it")
    monkeypatch.syspath_prepend(str(DASHBOARD))
    sys.modules.pop("lib.data", None)
    from lib import data
    yield data
    sys.modules.pop("lib.data", None)


def test_the_dashboard_lists_the_pct_airports_in_their_familiar_order(dashboard_data):
    assert list(dashboard_data.AIRPORTS) == ["KDCA", "KIAD", "KBWI", "KADW"]
    assert dashboard_data.AIRPORTS["KDCA"] == {"name": "Reagan National", "lat": 38.8521, "lon": -77.0377}


def test_the_dashboard_follows_the_scope_and_has_no_marker_for_an_unknown_airport(dashboard_data, monkeypatch):
    monkeypatch.setattr(settings, "SUPPORTED_AIRPORTS", {"KJFK", "KXYZ"})

    table = dashboard_data._in_scope_airports()

    assert list(table) == ["KJFK", "KXYZ"]
    assert table["KJFK"]["lat"] == pytest.approx(40.6398)
    assert table["KXYZ"] == {"name": "KXYZ", "lat": None, "lon": None}
