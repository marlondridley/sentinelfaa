"""
/metrics takes no credential, and that is acceptable only while nothing outside the compose network can reach it.

Every application process serves Prometheus metrics with no authentication: the API mounts /metrics (src/api.py),
and the worker, the watcher and the dashboard run the metrics server in src/metrics.py. What keeps them private is
that none of those services publishes a port on the host, that the edge proxy routes the API only at /health and
/v1/, and that the public listeners reach the dashboard and Keycloak and nothing else. These tests hold that.

A failure here is not a bug to patch around. It means something is about to be reachable that was not, and the
next step is a credential on /metrics (a scrape credential from Vault, in monitoring/prometheus.yml), and only then
a change to these tests.

Not held here, because it is not true yet: the compose network is flat, so any container can still read any other's
/metrics. Segmentation (README.md, "Not done here") is what narrows that, and it is not done.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

INFRA = Path(__file__).resolve().parent.parent
APP = INFRA.parent / "nas-feedops-sentinel"
EDGE = APP / "docker" / "edge" / "nginx.conf"

needs_app = pytest.mark.skipif(not EDGE.is_file(), reason="needs the application stack beside instraclustr/")

APP_IMAGE = "nas-feedops-sentinel:latest"
API_EDGE_PORT = 8444
PUBLIC_EDGE_PORTS = (8450, 8451, 8452)           # reached only by the Cloudflare tunnel
PUBLIC_UPSTREAMS = {"dashboard", "keycloak"}


def _application_services() -> list[str]:
    services = yaml.safe_load((APP / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    return sorted(name for name, svc in services.items() if svc.get("image") == APP_IMAGE)


def _compose_service(name: str) -> dict:
    return yaml.safe_load((APP / "docker-compose.yml").read_text(encoding="utf-8"))["services"][name]


def _edge_servers() -> dict[int, list[tuple[str, str]]]:
    """The edge proxy's listeners: port -> [(location path, location body)]. Enough of nginx for this file's shape."""
    servers: dict[int, list[tuple[str, str]]] = {}
    for block in re.split(r"\n\s*server\s*\{", EDGE.read_text(encoding="utf-8"))[1:]:
        port = int(re.search(r"listen\s+(\d+)", block).group(1))
        servers[port] = [(re.sub(r"\s+", " ", path), body)
                         for path, body in re.findall(r"location\s+((?:=\s+)?\S+)\s*\{(.*?)\}", block, re.S)]
    return servers


def _upstreams(body: str) -> set[str]:
    return set(re.findall(r"set\s+\$upstream\s+https?://([\w.-]+)", body))


@needs_app
def test_the_application_services_are_the_ones_this_file_reasons_about():
    # If a service is added from the application image it takes the same unauthenticated /metrics, so it is
    # named here by being found, not by anyone remembering to.
    assert {"api", "sentinel", "temporal-worker", "dashboard", "migrate"} <= set(_application_services())


@needs_app
@pytest.mark.parametrize("name", _application_services() if EDGE.is_file() else [])
def test_no_application_service_publishes_a_port_on_the_host(name):
    assert "ports" not in _compose_service(name), (
        f"{name} publishes a host port, and it serves /metrics with no credential. Put a credential on /metrics "
        "first, then change this test.")


@needs_app
def test_the_edge_routes_the_api_only_at_health_and_v1():
    locations = _edge_servers()[API_EDGE_PORT]

    assert {path for path, body in locations if "proxy_pass" in body} == {"= /health", "/v1/"}
    assert any(path == "/" and "return 404" in body for path, body in locations), "and everything else is refused"


@needs_app
def test_no_edge_location_proxies_a_metrics_path():
    for port, locations in _edge_servers().items():
        for path, body in locations:
            assert not ("metrics" in path and "proxy_pass" in body), f"port {port} routes {path}"


@needs_app
@pytest.mark.parametrize("port", PUBLIC_EDGE_PORTS)
def test_the_public_listeners_reach_only_the_dashboard_and_keycloak(port):
    upstreams = set().union(*(_upstreams(body) for _, body in _edge_servers()[port]))

    assert upstreams <= PUBLIC_UPSTREAMS, f"port {port} reaches {sorted(upstreams - PUBLIC_UPSTREAMS)} from the internet"


@needs_app
def test_the_edge_parser_sees_the_listeners_it_is_asked_about():
    # A parser that finds nothing would pass every test above for the wrong reason.
    servers = _edge_servers()

    assert {API_EDGE_PORT, *PUBLIC_EDGE_PORTS} <= set(servers)
    assert _upstreams(" ".join(body for _, body in servers[API_EDGE_PORT])) == {"api"}
    assert _upstreams(" ".join(body for _, body in servers[8450])) == {"dashboard"}
