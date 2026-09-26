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
    WithheldMessage,
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


def test_rvr_zero_is_a_not_reporting_code_not_zero_visibility(tmp_path: Path):
    """
    Live, 2026-09-25: KBWI 10/28 read 0 on both ends while its other runways read
    5000-6000 ft. Read as feet, every update staged a CRITICAL ALERT_LOW_RVR.
    """
    from src.alert_engine import check_alerts

    body = {"ns2:RVRDataUpdateMessage": {"airport": "KBWI", "runwayData": [
        {"runwayID": {"numericRunwayID": 10},
         "touchdownVisualRange": 0, "midpointVisualRange": 0, "rolloutVisualRange": 0},
        {"runwayID": {"numericRunwayID": 15, "runwaySubID": "R"},
         "touchdownVisualRange": 55, "midpointVisualRange": 0, "rolloutVisualRange": 60},
        {"runwayID": {"numericRunwayID": 33, "runwaySubID": "L"}, "touchdownVisualRange": 10},
    ]}}
    records, error = _parse_file(_kit_file(tmp_path, "1790379092990", "APDS", "RR", body))

    assert error is None
    by_runway = {r["runway"]: r for r in records}
    assert "10" not in by_runway, "a runway reporting only zeros carries no reading"
    assert (by_runway["15R"]["rvr_touchdown"], by_runway["15R"]["rvr_midpoint"]) == (5500, None)
    events = [_normalise_record(r, "rvr")[0] for r in records]
    low = [a for a in check_alerts(events) if a.alert_type == "ALERT_LOW_RVR"]
    assert [a.airport_code for a in low] == ["KBWI"], "a real 1000 ft reading still alerts once"


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
    A message that also carries a report type no one has verified must be
    refused whole, naming the unverified key, rather than ingesting the known
    half and dropping the rest.
    """
    body = {"ns2:asdexMsg": {
        "airport": "KIAD",
        "adsbReport": [{"report": {"basicReport": {
            "time": "2026-08-27T20:42:24.563Z", "track": 1}}}],
        "mlatReport": [{"track": 2}],
    }}
    records, error = _parse_file(_kit_file(tmp_path, "1787863344880", "SMES", "AD", body))
    assert records == []
    assert error is not None
    assert "mlatReport" in error


def test_real_position_report_message_yields_one_event_per_track():
    """ns2:asdexMsg positionReport (msgType AT), from live KBWI traffic, 2026-09-25."""
    from src.validators import validate_event

    records, error = _parse_file(FIXTURES / "smes_position_sample")
    assert error is None
    by_track = {r["track_id"]: r for r in records}
    assert set(by_track) == {"676", "1479"}
    arriving = by_track["676"]
    assert (arriving["airport_code"], arriving["message_type"]) == ("KBWI", "SMES_AT")
    assert arriving["surface_event_type"] == "POSITION_REPORT"
    assert arriving["aircraft_id"] == "SWA4864"
    assert (arriving["lat"], arriving["lon"]) == (pytest.approx(39.05144), pytest.approx(-76.5581))
    assert arriving["event_time_utc"] == "2026-09-25T23:31:30.000Z"
    assert by_track["1479"]["aircraft_id"] is None

    for raw in records:
        event, err = _normalise_record(raw, "smes_position_sample")
        assert event is not None, err
        assert validate_event(event).passed


def test_position_reports_are_sampled_per_track_and_callsigns_unwrapped(tmp_path: Path, monkeypatch):
    """One report per track every N seconds, chosen by time so re-ingesting is identical."""
    from src import config

    def message(second: int):
        time = f"2026-09-25T23:31:{second:02d}.000Z"
        return {"ns2:asdexMsg": {"airport": "KIAD", "positionReport": [
            {"time": time, "track": 7, "position": {"latitude": 38.94, "longitude": -77.45},
             "flightId": {"aircraftId": {"content": "UAL1831", "r": 1}}},
            {"time": time, "track": 8, "position": {"latitude": 38.95, "longitude": -77.46},
             "flightId": {"aircraftId": "UNKN"}},
        ]}}

    monkeypatch.setattr(config.settings, "SMES_POSITION_SAMPLE_SECONDS", 10)
    kept, error = _parse_file(_kit_file(tmp_path, "1790379092981", "SMES", "AT", message(30)))
    assert error is None
    assert [(r["track_id"], r["aircraft_id"]) for r in kept] == [("7", "UAL1831"), ("8", None)]
    skipped, error = _parse_file(_kit_file(tmp_path, "1790379092982", "SMES", "AT", message(31)))
    assert (skipped, error) == ([], None), "a sampled-out message is normal, not a failure"

    monkeypatch.setattr(config.settings, "SMES_POSITION_SAMPLE_SECONDS", 1)
    everything, _ = _parse_file(_kit_file(tmp_path, "1790379092983", "SMES", "AT", message(31)))
    assert len(everything) == 2


def test_real_surface_event_message_yields_one_discrete_event():
    """ns2:SurfaceMovementEventMessage (msgType SE): a real KIAD takeoff, 2026-09-25."""
    from src.models import is_surface_event
    from src.validators import validate_event

    records, error = _parse_file(FIXTURES / "smes_event_sample")
    assert error is None
    [raw] = records
    assert (raw["airport_code"], raw["message_type"], raw["surface_event_type"]) == ("KIAD", "SMES_SE", "WHEELS_OFF")
    assert (raw["runway"], raw["aircraft_id"], raw["beacon_code"]) == ("12/30", "SVA038", "7056")
    assert (raw["altitude_ft"], raw["status_code"]) == (419, "status=airborne")
    assert raw["event_time_utc"] == "2026-09-25T23:09:11.665Z"

    event, err = _normalise_record(raw, "smes_event_sample")
    assert event is not None, err
    assert validate_event(event).passed
    assert is_surface_event(event), "a takeoff is a discrete surface event and counts toward spikes"


@pytest.mark.parametrize("wire, named", [
    ("on", "WHEELS_ON"), ("off", "WHEELS_OFF"), ("runwayin", "RUNWAY_ENTRY"),
    ("runwayout", "RUNWAY_EXIT"), ("spotin", "SPOT_IN"), ("spotout", "SPOT_OUT"),
    ("holdshort", "SURFACE_HOLDSHORT"),
])
def test_surface_event_vocabulary(tmp_path: Path, wire, named):
    """Seen events get plain names; an unseen one is kept verbatim rather than guessed at."""
    body = {"ns2:SurfaceMovementEventMessage": {
        "airport": "KDCA", "time": "2026-09-25T23:00:00.000Z", "track": 5, "callsign": "UNKN",
        "mode3ACode": 566, "event": wire, "status": "onsurface",
        "position": {"latitude": 38.85, "longitude": -77.04},
    }}
    [raw], error = _parse_file(_kit_file(tmp_path, "1790380000000", "SMES", "SE", body))
    assert error is None
    assert raw["surface_event_type"] == named
    assert (raw["aircraft_id"], raw["beacon_code"]) == (None, "0566")


def test_real_asdex_system_status_is_recorded_verbatim_not_as_feed_status():
    """systemStatus (msgType AY) read state=degraded all day; mapping it would mark SMES degraded forever."""
    from src.feed_health import compute_feed_health
    from src.validators import validate_event

    records, error = _parse_file(FIXTURES / "smes_status_sample")
    assert error is None
    [raw] = records
    assert raw["surface_event_type"] == "ASDEX_SYSTEM_STATUS"
    assert raw["status_code"] == "mode=operational;state=degraded"
    assert raw["feed_status"] == "operational"

    event, err = _normalise_record(raw, "smes_status_sample")
    assert event is not None, err
    assert validate_event(event).passed, "equipment status names no moving entity by design"
    assert compute_feed_health([event])["smes_counts"] == {}


# ── The ten-message live capture (2026-08-27, KBWI/KDCA/KIAD) ──────────────────
#
# Every file the Jumpstart Kit wrote during one real subscription session,
# copied verbatim. Before the TDLS and SafetyLogicHoldBar extractors existed,
# five of these ten landed in data/failed/: a 50% quarantine rate on real FAA
# data. The corpus test below is the standing proof that it is now 10-for-10.

CAPTURE = FIXTURES / "capture_2026-08-27"
_TDLS_FILE = CAPTURE / "1787863348274"
_HOLDBAR_FILE = CAPTURE / "1787863349572"


def test_real_capture_ingests_ten_for_ten(tmp_path: Path):
    """
    Every captured file parses without error, and every record becomes a
    NASEvent that passes validation. A recognised feed may legitimately yield
    no records: both TAIS messages here (1787863346003, 1787863350167) carry
    only flight plans to KFTY/KCHO/KRIC and tracks with no flight plan, so
    nothing is attributable to KDCA/KIAD/KBWI/KADW. The bar is therefore "no
    file errors", with the other eight files all producing events.
    """
    from src.validators import validate_event

    files = sorted(p for p in CAPTURE.iterdir() if p.is_file())
    assert len(files) == 10

    files_with_events = 0
    for path in files:
        records, error = _parse_file(path)
        assert error is None, f"{path.name}: {error}"
        files_with_events += bool(records)
        for raw in records:
            event, err = _normalise_record(raw, str(path))
            assert event is not None, f"{path.name}: {err}"
            result = validate_event(event)
            assert result.passed, f"{path.name}: {result.errors}"

    assert files_with_events == 8


def test_real_capture_scan_quarantines_nothing(tmp_path: Path, monkeypatch):
    """The scanner itself, not just the parser: all ten files end up in processed/, none in failed/."""
    import shutil

    from src import swim_file_ingest

    src_dir, processed, failed = tmp_path / "in", tmp_path / "processed", tmp_path / "failed"
    src_dir.mkdir()
    for path in CAPTURE.iterdir():
        shutil.copy(path, src_dir / path.name)

    # Isolate from the ingest_log table: nothing has been seen before, and
    # log writes go nowhere.
    monkeypatch.setattr(swim_file_ingest, "_get_already_processed", lambda names: set())
    monkeypatch.setattr(swim_file_ingest, "_append_log_entries", lambda entries: None)

    events, stats = swim_file_ingest.scan_swim_folder(
        src_dir, processed_dir=processed, failed_dir=failed
    )

    assert stats.files_failed == 0
    assert list(failed.iterdir()) == []
    assert len(list(processed.iterdir())) == 10
    assert events


def test_real_tdls_dcl_message_yields_a_departure_clearance_event():
    """ns4:TDLSCSPMessage (CPDLC departure clearance) -> one TOWER_DEPARTURE event."""
    records, error = _parse_file(_TDLS_FILE)
    assert error is None
    assert len(records) == 1

    rec = records[0]
    assert rec["airport_code"] == "KBWI"
    assert rec["service_name"] == "TOWER_DEPARTURE"
    assert rec["aircraft_id"] == "SWA1085"
    assert rec["beacon_code"] == "2172"
    assert rec["flight_phase"] == "DEPARTURE"
    assert rec["surface_event_type"] == "DEPARTURE_CLEARANCE_DCL"
    # "time": "08272026204225" is MMDDYYYYHHMMSS; it must agree with the
    # message's own JMS timestamp header (2026-08-27T20:42:25.503Z).
    assert rec["event_time_utc"] == "2026-08-27T20:42:25Z"
    # The free-text clearance (full route, registration) is deliberately not kept.
    assert not any("KBWI*.TERPZ8" in str(v) for v in rec.values())


def test_tdls_unparseable_time_falls_back_to_the_header_timestamp(tmp_path: Path):
    path = _kit_file(tmp_path, "1787863348999", "TDES", "RD", {
        "ns4:TDLSCSPMessage": {"airportID": "KDCA", "aircraftID": "AAL1", "time": "garbage"},
    })
    path.write_text(
        "Property name = timestamp; value = 2026-08-27T20:42:25.503Z\n" + path.read_text(),
        encoding="utf-8",
    )
    records, error = _parse_file(path)
    assert error is None
    assert records[0]["event_time_utc"] == "2026-08-27T20:42:25.503Z"


def test_real_holdbar_message_is_a_valid_facility_status_event():
    """
    ns2:SafetyLogicHoldBar names no aircraft, vehicle or track. It is ingested
    as an airport-level SMES status event, exempt from the moving-entity rule,
    with its undecoded bitmask preserved.
    """
    from src.validators import validate_event

    records, error = _parse_file(_HOLDBAR_FILE)
    assert error is None
    assert len(records) == 1

    rec = records[0]
    assert rec["airport_code"] == "KIAD"
    assert rec["service_name"] == "SMES"
    assert rec["surface_event_type"] == "SAFETY_LOGIC_HOLD_BAR"
    assert rec["status_code"].startswith("control=1;status=0000")

    event, err = _normalise_record(rec, str(_HOLDBAR_FILE))
    assert event is not None, err
    assert validate_event(event).passed


def test_smes_movement_event_without_an_entity_still_fails_validation():
    """The hold-bar exemption must not weaken Rule 8 for real movement events."""
    from datetime import datetime

    from src.models import NASEvent
    from src.validators import validate_event

    now = datetime(2026, 8, 27, 20, 42)
    event = NASEvent(
        airport_code="KIAD", tracon="PCT", service_name="SMES", message_type="SMES_AD",
        event_time_utc=now, received_time_utc=now, latency_ms=0.0,
        surface_event_type="ADSB_REPORT",
    )
    assert not validate_event(event).passed


def test_holdbar_status_does_not_count_toward_a_surface_spike():
    """A burst of equipment-status messages is not a traffic spike."""
    from datetime import datetime, timezone

    from src import config
    from src.alert_engine import check_alerts
    from src.models import NASEvent

    now = datetime.now(timezone.utc)
    burst = [
        NASEvent(
            airport_code="KIAD", tracon="PCT", service_name="SMES", message_type="SMES_SH",
            event_time_utc=now, received_time_utc=now, latency_ms=0.0,
            surface_event_type="SAFETY_LOGIC_HOLD_BAR", feed_status="operational",
        )
        for _ in range(config.SURFACE_SPIKE_COUNT * 2)
    ]
    assert not [a for a in check_alerts(burst) if a.alert_type == "ALERT_SURFACE_EVENT_SPIKE"]


@pytest.mark.parametrize("surface_event_type, spikes", [("POSITION_REPORT", False), ("ADSB_REPORT", False), ("TAKEOFF", True)])
def test_only_discrete_surface_events_count_toward_a_spike(surface_event_type, spikes):
    """ASDE-X sends a position update per target per second; that is routine traffic, not a spike."""
    from datetime import datetime, timezone

    from src import config
    from src.alert_engine import check_alerts
    from src.models import NASEvent

    now = datetime.now(timezone.utc)
    burst = [
        NASEvent(
            airport_code="KIAD", tracon="PCT", service_name="SMES", message_type="SMES_AT",
            event_time_utc=now, received_time_utc=now, latency_ms=0.0, track_id=str(n),
            surface_event_type=surface_event_type, feed_status="operational",
        )
        for n in range(config.SURFACE_SPIKE_COUNT * 2)
    ]
    found = [a for a in check_alerts(burst) if a.alert_type == "ALERT_SURFACE_EVENT_SPIKE"]
    assert bool(found) is spikes


def test_polling_watcher_ingests_a_new_file_within_seconds(tmp_path: Path, monkeypatch):
    """
    With SWIM_WATCH_POLLING (on in compose), a file the kit writes is picked up
    in seconds, not at the next 5-minute reconcile. Native file events do not
    cross a Windows/macOS host bind mount, which is how the live watcher was
    waiting up to five minutes for every message.
    """
    import shutil
    import threading
    import time

    from src import swim_file_ingest

    monkeypatch.setattr(config.settings, "SWIM_WATCH_POLLING", True)
    monkeypatch.setattr(config.settings, "SWIM_WATCH_POLL_SECONDS", 0.5)
    monkeypatch.setattr(swim_file_ingest, "_get_already_processed", lambda names: set())
    monkeypatch.setattr(swim_file_ingest, "_append_log_entries", lambda entries: None)
    monkeypatch.setattr(swim_file_ingest, "PROCESSED_DIR", tmp_path / "processed")
    monkeypatch.setattr(swim_file_ingest, "FAILED_DIR", tmp_path / "failed")

    watched = tmp_path / "log"
    watched.mkdir()
    got: list[int] = []
    worker = threading.Thread(
        target=swim_file_ingest.watch_swim_folder,
        kwargs={"source_dir": watched, "reconcile_interval": 3600, "debounce_seconds": 0.2,
                "on_events": lambda events, stats: got.append(len(events))},
        daemon=True,
    )
    worker.start()
    try:
        time.sleep(1.5)  # past the startup scan of an empty folder
        shutil.copy(CAPTURE / "1787863348274", watched / "1787863348274")
        deadline = time.monotonic() + 10
        while not got and time.monotonic() < deadline:
            time.sleep(0.2)
        assert got == [1], "the new TDLS message should be ingested within seconds"
    finally:
        swim_file_ingest.request_shutdown()
        worker.join(timeout=10)
        swim_file_ingest._shutdown_requested.clear()


# ── One consumer per file ─────────────────────────────────────────────────────

def _isolate_ingest_log(monkeypatch):
    from src import swim_file_ingest
    monkeypatch.setattr(swim_file_ingest, "_get_already_processed", lambda names: set())
    monkeypatch.setattr(swim_file_ingest, "_append_log_entries", lambda entries: None)


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_two_scanners_on_one_folder_ingest_each_file_exactly_once(tmp_path: Path, monkeypatch):
    """The live watcher and a Temporal ingest run scanning the same folder at once."""
    import shutil
    import threading

    from src import swim_file_ingest

    _isolate_ingest_log(monkeypatch)
    folder = tmp_path / "log"
    folder.mkdir()
    for f in CAPTURE.iterdir():
        shutil.copy(f, folder / f.name)

    results = {}

    def scan(name):
        events, stats = swim_file_ingest.scan_swim_folder(
            folder, processed_dir=tmp_path / f"processed_{name}", failed_dir=tmp_path / f"failed_{name}")
        results[name] = (stats.files_processed, {e.event_id for e in events})

    threads = [threading.Thread(target=scan, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    (processed_a, ids_a), (processed_b, ids_b) = results["a"], results["b"]
    assert processed_a + processed_b == 10, "every file processed once, by exactly one scanner"
    assert not (ids_a & ids_b), "no event ingested by both"
    assert not list(folder.glob("*")) or all(p.is_dir() for p in folder.iterdir())


def test_a_claimed_file_is_invisible_to_another_scanner(tmp_path: Path, monkeypatch):
    import shutil

    from src import swim_file_ingest

    _isolate_ingest_log(monkeypatch)
    folder = tmp_path / "log"
    folder.mkdir()
    shutil.copy(CAPTURE / "1787863348274", folder / "1787863348274")
    assert swim_file_ingest._claim(folder / "1787863348274") is not None
    assert swim_file_ingest._claim(folder / "1787863348274") is None      # already taken
    _, stats = swim_file_ingest.scan_swim_folder(
        folder, processed_dir=tmp_path / "p", failed_dir=tmp_path / "f")
    assert stats.files_processed == 0


def test_a_claim_abandoned_by_a_dead_scanner_is_released_and_processed(tmp_path: Path, monkeypatch):
    import os
    import shutil
    import time

    from src import swim_file_ingest

    _isolate_ingest_log(monkeypatch)
    folder = tmp_path / "log"
    (folder / swim_file_ingest.INFLIGHT_DIR).mkdir(parents=True)
    stale = folder / swim_file_ingest.INFLIGHT_DIR / "1787863348274"
    shutil.copy(CAPTURE / "1787863348274", stale)
    old = time.time() - swim_file_ingest.CLAIM_TIMEOUT_SECONDS - 5
    os.utime(stale, (old, old))

    events, stats = swim_file_ingest.scan_swim_folder(
        folder, processed_dir=tmp_path / "p", failed_dir=tmp_path / "f")
    assert stats.files_processed == 1 and len(events) == 1
    assert (tmp_path / "p" / "1787863348274").exists()


def test_a_read_only_folder_is_still_ingested_in_place(tmp_path: Path, monkeypatch):
    """No claim is possible on a read-only mount; files are read in place, as before."""
    import os
    import shutil
    import stat

    from src import swim_file_ingest

    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    _isolate_ingest_log(monkeypatch)
    folder = tmp_path / "ro"
    folder.mkdir()
    shutil.copy(CAPTURE / "1787863348274", folder / "1787863348274")
    folder.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        events, stats = swim_file_ingest.scan_swim_folder(
            folder, processed_dir=tmp_path / "p", failed_dir=tmp_path / "f")
    finally:
        folder.chmod(stat.S_IRWXU)
    assert stats.files_processed == 1 and len(events) == 1
    assert (folder / "1787863348274").exists(), "left in place on a read-only mount"


# ── FAA data-handling flags ───────────────────────────────────────────────────
#
# blockAtFAA: every captured message says false, so these tests set it on a
# copy of a real capture. See _honor_block_at_faa() for why true is withheld,
# absent is not flagged, and anything else fails closed.

_FLAGGED = CAPTURE / "1787863346109"   # TDES departure at KDCA: yields one event
_FLAG_LINE = "Property name = blockAtFAA; value = false\n"


def _with_block_flag(directory: Path, value: str | None) -> Path:
    """The real capture with blockAtFAA set to `value` (None removes the header)."""
    text = _FLAGGED.read_text(encoding="utf-8")
    assert _FLAG_LINE in text
    replacement = "" if value is None else f"Property name = blockAtFAA; value = {value}\n"
    path = directory / _FLAGGED.name
    path.write_text(text.replace(_FLAG_LINE, replacement), encoding="utf-8")
    return path


@pytest.mark.parametrize("value", ["false", "False", None])
def test_block_at_faa_false_or_absent_is_ingested(tmp_path: Path, value):
    records, error = _parse_file(_with_block_flag(tmp_path, value))
    assert error is None
    assert len(records) == 1


@pytest.mark.parametrize("value", ["true", "TRUE"])
def test_block_at_faa_true_withholds_the_message(tmp_path: Path, value):
    with pytest.raises(WithheldMessage):
        _parse_file(_with_block_flag(tmp_path, value))


@pytest.mark.parametrize("value", ["yes", "1", ""])
def test_unrecognised_block_at_faa_value_fails_closed(tmp_path: Path, value):
    records, error = _parse_file(_with_block_flag(tmp_path, value))
    assert records == []
    assert "blockAtFAA" in error


def test_a_flagged_message_with_no_body_is_withheld_not_summarised(tmp_path: Path):
    """The headers-only fallback would otherwise turn it into a metadata event."""
    path = tmp_path / "1789928000099"
    path.write_text(
        "Property name = mex; value = TAIS\nProperty name = blockAtFAA; value = true\n",
        encoding="utf-8",
    )
    with pytest.raises(WithheldMessage):
        _parse_file(path)


def test_scan_archives_a_withheld_message_and_records_why(tmp_path: Path, monkeypatch):
    from prometheus_client import REGISTRY

    from src import swim_file_ingest

    logged: list[dict] = []
    monkeypatch.setattr(swim_file_ingest, "_get_already_processed", lambda names: set())
    monkeypatch.setattr(swim_file_ingest, "_append_log_entries", logged.extend)  # one batch per scan
    src_dir, processed, failed = tmp_path / "in", tmp_path / "processed", tmp_path / "failed"
    src_dir.mkdir()
    _with_block_flag(src_dir, "true")
    before = REGISTRY.get_sample_value("nas_swim_messages_withheld_total") or 0.0

    events, stats = swim_file_ingest.scan_swim_folder(
        src_dir, processed_dir=processed, failed_dir=failed
    )

    assert events == []
    assert (stats.files_withheld, stats.files_failed, stats.files_processed) == (1, 0, 0)
    assert list(failed.iterdir()) == [], "withheld is not a parse failure"
    assert (processed / _FLAGGED.name).exists(), "archived, so it is not rescanned"
    assert [(e["status"], e["error"]) for e in logged] == [("withheld", "blockAtFAA=true")]
    assert REGISTRY.get_sample_value("nas_swim_messages_withheld_total") == before + 1


# Beacon codes: FAA STDDS beacon code notice (2017-10-11). A TAIS flight plan's
# codes are released only once the plan is active.

def _tais_records(tmp_path: Path, record: dict) -> list[dict]:
    path = _kit_file(tmp_path, "1789928000100", "TAIS", "FP",
                     {"ns2:TATrackAndFlightPlan": {"record": [record]}})
    records, error = _parse_file(path)
    assert error is None
    return records


@pytest.mark.parametrize("status, expected", [
    ("active", "4521"),
    ("ACTIVE", "4521"),
    ("pending", None),
    ("passive", None),
    (None, None),
])
def test_tais_beacon_code_is_kept_only_for_an_active_flight_plan(tmp_path: Path, status, expected):
    flight_plan = {"airport": "KDCA", "acid": "AAL123", "assignedBeaconCode": "4521"}
    if status is not None:
        flight_plan["status"] = status
    records = _tais_records(tmp_path, {
        "flightPlan": flight_plan,
        "track": {"reportedBeaconCode": "4521", "lat": 38.85, "lon": -77.04},
    })
    assert [r["beacon_code"] for r in records] == [expected]


def test_tais_track_without_a_flight_plan_keeps_its_reported_code(tmp_path: Path):
    records = _tais_records(tmp_path, {
        "enhancedData": {"destinationAirport": "KDCA"},
        "track": {"reportedBeaconCode": "1200", "lat": 38.85, "lon": -77.04},
    })
    assert [r["beacon_code"] for r in records] == ["1200"]
