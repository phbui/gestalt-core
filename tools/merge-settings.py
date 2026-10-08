#!/usr/bin/env python3
"""Merge gestalt hook config into an existing Claude Code settings.json.

Usage: python3 merge-settings.py <settings_path> <hooks_config_path>

Appends hook entries from hooks_config into the target settings.json
without clobbering existing entries. Idempotent — checks for duplicates
by command path before inserting.
"""

import json
import os
import sys
from pathlib import Path


def merge_hooks(target: dict, source: dict) -> dict:
    """Merge source hook entries into target, avoiding duplicates."""
    target_hooks = target.setdefault("hooks", {})
    source_hooks = source.get("hooks", {})

    for event, entries in source_hooks.items():
        if event not in target_hooks:
            target_hooks[event] = entries
            continue

        existing = target_hooks[event]
        for new_entry in entries:
            # Compare FULL commands. Truncating the key to 80 chars made two hooks
            # sharing a long path prefix look identical, and the second was then
            # dropped with no error — registered commands here already reach 178
            # chars. Silently discarding a safety hook is the worst available
            # failure for this tool, so the key must not be lossy.
            new_cmd = ""
            for h in new_entry.get("hooks", []):
                new_cmd = h.get("command", h.get("prompt", ""))

            duplicate = False
            for existing_entry in existing:
                for h in existing_entry.get("hooks", []):
                    existing_cmd = h.get("command", h.get("prompt", ""))
                    if existing_cmd and existing_cmd == new_cmd:
                        duplicate = True
                        break

            if not duplicate:
                existing.append(new_entry)

    return target


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <settings.json> <hooks-config.json>")
        sys.exit(1)

    settings_path = Path(sys.argv[1])
    hooks_path = Path(sys.argv[2])

    if not hooks_path.exists():
        print(f"Error: {hooks_path} not found")
        sys.exit(1)

    # Read existing settings or start fresh
    try:
        if settings_path.exists():
            target = json.loads(settings_path.read_text(encoding="utf-8"))
        else:
            target = {}
    except json.JSONDecodeError as e:
        print(f"Error: {settings_path} contains invalid JSON: {e}")
        print("Fix the JSON manually or delete the file to start fresh.")
        sys.exit(1)

    try:
        source = json.loads(hooks_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Error: {hooks_path} contains invalid JSON: {e}")
        sys.exit(1)

    merged = merge_hooks(target, source)

    # Atomic write — write to temp file then rename
    import tempfile
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=settings_path.parent, suffix=".tmp", delete=False
    )
    try:
        tmp.write(json.dumps(merged, indent=2) + "\n")
        tmp.close()
        # os.replace, not Path.rename: on Windows rename raises when the
        # destination exists, so the "atomic overwrite" never happened there.
        os.replace(tmp.name, settings_path)
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise

    print(f"Merged hooks into {settings_path}")


if __name__ == "__main__":
    main()
