#!/usr/bin/env bash
# Watch a PR's checks and report state CHANGES until they settle.
#
#   watch-pr-checks.sh <owner/repo> <pr-number> [min-expected-checks] [interval-s]
#
# Companion to gestalt/knowledge/<kb-entry>.md. Every guard here exists
# because a simpler version produced a confident wrong answer instead of an error:
#
#   1. Poll unconditionally. An earlier version wrapped the poll in
#      `if github_is_healthy`; GitHub stayed unhealthy and it emitted NOTHING
#      while 12 checks failed. A precondition is data to report, not a guard.
#   2. A failed query is an EVENT. Going quiet on error makes the watcher blind
#      exactly when things are worst, and blind looks identical to green.
#   3. Empty is NOT settled. `any(pending)` over an empty list is False, and the
#      API genuinely reports no checks for a window after a push (cli/cli#7401),
#      so "nothing pending" right after a push is a false green.
#   4. Follow the CURRENT head. `gh pr checks` always reports the PR head, never
#      a SHA you pinned; labelling its output with a pinned SHA mislabels it.
#   5. State facts, not causes. Do not explain a head move you did not observe.
#
# Exit codes are deliberately NOT used as the signal: `gh pr checks` returns 8
# for "checks pending" and 1 for real errors, so a bare `if ! gh ...` conflates
# "still running" with "command broke". The JSON payload is validated instead.

set -uo pipefail

REPO="${1:?usage: watch-pr-checks.sh <owner/repo> <pr> [min-checks] [interval]}"
PR="${2:?missing PR number}"
MIN_EXPECTED="${3:-1}"   # raise this to the real expected count to catch false greens
INTERVAL="${4:-120}"

prev=""; last_head=""; fail_streak=0; first=1

while :; do
  raw=$(gh pr checks "$PR" --repo "$REPO" --json name,bucket,link 2>/dev/null)
  rc=$?
  if ! printf '%s' "$raw" | python3 -c 'import sys,json;json.load(sys.stdin)' 2>/dev/null; then
    fail_streak=$((fail_streak + 1))
    [ "$fail_streak" = 1 ] && echo "QUERY FAILING (rc=$rc) — watcher is blind, not green"
    sleep "$INTERVAL"; continue
  fi
  [ "$fail_streak" -gt 0 ] && { echo "QUERY RECOVERED after $fail_streak attempt(s)"; fail_streak=0; }

  # A FAILED head lookup is not a head. An earlier version substituted the
  # literal "unknown" and then reported `HEAD MOVED unknown -> abc1234`, which
  # is this file's own bug class turned on itself: a failure laundered into
  # data. On failure, keep the last known head and say the lookup failed.
  head=$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq .headRefOid 2>/dev/null)
  if [ -z "$head" ]; then
    [ "$first" = 1 ] && echo "HEAD LOOKUP FAILED — counts below are for the PR head, exact SHA unconfirmed"
    head="$last_head"
  elif [ -n "$last_head" ] && [ "$head" != "$last_head" ]; then
    echo "HEAD MOVED ${last_head:0:9} -> ${head:0:9} — all counts below refer to ${head:0:9}"
    prev=""
  fi
  [ -n "$head" ] && last_head="$head"

  read -r line fails pend total <<<"$(printf '%s' "$raw" | python3 -c "
import sys, json
from collections import Counter
d = json.load(sys.stdin)
c = Counter(x['bucket'] for x in d)
fails = ','.join(sorted({x['name'] for x in d if x['bucket'] == 'fail'})) or '-'
line = 'total=%d ' % len(d) + ' '.join('%s=%d' % kv for kv in sorted(c.items()))
pend = 'PENDING' if any(x['bucket'] == 'pending' for x in d) else 'NOPENDING'
print(line.replace(' ', '~'), fails.replace(' ', '~'), pend, len(d))
")"
  line="${line//\~/ }"; fails="${fails//\~/ }"

  # The failing list prints only alongside a first/changed report. Printing it
  # every cycle re-emits the same names forever, which trains the reader to
  # skim past the one line that matters.
  if [ "$first" = 1 ]; then
    echo "WATCHING head ${head:0:9}: $line"
    [ "$fails" != "-" ] && echo "  failing: $fails"
    first=0; prev="$line$fails"
  elif [ "$line$fails" != "$prev" ]; then
    echo "CHANGE: $line"
    [ "$fails" != "-" ] && echo "  failing: $fails"
    prev="$line$fails"
  fi

  if [ "$pend" = "NOPENDING" ]; then
    if [ "$total" -ge "$MIN_EXPECTED" ]; then
      echo "SETTLED on head ${head:0:9}: $line"
      if [ "$fails" = "-" ]; then
        echo "  ALL GREEN"
      else
        printf '%s' "$raw" | python3 -c "
import sys, json
for x in json.load(sys.stdin):
    if x['bucket'] == 'fail':
        print('   FAIL', x['name'], x['link'])
"
      fi
      exit 0
    fi
    echo "NOT settled: only $total checks (< $MIN_EXPECTED) — still materialising, NOT green"
  fi
  sleep "$INTERVAL"
done
