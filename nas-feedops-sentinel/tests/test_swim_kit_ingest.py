"""
Contract tests for the FAA SWIM Jumpstart Kit -> ingest path.

This is the seam the whole live pipeline depends on and, until these tests,
it had no coverage at all. The kit is not containerized (see the jumpstart
comment in docker-compose.yml); it runs natively via jumpstart/run-live.ps1
and writes one file per JMS message into data/raw/swim_live/log/.

The contract, verified against the vendor's own source rather than assumed:

  * jumpstart/src/.../solace/SolaceConsumerV1.java:83 builds header lines as
        "Property name = " + propName + "; value = " + value + "\n"
    which is exactly what _JMS_PROPERTY_RE in src/swim_file_ingest.py matches.
    Running the kit WITHOUT -Dheaders=true drops these, and with them the
    mex/tracon/msgType routing information.

  * MessageFileOutput names files by epoch millisecond with no extension,
    which is why SCAN_EXTENSIONS includes "".

  * -Djson=true makes Output.convert() run XML.toJSONObject(), producing the
    single JSON object per file that _parse_jms_message_file expects.

Break any of those three flags and every message lands in data/failed/.
"""
from __future__ import annotations

import json
import pytest
from pathlib import Path

from src import config
from src.swim_file_ingest import (
    SCAN_EXTENSIONS,
    _JMS_PROPERTY_RE,
    _normalise_record,
    _parse_file,
    _service_from_mex,
)

_HEADERS = (
    "Property name = mex; value = {mex}\n"
    "Property name = tracon; value = PCT\n"
    "Property name = msgType; value = {msg_type}\n"
)


def _kit_file(tmp_path: Path, name: str, mex: str, msg_type: str, body) -> Path:
    """Write a file in exactly the shape MessageFileOutput produces."""
    path = tmp_path / name  # epoch-millis style, deliberately no extension
    text = _HEADERS.format(mex=mex, msg_type=msg_type)
    if body is not None:
        text += json.dumps(body)
    path.write_text(text, encoding="utf-8")
    return path


def test_scanner_accepts_extensionless_files():
    """MessageFileOutput writes no extension; the scanner must still see them."""
    assert "" in SCAN_EXTENSIONS


def test_header_regex_matches_the_kits_exact_output_format():
    """
    Guards the literal string built in SolaceConsumerV1/V2.java:83. If the
    vendor changes that format, this fails loudly instead of every message
    silently losing its routing headers.
    """
    match = _JMS_PROPERTY_RE.match("Property name = mex; value = TAIS")
    assert match is not None
    assert match.group("key") == "mex"
    assert match.group("val") == "TAIS"


def test_mex_header_maps_to_supported_service_names():
    assert _service_from_mex("TAIS") == "TERMINAL_AUTO"
    assert _service_from_mex("SMES") == "SMES"
    assert _service_from_mex("APDS") == "APDS_RVR"
    assert _service_from_mex("ISMC") == "ISMC"
    assert _service_from_mex("TFDM") == "TOWER_DEPARTURE"


def test_tais_message_in_pct_scope_yields_records(tmp_path: Path):
    path = _kit_file(
        tmp_path, "1789928000001", "TAIS", "track",
        {"ns2:TATrackAndFlightPlan": {"record": [
            {"flightPlan": {"airport": "KDCA", "acid": "AAL123"},
             "track": {"altitude": 3000}},
        ]}},
    )
    records, error = _parse_file(path)
    assert error is None
    assert len(records) == 1


def test_tais_message_outside_pct_scope_is_empty_but_not_an_error(tmp_path: Path):
    """
    Out-of-area traffic is a legitimate zero-record outcome, not a failure.
    Treating it as one would flood data/failed/ with normal traffic.
    """
    path = _kit_file(
        tmp_path, "1789928000002", "TAIS", "track",
        {"ns2:TATrackAndFlightPlan": {"record": [
            {"flightPlan": {"airport": "KLAX", "acid": "UAL9"},
             "track": {"altitude": 30000}},
        ]}},
    )
    records, error = _parse_file(path)
    assert error is None
    assert records == []


def test_unrecognised_payload_shape_is_surfaced_not_silently_dropped(tmp_path: Path):
    """
    Regression test for silent data loss.

    _extract_records understands the TAIS TATrackAndFlightPlan shape only.
    A different STDDS feed parses as valid JSON, matches nothing, and used to
    produce (records=[], error=None) -- which took neither failure branch in
    scan_swim_folder(), so the file was moved to data/processed/ and counted
    as a successful ingest with event_count=0. An entire feed could be
    dropped while the ingest stats and dashboard both showed healthy.

    It must now report an error, and that error must name what actually
    arrived so an extractor can be written for it.
    """
    path = _kit_file(
        tmp_path, "1789928000003", "SMES", "surfaceMovement",
        {"surfaceMovementEvent": {"airport": "KDCA", "gate": "A12"}},
    )
    records, error = _parse_file(path)

    assert records == []
    assert error is not None, "an unreadable feed must not be counted as processed"
    assert "SMES" in error                      # which feed
    assert "surfaceMovement" in error           # which message type
    assert "surfaceMovementEvent" in error      # the actual payload shape


def test_headers_only_message_still_yields_a_metadata_record(tmp_path: Path):
    """A message with routing headers but no JSON body is not a failure."""
    path = _kit_file(tmp_path, "1789928000004", "ISMC", "linkStatus", None)
    records, error = _parse_file(path)

    assert error is None
    assert len(records) == 1
    assert records[0]["service_name"] == "ISMC"
    assert records[0]["message_type"] == "linkStatus"


# ---------------------------------------------------------------------------
# Real captured FAA STDDS messages
#
# tests/fixtures/swim_kit/ holds four real messages captured from the live
# subscription (one per feed), copied out of jumpstart-latest/log/ which is
# gitignored. They are the only real FAA data in the repo and the only honest
# basis for building extractors, so they are version-controlled here.
# Checked for credential-like content before being tracked.
#
# These tests assert the SAFETY NET behaves correctly on real data -- that an
# unreadable feed is surfaced with a usable diagnostic. They deliberately do
# NOT assert that these feeds produce events, because right now three of the
# four have no extractor at all. See _extract_records().
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "swim_kit"


def test_real_tais_message_parses_without_error():
    """
    Recognised feed, no PCT-primary traffic in this particular message.

    Every track in this capture is either unassociated with an airport or
    bound for somewhere outside the four PCT primaries (one is KHGR->KFTY).
    NASEvent.airport_code is a Literal of those four, so these genuinely
    cannot become events -- and that must NOT be treated as a failure.
    """
    records, error = _parse_file(FIXTURES / "tais_sample")
    assert error is None
    assert records == []


def test_real_apds_message_yields_one_event_per_runway():
    """
    ns2:RVRDataUpdateMessage -> six runway ends at KBWI, each with its own
    touchdown/midpoint/rollout reading.
    """
    records, error = _parse_file(FIXTURES / "apds_sample")
    assert error is None
    assert len(records) == 6

    assert {r["airport_code"] for r in records} == {"KBWI"}
    assert {r["service_name"] for r in records} == {"APDS_RVR"}
    # Runway IDs are numericRunwayID zero-padded plus the L/R sub-ID.
    assert {r["runway"] for r in records} == {"10", "28", "15L", "33R", "15R", "33L"}


def test_real_apds_rvr_scales_to_feet_and_does_not_false_alarm():
    """
    The capture reads 60 on every runway end. Under APDS_RVR_SCALE_FT=100
    that is 6000 ft -- the standard maximum reportable RVR, i.e. a clear day.

    The assertion that matters operationally is the second one: routine
    clear-weather traffic must sit ABOVE RVR_LOW_THRESHOLD_FT. If the scale
    factor were wrong in the other direction every message would read 60 ft
    and ALERT_LOW_RVR would fire continuously, which destroys the value of a
    safety alert just as surely as never firing at all.

    See the APDS_RVR_SCALE_FT comment in src/config.py: KBWI's ASOS reported
    10 SM visibility around this capture, which rules out reading 60 as feet.
    """
    records, _ = _parse_file(FIXTURES / "apds_sample")
    touchdowns = [r["rvr_touchdown"] for r in records]

    assert all(v == 6000 for v in touchdowns)
    assert all(v > config.RVR_LOW_THRESHOLD_FT for v in touchdowns)


def test_real_apds_missing_reading_becomes_none_not_zero(tmp_path: Path):
    """
    STDDS sends "" for an unavailable reading. It must become None, never 0 --
    a zero would read as RVR 0 ft and trip ALERT_LOW_RVR on a runway that
    simply is not reporting.
    """
    records, _ = _parse_file(FIXTURES / "apds_sample")
    by_runway = {r["runway"]: r for r in records}

    # 15L/33R report touchdown and rollout but send "" for midpoint.
    assert by_runway["15L"]["rvr_midpoint"] is None
    assert by_runway["15L"]["rvr_touchdown"] == 6000


def test_implausible_rvr_refuses_the_message_instead_of_misreading_it(tmp_path: Path):
    """
    If the wire value were already in feet, a real 1800 ft reading would scale
    to 180,000 ft -- far above the low-RVR threshold, so it would never alert.
    A physically impossible RVR must refuse the message and name the scale
    factor, not be stored as a reading.
    """
    body = {"ns2:RVRDataUpdateMessage": {
        "airport": "KBWI",
        "runwayData": [{
            "runwayID": {"numericRunwayID": 15, "runwaySubID": "L"},
            "touchdownVisualRange": 1800,
        }],
    }}
    records, error = _parse_file(_kit_file(tmp_path, "1787863344881", "APDS", "RR", body))

    assert records == []
    assert error is not None
    assert "APDS_RVR_SCALE_FT" in error


def test_real_tdes_message_yields_a_departure_event():
    """
    ns5:TowerDepartureEventMessage -> one TOWER_DEPARTURE event at the
    departure airport.
    """
    records, error = _parse_file(FIXTURES / "tdes_sample")
    assert error is None
    assert len(records) == 1

    rec = records[0]
    assert rec["airport_code"] == "KDCA"
    # Regression: mex=TDES was absent from _service_from_mex and fell through
    # to the TERMINAL_AUTO default, mislabelling every departure clearance.
    assert rec["service_name"] == "TOWER_DEPARTURE"
    assert rec["aircraft_id"] == "JIA5567"
    assert rec["flight_phase"] == "DEPARTURE"


def test_real_messages_become_valid_nasevents():
    """
    End-to-end: real captured message -> parsed record -> validated NASEvent.

    This is the assertion that would have caught the Zulu-timestamp bug.
    STDDS sends 2026-08-27T20:42:16.000Z, and datetime.fromisoformat() only
    accepts a trailing Z from Python 3.11. On 3.10 every timestamped message
    was rejected with "invalid event_time_utc" -- total silent feed loss on
    any interpreter older than the container's.
    """
    for fixture in ("apds_sample", "tdes_sample", "smes_sample"):
        records, error = _parse_file(FIXTURES / fixture)
        assert error is None
        assert records, f"{fixture} produced no records"

        for raw in records:
            event, err = _normalise_record(raw, str(FIXTURES / fixture))
            assert event is not None, f"{fixture}: {err}"
            assert event.event_time_utc is not None
            assert event.latency_ms is not None


def test_real_smes_message_yields_one_event_per_adsb_report():
    """ns2:asdexMsg -> one surface event per ADS-B report, each with its own time."""
    from src.validators import validate_event

    records, error = _parse_file(FIXTURES / "smes_sample")
    assert error is None
    assert len(records) == 2

    by_track = {r["track_id"]: r for r in records}
    assert set(by_track) == {"417", "890"}
    first = by_track["417"]
    assert first["airport_code"] == "KIAD"
    assert first["service_name"] == "SMES"
    assert first["message_type"] == "SMES_AD"
    assert first["lat"] == pytest.approx(38.93986)
    assert first["lon"] == pytest.approx(-77.44608)
    # Reports in one message are taken at different instants.
    assert by_track["417"]["event_time_utc"] != by_track["890"]["event_time_utc"]

    for raw in records:
        event, err = _normalise_record(raw, "smes_sample")
        assert event is not None, err
        result = validate_event(event)
        assert result.passed, result.errors


def test_smes_single_report_as_object_is_not_dropped(tmp_path: Path):
    """XML-to-JSON turns a one-element list into a bare object."""
    body = {"ns2:asdexMsg": {
        "airport": "KDCA",
        "adsbReport": {"report": {"basicReport": {
            "time": "2026-08-27T20:42:24.563Z",
            "position": {"lat": 38.85, "lon": -77.04},
            "track": 12,
        }}},
    }}
    records, error = _parse_file(_kit_file(tmp_path, "1787863344879", "SMES", "AD", body))
    assert error is None
    assert [r["track_id"] for r in records] == ["12"]


def test_smes_unverified_report_type_is_refused_not_partly_ingested(tmp_path: Path):
    """
    Only adsbReport has been seen in captured traffic. A message that also
    carries another report type must be refused whole, naming the unverified
    key, rather than ingesting the ADS-B half and dropping the rest.
    """
    body = {"ns2:asdexMsg": {
        "airport": "KIAD",
        "adsbReport": [{"report": {"basicReport": {
            "time": "2026-08-27T20:42:24.563Z", "track": 1}}}],
        "positionReport": [{"track": 2}],
    }}
    records, error = _parse_file(_kit_file(tmp_path, "1787863344880", "SMES", "AD", body))
    assert records == []
    assert error is not None
    assert "positionReport" in error
