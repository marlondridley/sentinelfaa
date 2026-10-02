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
  (e.g. watch_officer_agent.py's context payload).
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

import json
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


def _redact_input_values(text: str) -> tuple[str, int]:
    out: list[str] = []
    i = 0
    count = 0
    while True:
        found = text.find(_INPUT_VALUE, i)
        if found == -1:
            out.append(text[i:])
            return "".join(out), count
        start = found + len(_INPUT_VALUE)
        out.append(text[i:found] + _INPUT_VALUE + REDACTED)
        count += 1
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
    return redact_text_counted(text)[0]


def redact_text_counted(text: str) -> tuple[str, int]:
    """
    redact_text() and how many substitutions it made: what the model-call record keeps as its
    redaction count, so a call that went out with nothing scrubbed is distinguishable from one
    that was scrubbed.
    """
    if not text:
        return text, 0
    n_fields = n_xml = 0

    def _field(m: re.Match[str]) -> str:
        nonlocal n_fields
        if _TOKEN_RE.fullmatch(m.group("value").strip("\"'")):
            return m.group(0)                       # a pseudonym chosen on purpose, not an identifier
        n_fields += 1
        return f"{m.group('prefix')}\"{REDACTED}\""

    def _xml(m: re.Match[str]) -> str:
        nonlocal n_xml
        if _TOKEN_RE.fullmatch(m.group("value").strip()):
            return m.group(0)
        n_xml += 1
        return f"<{m.group('tag')}{m.group('attrs') or ''}>{REDACTED}</{m.group('tag')}>"

    text = _FIELD_VALUE_RE.sub(_field, text)
    text = _XML_FIELD_RE.sub(_xml, text)
    text, n_input = _redact_input_values(text)
    text, n_tail = _TAIL_NUMBER_RE.subn(REDACTED, text)
    text, n_callsign = _CALLSIGN_RE.subn(REDACTED, text)
    text, n_coord = _COORD_PAIR_RE.subn(REDACTED, text)
    return text, n_fields + n_xml + n_input + n_tail + n_callsign + n_coord


def is_redacted(text: str) -> bool:
    """
    True when redact_text() would change nothing: there is nothing left in `text` that it removes.

    This says the scrub is stable, not that it found everything: it asks the same patterns that did the
    scrub. It is not an independent check; count_leaks() below is.
    """
    return redact_text(text) == text


# ── Pseudonyms: a model can tell two flights apart without learning which flights they are ────────────────
#
# redact_structured() drops every sensitive key, so a model shown two aircraft cannot say "the same aircraft
# lost track twice". pseudonymize_structured() keeps that distinction for the keys that name a flight or
# vehicle, as a token that means nothing outside one prompt, and still drops every measurement (a position,
# an altitude, a beacon code, a raw payload), which has no use as a name.

# The sensitive keys that identify a flight, aircraft or vehicle: these get a token. The rest of
# SENSITIVE_FIELDS (positions, altitudes, codes, raw payloads) are dropped.
IDENTITY_FIELDS: frozenset[str] = frozenset({
    "aircraft_id", "track_id", "callsign", "vehicle_id", "flight_id", "flightid", "acid", "gufi",
    "tail_number", "registration", "aircraftid", "acaddress", "tracknum",
})
_IDENTITY_LOWER = frozenset(f.lower() for f in IDENTITY_FIELDS)

# What a pseudonym looks like. redact_text() leaves a value of this shape alone, so the second pass over a
# serialized prompt does not blank out what the first pass chose to keep.
_TOKEN_RE = re.compile(r"AC-\d{1,6}")
_TOKEN_IN_TEXT_RE = re.compile(r"(?<![\w-])AC-\d{1,6}(?![\w-])")


class Pseudonymizer:
    """
    Stable tokens (AC-1, AC-2, ...) for the identifiers in ONE prompt.

    The same identifier gets the same token for as long as the instance lives, whatever its case or spacing, so
    a model can follow one flight through a prompt. Tokens are sequential and keep no key or hash of the value:
    an identifier is short and guessable, so a hash of it would be reversed by anyone who tried every candidate.
    Make one per prompt; a token must not mean the same flight in two prompts.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}      # normalised identifier -> token
        self._shown: dict[str, str] = {}       # token -> the identifier as first written

    def token(self, value: Any) -> str:
        written = " ".join(str(value).split())
        token = self._tokens.setdefault(written.upper(), f"AC-{len(self._tokens) + 1}")
        self._shown.setdefault(token, written)
        return token

    def reveal(self, text: str) -> str:
        """
        `text` with this prompt's tokens put back as the identifiers they stand for, for an answer that is
        shown to an authorised operator on this system and never sent anywhere. A token the model made up
        (AC-9 when only AC-1 to AC-3 were issued) has nothing to stand for and stays as it is.
        """
        if not self._shown or not text:
            return text
        return _TOKEN_IN_TEXT_RE.sub(lambda m: self._shown.get(m.group(0), m.group(0)), text)


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def pseudonymize_structured(obj: Any, pseudonyms: Pseudonymizer) -> Any:
    """
    redact_structured() that keeps who-is-who: an IDENTITY_FIELDS key keeps its place with a token for its
    value; every other SENSITIVE_FIELDS key is dropped. A sensitive key holding anything but a plain value
    (a nested object, a list) is dropped whole.
    """
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for key, value in obj.items():
            lowered = str(key).lower()
            if lowered in _IDENTITY_LOWER:
                if _is_scalar(value) and str(value).strip():
                    out[key] = pseudonyms.token(value)
            elif lowered not in _SENSITIVE_LOWER:
                out[key] = pseudonymize_structured(value, pseudonyms)
        return out
    if isinstance(obj, list):
        return [pseudonymize_structured(item, pseudonyms) for item in obj]
    return obj


# ── The leak check: look for the values themselves, not for what they look like ──────────────────────────
#
# Every pattern above is a guess about the shape of an identifier. The code that builds a prompt knows
# something better: the actual values it read from the record (the callsign, the registration, the
# coordinates). count_leaks() takes those and looks for them in what is about to be sent. It does not depend
# on a format anyone anticipated, so it finds what the patterns miss: a registration written G-ABCD, a
# callsign with a letter where the pattern expects a digit.
#
# It only verifies. If it also did the scrubbing it would ask the same question twice, which is the problem
# with is_redacted(). A hit refuses the call (policies/model-use.yaml), and the agent falls back to its rules.

# Shorter values are too common to mean anything, and a 4-digit code (a beacon code, 1200) cannot be told from
# a threshold in prose ("RVR below 1200 ft"). The keyed and pattern layers cover those; this one does not
# pretend to.
_MIN_VALUE_CHARS = 4
_MIN_NUMERIC_CHARS = 5
_MAX_VALUE_CHARS = 200       # an identifier is short; a longer value is a document, and a document is not a name
# A prompt names at most a few hundred values; the cap keeps the check bounded if a caller hands it a table.
MAX_PROTECTED_VALUES = 2000
_WORDS_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9\-_.]*[A-Za-z0-9])?")
_JSON_DEPTH = 12


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def collect_sensitive_values(*objs: Any) -> frozenset[str]:
    """
    The values held under a SENSITIVE_FIELDS key anywhere in `objs`, as the strings a prompt would show.

    Call it on the raw records BEFORE they are projected, scrubbed or pseudonymized: its answer is "what must
    not reach the model", and it has to be asked of the data that still has it. A raw payload carried as a JSON
    string is read too, since the identifiers inside it are the ones the keyed layer cannot see.
    """
    found: set[str] = set()

    def keep(value: Any) -> None:
        if isinstance(value, dict):
            for inner in value.values():
                keep(inner)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for inner in value:
                keep(inner)
        elif _is_scalar(value):
            text = str(value).strip()
            if text[:1] in ("{", "["):
                try:
                    parsed = json.loads(text)
                except ValueError:
                    pass
                else:
                    walk(parsed, 1)          # a document, not a name: what it holds under a sensitive key counts
                    return
            if (_MIN_VALUE_CHARS <= len(text) <= _MAX_VALUE_CHARS
                    and not (text.isdigit() and len(text) < _MIN_NUMERIC_CHARS)
                    and text != REDACTED and not _TOKEN_RE.fullmatch(text)):
                found.add(text)

    def walk(obj: Any, depth: int) -> None:
        if depth > _JSON_DEPTH:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                if str(key).lower() in _SENSITIVE_LOWER:
                    keep(value)
                else:
                    walk(value, depth + 1)
        elif isinstance(obj, (list, tuple, set, frozenset)):
            for item in obj:
                walk(item, depth + 1)

    for obj in objs:
        walk(obj, 0)
    return frozenset(found)


def count_leaks(text: str, protected: Any) -> int:
    """
    How many of the `protected` values appear in `text`. Never the values themselves: the answer is logged,
    audited and exported as a metric, and it must not carry what it found.

    A value is found when it stands alone (not inside a longer word), in any case, and an identifier is also
    found with its characters split by spaces or punctuation: AAL123 is found in "AAL 123" and "AAL-123", and
    G-ABCD in "GABCD". Numbers are compared as written, so a coordinate is found as -77.0364 or 77.0364 but
    not as a run of unrelated digits.
    """
    if not text or not protected:
        return 0
    values = sorted({v for v in protected if isinstance(v, str)}, key=lambda v: (-len(v), v))[:MAX_PROTECTED_VALUES]
    names: set[str] = set()      # has a letter: may be written split into words
    numbers: set[str] = set()    # digits only, with separators: compared whole
    for value in values:
        key = _normalise(value)
        if len(key) < _MIN_VALUE_CHARS:
            continue
        (names if any(c.isalpha() for c in key) else numbers).add(key)
    if not names and not numbers:
        return 0
    words = [_normalise(w) for w in _WORDS_RE.findall(text)]
    hits: set[str] = set()
    for i, word in enumerate(words):
        if word in names or word in numbers:
            hits.add(word)
        joined = word
        for width in (2, 3):                 # an identifier split across up to three words
            if i + width > len(words):
                break
            joined += words[i + width - 1]
            if joined in names:
                hits.add(joined)
    return len(hits)
