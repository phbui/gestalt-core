#!/usr/bin/env bash
# Print every tracked bash or sh script, found by extension or by shebang. Python files and
# data files are skipped. The extensionless scripts under tools/fleet/bin are why this exists:
# the old CI loop matched '*.sh' only and never saw them (2026-10-06, register R6 F4).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
git ls-files -z -co --exclude-standard -- tools bin claude-tree/hooks .cursor/hooks \
| while IFS= read -r -d '' f; do
    [ -f "$f" ] && [ ! -L "$f" ] || continue
    case "$f" in *.sh|*.bash) printf '%s\n' "$f"; continue;; esac
    first=""
    IFS= read -r first < "$f" || true
    case "$first" in
      '#!/bin/bash'*|'#!/usr/bin/env bash'*|'#!/bin/sh'*|'#!/usr/bin/env sh'*) printf '%s\n' "$f";;
    esac
  done
