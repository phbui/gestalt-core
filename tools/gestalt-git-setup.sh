#!/usr/bin/env bash
# gestalt-git-setup.sh — make this clone survive multi-machine pulls.
#
# Run once per clone (idempotent). Fixes the three things that make a fleet pull hurt:
#   1. `merge=ours` on MANIFEST.md/GRAPH.md is inert unless the driver exists in *this* clone's
#      config — the attribute names a driver, it does not define one. Without it every node's
#      regenerated indices conflict on every pull.
#   2. .git/hooks/{post-merge,post-checkout,post-commit,pre-push} ship as git-lfs stubs that
#      `exit 2` when git-lfs is absent, so a merge ends in an error even when it succeeded.
#      Ours call git-lfs only when it is installed.
#   3. Nothing regenerated the indices after a merge, so MANIFEST.md silently drifted from
#      knowledge/ until the next write. The post-merge hook now rebuilds when knowledge/ moved.
#
# See knowledge/gestalt.md ^git-crypt-merge for resolving an encrypted-entry conflict.
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$REPO"
HOOKS="$(git rev-parse --git-path hooks)"

git config merge.ours.driver true
echo "configured: merge.ours.driver = true"

install_hook() {
  local name path body
  name="$1"; body="$2"; path="$HOOKS/$name"
  if [ -f "$path" ] && ! grep -q "gestalt-git-setup" "$path"; then
    cp "$path" "$path.pre-gestalt.bak"
  fi
  printf '%s\n' "$body" > "$path"
  chmod +x "$path"
  echo "installed hook: $name"
}

LFS_SHIM='command -v git-lfs >/dev/null 2>&1 && git lfs %s "$@" || true'

install_hook post-merge "$(cat <<EOF
#!/bin/sh
# installed by tools/gestalt-git-setup.sh
$(printf "$LFS_SHIM" post-merge)
# A hook fired over SSH inherits a minimal PATH; ~/.local/bin holds rg and the index tools.
export PATH="\$HOME/.local/bin:\$HOME/bin:\$PATH"
# Regenerate the derived indices whenever a merge moved knowledge/ or the indices themselves, from what git
# holds rather than the working tree, so every node regenerates the same bytes (tools/gestalt, FROM_INDEX).
export GESTALT_INDEX_FROM_INDEX=1
if git diff-tree -r --name-only ORIG_HEAD HEAD 2>/dev/null | grep -qE '^(knowledge/|MANIFEST\.md|GRAPH\.md)'; then
  if "\$(git rev-parse --show-toplevel)/tools/gestalt" rebuild >/dev/null 2>&1; then
    echo "gestalt: indices regenerated after merge"
  else
    echo "gestalt: index regeneration FAILED (see tools/gestalt guards) — do not commit MANIFEST/GRAPH from this state" >&2
  fi
  # Hub only, stage 7 item C: a merge landing here with no Claude session afterwards used to
  # leave the search-index artifact (.search/gestalt.db) stale and unpublished, because the
  # only place that ran the builder and then published was claude-tree/hooks/gestalt-session-start.sh
  # (a SessionStart hook, so it never fires for a bare "git pull" on the hub). Run the same
  # builder here too (no --force, so it is a cheap no-op when the corpus did not actually
  # change per its own docstring: "skipped if up to date"), then publish immediately so peers
  # can fetch the artifact via fleet-sync index-fetch instead of embedding it themselves.
  REPO_ROOT="\$(git rev-parse --show-toplevel)"
  FLEET_ENV="\$HOME/.fleet/fleet.env"; [ -f "\$FLEET_ENV" ] && . "\$FLEET_ENV"
  HUB="\${FLEET_HUB:-node.example.ts.net}"; HUBN="\${FLEET_HUB_NAME:-\${HUB%%.*}}"
  THIS_HOST="\${FLEET_HOST:-\$(hostname -s 2>/dev/null || hostname)}"
  if [ "\$THIS_HOST" = "\$HUBN" ]; then
    if [ -f "\$REPO_ROOT/tools/gestalt-index-builder.py" ]; then
      "\${GESTALT_PYTHON:-python3}" "\$REPO_ROOT/tools/gestalt-index-builder.py" >/dev/null 2>&1 && echo "gestalt: search index builder ran (no-op if already current)"
    fi
    if command -v fleet-sync >/dev/null 2>&1; then
      fleet-sync index-publish >/dev/null 2>&1 && echo "gestalt: search index published (fleet-sync index-publish)"
    fi
  fi
fi
EOF
)"

install_hook post-checkout "$(cat <<EOF
#!/bin/sh
# installed by tools/gestalt-git-setup.sh
$(printf "$LFS_SHIM" post-checkout)
EOF
)"

install_hook post-commit "$(cat <<EOF
#!/bin/sh
# installed by tools/gestalt-git-setup.sh
$(printf "$LFS_SHIM" post-commit)
EOF
)"

# The pre-push body lives in tools/ci/pre-push.sh and the installed hook only execs it, so a pull
# updates the gate with no re-install and the hook cannot drift from the repo (2026-10-06).
install_hook pre-push "$(cat <<'PPEOF'
#!/bin/sh
# installed by tools/gestalt-git-setup.sh -- thin wrapper, the gate is tools/ci/pre-push.sh in the checkout.
REPO="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -n "$REPO" ] && [ -f "$REPO/tools/ci/pre-push.sh" ]; then
  exec sh "$REPO/tools/ci/pre-push.sh" "$@"
fi
echo "gestalt: pre-push gate script missing (tools/ci/pre-push.sh), not gating" >&2
exit 0
PPEOF
)"

echo
echo "done. pull with:  git pull --rebase   (indices auto-regenerate; knowledge/ conflicts -> tools/resolve-encrypted-conflict.sh)"
