#!/usr/bin/env bash
# Out-of-sample BEIR run for gestalt: ArguAna and SCIDOCS, sets the zoo never touched.
# Same configuration as the headline (results/summary.json environment, 2026-10-12): stella-400m float32,
# convex fusion at alpha 0 (the profile default), Qwen3-Reranker-0.6B float16 at depth 40, maxchars 2000.
# The per-dataset task lines below were written before any result was seen, in the form of the three headline lines.
# Launched from the mobile laptop on 2026-10-26 with Phi's approval; runs under nohup, writes done.flag at the end.
set -u
cd "$HOME/Documents/GitHub/gestalt" || exit 2
OUT="${OUT:-$HOME/artifacts/gestalt-heldout-2026-10-26}"
LIMIT="${LIMIT:-}"
mkdir -p "$OUT"
PY="$HOME/.claude/gestalt/venv/bin/python3"
export GESTALT_EMBED_PROFILE=stella-400m
export GESTALT_EMBED_DTYPE=float32
export GESTALT_RERANK_DTYPE=float16
export GESTALT_RERANK_MAXCHARS=2000
export GESTALT_GPU_DUTY="${GESTALT_GPU_DUTY:-0.7}"
rc_all=0
run_one() {
  local ds="$1" instr="$2" tag="$3"
  GESTALT_RERANK_INSTRUCTION="$instr" "$PY" evals/retrieval/beir_bench.py \
    --datasets "$ds" --rerank on --rerank-model qwen3-0.6b --rerank-depth 40 \
    --out "$OUT/$tag" --work-dir "$OUT/work" --resume ${LIMIT:+--limit-docs $LIMIT} \
    > "$OUT/$tag.log" 2>&1
  local rc=$?
  echo "$(date -u +%FT%TZ) $ds rc=$rc" >> "$OUT/progress.log"
  [ $rc -ne 0 ] && rc_all=$rc
  grep -E "nDCG@10|_vs_|subset" "$OUT/$tag.log" | sed "s/^/  $tag /" >> "$OUT/progress.log"
}
echo "$(date -u +%FT%TZ) start host=$(hostname) duty=$GESTALT_GPU_DUTY" >> "$OUT/progress.log"
run_one beir/arguana "Given a claim, retrieve documents that present a counterargument to the claim" beir-arguana
run_one beir/scidocs "Given a scientific paper title, retrieve paper abstracts that are cited by the paper" beir-scidocs
echo "$(date -u +%FT%TZ) end rc=$rc_all" >> "$OUT/progress.log"
echo "$rc_all" > "$OUT/done.flag"
