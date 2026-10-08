#!/bin/sh
# Discrimination test for the graphiti healthcheck's GPU-throttle gate.
#
# The healthcheck kills PID 1 on a stale heartbeat so restart:unless-stopped revives a
# dead queue worker. On 2026-08-31 that fired five times against a HEALTHY worker: the
# dGPU's enforced power limit collapses to 10W under sustained thermal load, every LLM
# call blocks, no heartbeat is written, and the kill discarded the in-flight episode and
# the whole in-memory queue. The gate forgives a stale beat while the host publishes a
# THROTTLED verdict (tools/gpu-state-publisher.py), without disarming the watchdog in
# any other case.
#
# Run:    sh graphiti/test-healthcheck-gate.sh
# Expect: 1 OK | 2 OK (the fix) | 3,4,5 KILL (watchdog still armed)

check() {                      # mirrors the compose healthcheck; curl assumed OK, kill stubbed
  D=$1
  age=$(( $(date +%s) - $(date -r "$D/queue-heartbeat" +%s) ))
  [ $age -lt 3600 ] && { echo "OK (beat fresh, age=${age}s)"; return 0; }
  if [ -f "$D/gpu-state" ]; then
    fa=$(( $(date +%s) - $(date -r "$D/gpu-state" +%s) ))
    if [ $fa -lt 300 ] && grep -q THROTTLED "$D/gpu-state"; then
      echo "OK gpu-throttled-not-killing (beat=${age}s, flag=${fa}s)"; return 0
    fi
  fi
  echo "KILL stale-heartbeat (beat=${age}s)"; return 1
}

run() {                        # $1 label  $2 fresh|stale  $3 flag json  $4 stalefl
  d=$(mktemp -d); touch "$d/queue-heartbeat"
  [ "$2" = "stale" ] && touch -d '2 hours ago' "$d/queue-heartbeat"
  [ -n "$3" ] && { printf '%s\n' "$3" > "$d/gpu-state"; [ "$4" = "stalefl" ] && touch -d '20 minutes ago' "$d/gpu-state"; }
  printf '%-46s -> %s\n' "$1" "$(check "$d")"
  rm -rf "$d"
}

fail=0
out=$(run "1. beat fresh, no flag"                    fresh "");                            echo "$out"; echo "$out" | grep -q '^.*-> OK'   || fail=1
out=$(run "2. beat STALE, GPU THROTTLED (fresh flag)" stale '{"state": "THROTTLED"}');       echo "$out"; echo "$out" | grep -q 'not-killing' || fail=1
out=$(run "3. beat STALE, GPU OK"                     stale '{"state": "OK"}');              echo "$out"; echo "$out" | grep -q 'KILL'       || fail=1
out=$(run "4. beat STALE, THROTTLED but flag STALE"   stale '{"state": "THROTTLED"}' stalefl); echo "$out"; echo "$out" | grep -q 'KILL'     || fail=1
out=$(run "5. beat STALE, no flag at all"             stale "");                             echo "$out"; echo "$out" | grep -q 'KILL'       || fail=1
echo
[ $fail -eq 0 ] && echo "PASS — gate forgives only a fresh THROTTLED verdict; watchdog armed otherwise" \
                || echo "FAIL — gate does not discriminate"
exit $fail
