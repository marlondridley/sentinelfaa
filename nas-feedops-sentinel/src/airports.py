"""
The airports this system knows: where each is, and which TRACON runs its approach control.

What is IN SCOPE is not decided here. It is config: SUPPORTED_AIRPORTS and SUPPORTED_TRACON
(src/config.py, set per deployment). This is only reference data, so that scope can widen to
other airports and TRACONs without a code change:

    SUPPORTED_AIRPORTS=KJFK,KLGA,KEWR
    SUPPORTED_TRACON=N90

An airport missing from the registry still works, as the dashboard just has no map marker for it
and its TRACON is the deployment's SUPPORTED_TRACON. Add a row to the table when you want either.

Pure data, no imports from the rest of the app, so config, models and the dashboard can all use it.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Airport:
    code: str       # ICAO
    name: str
    lat: float
    lon: float
    tracon: str     # the TRACON whose STDDS feed reports it


# The four Potomac TRACON (PCT) primaries are what this system was built and verified on
# (tests/fixtures/swim_kit/capture_2026-08-27). The rest are reference points for widening scope: their
# coordinates place a map marker and their TRACON is the standard FAA identifier. No feed for them has
# been captured or verified here.
_TABLE: tuple[Airport, ...] = (
    Airport("KDCA", "Reagan National",       38.8521, -77.0377, "PCT"),
    Airport("KIAD", "Dulles International",  38.9531, -77.4565, "PCT"),
    Airport("KBWI", "Baltimore-Washington",  39.1754, -76.6683, "PCT"),
    Airport("KADW", "Joint Base Andrews",    38.8108, -76.8669, "PCT"),
    Airport("KJFK", "John F. Kennedy",       40.6398, -73.7789, "N90"),
    Airport("KLGA", "LaGuardia",             40.7769, -73.8740, "N90"),
    Airport("KEWR", "Newark Liberty",        40.6925, -74.1687, "N90"),
    Airport("KBOS", "Boston Logan",          42.3656, -71.0096, "A90"),
    Airport("KPHL", "Philadelphia",          39.8744, -75.2424, "PHL"),
    Airport("KATL", "Atlanta Hartsfield-Jackson", 33.6407, -84.4277, "A80"),
    Airport("KORD", "Chicago O'Hare",        41.9742, -87.9073, "C90"),
    Airport("KMDW", "Chicago Midway",        41.7868, -87.7522, "C90"),
)

REGISTRY: dict[str, Airport] = {a.code: a for a in _TABLE}


def get(code: str) -> Airport | None:
    return REGISTRY.get(code)


def tracon_of(code: str) -> str | None:
    """The airport's TRACON, or None when the registry does not know it."""
    airport = REGISTRY.get(code)
    return airport.tracon if airport else None
