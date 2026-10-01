"""
Prometheus scrapes the dashboard and the Temporal server, not only the three Python services.

`ScrapeTargetDown` (up == 0) can only report a target that is in monitoring/prometheus.yml. The dashboard was not, so
it could be down with nothing saying so; and the Temporal server, which every pipeline and ingest run depends on,
exported no metrics at all.
"""
from __future__ import annotations

import re
import socket
import sys
import urllib.request
from pathlib import Path

import pytest
import yaml

from src import metrics
from src.config import settings

APP = Path(__file__).resolve().parent.parent
DASHBOARD = APP / "dashboard"
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))


def _prometheus() -> dict:
    return yaml.safe_load((APP / "monitoring" / "prometheus.yml").read_text(encoding="utf-8"))


def _compose() -> dict:
    return yaml.safe_load((APP / "docker-compose.yml").read_text(encoding="utf-8"))


def _targets() -> dict[str, list[str]]:
    return {job["job_name"]: [t for sc in job["static_configs"] for t in sc["targets"]]
            for job in _prometheus()["scrape_configs"]}


def test_the_dashboard_and_the_temporal_server_are_scrape_targets():
    targets = _targets()
    assert targets["nas-dashboard"] == [f"dashboard:{settings.METRICS_PORT}"]
    assert targets["nas-temporal"] == ["temporal:8000"]


def test_every_scrape_target_is_a_service_in_the_compose_file():
    services = set(_compose()["services"])
    for job, targets in _targets().items():
        for target in targets:
            host = target.rsplit(":", 1)[0]
            assert host == "localhost" or host in services, f"{job} scrapes {target}, which is not a service"


def test_the_temporal_server_serves_metrics_where_prometheus_looks_and_not_on_the_host():
    temporal = _compose()["services"]["temporal"]
    environment = dict(item.split("=", 1) for item in temporal["environment"])
    assert environment["PROMETHEUS_ENDPOINT"] == "0.0.0.0:8000"
    assert not any("8000" in str(port) for port in temporal.get("ports", [])), "only Prometheus, on the network, reads it"


def test_the_dashboard_does_not_move_its_metrics_port():
    """The scrape target names 9108; an override in the compose file would silently orphan it."""
    assert settings.METRICS_PORT == 9108
    text = (APP / "docker-compose.yml").read_text(encoding="utf-8")
    assert not re.search(r"(?m)^\s*METRICS_PORT:", text)


def test_the_down_alert_covers_every_job_so_the_new_targets_are_included():
    rules = yaml.safe_load((APP / "monitoring" / "alert-rules.yml").read_text(encoding="utf-8"))
    rule = next(r for g in rules["groups"] for r in g["rules"] if r.get("alert") == "ScrapeTargetDown")
    assert rule["expr"].replace(" ", "") == "up==0", "no job filter: a job added to prometheus.yml is covered"


# ── the dashboard process serves them ───────────────────────────────────────────

def test_the_container_starts_the_metrics_server_with_the_process_not_with_the_first_page():
    """A page-triggered server does not exist until someone visits, so an idle dashboard read as down."""
    command = _compose()["services"]["dashboard"]["command"]
    assert command.startswith("python -m dashboard.serve ") and "--server.address=0.0.0.0" in command


def test_serve_starts_the_metrics_server_then_runs_streamlit_with_the_same_arguments(monkeypatch):
    from streamlit.web import cli

    from dashboard import serve

    order: list = []
    monkeypatch.setattr(serve, "start_metrics_server", lambda **kw: order.append(("metrics", kw)))
    monkeypatch.setattr(cli, "main", lambda: order.append(("streamlit", list(sys.argv))))

    assert serve.main(["--server.address=0.0.0.0", "--server.headless=true"]) == 0

    assert order[0] == ("metrics", {"port": settings.METRICS_PORT}), "the metrics server comes first"
    assert order[1] == ("streamlit", ["streamlit", "run", "dashboard/app.py", "--server.address=0.0.0.0", "--server.headless=true"])


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def page_setup():
    pytest.importorskip("streamlit")
    from lib import page_setup as module
    return module


def test_a_dashboard_page_starts_the_metrics_server_before_anything_else(page_setup):
    import inspect

    source = inspect.getsource(page_setup.bootstrap_page).split('"""')[-1]      # the code, not the docstring
    assert source.index("_expose_metrics()") < source.index("st.set_page_config("), "set_page_config stays the first Streamlit call"


def test_the_dashboards_metrics_are_served(page_setup, monkeypatch):
    port = _free_port()
    monkeypatch.setattr(settings, "METRICS_PORT", port)
    monkeypatch.setattr(metrics, "_server_thread", None)

    page_setup._expose_metrics()
    page_setup._expose_metrics()           # a rerun, or the next page: nothing starts twice

    import time
    deadline = time.time() + 5
    body = ""
    while time.time() < deadline:
        try:
            body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=1).read().decode()
            break
        except OSError:
            time.sleep(0.1)
    assert "nas_llm_calls_total" in body


def test_a_metrics_server_that_cannot_start_does_not_stop_the_page(page_setup, monkeypatch):
    def boom(**kwargs):
        raise OSError("address already in use")

    monkeypatch.setattr(page_setup, "start_metrics_server", boom)
    page_setup._expose_metrics()           # logs, and returns
