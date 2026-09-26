"""/v1/ready probes every dependency /ingest needs, and a hung one cannot hang the probe."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import src.api as api_module


async def _ok() -> None:
    return None


async def _down() -> None:
    raise ConnectionError("unreachable")


async def _hangs() -> None:
    await asyncio.sleep(60)


@pytest.fixture
def client(monkeypatch):
    for name in list(api_module._READINESS_CHECKS):
        monkeypatch.setitem(api_module._READINESS_CHECKS, name, _ok)
    return TestClient(api_module.app)


def test_ready_when_every_dependency_answers(client):
    resp = client.get("/v1/ready")
    assert resp.status_code == 200
    assert resp.json()["checks"] == {"storage": "ok", "temporal": "ok", "model_proxy": "ok"}


@pytest.mark.parametrize("dependency", ["storage", "temporal", "model_proxy"])
def test_one_failing_dependency_makes_the_api_not_ready(client, monkeypatch, dependency):
    monkeypatch.setitem(api_module._READINESS_CHECKS, dependency, _down)
    resp = client.get("/v1/ready")
    assert resp.status_code == 503
    assert [name for name, state in resp.json()["checks"].items() if state == "error"] == [dependency]


def test_a_hung_dependency_is_bounded_by_the_timeout(client, monkeypatch):
    monkeypatch.setattr(api_module, "READINESS_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setitem(api_module._READINESS_CHECKS, "temporal", _hangs)
    resp = client.get("/v1/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["temporal"] == "error"


def test_model_proxy_probe_uses_the_unauthenticated_readiness_route():
    assert (api_module.model_proxy_readiness_url("http://nas-litellm:4000/v1")
            == "http://nas-litellm:4000/health/readiness")


async def test_temporal_probe_fails_when_the_service_reports_unavailable(monkeypatch):
    class _Service:
        async def check_health(self) -> bool:
            return False

    class _Client:
        service_client = _Service()

    async def _client() -> _Client:
        return _Client()

    monkeypatch.setattr(api_module, "get_or_create_client", _client)
    with pytest.raises(RuntimeError, match="unavailable"):
        await api_module._check_temporal()
