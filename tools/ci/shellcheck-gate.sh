#!/usr/bin/env bash
# Fail only on NEW shellcheck findings at -S warning. Existing findings live in a committed baseline,
# keyed by file, SC code and the trimmed source line (not the line number, so an edit above a finding
# does not break the gate). Fix a finding and delete its baseline row, or run with --update.
#
#   Usage: shellcheck-gate.sh with an optional --update flag.
#   Test hooks: the SHELLCHECK_GATE_FILES variable names a file listing paths, and SHELLCHECK_GATE_BASELINE names a baseline.
#
# The CI runner ships shellcheck 0.9.0 and a dev box may have 0.11. Newer versions can report a few
# findings 0.9.0 does not. Regenerate the baseline with 0.9.0 when the gate disagrees: run the gate
# under uv with the pinned py package, version 0.9.0.6, on PATH. The gate exits 0 with a warning when
# the checker or jq is missing.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(git -C "$HERE" rev-parse --show-toplevel)"
base="${SHELLCHECK_GATE_BASELINE:-tools/ci/shellcheck-baseline.txt}"
SC="${SHELLCHECK_BIN:-shellcheck}"
command -v "$SC" >/dev/null 2>&1 || { echo "shellcheck-gate: shellcheck not installed, skipping" >&2; exit 0; }
command -v jq >/dev/null 2>&1 || { echo "shellcheck-gate: jq not installed, skipping" >&2; exit 0; }
if [ -n "${SHELLCHECK_GATE_FILES:-}" ]; then
  mapfile -t files < "$SHELLCHECK_GATE_FILES"
else
  mapfile -t files < <(bash tools/ci/shell-files.sh)
fi
[ "${#files[@]}" -gt 0 ] || { echo "shellcheck-gate: no files" >&2; exit 0; }
# The checker exits 1 when it finds anything, so its status is ignored and the JSON is the verdict.
json=$("$SC" -S warning -f json1 -P SCRIPTDIR "${files[@]}" 2>/dev/null || true)
[ -n "$json" ] || json='{"comments":[]}'
now=$(printf '%s\n' "$json" | jq -r '.comments[] | [.file, "SC\(.code)", (.line|tostring)] | @tsv' \
  | while IFS=$'\t' read -r f c l; do
      printf '%s\t%s\t%s\n' "$f" "$c" "$(sed -n "${l}p" "$f" | sed 's/^[[:space:]]*//')"
    done | LC_ALL=C sort -u)
if [ "${1:-}" = "--update" ]; then
  printf '%s\n' "$now" | grep . > "$base" || : > "$base"
  echo "shellcheck-gate: baseline updated ($(wc -l < "$base") rows)"
  exit 0
fi
[ -f "$base" ] || : > "$base"
new=$(LC_ALL=C comm -13 <(LC_ALL=C sort -u "$base") <(printf '%s\n' "$now" | grep . || true) || true)
if [ -n "$new" ]; then
  echo "::error::new shellcheck findings (not in $base):"
  printf '%s\n' "$new"
  exit 1
fi
echo "shellcheck-gate: no new findings"
