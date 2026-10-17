"""lib.sh gestalt_init: a value the caller exported wins over fleet.env and gestalt/.env.

gestalt_init sources both files with `set -a`, which used to replace GESTALT_WORKSPACE and
GESTALT_STATE_DIR the caller had already exported. On every installed node gestalt/.env names the
workspace, so a hook or test that pointed the hooks at a temporary workspace was silently redirected
to the real one (2026-10-17, test_git_graphiti_feed.py). Hermetic: a throwaway GESTALT_DIR with its
own .env and a throwaway FLEET_ENV, no network, no writes outside tmp_path.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "claude-tree" / "hooks" / "lib.sh"


def _init_and_echo(tmp_path: Path, exported: dict[str, str]) -> tuple[str, str]:
    gdir = tmp_path / "gestalt"
    gdir.mkdir()
    (gdir / ".env").write_text("GESTALT_WORKSPACE=/from/dot-env\nGESTALT_STATE_DIR=/from/dot-env/state\n")
    fleet = tmp_path / "fleet.env"
    fleet.write_text("GESTALT_WORKSPACE=/from/fleet-env\n")
    env = {k: v for k, v in os.environ.items() if k not in ("GESTALT_WORKSPACE", "GESTALT_STATE_DIR")}
    env.update({"GESTALT_DIR": str(gdir), "FLEET_ENV": str(fleet), **exported})
    script = f'SCRIPT_DIR="{LIB.parent}"; source "{LIB}"; gestalt_init; printf "%s\\n%s\\n" "$WORKSPACE" "$STATE_DIR"'
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=True)
    ws, sd = r.stdout.split("\n")[:2]
    return ws, sd


def test_exported_workspace_and_state_dir_win_over_env_files(tmp_path: Path) -> None:
    ws, sd = _init_and_echo(tmp_path, {"GESTALT_WORKSPACE": "/from/caller", "GESTALT_STATE_DIR": "/from/caller/state"})
    assert (ws, sd) == ("/from/caller", "/from/caller/state")


def test_env_files_still_apply_when_nothing_is_exported(tmp_path: Path) -> None:
    ws, sd = _init_and_echo(tmp_path, {})
    assert (ws, sd) == ("/from/dot-env", "/from/dot-env/state")
