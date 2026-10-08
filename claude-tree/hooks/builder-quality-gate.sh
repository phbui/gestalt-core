#!/usr/bin/env bash
# Stop hook for builder agents — enforces lint/typecheck on modified files.
# Exit 0 = allow stop. Exit 2 = block stop (force rework).
#
# Strategy:
#   1. Taskfile present → discover tasks matching lint/format/type/check, run them
#   2. No Taskfile → detect tools by config file presence, run from correct directory
# Nothing is hardcoded — the repo's own config determines what runs.

set -uo pipefail

INPUT=$(cat)

if echo "$INPUT" | jq -e '.stop_hook_active // false' > /dev/null 2>&1; then
  exit 0
fi

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
if [ -z "$REPO_ROOT" ]; then
  exit 0
fi

MODIFIED=$(git diff --name-only HEAD 2>/dev/null; git diff --name-only --cached 2>/dev/null)
MODIFIED=$(echo "$MODIFIED" | sort -u | grep -v '^$')

if [ -z "$MODIFIED" ]; then
  exit 0
fi

# Hooks run without the login shell's nvm setup, so `npx`, `node` and every
# node_modules/.bin shim are missing and each JS check "fails" with
# "command not found". Use the repo's pinned major from .nvmrc when nvm has it,
# else the newest installed Node.
if ! command -v node &>/dev/null && [ -d "$HOME/.nvm/versions/node" ]; then
  WANT=$(tr -d 'v[:space:]' < "$REPO_ROOT/.nvmrc" 2>/dev/null || true)
  NODE_BIN=$(ls -d "$HOME/.nvm/versions/node/v${WANT}"*/bin 2>/dev/null | sort -V | tail -1)
  [ -z "$NODE_BIN" ] && NODE_BIN=$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)
  [ -n "$NODE_BIN" ] && export PATH="$NODE_BIN:$PATH"
fi

ERRORS=""
HAS_PY=$(echo "$MODIFIED" | grep -qE '\.py$' && echo 1 || true)
HAS_TS=$(echo "$MODIFIED" | grep -qE '\.(ts|tsx|js|jsx)$' && echo 1 || true)

# === Strategy 1: Taskfile (run whatever tasks the repo defines) ===
if { [ -f "$REPO_ROOT/Taskfile.yaml" ] || [ -f "$REPO_ROOT/Taskfile.yml" ]; } && command -v task &>/dev/null; then
  # Add repo-local node_modules/.bin so turbo and local CLIs are found
  export PATH="$REPO_ROOT/node_modules/.bin:$PATH"
  TASK_LIST=$(cd "$REPO_ROOT" && task --list 2>/dev/null || true)

  # Collect quality-check task names into an array
  declare -a ALL_TASKS=()
  while IFS= read -r line; do
    TASK_NAME=$(echo "$line" | sed -n 's/^\* \(.*\):  .*/\1/p')
    [ -z "$TASK_NAME" ] && continue
    LAST_SEG=$(echo "$TASK_NAME" | sed 's/.*[:\-]//')
    echo "$LAST_SEG" | grep -qxiE '(lint|format|formatting|types?|check|style|typecheck|check-types)' || continue
    echo "$TASK_NAME" | grep -qiE '(lockfile|startup|codegen|install|fix|ensure|setup|ecr|build|deploy|terraform|argocd)' && continue
    ALL_TASKS+=("$TASK_NAME")
  done <<< "$TASK_LIST"

  # If workspace-level tasks exist (py:*, ts:*), only run those — they cover the whole repo.
  # Otherwise run whatever app-specific tasks were found.
  declare -a RUN_TASKS=()
  HAS_WS_PY=$(printf '%s\n' "${ALL_TASKS[@]}" | grep -cE '^py:' || true)
  HAS_WS_TS=$(printf '%s\n' "${ALL_TASKS[@]}" | grep -cE '^ts:' || true)

  for t in "${ALL_TASKS[@]}"; do
    PREFIX=$(echo "$t" | cut -d: -f1)
    case "$PREFIX" in
      py|ts|go|rs)
        # Workspace-level task — always include
        RUN_TASKS+=("$t") ;;
      *)
        # App-specific task — only include if no workspace-level task for that language
        if [ "$HAS_WS_PY" -gt 0 ] || [ "$HAS_WS_TS" -gt 0 ]; then continue; fi
        RUN_TASKS+=("$t") ;;
    esac
  done

  for TASK_NAME in "${RUN_TASKS[@]}"; do
    # Language gating
    if echo "$TASK_NAME" | grep -qi '^py[:\-]' && [ -z "$HAS_PY" ]; then continue; fi
    if echo "$TASK_NAME" | grep -qi '^ts[:\-]' && [ -z "$HAS_TS" ]; then continue; fi
    if echo "$TASK_NAME" | grep -qi '^go[:\-]' && ! echo "$MODIFIED" | grep -qE '\.go$'; then continue; fi
    if echo "$TASK_NAME" | grep -qi '^rs[:\-]' && ! echo "$MODIFIED" | grep -qE '\.rs$'; then continue; fi

    TASK_OUT=$(cd "$REPO_ROOT" && task "$TASK_NAME" 2>&1 | tail -30)
    if [ $? -ne 0 ]; then
      ERRORS="${ERRORS}\n## task $TASK_NAME\n\`\`\`\n${TASK_OUT}\n\`\`\`\n"
    fi
  done

# === Strategy 2: No Taskfile — detect tools by config file presence ===
else
  find_project_root() {
    local file="$1" config="$2"
    local dir
    dir=$(dirname "$REPO_ROOT/$file")
    while [ "$dir" != "/" ]; do
      if [ -f "$dir/$config" ]; then echo "$dir"; return; fi
      dir=$(dirname "$dir")
    done
  }

  declare -A SEEN_CHECKS

  for file in $MODIFIED; do
    for config in pyrightconfig.json pyproject.toml ruff.toml biome.json biome.jsonc .eslintrc.js .eslintrc.json tsconfig.json; do
      PROOT=$(find_project_root "$file" "$config")
      [ -z "$PROOT" ] && continue
      KEY="${PROOT}::${config}"
      [ -n "${SEEN_CHECKS[$KEY]+x}" ] && continue
      SEEN_CHECKS[$KEY]=1

      case "$config" in
        pyrightconfig.json)
          [ -z "$HAS_PY" ] && continue
          if command -v basedpyright &>/dev/null; then
            OUT=$(cd "$PROOT" && basedpyright --level error 2>&1 | tail -20)
            [ $? -ne 0 ] && ERRORS="${ERRORS}\n## basedpyright ($(basename "$PROOT"))\n\`\`\`\n${OUT}\n\`\`\`\n"
          elif command -v pyright &>/dev/null; then
            OUT=$(cd "$PROOT" && pyright 2>&1 | tail -20)
            [ $? -ne 0 ] && ERRORS="${ERRORS}\n## pyright ($(basename "$PROOT"))\n\`\`\`\n${OUT}\n\`\`\`\n"
          fi
          ;;
        pyproject.toml)
          [ -z "$HAS_PY" ] && continue
          if grep -qE '\[tool\.(based)?pyright\]' "$PROOT/pyproject.toml" 2>/dev/null; then
            [ -n "${SEEN_CHECKS["${PROOT}::pyrightconfig.json"]+x}" ] && continue
            if command -v basedpyright &>/dev/null; then
              OUT=$(cd "$PROOT" && basedpyright --level error 2>&1 | tail -20)
              [ $? -ne 0 ] && ERRORS="${ERRORS}\n## basedpyright ($(basename "$PROOT"))\n\`\`\`\n${OUT}\n\`\`\`\n"
            elif command -v pyright &>/dev/null; then
              OUT=$(cd "$PROOT" && pyright 2>&1 | tail -20)
              [ $? -ne 0 ] && ERRORS="${ERRORS}\n## pyright ($(basename "$PROOT"))\n\`\`\`\n${OUT}\n\`\`\`\n"
            fi
          fi
          if { [ -f "$PROOT/ruff.toml" ] || grep -q '\[tool\.ruff\]' "$PROOT/pyproject.toml" 2>/dev/null; } && command -v ruff &>/dev/null; then
            OUT=$(cd "$PROOT" && ruff check 2>&1 | tail -20)
            [ $? -ne 0 ] && ERRORS="${ERRORS}\n## ruff ($(basename "$PROOT"))\n\`\`\`\n${OUT}\n\`\`\`\n"
          fi
          ;;
        ruff.toml)
          [ -z "$HAS_PY" ] && continue
          command -v ruff &>/dev/null || continue
          [ -n "${SEEN_CHECKS["${PROOT}::pyproject.toml"]+x}" ] && continue
          OUT=$(cd "$PROOT" && ruff check 2>&1 | tail -20)
          [ $? -ne 0 ] && ERRORS="${ERRORS}\n## ruff ($(basename "$PROOT"))\n\`\`\`\n${OUT}\n\`\`\`\n"
          ;;
        biome.json|biome.jsonc)
          [ -z "$HAS_TS" ] && continue
          command -v biome &>/dev/null || continue
          OUT=$(cd "$PROOT" && biome ci . 2>&1 | tail -20)
          [ $? -ne 0 ] && ERRORS="${ERRORS}\n## biome ($(basename "$PROOT"))\n\`\`\`\n${OUT}\n\`\`\`\n"
          ;;
        .eslintrc.js|.eslintrc.json)
          [ -z "$HAS_TS" ] && continue
          [ -f "$PROOT/node_modules/.bin/eslint" ] || continue
          OUT=$(cd "$PROOT" && npx eslint . 2>&1 | tail -20)
          [ $? -ne 0 ] && ERRORS="${ERRORS}\n## eslint ($(basename "$PROOT"))\n\`\`\`\n${OUT}\n\`\`\`\n"
          ;;
        tsconfig.json)
          [ -z "$HAS_TS" ] && continue
          [ -n "${SEEN_CHECKS["${PROOT}::biome.json"]+x}" ] && continue
          [ -n "${SEEN_CHECKS["${PROOT}::biome.jsonc"]+x}" ] && continue
          [ -n "${SEEN_CHECKS["${PROOT}::.eslintrc.js"]+x}" ] && continue
          [ -n "${SEEN_CHECKS["${PROOT}::.eslintrc.json"]+x}" ] && continue
          [ -f "$PROOT/node_modules/.bin/tsc" ] || continue
          OUT=$(cd "$PROOT" && npx tsc --noEmit 2>&1 | tail -20)
          [ $? -ne 0 ] && ERRORS="${ERRORS}\n## tsc ($(basename "$PROOT"))\n\`\`\`\n${OUT}\n\`\`\`\n"
          ;;
      esac
    done
  done
fi

# --- Verdict ---
if [ -n "$ERRORS" ]; then
  printf '{"additionalContext":"QUALITY GATE FAILED. Fix these issues before stopping:\\n%s"}' "$ERRORS" >&2
  exit 2
else
  cat <<'EOF'
{"additionalContext":"Quality gate passed — lint and type checks clean on all modified files."}
EOF
  exit 0
fi
