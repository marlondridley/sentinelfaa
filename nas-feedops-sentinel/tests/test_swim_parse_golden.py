"""
What the ingest parsers return, pinned.

src/swim_file_ingest.py was one 1,800-line module (maintainability index 1 out of 100) holding the file parsers,
every feed's extractor, the record normaliser, the claim-and-archive logic and the watcher. Splitting it is the
kind of change that looks safe and is not: an extractor has dozens of branches for the shapes a feed can send.

tests/fixtures/swim_parse_golden.json holds a fingerprint of what _parse_file and _normalise_record returned,
before the split, for every real message in tests/fixtures (TAIS, TDES, SMES in its three shapes, APDS, the
captured batch), for 30 seeded mutations of each (keys deleted, values of the wrong type, out-of-range numbers),
and for mutations of each parsed record. The same inputs must give the same fingerprints. If a change to the
parsers is meant to change an output, regenerate the file in the same commit:

    REGENERATE_GOLDEN=1 python -m pytest tests/test_swim_parse_golden.py
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.swim_file_ingest import _normalise_record, _parse_file
from tests.swim_parse_cases import files, record_variants

GOLDEN = Path(__file__).parent / "fixtures" / "swim_parse_golden.json"
RECEIVED = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:20]


def _event(record: dict, name: str, index: int):
    # The scan wraps this call and reports an exception as the record's reason, so what it raises is behaviour too.
    try:
        event, error = _normalise_record(record, name, index=index, received_at=RECEIVED)
    except Exception as exc:  # noqa: BLE001
        return {"raised": f"{type(exc).__name__}: {exc}"}
    return {"event": event.model_dump(mode="json") if event else None, "error": error}


def _snapshot(tmp_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for n, (name, (suffix, text)) in enumerate(files().items()):
        path = tmp_path / f"{1789000000000 + n}{suffix}"    # the Kit's epoch-millis name; no extension for Kit files
        path.write_text(text, encoding="utf-8")
        records, error = _parse_file(path)
        events = [_event(r, path.name, i) for i, r in enumerate(records)]
        out[name] = _fingerprint({"records": records, "error": error and error.replace(str(path), "<path>"),
                                  "events": events})
        if "#" not in name and records and isinstance(records[0], dict):   # the unmutated ones also get their records mutated
            for k, mutated in enumerate(record_variants(records[0], name)):
                out[f"{name}@record{k}"] = _fingerprint(_event(mutated, path.name, 0))
    return out


def test_the_inputs_cover_every_feed_every_format_and_a_real_spread_of_outcomes(tmp_path):
    cases = files()
    assert len(cases) >= 600
    assert {suffix for suffix, _ in cases.values()} == {"", ".json", ".txt", ".xml", ".swimmsg", ".csv"}
    services, outcomes = set(), {"records": 0, "error": 0}
    for n, (name, (suffix, text)) in enumerate(list(cases.items())[::5]):
        path = tmp_path / f"{1789000000000 + n}{suffix}"
        path.write_text(text, encoding="utf-8")
        records, error = _parse_file(path)
        outcomes["records" if records else "error"] += 1
        services |= {r.get("service_name") for r in records if isinstance(r, dict)}
    assert {"TOWER_DEPARTURE", "APDS_RVR", "TERMINAL_AUTO"} <= services, services
    assert outcomes["records"] > 40 and outcomes["error"] > 10, outcomes


def test_the_parsers_return_what_they_returned_before_the_module_was_split(tmp_path):
    snapshot = _snapshot(tmp_path)
    if os.environ.get("REGENERATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        pytest.skip("golden file regenerated")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    assert snapshot.keys() == golden.keys()
    changed = sorted(name for name in snapshot if snapshot[name] != golden[name])
    assert not changed, f"{len(changed)} input(s) now parse differently, e.g. {changed[:5]}"


def test_the_fingerprints_are_the_same_twice_so_the_comparison_is_not_noise(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir(), second.mkdir()
    assert _snapshot(first) == _snapshot(second)
