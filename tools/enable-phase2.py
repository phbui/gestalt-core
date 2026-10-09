#!/usr/bin/env python3
"""Gestalt Memory Expansion — Enable Phase 2 (Graphiti temporal graph).

Uncomments Phase 2 service blocks in docker-compose.yml (idempotent),
checks for OPENAI_API_KEY, registers the Graphiti MCP server with Claude Code,
and restarts the systemd service.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_real_home() -> Path:
    """Return the real user's HOME, even when running under sudo."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        import pwd
        return Path(pwd.getpwnam(sudo_user).pw_dir)
    return Path.home()


def step(n: int, total: int, msg: str) -> None:
    print(f"[{n}/{total}] {msg}")


def warn(msg: str) -> None:
    print(f"  WARN: {msg}", file=sys.stderr)


def info(msg: str) -> None:
    print(f"  {msg}")


# ---------------------------------------------------------------------------
# Phase 2 uncomment logic
# ---------------------------------------------------------------------------

# Each rule is (pattern, replacement).
# Pattern matches the commented line; replacement is the uncommented form.
# We match the leading whitespace precisely so indentation is preserved.
#
# Strategy: a commented Phase 2 line looks like:
#   <indent># <rest>
# where the indent is 2 spaces (service name level) or 4 spaces (key level)
# or 6 spaces (value level), etc.
#
# We strip exactly "# " (hash + one space) from within the indented prefix.
# Lines that are already uncommented are left alone (idempotent).

# Sentinel comments that mark Phase 2 blocks — used to scope the replacement
# so we don't accidentally uncomment unrelated commented lines elsewhere.
PHASE2_ANCHORS = [
    "# falkordb:",
    "# graphiti-mcp:",
    "# falkordb-data:",
]


def _is_anchor(stripped_rstripped: str) -> bool:
    """Return True if a line (lstripped, rstripped) matches a Phase 2 anchor."""
    return any(stripped_rstripped == anchor for anchor in PHASE2_ANCHORS)


def _is_phase2_commented(lines: list[str]) -> bool:
    """Return True if any Phase 2 anchor line is still commented."""
    for line in lines:
        if _is_anchor(line.lstrip().rstrip()):
            return True
    return False


def _uncomment_phase2_blocks(lines: list[str]) -> list[str]:
    """
    Remove '# ' comment markers from Phase 2 service blocks.

    Operates in two passes:
    1. Identify line ranges that belong to Phase 2 blocks.
    2. Strip '# ' from each line in those ranges.

    A Phase 2 block starts at an anchor line and ends just before the next
    top-level service/volume key (non-indented, non-blank, non-comment line)
    or at the end of the file.

    If Phase 2 is already uncommented the function is a no-op (idempotent).
    """
    if not _is_phase2_commented(lines):
        return lines  # already live

    # Find start indices of Phase 2 anchors
    anchor_indices: list[int] = []
    for i, line in enumerate(lines):
        if _is_anchor(line.lstrip().rstrip()):
            anchor_indices.append(i)

    if not anchor_indices:
        return lines

    # For each anchor, compute the range [start, end) of lines to uncomment.
    #
    # All child lines in a commented Phase 2 block look like:
    #   "  #   key: value"  ← same leading indent as anchor, then "# " then spaces+content
    #
    # Block termination rules (checked in order):
    #   1. Another Phase 2 anchor → sibling block starts; stop.
    #   2. A non-comment, non-blank line → uncommented YAML; stop.
    #   3. A blank line followed eventually by a non-Phase2-child line → stop at blank.
    #      (docker-compose blocks are separated by blank lines)
    #
    # Simpler implementation: scan forward; the block ends at the first blank line
    # that is NOT followed immediately by more Phase2-child lines at the same indent.
    def find_block_end(start: int) -> int:
        anchor_line = lines[start]
        anchor_indent = len(anchor_line) - len(anchor_line.lstrip())

        j = start + 1
        while j < len(lines):
            line = lines[j]
            stripped_r = line.lstrip().rstrip()

            # Blank line: peek ahead to see if the block continues
            if not stripped_r:
                # Look for the next non-blank line
                k = j + 1
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if k >= len(lines):
                    return j  # EOF after blank → end block at blank
                next_line = lines[k]
                next_stripped = next_line.lstrip().rstrip()
                # If the next non-blank is a Phase 2 anchor → sibling block
                if _is_anchor(next_stripped):
                    return j
                # If next non-blank is a comment at same leading indent → still in block
                next_indent = len(next_line) - len(next_line.lstrip())
                if next_indent == anchor_indent and next_stripped.startswith("#"):
                    # Could be a sibling block's content or another block separator;
                    # include the blank and keep going only if it's another anchor
                    # (already handled above) — otherwise end here.
                    return j
                # Non-comment content after blank → block ended at blank
                return j

            # Another Phase 2 anchor at same indent → sibling block
            if _is_anchor(stripped_r):
                return j

            # Non-comment, non-blank line → uncommented YAML content (end of block)
            if not stripped_r.startswith("#"):
                return j

            # A comment indented LESS than the anchor cannot belong to this block —
            # the block's own lines are all nested under it. Without this the scan
            # ran past the block into any following comment and uncommented it,
            # turning a prose comment into a bare YAML key and breaking the file.
            if len(line) - len(line.lstrip()) < anchor_indent:
                return j

            j += 1

        return len(lines)

    # Build the set of line indices to uncomment
    to_uncomment: set[int] = set()
    for start in anchor_indices:
        end = find_block_end(start)
        for k in range(start, end):
            to_uncomment.add(k)

    result: list[str] = []
    for i, line in enumerate(lines):
        if i in to_uncomment:
            # Strip exactly one "# " occurrence after the leading whitespace.
            # e.g. "  # falkordb:" → "  falkordb:"
            # e.g. "  #   image: foo" → "    image: foo"
            # (the "# " occupies 2 chars; the space after # becomes part of indent)
            m = re.match(r'^(\s*)# (.*)', line)
            if m:
                line = m.group(1) + m.group(2) + "\n"
                # Trim trailing newline if original didn't have one (edge case)
                if not lines[i].endswith("\n"):
                    line = line.rstrip("\n")
        result.append(line)

    return result


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def check_openai_key(env_file: Path) -> None:
    step(1, 4, "Checking OpenAI API key...")
    env_file.parent.mkdir(parents=True, exist_ok=True)

    if env_file.exists():
        content = env_file.read_text(encoding="utf-8")
        if "OPENAI_API_KEY" in content:
            info("OpenAI key already in env file")
            return

    # Prompt interactively
    try:
        key = input("  Enter OpenAI API key (for Graphiti embeddings): ").strip()
    except (EOFError, KeyboardInterrupt):
        key = ""

    if key:
        with env_file.open("a", encoding="utf-8") as f:
            f.write(f"OPENAI_API_KEY={key}\n")
        info(f"Key added to {env_file}")
    else:
        warn("No OpenAI key provided. Graphiti embeddings will not work.")
        warn(f"You can add it later: echo 'OPENAI_API_KEY=sk-...' >> {env_file}")


def enable_phase2_services(compose_file: Path) -> None:
    step(2, 4, "Enabling Phase 2 services in docker-compose.yml...")

    original = compose_file.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)

    if not _is_phase2_commented(lines):
        info("Phase 2 services already enabled (no-op)")
        return

    updated_lines = _uncomment_phase2_blocks(lines)
    updated = "".join(updated_lines)

    compose_file.write_text(updated, encoding="utf-8")
    info("Phase 2 services enabled")


def register_mcp(real_home: Path) -> None:
    step(3, 4, "Registering Graphiti MCP server...")
    try:
        result = subprocess.run(
            ["claude", "mcp", "add", "graphiti-memory",
             "--transport", "http", "http://localhost:8200/mcp/"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            info("graphiti-memory MCP server registered")
        else:
            warn("claude mcp add returned non-zero. It may already be registered.")
            if result.stderr:
                info(f"stderr: {result.stderr.strip()}")
    except FileNotFoundError:
        warn("'claude' not found in PATH. Register MCP manually:")
        warn("  claude mcp add graphiti-memory --transport http http://localhost:8200/mcp/")


def restart_services() -> None:
    # HUB SAFETY (gestalt hub-invariants): restarting gestalt-services runs `docker compose down`
    # via the system unit's ExecStop and briefly drops the whole memory stack. On the always-on hub
    # that is a maintenance act — refuse unless HUB_MAINTENANCE=1 is set (or a tty confirms).
    if os.environ.get("HUB_MAINTENANCE") != "1":
        if sys.stdin.isatty():
            if input("Restart gestalt-services now? this briefly drops the memory stack [y/N] ").strip().lower() != "y":
                warn("skipped restart; run with HUB_MAINTENANCE=1 to restart non-interactively"); return
        else:
            warn("skipping gestalt-services restart — set HUB_MAINTENANCE=1 to allow (hub-invariants)"); return
    step(4, 4, "Restarting gestalt services...")
    result = subprocess.run(
        ["sudo", "systemctl", "restart", "gestalt-services"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        warn(f"systemctl restart failed: {result.stderr.strip()}")
        sys.exit(1)
    info("gestalt-services restarted")


def wait_for_health(timeout: int = 120, interval: int = 3) -> None:
    import urllib.error
    import urllib.request

    print()
    print(f"Waiting for services to be healthy (up to {timeout}s)...")

    def check(url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    letta_url = "http://localhost:8283/v1/health"
    graphiti_url = "http://localhost:8200/health"

    deadline = time.monotonic() + timeout
    letta_ok = False
    graphiti_ok = False

    while time.monotonic() < deadline:
        letta_ok = check(letta_url)
        graphiti_ok = check(graphiti_url)
        if letta_ok and graphiti_ok:
            break
        time.sleep(interval)

    print()
    if letta_ok and graphiti_ok:
        print("=== Phase 2 Enabled ===")
        print("  Letta:    http://localhost:8283 OK")
        print("  FalkorDB: localhost:6379")
        print("  Graphiti: http://localhost:8200 OK")
        print()
        print("MCP tools available: add_memory, search_memory_facts, search_nodes")
        print("Try: /recall 'platform deploy'")
    else:
        warn("Services not fully healthy after timeout.")
        warn("Check: sudo journalctl -u gestalt-services -f")
        print(f"  Letta:    {'OK' if letta_ok else 'FAIL'}")
        print(f"  Graphiti: {'OK' if graphiti_ok else 'FAIL'}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    script_dir = Path(__file__).resolve().parent
    gestalt_dir = script_dir.parent
    compose_file = gestalt_dir / "docker-compose.yml"
    real_home = get_real_home()
    env_file = real_home / ".claude" / "gestalt" / "env"

    print("=== Enabling Phase 2: Graphiti Temporal Graph ===")
    print()

    if not compose_file.exists():
        print(f"ERROR: docker-compose.yml not found at {compose_file}", file=sys.stderr)
        sys.exit(1)

    check_openai_key(env_file)
    enable_phase2_services(compose_file)
    register_mcp(real_home)
    restart_services()
    wait_for_health()


if __name__ == "__main__":
    main()
