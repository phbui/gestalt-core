"""The personal-repo loosening of safety-guard-bash.sh is opt-in.

The shipped default matches no repository. A repo list comes from GESTALT_PERSONAL_REPO_RE or from the first line of $GESTALT_DIR/.personal-repos."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "claude-tree" / "hooks" / "safety-guard-bash.sh"
ORIGIN = "https://github.com/example-owner/example-repo.git"
PUSH_TO_MAIN = "git push origin " + "main"


def _denies_push_to_main(tmp_path: Path, *, env_re: str | None = None, cfg: str | None = None) -> bool:
    work = tmp_path / "work"
    if not work.exists():
        subprocess.run(["git", "init", "-q", str(work)], check=True)
        subprocess.run(["git", "-C", str(work), "remote", "add", "origin", ORIGIN], check=True)
    gdir = tmp_path / "gestalt"
    gdir.mkdir(exist_ok=True)
    cfg_path = gdir / ".personal-repos"
    cfg_path.unlink(missing_ok=True)
    if cfg is not None:
        cfg_path.write_text(cfg + "\n", encoding="utf-8")
    env = {**os.environ, "GESTALT_DIR": str(gdir), "GESTALT_STATE_DIR": str(tmp_path / "state")}
    env.pop("GESTALT_PERSONAL_REPO_RE", None)
    env.pop("HUB_MAINTENANCE", None)
    if env_re is not None:
        env["GESTALT_PERSONAL_REPO_RE"] = env_re
    payload = json.dumps({"tool_name": "Bash", "cwd": str(work), "tool_input": {"command": PUSH_TO_MAIN}})
    r = subprocess.run(["bash", str(HOOK)], input=payload, capture_output=True, text=True, env=env)
    return '"deny"' in r.stdout


def test_default_loosens_nothing(tmp_path):
    assert _denies_push_to_main(tmp_path) is True


def test_env_pattern_allows_a_matching_origin(tmp_path):
    assert _denies_push_to_main(tmp_path, env_re=r"github\.com[:/]example-owner/example-repo") is False
    assert _denies_push_to_main(tmp_path, env_re=r"github\.com[:/]someone-else/") is True


def test_config_file_pattern_allows_a_matching_origin(tmp_path):
    assert _denies_push_to_main(tmp_path, cfg=r"github\.com[:/]example-owner/") is False
    assert _denies_push_to_main(tmp_path, cfg=r"github\.com[:/]someone-else/") is True


def test_the_shipped_hook_names_no_maintainer_repo():
    assert "phbui/" not in HOOK.read_text(encoding="utf-8")
