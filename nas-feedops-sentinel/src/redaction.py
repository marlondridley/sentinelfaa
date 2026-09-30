"""
Shared PII/telemetry redaction helpers for anything that reaches an LLM.

FAA NAS telemetry (the ADS-B/STDDS feeds this system ingests) carries data
that identifies specific aircraft and, by extension, specific flights and
their occupants -- tail numbers, squawk/beacon codes, live lat/lon/altitude,
callsigns, raw payloads. None of that has a reason to leave this system in
a prompt sent to a third-party model host. This module is the single
source of truth in this codebase for what counts as sensitive here,
generalizing what used to be a local, non-shared ``_REDACT_FIELDS``/
``_redact()`` pair defined only inside watch_officer_agent.py (and absent
entirely from critic_agent.py and planner_agent.py, which had no
redaction at all).

Two redaction shapes are provided because the codebase has two shapes of
LLM call site:

- ``redact_structured(obj)`` -- for a dict/list payload *before* it is
  serialized. Use this wherever a caller controls the payload directly
  (e.g. watch_officer_agent.py's CrewAI ``Task`` payload).
- ``redact_text(text)`` -- for an already-built free-text prompt string.
  This is applied unconditionally inside
  ``LiteLLMClient.acall_with_tool()`` (see llm_client.py) so *every*
  direct-LiteLLM caller is covered -- critic_agent.py's f-string prompt,
  planner_agent.py's ``json.dumps()``'d context, and any future caller --
  without each one having to remember to call it. That is what makes this
  "mandatory" rather than opt-in.

This is a best-effort scrub, similar in spirit to the FAA's own LADD
(Limiting Aircraft Data Displayed) concept: it narrows what leaves the
system, it does not guarantee zero leakage of every conceivable
identifying detail buried in free text.
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# Field names treated as identifying wherever they appear -- as a dict key
# (redact_structured) or as a "key": value / key=value pair inside an
# already-serialized prompt string (redact_text). This is the generalized
# version of what used to be watch_officer_agent.py's private
# ``_REDACT_FIELDS`` -- kept here as the one shared list so every call
# site agrees on what "sensitive" means.
SENSITIVE_FIELDS: frozenset[str] = frozenset({
    "aircraft_id",
    "beacon_code",
    "squawk",
    "track_id",
    "callsign",
    "vehicle_id",
    "latitude",
    "longitude",
    "lat",
    "lon",
    "altitude",
    "raw_payload",
    # The feeds' own names for the same things, and other ways to say "this flight": a flight id, a plan's
    # acid or gufi, a registration. Compared without regard to case, so aircraftId and AIRCRAFT_ID are one.
    "flight_id",
    "flightid",
    "acid",
    "gufi",
    "tail_number",
    "registration",
    "aircraftid",
    "acaddress",
    "beaconcode",
    "reportedbeaconcode",
    "assignedbeaconcode",
    "tracknum",
})
_SENSITIVE_LOWER = frozenset(f.lower() for f in SENSITIVE_FIELDS)


def redact_structured(obj: Any) -> Any:
    """
    Recursively drop SENSITIVE_FIELDS keys from a dict/list payload before
    it is serialized and sent to an LLM. Non-dict/list values pass through
    unchanged. Safe to call on arbitrarily nested structures.
    """
    if isinstance(obj, dict):
        return {k: redact_structured(v) for k, v in obj.items() if str(k).lower() not in _SENSITIVE_LOWER}
    if isinstance(obj, list):
        return [redact_structured(item) for item in obj]
    return obj


# Matches "key": value / 'key': value / key: value / key=value for any
# SENSITIVE_FIELDS name, as produced by json.dumps() or an f-string --
# this is what lets redact_text() catch a sensitive field mentioned inside
# an *already-serialized* prompt string, where redact_structured() can no
# longer help because the dict has already become text by the time
# critic_agent.py / planner_agent.py hand it to the LLM client.
#
# The value is a quoted string (escaped quotes inside it included, so a JSON document carried as a string
# value, as raw_payload is, is taken whole rather than cut at its first inner quote), or a bare token up to
# the next space or separator: `aircraft_id: AAL123` and `callsign=UAL1234` are not quoted by a person
# writing a sentence, and used to pass through.
_FIELD_VALUE_RE = re.compile(
    r'(?P<prefix>["\']?(?:' + "|".join(re.escape(f) for f in sorted(SENSITIVE_FIELDS)) + r')["\']?\s*[:=]\s*)'
    r'(?P<value>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^\s,;}\])"\']+)',
    re.IGNORECASE,
)

# The same fields as XML elements: <aircraftId>AAL123</aircraftId>.
_XML_FIELD_RE = re.compile(
    r"<(?P<tag>" + "|".join(re.escape(f) for f in sorted(SENSITIVE_FIELDS)) + r")(?P<attrs>\s[^>]*)?>"
    r"(?P<value>[^<]*)</(?P=tag)>",
    re.IGNORECASE,
)

# A flight's callsign: three letters and one to four digits, and possibly a letter (AAL123, UAL1234, RPA4451A),
# in any case. Not the codes that look like one but are not a flight: runway, RVR, ILS and the like, and the
# aircraft types written the same way (CRJ700, ATR72). A hyphen counts as part of the word, so a segment of a
# UUID (abc1-...) that happens to have the same shape is left alone: those are the ids a prompt must cite.
_CALLSIGN_RE = re.compile(
    r"(?<![\w-])(?!(?:RWY|RVR|ILS|UTC|GMT|VOR|SID|MSL|AGL|TWY|FAA|CRJ|ATR|EMB|DHC|BAE)\d)"
    r"[A-Za-z]{3}\d{1,4}[A-Za-z]?(?![\w-])",
    re.IGNORECASE,
)

# US civil aircraft registration marks (N-numbers): "N" followed by 1-5
# digits and up to 2 trailing letters. Catches a tail number mentioned in
# narrative text even when it isn't attached to a recognized field name
# (e.g. "...aircraft N12345 diverted to...").
_TAIL_NUMBER_RE = re.compile(r"\bN[0-9]{1,5}[A-Z]{0,2}\b", re.IGNORECASE)

# A bare "lat,lon"-shaped decimal coordinate pair in narrative text, for
# the case where it isn't attached to a "latitude"/"longitude" key that
# _FIELD_VALUE_RE would already catch.
_COORD_PAIR_RE = re.compile(r"-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}")


# A pydantic validation error quotes the offending value: "... [type=extra_forbidden,
# input_value='AAL123', input_type=str]". The field name sits on the line before, so
# neither pattern above can tie the two together, and the value would go out as-is.
# Ingest failure text reaches the Feed Diagnostician in exactly this form.
_INPUT_VALUE = "input_value="
_INPUT_TYPE = ", input_type="


def _input_value_end(text: str, start: int) -> int:
    """
    Where the value that begins at `start` ends. A scan, not a pattern, because the value is a Python
    repr: a dict or list nests, and a pattern that stops at the first closing brace leaves everything
    after an inner one in the clear. Quotes are honoured (a brace inside a string is not structure)
    and so are escapes.

    pydantic cuts a long repr short in the middle, so one may never balance; the scan is bounded by
    pydantic's next field, ", input_type=", or the end of the line, and takes everything up to it.
    """
    limit = len(text)
    for marker in (_INPUT_TYPE, "\n"):
        found = text.find(marker, start)
        if found != -1:
            limit = min(limit, found)
    depth, quote, k = 0, None, start
    while k < limit:
        c = text[k]
        if quote:
            if c == "\\":
                k += 2
                continue
            if c == quote:
                quote = None
                if depth == 0:
                    return k + 1                      # a quoted string on its own ends with its quote
        elif c in "'\"":
            quote = c
        elif c in "{[(":
            depth += 1
        elif c in "}])":
            if depth == 0:
                return k                              # closes something outside the value: the error's own "]"
            depth -= 1
            if depth == 0:
                return k + 1
        elif depth == 0 and c in ", \t\r":
            return k                                  # a bare number or word ends at the next separator
        k += 1
    return limit


def _redact_input_values(text: str) -> str:
    out: list[str] = []
    i = 0
    while True:
        found = text.find(_INPUT_VALUE, i)
        if found == -1:
            out.append(text[i:])
            return "".join(out)
        start = found + len(_INPUT_VALUE)
        out.append(text[i:found] + _INPUT_VALUE + REDACTED)
        i = _input_value_end(text, start)


def redact_text(text: str) -> str:
    """
    Best-effort scrub of a free-text prompt before it reaches an LLM.

    Applied unconditionally inside LiteLLMClient.acall_with_tool() (see
    llm_client.py), so callers don't need to remember to invoke this
    themselves -- it's a mandatory backstop, not a substitute for
    redacting structured data at the source with redact_structured() where
    that's practical (it still is, and is preferred, e.g. in
    watch_officer_agent.py).
    """
    if not text:
        return text
    text = _FIELD_VALUE_RE.sub(lambda m: f"{m.group('prefix')}\"{REDACTED}\"", text)
    text = _XML_FIELD_RE.sub(lambda m: f"<{m.group('tag')}{m.group('attrs') or ''}>{REDACTED}</{m.group('tag')}>", text)
    text = _redact_input_values(text)
    text = _TAIL_NUMBER_RE.sub(REDACTED, text)
    text = _CALLSIGN_RE.sub(REDACTED, text)
    text = _COORD_PAIR_RE.sub(REDACTED, text)
    return text


def is_redacted(text: str) -> bool:
    """True when redact_text() would change nothing: there is nothing left in `text` that it removes."""
    return redact_text(text) == text
