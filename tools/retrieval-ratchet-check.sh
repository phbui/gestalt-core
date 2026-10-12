#!/bin/sh
# Retrieval-quality ratchet (gbrain transfer, knowledge/gbrain.md ^transfer-list item 3).
# Called by the pre-push hook. Replays the golden set against the live index and exits 1
# on a regression vs the banked evals/retrieval/baseline.json.
#
# Guarded three ways — an index must exist, it must carry the vector leg, and the harness
# must actually run. A node that cannot run it honestly exits 0 (skip-clean): an FTS-only
# leaf measuring a different pipeline than the banked hybrid baseline would be a false
# regression, and the floors test (tests/test_eval_floors.py) still guards the committed
# baseline everywhere. Lives as a standalone file because macOS bash 3.2 miscounts quotes
# inside $(cat <<heredoc) bodies, which broke the installer when this logic was inline.
set -u
# N10 (2026-10-06): one line per run to ~/.fleet/ratchet.log: time, host, commit, result, reason. A node that
# skips every run used to leave no trace. Logging never changes the exit code and never fails the push.
RATCHET_LOG="${RATCHET_LOG:-$HOME/.fleet/ratchet.log}"
rlog() { # $1 PASS|FAIL|SKIP, $2 reason (optional)
  mkdir -p "$(dirname "$RATCHET_LOG")" 2>/dev/null || return 0
  printf '%s %s %s %s%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(hostname -s 2>/dev/null || echo unknown)" "$(git rev-parse --short HEAD 2>/dev/null || echo nogit)" "$1" "${2:+ reason=$2}" >> "$RATCHET_LOG" 2>/dev/null || true
}
[ -f .search/gestalt.db ] || { rlog SKIP no-index; exit 0; }
# Proper schema check (this file is parsed as a normal script, so the quoting that broke the
# heredoc is fine here). The earlier grep -a shortcut false-positived when corpus TEXT mentioned
# the table name — indexed content lives in the same file as the schema.
python3 -c "import sqlite3,sys; sys.exit(0 if sqlite3.connect('.search/gestalt.db').execute(\"select 1 from sqlite_master where type='table' and name='sections_vec'\").fetchone() else 1)" 2>/dev/null || { rlog SKIP no-vector-leg; exit 0; }

# Leaf guard (2026-10-12): since fleet-sync index-fetch, a laptop carries the hub's vector index but holds no embedding model, and
# the design keeps it that way (<kb-entry>). The same rule as the MCP server's leaf policy: a node that names a
# fleet hub, or has ~/.fleet/fleet.env, and is not that hub, skips clean unless GESTALT_LEAF_LOCAL_MODEL=1. A lone install runs it.
HUB_NAME="${FLEET_HUB_NAME:-}"; [ -z "$HUB_NAME" ] && HUB_NAME="${FLEET_HUB:-}" && HUB_NAME="${HUB_NAME%%.*}"
if [ -z "$HUB_NAME" ] && [ -f "$HOME/.fleet/fleet.env" ]; then HUB_NAME="$(sed -n 's/^FLEET_HUB_NAME=//p' "$HOME/.fleet/fleet.env" | head -1)"; HUB_NAME="${HUB_NAME:-hub}"; fi
if [ -n "$HUB_NAME" ] && [ "$(hostname -s 2>/dev/null | tr 'A-Z' 'a-z')" != "$(printf %s "$HUB_NAME" | tr 'A-Z' 'a-z')" ] && [ "${GESTALT_LEAF_LOCAL_MODEL:-}" != "1" ]; then
  echo "gestalt: retrieval ratchet SKIPPED (fleet leaf: the hub holds the model and runs the ratchet on its own pushes)"
  rlog SKIP leaf-no-model; exit 0
fi
# The interpreter is the gestalt venv when it exists: the pinned environment the baseline was banked in (transformers 5.15 to 5.16,
# which stella_en_400M_v5's modelling code needs; 5.17 removed the helper it calls, tools/requirements.txt). The old "transformers<5"
# pin here was the nomic era and cannot load the shipped profile. Without the venv, uv builds the same range.
if [ -x "$HOME/.claude/gestalt/venv/bin/python3" ]; then
  RETR_OUT=$("$HOME/.claude/gestalt/venv/bin/python3" evals/retrieval/run_retrieval_evals.py --baseline check 2>&1)
  RETR_RC=$?
else
  command -v uv >/dev/null 2>&1 || { echo "gestalt: retrieval ratchet SKIPPED (no gestalt venv and no uv, not a regression)"; rlog SKIP no-uv; exit 0; }
  RETR_OUT=$(uv run --quiet --with pyyaml --with sqlite-vec --with sentence-transformers --with einops --with "transformers>=5.5,<5.17" python3 evals/retrieval/run_retrieval_evals.py --baseline check 2>&1)
  RETR_RC=$?
fi
[ $RETR_RC -eq 0 ] && { rlog PASS; exit 0; }
# The runner refuses an index built for another embedding profile (2026-10-10). That is a setup error, not a quality regression,
# and it must not pass silently either: the push is stopped with the runner's own message.
if echo "$RETR_OUT" | grep -q "built for another embedding setup"; then
  echo "$RETR_OUT" | tail -2 >&2
  echo "gestalt: retrieval ratchet cannot run: the index and GESTALT_EMBED_PROFILE disagree. Rebuild the index under the profile, then push." >&2
  rlog FAIL index-profile-mismatch
  exit 1
fi
if echo "$RETR_OUT" | grep -q Traceback; then
  echo "gestalt: retrieval ratchet SKIPPED (harness error, not a regression):"
  echo "$RETR_OUT" | tail -2
  rlog SKIP harness-error
  exit 0
fi
rlog FAIL
echo "$RETR_OUT" | tail -20
echo "gestalt: Tier 1 RED -- retrieval golden set regressed vs evals/retrieval/baseline.json (re-bless with --baseline bank --config-name default ONLY for an understood, justified change)" >&2
exit 1
