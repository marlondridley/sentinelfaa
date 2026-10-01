"""
The gateway serves Claude Haiku and nothing else.

Decided 2026-09-30: one provider, one model, no fallback. A model behind the gateway is a place NAS-derived prompts
go, so adding one is a reviewed change, and this test is the review gate: it fails on a new provider, a second
deployment, a fallback chain, or a provider key that has no business in the stack, until it is edited deliberately
in the same change.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

INFRA = Path(__file__).resolve().parent.parent
APP = INFRA.parent / "nas-feedops-sentinel"

# The one model the gateway may serve. Changing it is a decision, made here.
ALLOWED_MODELS = {"anthropic/claude-haiku-4-5-20251001"}
ALLOWED_ALIASES = {"primary-model"}


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load((INFRA / "litellm-config.yaml").read_text(encoding="utf-8"))


def test_every_deployment_is_the_allowed_model(config):
    deployed = {entry["litellm_params"]["model"] for entry in config["model_list"]}
    assert deployed <= ALLOWED_MODELS, f"not allowed: {sorted(deployed - ALLOWED_MODELS)}"
    assert deployed, "the gateway serves something"


def test_there_is_one_alias_and_one_deployment_behind_it(config):
    aliases = [entry["model_name"] for entry in config["model_list"]]
    assert aliases == ["primary-model"], aliases


def test_every_deployment_authenticates_with_the_anthropic_key_only(config):
    for entry in config["model_list"]:
        assert entry["litellm_params"]["api_key"] == "os.environ/ANTHROPIC_API_KEY"
        assert "api_base" not in entry["litellm_params"], "no other endpoint behind the alias"


def test_there_is_no_fallback_chain(config):
    router = config.get("router_settings") or {}
    for key in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks", "default_fallbacks"):
        assert not router.get(key), f"{key} would send a prompt to a model this test does not allow"
    assert not (config.get("litellm_settings") or {}).get("fallbacks")


def test_the_application_policy_allows_the_one_alias_only():
    policy = yaml.safe_load((APP / "policies" / "model-use.yaml").read_text(encoding="utf-8"))
    rule = next(r for r in policy["rules"] if r["name"] == "gateway-aliases-only")
    assert set(rule["condition"]["value"]) == {f"openai/{a}" for a in ALLOWED_ALIASES}


def test_the_application_key_is_issued_for_the_one_alias_only():
    bootstrap = (INFRA / "litellm-bootstrap.sh").read_text(encoding="utf-8")
    models = re.search(r'\\"models\\":\[([^\]]*)\]', bootstrap).group(1)
    assert set(re.findall(r'\\"([\w-]+)\\"', models)) == ALLOWED_ALIASES


@pytest.mark.parametrize("path", [
    "docker-compose.yml", ".env.example", "scripts/vault-init.sh", "vault-unseal-and-seed.bash",
])
def test_no_other_provider_key_is_wired_into_the_stack(path):
    text = (INFRA / path).read_text(encoding="utf-8")
    other = re.findall(r"\b(DEEPSEEK|GEMINI|OPENAI|GOOGLE|MISTRAL|COHERE|GROQ|AZURE|XAI)\w*_API_KEY", text)
    assert not other, f"{path} references {sorted(set(other))}"


def test_the_one_provider_key_reaches_the_gateway():
    compose = yaml.safe_load((INFRA / "docker-compose.yml").read_text(encoding="utf-8"))
    environment = compose["services"]["litellm"]["environment"]
    assert any(str(v).startswith("ANTHROPIC_API_KEY=") for v in environment), environment
