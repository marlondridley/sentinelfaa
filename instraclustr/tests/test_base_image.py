"""
The runtime image takes the security fixes the base image has not got yet.

python:3.12-slim lags Debian's security archive, and the image-scan job fails the build on any fixable HIGH or
CRITICAL finding, so a patched package that the base image lacks fails every pull request until the image upgrades
it. The upgrade is in the runtime stage, before the application is installed, and adds no package.
"""
from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent.parent / "nas-feedops-sentinel" / "docker" / "Dockerfile"


def _runtime_stage() -> str:
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index("AS runtime")
    return text[start:text.index("\nFROM ", start)]


def test_the_runtime_stage_upgrades_installed_packages_from_the_security_archive():
    stage = _runtime_stage()
    assert re.search(r"RUN apt-get update \\\n\s+&& apt-get upgrade -y --no-install-recommends \\\n\s+&& rm -rf /var/lib/apt/lists/\*", stage), \
        "the runtime stage updates, upgrades and clears the package lists in one layer"


def test_the_upgrade_adds_no_package_and_comes_before_the_application_is_installed():
    stage = _runtime_stage()
    assert "apt-get install" not in stage, "upgrading what is there is not adding to it"
    assert stage.index("apt-get upgrade") < stage.index("pip install"), "the upgrade runs before the application layers"
