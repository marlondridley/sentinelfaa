"""
Helpers every STDDS extractor shares: coercing the feed's numbers and scalars, reading an airport,
mapping a JMS `mex` header to a service, and deriving a flight phase.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src import config
from src.swim_normalise import _as_utc


def _service_from_mex(mex: str) -> str:
    """Map JMS 'mex' (message exchange) header to a supported service_name."""
    mex_up = (mex or "").upper()
    return {
        "TAIS": "TERMINAL_AUTO",
        "SMES": "SMES",
        "APDS": "APDS_RVR",
        "APDS_RVR": "APDS_RVR",
        "ISMC": "ISMC",
        "TFDM": "TOWER_DEPARTURE",
        # The live STDDS subscription sends TDES (Tower Data Exchange /
        # TDLS), not TFDM. Without this it fell through to the
        # TERMINAL_AUTO default, mislabelling every departure-clearance
        # message as terminal-automation data. Confirmed against real
        # captured messages: mex=TDES, root ns5:TowerDepartureEventMessage.
        "TDES": "TOWER_DEPARTURE",
    }.get(mex_up, "TERMINAL_AUTO")

def _derive_flight_phase(
    altitude_ft: int | None,
    v_vert: int | None,
    this_airport: str,
    departure: str,
    destination: str,
) -> str:
    """
    Heuristic flight-phase classifier from track data.

    GROUND     -- altitude < 500 ft (taxiing / on surface)
    DEPARTURE  -- climbing AND this airport is the departure
    ARRIVAL    -- descending AND this airport is the destination
    ENROUTE    -- level flight at cruise altitude
    UNKNOWN    -- insufficient signal
    """
    if altitude_ft is None:
        return "UNKNOWN"
    if altitude_ft < 500:
        return "GROUND"
    if v_vert is not None:
        # A flight plan names the TRACON itself when the airport is not filed.
        if v_vert > 300 and (departure == this_airport or departure in config.SUPPORTED_TRACONS):
            return "DEPARTURE"
        if v_vert < -300 and (destination == this_airport or destination in config.SUPPORTED_TRACONS):
            return "ARRIVAL"
        if v_vert > 500:
            return "DEPARTURE"
        if v_vert < -500:
            return "ARRIVAL"
    if altitude_ft >= 10000:
        return "ENROUTE"
    if destination == this_airport:
        return "ARRIVAL"
    if departure == this_airport:
        return "DEPARTURE"
    return "ENROUTE"

def _normalise_icao(code: str | None) -> str:
    """Ensure an airport identifier is a K-prefixed ICAO (US convention)."""
    if not code:
        return config.SUPPORTED_TRACON
    c = code.strip().upper()
    if len(c) == 3 and c.isalpha():
        return f"K{c}"
    return c

def _drill_payload(body: dict) -> tuple[str, dict]:
    """
    Strip the namespace-prefixed root element and return (root_name, payload).

    Every STDDS message arrives as a single-key object whose key names the
    message type, e.g. "ns2:RVRDataUpdateMessage" -> the RVR payload. The root
    name is the most reliable discriminator between feeds, so unlike the
    previous drill-and-discard it is kept.
    """
    if isinstance(body, dict) and len(body) == 1:
        root_name = next(iter(body.keys()))
        inner = body[root_name]
        if isinstance(inner, dict):
            return root_name, inner
    return "", body if isinstance(body, dict) else {}

def _primary_airport_from(*candidates: str | None) -> str | None:
    """
    Return the first candidate identifier that is a supported (in-scope) airport.

    Deliberately checks EVERY candidate rather than normalising the first
    non-empty one and then testing it. A flight from HGR into KDCA carries
    flightPlan.airport="HGR" (its origin) alongside
    enhancedData.destinationAirport="KDCA"; first-wins logic normalised "HGR"
    to "KHGR", failed the primary-airport test, and dropped an arrival into
    National.
    """
    for candidate in candidates:
        if not candidate:
            continue
        code = _normalise_icao(candidate)
        if code in config.SUPPORTED_AIRPORTS:
            return code
    return None

def _coerce_int(value: Any) -> int | None:
    """STDDS uses "" for an unavailable numeric field; treat that as absent."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def _common_fields(headers: dict[str, str]) -> tuple[str, str, str | None]:
    """tracon, service_name and the header-derived timestamp shared by all feeds."""
    return (
        headers.get("tracon") or headers.get("srcTracon") or config.SUPPORTED_TRACON,
        _service_from_mex(headers.get("mex", "")),
        headers.get("safaRcvTimestamp") or headers.get("timestamp"),
    )

def _scale_rvr(value: Any) -> int | None:
    """
    Convert a wire RVR reading to feet via config.APDS_RVR_SCALE_FT.

    The scale factor is corroborated but not confirmed against the APDS ICD;
    see APDS_RVR_SCALE_FT in src/config.py for the evidence. A result above
    config.APDS_RVR_MAX_PLAUSIBLE_FT is physically impossible for RVR, so it
    is treated as proof the scale is wrong and the message is refused, rather
    than stored as a reading far above every alert threshold.
    """
    raw = _coerce_int(value)
    # 0 is a not-reporting code, not zero visibility. Live on 2026-09-25, KBWI
    # 10/28 and KIAD 01C/19C read 0 on both ends while every other runway at the
    # same airport read 5000-6000 ft, and flipped between 0 and 6000 from one
    # update to the next. Read as feet, it staged a CRITICAL ALERT_LOW_RVR per
    # update for an idle sensor.
    if raw is None or raw == 0:
        return None
    feet = raw * config.APDS_RVR_SCALE_FT
    if feet > config.APDS_RVR_MAX_PLAUSIBLE_FT:
        raise ValueError(
            f"APDS RVR wire value {raw} scales to {feet} ft, above the "
            f"{config.APDS_RVR_MAX_PLAUSIBLE_FT} ft maximum any RVR system "
            f"reports. APDS_RVR_SCALE_FT={config.APDS_RVR_SCALE_FT} is probably "
            f"wrong for this feed; see src/config.py."
        )
    return feet

def _parse_tdls_time(value: Any) -> str | None:
    """
    TDLS "time" is MMDDYYYYHHMMSS in UTC with no separators, e.g.
    "08272026204225" -> "2026-08-27T20:42:25Z" (cross-checked against the
    capture's own JMS timestamp header, 2026-08-27T20:42:25.503Z). Anything
    that does not parse returns None so the caller falls back to the header
    time rather than inventing one.
    """
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%m%d%Y%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")

def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]

def _scalar(value: Any) -> Any:
    # A field that carries an XML attribute arrives as {"content": v, "r": 1}.
    return value.get("content") if isinstance(value, dict) else value

def _keep_position(time_text: Any) -> bool:
    """Deterministic sampling; see SMES_POSITION_SAMPLE_SECONDS in src/config.py."""
    period = config.SMES_POSITION_SAMPLE_SECONDS
    if period <= 1:
        return True
    try:
        when = _as_utc(datetime.fromisoformat(str(time_text).replace("Z", "+00:00")))
    except ValueError:
        return True  # kept, so normalisation rejects the bad time visibly
    return int(when.timestamp()) % period == 0

def _coerce_float(value: Any) -> float | None:
    """STDDS numbers can arrive as JSON numbers or strings; "" means absent."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
