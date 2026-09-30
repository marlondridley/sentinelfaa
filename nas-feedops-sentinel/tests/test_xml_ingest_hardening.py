"""
.xml feed files are parsed with defusedxml (src/swim_parse.py): their
content comes from an external broker, so a DTD, an entity declaration or an
external reference is refused -- the file goes to quarantine with that reason
-- rather than left to the stdlib parser's defaults to bound. Found by the
Semgrep gate (.github/workflows/guardrails.yml).
"""
from __future__ import annotations

from src.swim_file_ingest import _parse_file


def test_ordinary_feed_xml_still_parses(tmp_path):
    feed = tmp_path / "capture.xml"
    feed.write_text("<message><flightId>AAL123</flightId><airport>KDCA</airport></message>", encoding="utf-8")

    records, error = _parse_file(feed)

    assert error is None and records


def test_entity_expansion_is_refused_not_expanded(tmp_path):
    feed = tmp_path / "laughs.xml"
    feed.write_text(
        '<?xml version="1.0"?>'
        '<!DOCTYPE message [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'
        "<message><flightId>&c;</flightId></message>", encoding="utf-8")

    records, error = _parse_file(feed)

    assert records == []
    assert error and "DTDForbidden" in error


def test_an_external_entity_is_never_read(tmp_path):
    secret = tmp_path / "host-secret.txt"
    secret.write_text("TOP-SECRET-HOST-FILE", encoding="utf-8")
    feed = tmp_path / "xxe.xml"
    feed.write_text(
        f'<?xml version="1.0"?><!DOCTYPE message [<!ENTITY x SYSTEM "file://{secret.as_posix()}">]>'
        "<message><flightId>&x;</flightId></message>", encoding="utf-8")

    records, error = _parse_file(feed)

    assert records == [] and error and "DTDForbidden" in error
    assert "TOP-SECRET-HOST-FILE" not in repr(records) + error


def test_a_hostile_feed_file_is_quarantined_with_the_reason_and_the_scan_goes_on(tmp_path, monkeypatch, caplog):
    """The real folder scan: the file lands in failed/, not processed/, and the log says why."""
    import src.swim_file_ingest as ingest

    monkeypatch.setattr(ingest, "_get_already_processed", lambda names: set())
    monkeypatch.setattr(ingest, "_append_log_entries", lambda entries: None)
    inbox, processed, failed = tmp_path / "log", tmp_path / "processed", tmp_path / "failed"
    inbox.mkdir()
    (inbox / "laughs.xml").write_text(
        '<?xml version="1.0"?><!DOCTYPE m [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
        "<message>&b;</message>", encoding="utf-8")

    with caplog.at_level("WARNING", logger="src.swim_file_ingest"):
        events, stats = ingest.scan_swim_folder(inbox, processed_dir=processed, failed_dir=failed)

    assert events == [] and stats.files_failed == 1
    assert [p.name for p in failed.iterdir()] == ["laughs.xml"]
    assert not processed.exists() or list(processed.iterdir()) == []
    assert [p for p in inbox.iterdir() if p.is_file()] == [], "nothing left behind to retry forever"
    reasons = [getattr(r, "reason", "") for r in caplog.records if r.getMessage() == "swim_ingest_file_failed"]
    assert reasons and "DTDForbidden" in reasons[0]
