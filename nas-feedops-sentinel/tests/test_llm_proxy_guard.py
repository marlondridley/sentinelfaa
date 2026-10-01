"""
A deployed container must not be aimed at a loopback LLM proxy.

Inside a container "localhost" is the container itself, so a LITELLM_PROXY_URL (or LLM_BASE_URL) of
localhost:4000 reaches nothing: every model call fails and the Critic and Watch Officer fall back to
their deterministic output without anyone noticing. docker-compose.yml sets REQUIRE_REMOTE_LLM_PROXY so
that mistake stops the service at start. The guard is opt-in: the test image is a container, and a
laptop run talks to a proxy on localhost on purpose.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import src.config


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("LITELLM_PROXY_URL", "LLM_BASE_URL", "REQUIRE_REMOTE_LLM_PROXY"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("url", [
    "http://localhost:4000/v1", "http://127.0.0.1:4000/v1", "http://[::1]:4000/v1",
    "http://0.0.0.0:4000/v1", "http://127.1.2.3/v1", "localhost:4000/v1", "http://LOCALHOST:4000/v1",
])
def test_guard_rejects_a_loopback_proxy(monkeypatch, url):
    monkeypatch.setenv("LLM_BASE_URL", url)
    monkeypatch.setenv("REQUIRE_REMOTE_LLM_PROXY", "1")
    with pytest.raises(ValidationError, match="loopback"):
        src.config.Settings()


def test_guard_rejects_the_default_url_when_nothing_is_set(monkeypatch):
    monkeypatch.setenv("REQUIRE_REMOTE_LLM_PROXY", "true")
    with pytest.raises(ValidationError, match="loopback"):
        src.config.Settings()


def test_guard_accepts_the_gateway(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://nas-litellm:4000/v1")
    monkeypatch.setenv("REQUIRE_REMOTE_LLM_PROXY", "1")
    assert src.config.Settings().LITELLM_PROXY_URL == "http://nas-litellm:4000/v1"


def test_guard_is_off_by_default(monkeypatch):
    """The test image and a laptop run keep the localhost default."""
    assert src.config.Settings().LITELLM_PROXY_URL.startswith("http://localhost")


@pytest.mark.parametrize("value", ["0", "false", ""])
def test_guard_can_be_turned_off_explicitly(monkeypatch, value):
    monkeypatch.setenv("REQUIRE_REMOTE_LLM_PROXY", value)
    assert src.config.Settings().REQUIRE_REMOTE_LLM_PROXY is False


def test_compose_turns_the_guard_on_for_the_application_services():
    compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'REQUIRE_REMOTE_LLM_PROXY: "1"' in compose
