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

# transformers<5: nomic-embed-text-v1.5's remote code calls get_extended_attention_mask,
# which transformers 5 removed (HF nomic-ai/nomic-bert-2048 discussion #23, open 2026-09);
# unpinned, the ratchet SKIPs on every hub run and measures nothing.
RETR_OUT=$(uv run --quiet --with pyyaml --with sqlite-vec --with sentence-transformers --with einops --with "transformers<5" python3 evals/retrieval/run_retrieval_evals.py --baseline check 2>&1)
RETR_RC=$?
[ $RETR_RC -eq 0 ] && { rlog PASS; exit 0; }
if echo "$RETR_OUT" | grep -q Traceback; then
  echo "gestalt: retrieval ratchet SKIPPED (harness error, not a regression):"
  echo "$RETR_OUT" | tail -2
  rlog SKIP harness-error
  exit 0
fi
rlog FAIL
echo "$RETR_OUT" | tail -20
echo "gestalt: Tier 1 RED -- retrieval golden set regressed vs evals/retrieval/baseline.json (re-bless with --baseline save ONLY for an understood, justified change)" >&2
exit 1
