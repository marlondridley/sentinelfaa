"""
The alerting path, as files: Prometheus hands what fires to Alertmanager, Alertmanager routes it to Slack, and
no secret is in the repository.

These read the shipped configuration and pin the joins between files that nothing else checks: the address
Prometheus sends to is the service compose defines, every severity a rule carries is one the router knows, the
path the start-up script writes the webhook to is the path the config reads, and every alert has a runbook
for the message to point at. That the tools accept the files and that a real Alertmanager delivers to a
webhook is proved by .github/scripts/check_alerting.py, which CI runs.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

APP = Path(__file__).resolve().parent.parent

ROUTED_SEVERITIES = {"critical", "warning"}
HELD = {"Watchdog"}            # fires all the time on purpose; routed to a receiver that sends nothing


def _load(relative: str) -> dict:
    return yaml.safe_load((APP / relative).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def alertmanager() -> dict:
    return _load("monitoring/alertmanager.yml")


@pytest.fixture(scope="module")
def prometheus() -> dict:
    return _load("monitoring/prometheus.yml")


@pytest.fixture(scope="module")
def service() -> dict:
    return _load("docker-compose.yml")["services"]["alertmanager"]


def _alerts() -> list[dict]:
    return [rule for group in _load("monitoring/alert-rules.yml")["groups"]
            for rule in group["rules"] if "alert" in rule]


def _receivers(alertmanager: dict) -> dict[str, dict]:
    return {r["name"]: r for r in alertmanager["receivers"]}


def test_prometheus_sends_what_fires_to_the_alertmanager_service_compose_defines(prometheus, service):
    targets = [t for am in prometheus["alerting"]["alertmanagers"]
               for sc in am["static_configs"] for t in sc["targets"]]

    assert targets == ["alertmanager:9093"], "the compose service name and Alertmanager's default port"
    assert "alertmanager" in _load("docker-compose.yml")["services"]
    assert service["healthcheck"]["test"][1].count("9093") == 1


def test_prometheus_scrapes_alertmanager_so_a_failed_notification_is_visible(prometheus):
    jobs = {j["job_name"]: j for j in prometheus["scrape_configs"]}

    assert jobs["nas-alertmanager"]["static_configs"][0]["targets"] == ["alertmanager:9093"]


def test_the_rule_file_prometheus_loads_is_the_one_compose_mounts():
    prometheus = _load("docker-compose.yml")["services"]["prometheus"]

    assert "./monitoring/alert-rules.yml:/etc/prometheus/alert-rules.yml:ro" in prometheus["volumes"]
    assert "/etc/prometheus/alert-rules.yml" in _load("monitoring/prometheus.yml")["rule_files"]


def test_the_alertmanager_container_is_pinned_by_digest_and_hardened(service):
    assert re.fullmatch(r"prom/alertmanager:v[\d.]+@sha256:[0-9a-f]{64}", service["image"]), \
        "a tag can be re-pushed; the digest cannot"
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["read_only"] is True
    assert any(t.startswith("/tmp") for t in service["tmpfs"]), "where the webhook URL is written: memory only"
    assert "ports" not in service, "only the edge proxy publishes a port"


def test_the_edge_publishes_alertmanager_on_loopback_only_and_proxies_it():
    edge = _load("docker-compose.yml")["services"]["edge"]
    nginx = (APP / "docker/edge/nginx.conf").read_text(encoding="utf-8")

    assert "127.0.0.1:8448:8448" in edge["ports"]
    assert re.search(r"listen 8448 ssl;\s+location / \{ set \$upstream http://alertmanager:9093;", nginx)


def test_the_slack_webhook_is_never_in_the_repository(alertmanager, service):
    text = (APP / "monitoring/alertmanager.yml").read_text(encoding="utf-8")
    compose = (APP / "docker-compose.yml").read_text(encoding="utf-8")

    assert "hooks.slack.com" not in text + compose
    assert "xox" not in text + compose, "a Slack token"
    slack = _receivers(alertmanager)["slack"]["slack_configs"][0]
    assert "api_url" not in slack, "the URL itself; only a file is allowed"
    assert service["environment"]["SLACK_WEBHOOK_URL"] == "${SLACK_WEBHOOK_URL:-}"


def test_the_path_the_start_up_script_writes_is_the_path_the_config_reads(alertmanager, service):
    path = _receivers(alertmanager)["slack"]["slack_configs"][0]["api_url_file"]
    script = service["command"][0]

    assert script.count(f"> {path}") == 2, "written both when the variable is set and when it is not"
    assert "exec /bin/alertmanager" in script
    assert "--config.file=/etc/alertmanager/alertmanager.yml" in script
    assert "./monitoring/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro" in service["volumes"]


def test_the_start_up_script_says_so_when_there_is_no_webhook(service):
    script = service["command"][0]

    assert 'if [ -n "$$SLACK_WEBHOOK_URL" ]' in script
    assert "SLACK_WEBHOOK_URL is not set" in script


def test_every_receiver_a_route_names_exists(alertmanager):
    names = set(_receivers(alertmanager))
    used = {alertmanager["route"]["receiver"]}
    used |= {r["receiver"] for r in alertmanager["route"]["routes"] if "receiver" in r}

    assert used <= names


def test_the_heartbeat_is_routed_first_to_a_receiver_that_sends_nothing(alertmanager):
    first = alertmanager["route"]["routes"][0]

    assert first["matchers"] == ['alertname="Watchdog"'], "routes match in order; this must stay on top"
    assert not first.get("continue")
    assert _receivers(alertmanager)[first["receiver"]].keys() == {"name"}, "no integration: nothing is sent"


def test_every_alert_has_a_severity_the_router_handles():
    for rule in _alerts():
        severity = rule["labels"]["severity"]
        assert severity in ROUTED_SEVERITIES or rule["alert"] in HELD, \
            f"{rule['alert']}: severity {severity!r} is not routed; it would use the default route unnoticed"


def test_critical_alerts_are_reminded_at_least_as_often_as_the_rest(alertmanager):
    route = alertmanager["route"]
    critical = next(r for r in route["routes"] if r["matchers"] == ['severity="critical"'])

    assert _seconds(critical["repeat_interval"]) <= _seconds(route["repeat_interval"])
    assert _seconds(critical["group_wait"]) <= _seconds(route["group_wait"])


def test_every_alert_has_a_runbook_for_the_message_to_point_at():
    runbooks = (APP / "docs/runbooks.md").read_text(encoding="utf-8")
    headings = set(re.findall(r"^### (\w+)", runbooks, flags=re.M))

    missing = sorted(r["alert"] for r in _alerts() if r["alert"] not in headings)
    assert not missing, f"alerts with no runbook in docs/runbooks.md: {missing}"


def test_the_alerting_path_is_watched_by_the_rules_that_need_it():
    alerts = {r["alert"]: r for r in _alerts()}

    assert alerts["Watchdog"]["expr"] == "vector(1)"
    assert "alertmanager_notifications_failed_total" in alerts["AlertNotificationsFailing"]["expr"]
    assert "prometheus_notifications_errors_total" in alerts["AlertsNotReachingAlertmanager"]["expr"]


def _seconds(duration: str) -> int:
    value, unit = re.fullmatch(r"(\d+)([smh])", duration).groups()
    return int(value) * {"s": 1, "m": 60, "h": 3600}[unit]
