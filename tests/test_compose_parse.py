"""docker-compose.yml must parse. Uses `docker compose config` when Docker is installed, else skips.

`config` is a pure parse and never contacts the daemon. The YAML-level checks live in
test_compose_hardening.py and run everywhere.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_compose_config_parses():
    r = subprocess.run(["docker", "compose", "-f", str(REPO / "docker-compose.yml"), "config"],
                       capture_output=True, text=True, timeout=60,
                       env={"PATH": __import__("os").environ["PATH"], "HOME": str(REPO), "ANTHROPIC_API_KEY": "x"})
    if "unknown shorthand flag" in r.stderr or "is not a docker command" in r.stderr:
        pytest.skip("docker compose plugin not installed")
    assert r.returncode == 0, r.stderr
