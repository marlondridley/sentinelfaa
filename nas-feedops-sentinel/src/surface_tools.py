"""
Taxi-time tools for the Planner, and the router that picks them.

The Planner answers operator questions in one pass over a small context. A
question about taxi times needs numbers it cannot get from recent alerts, so
these five governed read tools compute them from the SMES surface events:

  read_taxi_out_stats   average / longest taxi-out (spot out -> wheels off) and an hourly trend
  read_taxi_in_stats    the same for taxi-in (wheels on -> spot in)
  read_departure_queue  who has left the gate and not yet taken off, and for how long
  read_gate_returns     flights that left the gate and came back without taking off
  read_flight_timeline  one flight's surface events, with its taxi times worked out

route() reads the question with explicit patterns, not a model, and names the
tools it needs; gather() runs them. Each read goes through agent_tools.invoke,
so it is put to the tool-use policy first (only agents granted a tool may run
it), and a refused or failed tool is reported in the result instead of
stopping the answer. Every result carries its sample size and how fresh the
surface data is (src/surface_metrics.py). A question that is plainly about
taxi or departure timing but matches no tool says so ("not_fetched"), rather
than leaving the answer to imply it consulted the surface data.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from typing import Any

from src import agent_tools, storage, surface_metrics
from src.config import settings
from src.logging_config import get_logger
from src.timeutil import as_utc

_log = get_logger(__name__)

DEFAULT_WINDOW_MINUTES = 15
MAX_WINDOW_MINUTES = 24 * 60
TREND_HOURS = 6
TIMELINE_LOOKBACK = timedelta(hours=24)

_AIRPORT_CODES = {"IAD": "KIAD", "DCA": "KDCA", "BWI": "KBWI", "ADW": "KADW",
                  "KIAD": "KIAD", "KDCA": "KDCA", "KBWI": "KBWI", "KADW": "KADW"}
_AIRPORT = re.compile(r"\b(K?(?:IAD|DCA|BWI|ADW))\b", re.IGNORECASE)
_AIRPORT_PATTERN = r"k?(?:iad|dca|bwi|adw)"
# Three letters and one to four digits (UAL1234, ual1234, SWA890A). Codes that are not flights are skipped.
_CALLSIGN = re.compile(r"\b([A-Za-z]{3}\d{1,4}[A-Za-z]?)\b")
# ... and the aircraft types written the same way (CRJ700, ATR72): "which CRJ700 flights have departed" is not
# about a flight called CRJ700.
_NOT_CALLSIGNS = frozenset({"RWY", "RVR", "ILS", "UTC", "GMT", "VOR", "SID", "MSL", "AGL", "TWY", "FAA",
                            "CRJ", "ATR", "EMB", "DHC", "BAE"})
# "last 15 minutes", "past 2 hours", "last 1.5 hours", "last 2 days". At most four digits, so a run of thousands
# is not a number to parse (int() refuses more than 4,300 of them) and falls to the default window.
_WINDOW = re.compile(r"\b(?:last|past)\s+(\d{1,4}(?:\.\d{1,2})?)\s*(min(?:ute)?s?|h(?:ou)?rs?|d(?:ay)?s?)\b", re.IGNORECASE)
_ONE_HOUR = re.compile(r"\b(?:last|past)\s+hour\b", re.IGNORECASE)
_TODAY = re.compile(r"\btoday\b|\bthis (?:morning|shift)\b", re.IGNORECASE)
_LONGER_THAN = re.compile(r"(?:more than|over|longer than|at least)\s+(\d{1,4})\s*min", re.IGNORECASE)

_TAXI_OUT = re.compile(r"taxi[\s-]*out|taxiing out|push[\s-]*back|pushed back|spot[\s-]*out", re.IGNORECASE)
# "taxiing in the departure queue" is the queue, not a taxi-in: the preposition is not the direction.
_TAXI_IN = re.compile(
    r"taxi[\s-]*in\b|taxiing in(?!\s+(?:the\s+|a\s+)?(?:(?:departure|takeoff|take-off)\s+)?queue)"
    r"|spot[\s-]*in\b|reached (?:a|the) gate", re.IGNORECASE)
# The departure queue, not any queue: the review queue is a different thing on this dashboard.
_QUEUE = re.compile(
    rf"(?:departure|taxi|takeoff|take-off)[\s-]*queue|(?<!review )queue (?:at|for|of|to) {_AIRPORT_PATTERN}\b"
    r"|still taxiing|taxiing out|have(?:n't| not) (?:taken off|departed)", re.IGNORECASE)
_GATE_RETURN = re.compile(r"return(?:ed|s)? to (?:the )?gate|gate return|back to (?:the )?gate", re.IGNORECASE)
_FLIGHT_WORDS = re.compile(r"timeline|push|took off|take[\s-]?off|taken off|departed|landed|where is|when did|surface",
                           re.IGNORECASE)
_TAXI_GENERIC = re.compile(r"\btaxi(?:ing|\s+times?)?\b", re.IGNORECASE)
# Words that say a question is about surface timing, whether or not a tool matched it.
_SURFACE_TIMING = re.compile(
    r"taxi|push[\s-]*back|pushed back|departure (?:delay|wait|rate)|waiting to (?:depart|take ?off|get airborne)"
    r"|gate (?:hold|delay)|wheels[\s-]*(?:on|off)|spot[\s-]*(?:in|out)"
    r"|(?:taken|took) off|take[\s-]?offs?\b|\bdeparted\b", re.IGNORECASE)


def _plain(question: str) -> str:
    """The question with typographic apostrophes made plain: "haven\u2019t taken off" must match "haven't"."""
    return question.replace("\u2019", "'").replace("\u2018", "'")


def _airports(question: str) -> list[str]:
    named = {_AIRPORT_CODES[m.upper()] for m in _AIRPORT.findall(question)}
    named &= set(settings.SUPPORTED_AIRPORTS)
    return sorted(named) or sorted(settings.SUPPORTED_AIRPORTS)


def _callsign(question: str) -> str | None:
    for match in _CALLSIGN.finditer(question):
        code = match.group(1).upper()
        if code[:3] not in _NOT_CALLSIGNS and code[:3] not in _AIRPORT_CODES:
            return code
    return None


def _window_minutes(question: str, default: int = DEFAULT_WINDOW_MINUTES) -> int:
    match = _WINDOW.search(question)
    if match:
        unit = match.group(2).lower()
        minutes = round(float(match.group(1)) * (60 if unit.startswith("h") else 1440 if unit.startswith("d") else 1))
    elif _ONE_HOUR.search(question):
        minutes = 60
    elif _TODAY.search(question):
        minutes = MAX_WINDOW_MINUTES       # a rolling day; the results are worded "last 24 hours"
    else:
        minutes = default
    return max(1, min(minutes, MAX_WINDOW_MINUTES))


def route(question: str) -> list[tuple[str, dict[str, Any]]]:
    """The (tool, arguments) calls this question needs; an empty list when it is not a taxi-time question."""
    question = _plain(question)
    callsign = _callsign(question)
    if callsign and _FLIGHT_WORDS.search(question):
        return [("read_flight_timeline", {"aircraft_id": callsign})]      # about one flight, not the airport

    airports = _airports(question)
    calls: list[tuple[str, dict[str, Any]]] = []

    if _GATE_RETURN.search(question):
        window = _window_minutes(question, default=MAX_WINDOW_MINUTES)
        calls += [("read_gate_returns", {"airport": a, "window_minutes": window}) for a in airports]

    queue_asked = _QUEUE.search(question) and not _TAXI_IN.search(question)
    if queue_asked:
        longer = _LONGER_THAN.search(question)
        calls += [("read_departure_queue", {"airport": a, "min_minutes": float(longer.group(1)) if longer else 0.0})
                  for a in airports]

    if _TAXI_OUT.search(question) and not _GATE_RETURN.search(question):
        window = _window_minutes(question)
        calls += [("read_taxi_out_stats", {"airport": a, "window_minutes": window}) for a in airports]

    if _TAXI_IN.search(question):
        window = _window_minutes(question)
        calls += [("read_taxi_in_stats", {"airport": a, "window_minutes": window}) for a in airports]

    if not calls and _TAXI_GENERIC.search(question):        # "taxi time", with no direction: give both
        window = _window_minutes(question)
        for a in airports:
            calls += [("read_taxi_out_stats", {"airport": a, "window_minutes": window}),
                      ("read_taxi_in_stats", {"airport": a, "window_minutes": window})]

    seen: set[tuple] = set()
    unique = []
    for tool, args in calls:
        key = (tool, tuple(sorted(args.items())))
        if key not in seen:
            seen.add(key)
            unique.append((tool, args))
    return unique


# ── The tools ────────────────────────────────────────────────────────────────

def _now(as_of: datetime | None) -> datetime:
    return as_utc(as_of)


async def read_taxi_out_stats(airport: str | None, window_minutes: int = DEFAULT_WINDOW_MINUTES,
                              as_of: datetime | None = None) -> dict[str, Any]:
    as_of = _now(as_of)
    since = as_of - timedelta(minutes=max(window_minutes, TREND_HOURS * 60))

    async def read():
        return (await storage.get_taxi_out_pairs(airport, since, as_of),
                await storage.get_latest_surface_event(airport, as_of))

    pairs, latest = await agent_tools.invoke("read_taxi_out_stats", read)
    return surface_metrics.taxi_stats(pairs, "spot_out_at", "wheels_off_at", airport=airport,
                                      window_minutes=window_minutes, as_of=as_of, latest_event=latest)


async def read_taxi_in_stats(airport: str | None, window_minutes: int = DEFAULT_WINDOW_MINUTES,
                             as_of: datetime | None = None) -> dict[str, Any]:
    as_of = _now(as_of)
    since = as_of - timedelta(minutes=max(window_minutes, TREND_HOURS * 60))

    async def read():
        return (await storage.get_taxi_in_pairs(airport, since, as_of),
                await storage.get_latest_surface_event(airport, as_of))

    pairs, latest = await agent_tools.invoke("read_taxi_in_stats", read)
    return surface_metrics.taxi_stats(pairs, "wheels_on_at", "spot_in_at", airport=airport,
                                      window_minutes=window_minutes, as_of=as_of, latest_event=latest)


async def read_departure_queue(airport: str | None, min_minutes: float = 0.0,
                               as_of: datetime | None = None) -> dict[str, Any]:
    as_of = _now(as_of)

    async def read():
        return (await storage.get_departure_queue(airport, as_of),
                await storage.get_latest_surface_event(airport, as_of))

    rows, latest = await agent_tools.invoke("read_departure_queue", read)
    return surface_metrics.queue_summary(rows, airport=airport, as_of=as_of, latest_event=latest,
                                         min_minutes=min_minutes)


async def read_gate_returns(airport: str | None, window_minutes: int = MAX_WINDOW_MINUTES,
                            as_of: datetime | None = None) -> dict[str, Any]:
    as_of = _now(as_of)
    since = as_of - timedelta(minutes=window_minutes)

    async def read():
        return (await storage.get_gate_returns(airport, since, as_of),
                await storage.get_latest_surface_event(airport, as_of))

    rows, latest = await agent_tools.invoke("read_gate_returns", read)
    return surface_metrics.gate_return_summary(rows, airport=airport, window_minutes=window_minutes,
                                               as_of=as_of, latest_event=latest)


async def read_flight_timeline(aircraft_id: str, as_of: datetime | None = None) -> dict[str, Any]:
    as_of = _now(as_of)

    async def read():
        return await storage.get_flight_surface_events(aircraft_id, TIMELINE_LOOKBACK, as_of)

    events = await agent_tools.invoke("read_flight_timeline", read)
    return surface_metrics.flight_timeline(aircraft_id, events, as_of=as_of)


TOOL_FUNCTIONS = {
    "read_taxi_out_stats": read_taxi_out_stats,
    "read_taxi_in_stats": read_taxi_in_stats,
    "read_departure_queue": read_departure_queue,
    "read_gate_returns": read_gate_returns,
    "read_flight_timeline": read_flight_timeline,
}

NOT_FETCHED = ("This question is about taxi or departure timing, but no surface tool matched its wording, so no "
               "surface figures were fetched. Do not state a taxi time; say the figures were not consulted, and "
               "suggest rephrasing with an airport and a time window.")


async def _run(tool: str, args: dict[str, Any], as_of: datetime | None) -> tuple[str, Any]:
    try:
        return "ok", await TOOL_FUNCTIONS[tool](as_of=as_of, **args)
    except agent_tools.ToolCallDenied as denied:
        return "unavailable", {"tool": tool, "reason": str(denied)}
    except Exception as exc:  # noqa: BLE001 -- a failed read degrades the answer, never stops it
        _log.warning("surface_tool_failed", extra={"tool": tool, "error": f"{type(exc).__name__}: {exc}"})
        return "unavailable", {"tool": tool, "reason": f"{type(exc).__name__}: {exc}"}


async def gather(question: str, as_of: datetime | None = None) -> dict[str, Any]:
    """
    Run the tools this question needs, as the agent now acting, concurrently (the reads are
    independent). Returns {tool: [results]}, plus "unavailable" for a tool refused or failed, or
    "not_fetched" when the question is about taxi timing but matched no tool. Empty when the
    question has nothing to do with surface timing.
    """
    calls = route(question)
    if not calls:
        return {"not_fetched": NOT_FETCHED} if _SURFACE_TIMING.search(_plain(question)) else {}
    outcomes = await asyncio.gather(*(_run(tool, args, as_of) for tool, args in calls))
    results: dict[str, Any] = {}
    unavailable: list[dict[str, str]] = []
    for (tool, _), (status, value) in zip(calls, outcomes):
        if status == "ok":
            results.setdefault(tool, []).append(value)
        else:
            unavailable.append(value)
    if unavailable:
        results["unavailable"] = unavailable
    return results


def has_figures(results: dict[str, Any]) -> bool:
    """True when at least one tool returned a result (not just a note or a refusal)."""
    return any(isinstance(value, list) and tool != "unavailable" and value for tool, value in results.items())


def is_stale(results: dict[str, Any]) -> bool:
    """True when any result rests on surface data the feed has stopped supplying."""
    return any(r.get("freshness", {}).get("stale")
               for tool, rs in results.items() if tool != "unavailable" and isinstance(rs, list)
               for r in rs if isinstance(r, dict))


def describe(results: dict[str, Any]) -> list[str]:
    """
    The results as plain sentences: the whole answer when no model is available, and the part of a model's
    answer that names flights (the model never sees callsigns; they are redacted before it does).
    """
    lines: list[str] = []
    for r in results.get("read_taxi_out_stats", []):
        lines.append(_taxi_line("taxi-out", r))
    for r in results.get("read_taxi_in_stats", []):
        lines.append(_taxi_line("taxi-in", r))
    for r in results.get("read_departure_queue", []):
        lines.append(_queue_line(r))
    for r in results.get("read_gate_returns", []):
        flights = ", ".join(f"{f['aircraft_id']} ({f['minutes_out']} min out)" for f in r["flights"][:8])
        lines.append(f"{r['airport']}: {r['returned_to_gate']} flight(s) returned to the gate in the {r['window_label']}"
                     + (f": {flights}" if flights else "") + f". {r['freshness']['note']}")
    for r in results.get("read_flight_timeline", []):
        parts = [f"{r['aircraft_id']}: " + (", ".join(f"{e['event']} at {e['airport']} {e['at'][11:16]}Z" for e in r["events"])
                                            or r.get("note", "no surface events found"))]
        if r["taxi_out"]:
            parts.append(f"taxi-out {r['taxi_out']['minutes']} min")
        if r["taxi_in"]:
            parts.append(f"taxi-in {r['taxi_in']['minutes']} min")
        if r["returned_to_gate"]:
            parts.append("returned to the gate")
        lines.append("; ".join(parts) + ".")
    for u in results.get("unavailable", []):
        lines.append(f"{u['tool']} was unavailable ({u['reason']}).")
    if "not_fetched" in results:
        lines.append("No surface figures were fetched for this question: its wording matched none of the taxi-time "
                     "tools. Rephrase with an airport and a time window (for example 'average taxi-out at IAD over "
                     "the last 15 minutes').")
    return lines


def _taxi_line(kind: str, r: dict[str, Any]) -> str:
    if not r["completed"]:
        return f"{r['airport']}: no {kind} completed in the {r['window_label']}. {r['freshness']['note']}"
    line = (f"{r['airport']}: average {kind} {r['avg_minutes']} min over {r['completed']} flight(s) in the "
            f"{r['window_label']} (longest {r['max_minutes']} min")
    line += f", {r['longest']['aircraft_id']}" if r.get("longest") else ""
    line += "). " + (r.get("note") + " " if r.get("note") else "") + r["freshness"]["note"]
    return line


def _queue_line(r: dict[str, Any]) -> str:
    shown = ", ".join(
        f"{f['aircraft_id']} {f['taxiing_minutes']} min" + (" (take-off probably not recorded)" if f["suspect"] else "")
        for f in r["flights"][:8])
    return (f"{r['airport']}: {r['in_departure_queue']} aircraft have left the gate and not yet taken off"
            + (f" ({r['matching']} at {r['min_taxiing_minutes']:g}+ min)" if r["min_taxiing_minutes"] else "")
            + (f": {shown}" if shown else "") + f". {r.get('note', '')} {r['freshness']['note']}".replace("  ", " "))
