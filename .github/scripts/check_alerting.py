#!/usr/bin/env python3
"""
Proves the alerting path works, with the real tools and the shipped settings.

  1. promtool checks prometheus.yml and alert-rules.yml (syntax, and every rule's expression).
  2. amtool checks alertmanager.yml and routes sample alerts: a critical and a warning alert go to the
     slack receiver, the Watchdog heartbeat to the one that sends nothing.
  3. A real Alertmanager is started the way docker-compose.yml starts it (same image, entrypoint, command,
     read-only root, no capabilities). A real Prometheus, loaded with the shipped config and rules, must hand
     it the Watchdog heartbeat under the service name prometheus.yml points at. Given a critical alert, a
     stand-in for Slack must receive it with the right title and colour, and must NOT receive the heartbeat.
  4. Started without SLACK_WEBHOOK_URL it still comes up healthy and says so, instead of crash-looping.
  5. The morning and evening briefs: promtool unit-tests the rules that write them (monitoring/tests), and a real
     Alertmanager sends one through the shipped route and receiver when its window is open and nothing when it
     is closed. The windows are rewritten to "all day" and "six hours from now" for the test, since the shipped
     ones are ten minutes a day; amtool check-config has already loaded the shipped ones and their time zone.

The image references and the start-up script are read from nas-feedops-sentinel/docker-compose.yml, so this
cannot pass against something other than what ships. Needs Docker and PyYAML.

    python .github/scripts/check_alerting.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "nas-feedops-sentinel"
MONITORING = APP / "monitoring"

# Stands in for Slack's incoming webhook: prints each POST body on one line.
CATCHER = """
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        print("POST " + self.path + " " + body.replace("\\n", " "), flush=True)
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *args): pass
http.server.HTTPServer(("0.0.0.0", 8080), H).serve_forever()
"""

CATCHER_IMAGE = "python:3.12-slim"


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise SystemExit(f"FAIL: docker {' '.join(args[:3])} ...\n{proc.stdout}\n{proc.stderr}")
    return proc


def ok(message: str) -> None:
    print(f"ok   {message}")


def fail(message: str) -> None:
    raise SystemExit(f"FAIL {message}")


def shipped() -> dict:
    services = yaml.safe_load((APP / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    am = services["alertmanager"]
    return {
        "prometheus_image": services["prometheus"]["image"],
        "alertmanager_image": am["image"],
        "entrypoint": am["entrypoint"],
        # Compose writes $$ for a literal $; the shell must see $.
        "command": [part.replace("$$", "$") for part in am["command"]],
    }


def check_prometheus(image: str) -> None:
    mounts = ["-v", f"{MONITORING / 'prometheus.yml'}:/etc/prometheus/prometheus.yml:ro",
              "-v", f"{MONITORING / 'alert-rules.yml'}:/etc/prometheus/alert-rules.yml:ro"]
    out = docker("run", "--rm", "--entrypoint", "promtool", *mounts, image,
                 "check", "config", "/etc/prometheus/prometheus.yml").stdout
    if "alert-rules.yml" not in out or "rules found" not in out:
        fail(f"promtool did not load the rule file:\n{out}")
    ok("promtool: prometheus.yml and alert-rules.yml are valid, and the rule file is loaded")


def check_brief_rules(image: str) -> None:
    """promtool runs monitoring/tests/briefs.test.yml: the figures and the text of both briefs, from known series."""
    proc = docker("run", "--rm", "--entrypoint", "promtool", "-v", f"{MONITORING}:/etc/prometheus:ro", image,
                  "test", "rules", "/etc/prometheus/tests/briefs.test.yml", check=False)
    if proc.returncode != 0 or "SUCCESS" not in proc.stdout + proc.stderr:
        fail(f"the brief rules do not produce the expected brief:\n{proc.stdout}\n{proc.stderr}")
    ok("promtool: the brief rules produce the expected text from known series (data, no data, halted)")


ROUTES = (  # (labels, receiver it must reach)
    ("alertname=ScrapeTargetDown severity=critical job=nas-sentinel", "slack"),
    ("alertname=CriticalAlertsUnstaged severity=critical", "slack"),
    ("alertname=AirportFeedDegraded severity=warning airport=KIAD", "slack"),
    ("alertname=LLMErrorRateHigh severity=warning", "slack"),
    ("alertname=Watchdog severity=none", "null"),
    ("alertname=MorningBrief severity=brief", "slack-brief"),
    ("alertname=EveningBrief severity=brief", "slack-brief"),
)


def check_routing(image: str) -> None:
    cfg = "/etc/alertmanager/alertmanager.yml"
    script = f"touch /tmp/slack_api_url && amtool check-config {cfg}"
    for labels, receiver in ROUTES:
        script += f" && amtool config routes test --config.file={cfg} --verify.receivers={receiver} {labels}"
    out = docker("run", "--rm", "--entrypoint", "sh", "-v", f"{MONITORING / 'alertmanager.yml'}:{cfg}:ro",
                 image, "-c", script).stdout
    if "SUCCESS" not in out:
        fail(f"amtool did not report the config valid:\n{out}")
    ok(f"amtool: alertmanager.yml is valid; {len(ROUTES)} sample alerts route to the expected receiver")


def start_alertmanager(ship: dict, network: str, name: str, webhook: str, alias: str | None = None,
                       config: Path | None = None) -> None:
    """Start it as compose does: its entrypoint and command, a read-only root, nothing added.

    `config` replaces the shipped alertmanager.yml, for a test that must change one thing in it.
    """
    docker("run", "-d", "--name", name, "--network", network,
           *(["--network-alias", alias] if alias else []),
           "--read-only", "--tmpfs", "/tmp:size=64m", "--cap-drop", "ALL",
           "--security-opt", "no-new-privileges:true",
           "-e", f"SLACK_WEBHOOK_URL={webhook}",
           "-v", f"{config or MONITORING / 'alertmanager.yml'}:/etc/alertmanager/alertmanager.yml:ro",
           "-v", "/alertmanager",
           "--entrypoint", ship["entrypoint"][0], ship["alertmanager_image"],
           *ship["entrypoint"][1:], *ship["command"])


def wait_healthy(name: str, seconds: int = 40) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if docker("exec", name, "wget", "-q", "-O", "/dev/null", "http://localhost:9093/-/healthy",
                  check=False).returncode == 0:
            return
        time.sleep(1)
    fail(f"{name} did not become healthy:\n{docker('logs', name, check=False).stdout}"
         f"{docker('logs', name, check=False).stderr}")


def fire(name: str, *labels: str, summary: str, description: str) -> None:
    docker("exec", name, "amtool", "--alertmanager.url=http://localhost:9093", "alert", "add", *labels,
           f"--annotation=summary={summary}", f"--annotation=description={description}")


def posts(catcher: str) -> list[dict]:
    lines = (docker("logs", catcher, check=False).stdout or "").splitlines()
    return [json.loads(line.split(" ", 2)[2]) for line in lines if line.startswith("POST ")]


def check_prometheus_hands_over(ship: dict, network: str, suffix: str, am: str) -> None:
    """The real Prometheus, with the shipped config and rules, must hand its heartbeat to this Alertmanager.

    The Alertmanager is reachable as `alertmanager`, the compose service name prometheus.yml points at, so this
    proves that address and the `alerting:` block, which no test of either file alone can.
    """
    name = f"alerting-prom-{suffix}"
    docker("run", "-d", "--name", name, "--network", network, "--tmpfs", "/prometheus:mode=1777",
           "-v", f"{MONITORING / 'prometheus.yml'}:/etc/prometheus/prometheus.yml:ro",
           "-v", f"{MONITORING / 'alert-rules.yml'}:/etc/prometheus/alert-rules.yml:ro",
           ship["prometheus_image"], "--config.file=/etc/prometheus/prometheus.yml", "--storage.tsdb.path=/prometheus")
    deadline = time.time() + 75           # rules are evaluated every 15 s
    while time.time() < deadline:
        out = docker("exec", am, "wget", "-q", "-O", "-", "http://localhost:9093/api/v2/alerts", check=False).stdout
        try:
            alerts = json.loads(out) if out.strip() else []
        except ValueError:
            alerts = []
        # Only Prometheus sets a generatorURL; the alerts this script adds by hand do not.
        if any(a["labels"].get("alertname") == "Watchdog" and "/graph" in a.get("generatorURL", "") for a in alerts):
            ok("prometheus: the shipped config and rules hand the Watchdog heartbeat to Alertmanager")
            return
        time.sleep(2)
    fail(f"Prometheus never handed an alert to Alertmanager:\n{docker('logs', name, check=False).stderr[-2000:]}")


def check_delivery(ship: dict, network: str, suffix: str) -> None:
    catcher, am = f"alerting-catcher-{suffix}", f"alerting-am-{suffix}"
    docker("run", "-d", "--name", catcher, "--network", network, CATCHER_IMAGE, "python", "-c", CATCHER)
    start_alertmanager(ship, network, am, f"http://{catcher}:8080/hook", alias="alertmanager")
    wait_healthy(am)
    check_prometheus_hands_over(ship, network, suffix, am)

    fired_at = time.time()
    fire(am, "alertname=Watchdog", "severity=none",
         summary="Alerting heartbeat: this always fires", description="held")
    fire(am, "alertname=ScrapeTargetDown", "severity=critical", "job=nas-sentinel",
         summary="nas-sentinel is not answering Prometheus", description="docker ps and the service's logs")

    deadline = time.time() + 45            # the critical route waits 10 s before the first message
    got: list[dict] = []
    while time.time() < deadline and not got:
        time.sleep(1)
        got = posts(catcher)
    if not got:
        fail(f"no message reached the stand-in for Slack within 45 s:\n{docker('logs', am, check=False).stderr}")

    # A heartbeat that fell through to the default route would wait group_wait (30 s) before it was sent, so
    # "nothing yet" proves nothing until that has passed.
    time.sleep(max(0.0, fired_at + 40 - time.time()))
    got = posts(catcher)
    attachments = [a for post in got for a in post.get("attachments", [])]
    titles = [a.get("title", "") for a in attachments]
    if not any("CRITICAL" in t and "ScrapeTargetDown" in t for t in titles):
        fail(f"the critical alert was not delivered with its title: {titles}")
    critical = next(a for a in attachments if "ScrapeTargetDown" in a.get("title", ""))
    if critical.get("color") != "danger":
        fail(f"a critical alert is not red: {critical.get('color')}")
    if "nas-sentinel is not answering Prometheus" not in critical.get("text", ""):
        fail(f"the alert's summary is missing from the message: {critical.get('text')}")
    if "Runbook:" not in critical.get("text", ""):
        fail("the message does not point to a runbook")
    if any("Watchdog" in t for t in titles):
        fail(f"the heartbeat was sent to Slack: {titles}")
    ok("delivery: the critical alert reached the webhook (title, red, summary, runbook); the heartbeat did not")


def brief_config(directory: Path, start: str, end: str) -> Path:
    """The shipped alertmanager.yml with both brief windows set to start-end UTC, and nothing else changed."""
    cfg = yaml.safe_load((MONITORING / "alertmanager.yml").read_text(encoding="utf-8"))
    names = {"morning-brief-window", "evening-brief-window"}
    windows = [i for i in cfg["time_intervals"] if i["name"] in names]
    if len(windows) != 2:
        fail(f"expected the morning and evening brief windows in alertmanager.yml, found {len(windows)}")
    for window in windows:
        window["time_intervals"] = [{"times": [{"start_time": start, "end_time": end}], "location": "UTC"}]
    path = directory / f"alertmanager-{start.replace(':', '')}.yml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


BRIEF_TEXT = "Alerts raised: 14 (was 9), CRITICAL 3 (was 1)\nMonitoring: 0 scrape target(s) down"


def check_briefs(ship: dict, network: str, suffix: str) -> None:
    """A brief goes out in its window as a neutral message with its text; outside the window, nothing."""
    later = datetime.now(timezone.utc) + timedelta(hours=6)           # a ten-minute window six hours from now
    if later.date() != datetime.now(timezone.utc).date():            # never across midnight: HH:MM has no date
        later -= timedelta(hours=12)
    closed = (later.strftime("%H:%M"), (later + timedelta(minutes=10)).strftime("%H:%M"))
    with tempfile.TemporaryDirectory() as tmp:
        runs = {}
        for label, (start, end) in (("open", ("00:00", "24:00")), ("closed", closed)):
            cfg = brief_config(Path(tmp), start, end)
            catcher, am = f"alerting-brief-{label}-catcher-{suffix}", f"alerting-brief-{label}-am-{suffix}"
            docker("run", "-d", "--name", catcher, "--network", network, CATCHER_IMAGE, "python", "-c", CATCHER)
            start_alertmanager(ship, network, am, f"http://{catcher}:8080/hook", config=cfg)
            runs[label] = (catcher, am)
        for _, am in runs.values():
            wait_healthy(am)
        for _, am in runs.values():
            fire(am, "alertname=MorningBrief", "severity=brief",
                 summary="Morning brief: the last 12 hours", description=BRIEF_TEXT)

        deadline = time.time() + 40                 # the brief route waits 10 s before the first message
        got: list[dict] = []
        while time.time() < deadline and not got:
            time.sleep(1)
            got = posts(runs["open"][0])
        if not got:
            fail(f"a brief did not reach the webhook inside its window:\n"
                 f"{docker('logs', runs['open'][1], check=False).stderr}")
        attachment = got[0]["attachments"][0]
        if attachment.get("title") != "Morning brief: the last 12 hours":
            fail(f"the brief has the wrong title: {attachment.get('title')!r}")
        if attachment.get("color") != "#439FE0":
            fail(f"the brief is not the neutral colour: {attachment.get('color')!r}")
        for line in BRIEF_TEXT.splitlines():
            if line not in attachment.get("text", ""):
                fail(f"the brief's text is missing {line!r}: {attachment.get('text')!r}")
        if "Runbook:" in attachment.get("text", ""):
            fail("a brief went out through the alert receiver, with a runbook line")
        ok("briefs: inside its window a brief reaches the webhook with its title, its text and the neutral colour")

        # Same alert, same config but for the window: well past the route's 10 s group_wait, still nothing.
        time.sleep(25)
        closed_posts = posts(runs["closed"][0])
        if closed_posts:
            fail(f"a brief was sent outside its window: {closed_posts}")
        ok(f"briefs: outside its window ({closed[0]}-{closed[1]} UTC) nothing is sent")


def check_unconfigured(ship: dict, network: str, suffix: str) -> None:
    name = f"alerting-unset-{suffix}"
    start_alertmanager(ship, network, name, "")
    wait_healthy(name)
    logs = docker("logs", name, check=False)
    if "SLACK_WEBHOOK_URL is not set" not in logs.stdout + logs.stderr:
        fail("without a webhook it started but did not say so")
    ok("no webhook configured: it still starts healthy and says nothing is sent")


def main() -> None:
    ship = shipped()
    check_prometheus(ship["prometheus_image"])
    check_brief_rules(ship["prometheus_image"])
    check_routing(ship["alertmanager_image"])

    suffix = uuid.uuid4().hex[:8]
    network = f"alerting-smoke-{suffix}"
    docker("network", "create", network)
    try:
        check_delivery(ship, network, suffix)
        check_briefs(ship, network, suffix)
        check_unconfigured(ship, network, suffix)
    finally:
        names = docker("ps", "-aq", "--filter", f"name=alerting-.*-{suffix}").stdout.split()
        if names:
            docker("rm", "-f", *names, check=False)
        docker("network", "rm", network, check=False)
    print("alerting: all checks passed")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        if exc.code not in (0, None):
            print(exc.code, file=sys.stderr)
        sys.exit(1 if exc.code not in (0, None) else 0)
