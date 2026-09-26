"""
What operators see about ingest and the review queue.

The Ingest page counted every file in the Kit folder as pending (the folder's
.gitkeep, the Kit's own messages.log) and showed no reason for 10,086 failed
files; the review-queue depth gauge read 0 beside a 20-hour-old pending
review; and the Prometheus console opened blank. These pin the fixes.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from src import storage
from src.swim_file_ingest import message_files

APP = Path(__file__).resolve().parent.parent


# ── Pending means a message a scan would take ─────────────────────────────────

def test_only_kit_messages_count_as_pending(tmp_path: Path):
    for name in ("1790378386954", "capture.json", "capture.xml", "note.txt", ".gitkeep", "messages.log"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / ".inflight").mkdir()
    assert sorted(p.name for p in message_files(tmp_path)) == [
        "1790378386954", "capture.json", "capture.xml", "note.txt"]


# ── Why files failed ──────────────────────────────────────────────────────────

@pytest.fixture
def db():
    storage.override_db_path(":memory:")
    yield storage
    storage.override_db_path(":memory:")


async def test_failures_are_grouped_by_reason_most_frequent_first(db):
    tais = "Unrecognised STDDS payload shape (mex=TAIS, msgType=ST): root=ns2:TAStatus"
    await db.record_ingest_log_entries(
        [{"file": f"17903783869{i:02d}", "status": "failed", "error": f"{tais} in 17903783869{i:02d}",
          "timestamp": "2026-09-26T10:38:00+00:00"} for i in range(3)]
        + [{"file": "1790378386999", "status": "failed", "error": "Unexpected UTF-8 BOM",
            "timestamp": "2026-09-26T10:40:00+00:00"},
           {"file": "1790378387000", "status": "processed", "event_count": 4,
            "timestamp": "2026-09-26T10:41:00+00:00"}])

    reasons = await db.get_ingest_failure_reasons(limit=10)

    assert [(r["reason"], r["files"]) for r in reasons] == [
        (f"{tais} in N", 3), ("Unexpected UTF-8 BOM", 1)], "file numbers fold into one reason; processed rows are not failures"
    assert reasons[1]["last_seen"].isoformat().startswith("2026-09-26T10:40")


# ── The review-queue gauges agree ─────────────────────────────────────────────

async def test_the_reconciler_sets_queue_depth_with_the_oldest_wait(db):
    from datetime import datetime, timedelta, timezone

    from src.metrics import HITL_OLDEST_PENDING_SECONDS, HITL_QUEUE_DEPTH
    from src.models import AlertEvent
    from src.reconcile import reconcile_unstaged

    HITL_QUEUE_DEPTH.set(0)
    alerts = [AlertEvent(alert_type="ALERT_FEED_DISCONNECTED", severity="CRITICAL", airport_code=code,
                         service_name="ISMC", description="gauge test",
                         triggered_at=datetime.now(timezone.utc) - timedelta(minutes=5)) for code in ("KDCA", "KIAD")]
    await db.save_alerts(alerts)
    await reconcile_unstaged()

    assert HITL_QUEUE_DEPTH._value.get() == len(await db.get_pending_staged_actions()) == 2
    assert HITL_OLDEST_PENDING_SECONDS._value.get() >= 0


# ── The Prometheus console opens on five questions ────────────────────────────

def test_the_prometheus_console_opens_with_five_queries_on_defined_metrics():
    pytest.importorskip("streamlit", reason="page_setup renders through Streamlit")
    dashboard = str(APP / "dashboard")
    if dashboard not in sys.path:
        sys.path.insert(0, dashboard)
    from lib import page_setup

    url = urlsplit(page_setup.prometheus_queries_url("https://localhost:8447"))
    params = parse_qs(url.query)
    assert url.path == "/query"
    assert [params[f"g{i}.expr"][0] for i in range(5)] == list(page_setup.PROMETHEUS_QUERIES)
    assert "g5.expr" not in params
    defined = (APP / "src" / "metrics.py").read_text(encoding="utf-8")
    for expr in page_setup.PROMETHEUS_QUERIES:
        for metric in re.findall(r"\bnas_[a-z_]+", expr):
            assert f'"{metric}"' in defined, f"{metric} is not a metric src/metrics.py defines"
