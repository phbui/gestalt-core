#!/bin/sh
# The pre-push gate. tools/gestalt-git-setup.sh installs .git/hooks/pre-push as a thin wrapper that
# execs this file, so the hook body lives in the repo and a pull updates it (2026-10-06, no drift).
# Tier 0 is the hermetic fast list (tests/fast-tier.txt) and runs on every clone, locked or not.
# Tier 1 is the three-tier regression gate (CI synthetic / this local pre-push stamp / fleet live).
# See knowledge/<kb-entry>.md ^ci-tiers.
command -v git-lfs >/dev/null 2>&1 && git lfs pre-push "$@" || true

# Encrypted-blob guard (2026-10-06). A commit carried a knowledge entry whose git-crypt blob failed its
# integrity check on a full decrypt ("encrypted file has been tampered with"). Every node's checkout of
# that head then died in the smudge filter, the hub included. CI cannot see this: it has no key. So before
# any push, fully decrypt every knowledge/ and personal/ blob this push adds or changes. It runs before the
# skip switches on purpose: a corrupt blob is never a deliberate push. Skips on a locked clone.
_GR="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -n "$_GR" ] && [ -f "$_GR/.git/git-crypt/keys/default" ] && command -v git-crypt >/dev/null 2>&1; then
  _UP="$(git -C "$_GR" rev-parse --verify -q '@{u}' 2>/dev/null)"
  if [ -n "$_UP" ]; then
    _BAD="$(git -C "$_GR" -c core.quotePath=false diff --name-only --diff-filter=AM "$_UP" HEAD -- knowledge personal 2>/dev/null | while IFS= read -r _f; do
      case "$_f" in */.gitkeep) continue ;; esac
      git -C "$_GR" cat-file -p "HEAD:$_f" 2>/dev/null | git-crypt smudge >/dev/null 2>&1 || printf '%s\n' "$_f"
    done)"
    if [ -n "$_BAD" ]; then
      echo "gestalt: PUSH BLOCKED -- these encrypted blobs do not decrypt cleanly at HEAD:" >&2
      printf '%s\n' "$_BAD" | sed 's/^/  /' >&2
      echo "gestalt: re-add each file from a good working copy (git add <file>) and commit, then push again." >&2
      exit 1
    fi
  fi
fi

# Escape hatch for a deliberate untested push (e.g. shipping a WIP fix to another
# node). fleet-sync's push_one still gates on the stamp, so this only lets `git
# push` itself through -- it does not silently mark HEAD as tested.
if [ "${GESTALT_TIER1_SKIP:-}" = "1" ]; then
  echo "gestalt: Tier 1 SKIPPED (GESTALT_TIER1_SKIP=1) -- not stamping HEAD"
  exit 0
fi

REPO="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -n "$REPO" ] || exit 0
cd "$REPO" || exit 0
# A push from a non-login shell (ssh hub 'git push', fleet-sync) has no ~/.local/bin on PATH, so
# `uv` was "not found" and Tier 1 went RED on the hub 2026-09-27 while the same command passed
# in a login shell. The post-merge template already exports this.
export PATH="$HOME/.local/bin:$HOME/bin:$PATH"

# Tier 0 — hermetic fast list. Runs before the locked-corpus check so a locked clone still runs it.
# It never stamps. Skips clean when uv or the list is missing, and GESTALT_TIER0_SKIP=1 skips it alone.
if [ "${GESTALT_TIER0_SKIP:-}" = "1" ]; then
  echo "gestalt: Tier 0 SKIPPED (GESTALT_TIER0_SKIP=1)"
elif [ -f tests/fast-tier.txt ] && command -v uv >/dev/null 2>&1; then
  FAST_LIST=$(grep -v '^[[:space:]]*#' tests/fast-tier.txt | grep .)
  TMO=""
  command -v timeout >/dev/null 2>&1 && TMO="timeout 90"
  # shellcheck disable=SC2086
  FAST_OUT=$($TMO uv run --quiet --with pytest --with pyyaml --with markdown \
    python -m pytest $FAST_LIST -q -x -p no:cacheprovider 2>&1)
  FAST_RC=$?
  if [ $FAST_RC -ne 0 ]; then
    echo "$FAST_OUT" | tail -40
    echo "gestalt: Tier 0 RED (rc=$FAST_RC; 124 means timeout) -- not pushing" >&2
    exit 1
  fi
  echo "gestalt: Tier 0 green (fast list)"
else
  echo "gestalt: Tier 0 SKIPPED (no tests/fast-tier.txt or no uv)"
fi

# knowledge/ is git-crypt encrypted; a locked clone cannot run the corpus-reading
# tests (test_golden_set.py) or check-staleness.sh meaningfully. Skip clean rather
# than fail a push over a capability this clone doesn't have.
if [ -f knowledge/<kb-entry>.md ] && head -c 9 knowledge/<kb-entry>.md 2>/dev/null | grep -q GITCRYPT; then
  echo "gestalt: Tier 1 SKIPPED (corpus locked -- git-crypt unlock to run tests)"
  exit 0
fi

TIER1_OUT=$(uv run --quiet --with pytest --with pyyaml python -m pytest tests/test_golden_set.py tests/test_routing.py tests/test_eval_floors.py -k 'not hybrid' -q 2>&1)
TIER1_RC=$?
if [ $TIER1_RC -ne 0 ]; then
  echo "$TIER1_OUT" | tail -40
  echo "gestalt: Tier 1 RED -- pre-push tests failed, not stamping HEAD" >&2
  exit 1
fi

# Retrieval-quality ratchet — logic lives in tools/retrieval-ratchet-check.sh (skip-clean
# on nodes that cannot run it honestly; standalone file because macOS bash 3.2 cannot parse
# this logic inline in a heredoc).
sh tools/retrieval-ratchet-check.sh || exit 1

STALE_OUT=$(bash bin/check-staleness.sh 2>&1)
STALE_RC=$?
if [ $STALE_RC -ne 0 ]; then
  echo "$STALE_OUT" | tail -40
  echo "gestalt: Tier 1 RED -- check-staleness.sh failed, not stamping HEAD" >&2
  exit 1
fi

git rev-parse HEAD > "$REPO/.git/gestalt-tests-ok"
echo "gestalt: Tier 1 green, stamped $(git rev-parse --short HEAD)"
