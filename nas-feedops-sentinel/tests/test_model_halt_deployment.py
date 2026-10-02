"""
The halt's settings reach the containers.

The automatic trip is optional and off by default, so the only way to turn it on in a deployment is a variable in
.env. A setting that compose does not forward to the services cannot be set that way: the switch exists in the
code and the register and can never be used. Each setting must be forwarded, with the default the code has, and be
documented where an operator looks.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from src.config import Settings

APP = Path(__file__).resolve().parent.parent
SETTINGS = ("MODEL_HALT_CACHE_SECONDS", "MODEL_AUTO_HALT_ENABLED", "MODEL_AUTO_HALT_THRESHOLD",
            "MODEL_AUTO_HALT_WINDOW_SECONDS")
# The services that call a model, and so read the halt state before each call.
MODEL_CALLERS = ("sentinel", "temporal-worker", "dashboard")

COMPOSE = yaml.safe_load((APP / "docker-compose.yml").read_text(encoding="utf-8"))["services"]


def _forwarded(service: str) -> dict:
    environment = COMPOSE[service].get("environment") or {}
    return environment if isinstance(environment, dict) else dict(item.split("=", 1) for item in environment)


@pytest.mark.parametrize("service", MODEL_CALLERS)
@pytest.mark.parametrize("name", SETTINGS)
def test_a_halt_setting_is_forwarded_to_every_service_that_calls_a_model(name, service):
    assert name in Settings.model_fields, f"{name} is no longer a setting"
    assert name in _forwarded(service), f"compose does not pass {name} to {service}, so .env cannot set it"


@pytest.mark.parametrize("name", SETTINGS)
def test_the_default_compose_gives_a_halt_setting_is_the_one_the_code_has(name):
    expression = str(_forwarded("sentinel")[name])
    default = re.fullmatch(r"\$\{" + name + r":-([^}]*)\}", expression)
    assert default, f"expected ${{{name}:-default}}, compose has {expression!r}"

    assert getattr(Settings(**{name: default.group(1)}), name) == Settings.model_fields[name].default


def test_the_automatic_trip_is_off_in_a_deployment_that_does_not_ask_for_it():
    default = re.fullmatch(r"\$\{MODEL_AUTO_HALT_ENABLED:-([^}]*)\}", str(_forwarded("sentinel")["MODEL_AUTO_HALT_ENABLED"]))
    assert default and Settings(MODEL_AUTO_HALT_ENABLED=default.group(1)).MODEL_AUTO_HALT_ENABLED is False


@pytest.mark.parametrize("name", SETTINGS)
def test_a_halt_setting_is_documented_in_the_example_env(name):
    lines = (APP / ".env.example").read_text(encoding="utf-8").splitlines()
    assert any(line.startswith(f"{name}=") for line in lines), f"{name} is not in .env.example"
