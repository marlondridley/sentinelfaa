"""
The repository's own controls stay wired: CODEOWNERS, gitleaks and mypy.

None of these runs at runtime; each is a file a later edit could quietly empty. So the facts that make them mean
something are pinned here: that CODEOWNERS names the paths that can weaken a control and every path exists, that the
secret scan reads the whole history from a digest-pinned image and proves it finds a planted token, that every
finding set aside in .gitleaksignore carries its reason, and that mypy is locked, configured and run in CI.
"""
from __future__ import annotations

import configparser
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
APP = ROOT / "nas-feedops-sentinel"

pytestmark = pytest.mark.skipif(not (ROOT / ".github").is_dir(), reason="needs the whole repository mounted")


# ── CODEOWNERS ─────────────────────────────────────────────────────────────────

def _owners() -> list[tuple[str, list[str]]]:
    rules = []
    for line in (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pattern, *owners = line.split()
            rules.append((pattern, owners))
    return rules


def test_every_pattern_has_an_owner_in_the_form_github_reads():
    rules = _owners()
    assert rules and rules[0][0] == "*", "a default owner comes first; later patterns override it"
    for pattern, owners in rules:
        assert owners and all(re.fullmatch(r"@[A-Za-z0-9][A-Za-z0-9-]*(/[A-Za-z0-9._-]+)?", o) for o in owners), (pattern, owners)


def test_every_pattern_names_something_that_exists():
    for pattern, _ in _owners()[1:]:
        relative = pattern.lstrip("/")
        assert any(ROOT.glob(relative)), f"CODEOWNERS names {pattern}, which matches nothing"


@pytest.mark.parametrize("path", [
    ".github/workflows/pyapp-ci.yml", "nas-feedops-sentinel/policies/model-use.yaml",
    "nas-feedops-sentinel/src/execution_gate.py", "nas-feedops-sentinel/src/auth.py",
    "nas-feedops-sentinel/src/storage/schema.py", "instraclustr/docker-compose.yml",
    "instraclustr/litellm-config.yaml", "nas-feedops-sentinel/requirements.lock", ".gitleaksignore",
])
def test_the_paths_that_can_weaken_a_control_have_an_explicit_owner(path):
    covered = False
    for pattern, _ in _owners()[1:]:
        regex = "^" + re.escape(pattern.lstrip("/")).replace(r"\*", "[^/]*") + ("" if not pattern.endswith("/") else ".*")
        covered = covered or bool(re.match(regex, path))
    assert covered, f"{path} falls through to the default owner only"


# ── gitleaks ───────────────────────────────────────────────────────────────────

def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "secrets.yml").read_text(encoding="utf-8"))


def test_the_scan_reads_the_whole_history_from_an_image_pinned_by_digest():
    workflow = _workflow()
    assert re.fullmatch(r"ghcr\.io/gitleaks/gitleaks@sha256:[0-9a-f]{64}", workflow["env"]["GITLEAKS_IMAGE"])
    steps = workflow["jobs"]["gitleaks"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0, "a shallow checkout would scan one commit"
    scan = next(s for s in steps if s.get("name") == "Scan the history")["run"]
    assert "detect --source /repo" in scan and "--no-git" not in scan
    assert "--gitleaks-ignore-path /repo/.gitleaksignore" in scan and "--config /repo/.gitleaks.toml" in scan


def test_the_scan_proves_it_can_find_a_secret_and_the_workflow_does_not_contain_one():
    steps = _workflow()["jobs"]["gitleaks"]["steps"]
    self_test = next(s for s in steps if str(s.get("name", "")).startswith("Self-test"))["run"]
    assert "exit 1" in self_test and "--no-git" in self_test
    text = (ROOT / ".github" / "workflows" / "secrets.yml").read_text(encoding="utf-8")
    assert not re.search(r"ghp_[A-Za-z0-9]{36}", text), "the planted token is assembled at run time, never written"


def test_the_ruleset_is_gitleaks_default_with_nothing_loosened():
    config = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    assert re.search(r"(?m)^useDefault\s*=\s*true", config)
    assert "[[rules]]" not in config and "allowlist" not in config.lower().replace("gitleaksignore", ""), \
        "set a finding aside by fingerprint in .gitleaksignore, with its reason, not by a pattern"


def test_every_finding_set_aside_has_a_reason_above_it():
    lines = (ROOT / ".gitleaksignore").read_text(encoding="utf-8").splitlines()
    fingerprints = [i for i, line in enumerate(lines) if line and not line.startswith("#")]
    assert fingerprints, "the file lists the findings that were read"
    for i in fingerprints:
        assert re.fullmatch(r"[0-9a-f]{40}:[^:]+:[a-z0-9-]+:\d+", lines[i]), lines[i]
        assert i > 0 and lines[i - 1].startswith("# ") and len(lines[i - 1]) > 12, f"no reason above {lines[i]}"


# ── mypy ───────────────────────────────────────────────────────────────────────

def test_mypy_is_locked_with_hashes_and_listed_as_a_dev_dependency():
    assert re.search(r"(?m)^mypy[>=]", (APP / "requirements-dev.txt").read_text(encoding="utf-8"))
    lock = (APP / "requirements-dev.lock").read_text(encoding="utf-8")
    block = re.search(r"(?ms)^mypy==\S+ \\\n((?:\s+--hash=sha256:[0-9a-f]{64}.*\n)+)", lock)
    assert block, "mypy is pinned in the lock with hashes"


def test_mypy_covers_storage_the_execution_gate_and_auth():
    parser = configparser.ConfigParser()
    parser.read(APP / "mypy.ini", encoding="utf-8")
    files = parser["mypy"]["files"]
    for needed in ("src/storage", "src/execution_gate.py", "src/auth.py"):
        assert needed in files
    assert parser["mypy"].getboolean("check_untyped_defs"), "untyped function bodies are checked too"


def test_ci_runs_mypy_in_the_test_image():
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "pyapp-ci.yml").read_text(encoding="utf-8"))
    runs = [s.get("run", "") for s in ci["jobs"]["test"]["steps"]]
    assert any("nas-feedops-sentinel:test python -m mypy" in r for r in runs)
