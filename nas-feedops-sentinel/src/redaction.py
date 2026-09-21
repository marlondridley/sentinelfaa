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
    "latitude",
    "longitude",
    "lat",
    "lon",
    "altitude",
    "raw_payload",
})


def redact_structured(obj: Any) -> Any:
    """
    Recursively drop SENSITIVE_FIELDS keys from a dict/list payload before
    it is serialized and sent to an LLM. Non-dict/list values pass through
    unchanged. Safe to call on arbitrarily nested structures.
    """
    if isinstance(obj, dict):
        return {k: redact_structured(v) for k, v in obj.items() if k not in SENSITIVE_FIELDS}
    if isinstance(obj, list):
        return [redact_structured(item) for item in obj]
    return obj


# Matches "key": value / 'key': value / key: value / key=value for any
# SENSITIVE_FIELDS name, as produced by json.dumps() or an f-string --
# this is what lets redact_text() catch a sensitive field mentioned inside
# an *already-serialized* prompt string, where redact_structured() can no
# longer help because the dict has already become text by the time
# critic_agent.py / planner_agent.py hand it to the LLM client.
_FIELD_VALUE_RE = re.compile(
    r'(?P<prefix>["\']?(?:' + "|".join(re.escape(f) for f in sorted(SENSITIVE_FIELDS)) + r')["\']?\s*[:=]\s*)'
    r'(?P<value>"[^"]*"|\'[^\']*\'|-?\d+(?:\.\d+)?)',
    re.IGNORECASE,
)

# US civil aircraft registration marks (N-numbers): "N" followed by 1-5
# digits and up to 2 trailing letters. Catches a tail number mentioned in
# narrative text even when it isn't attached to a recognized field name
# (e.g. "...aircraft N12345 diverted to...").
_TAIL_NUMBER_RE = re.compile(r"\bN[0-9]{1,5}[A-Z]{0,2}\b")

# A bare "lat,lon"-shaped decimal coordinate pair in narrative text, for
# the case where it isn't attached to a "latitude"/"longitude" key that
# _FIELD_VALUE_RE would already catch.
_COORD_PAIR_RE = re.compile(r"-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}")


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
    text = _TAIL_NUMBER_RE.sub(REDACTED, text)
    text = _COORD_PAIR_RE.sub(REDACTED, text)
    return text
