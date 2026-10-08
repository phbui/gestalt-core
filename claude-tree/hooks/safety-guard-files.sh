#!/usr/bin/env bash
# PreToolUse hook for Write|Edit tools — Tier 3
# Blocks access to sensitive files: credentials, keys, secrets, env files.

INPUT=$(cat)
FILE_PATH=$(jq -r '.tool_input.file_path // empty' <<< "$INPUT")
[ -z "$FILE_PATH" ] && exit 0

deny() {
  jq -n --arg reason "BLOCKED by safety-guard-files: $1. Ask the user to modify this file manually." \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$reason}}'
  exit 0
}

# ============================================================
# ALLOWLIST — safe patterns that look sensitive but aren't
# ============================================================

echo "$FILE_PATH" | grep -qE '\.env\.(example|sample|template|test)$' && exit 0
echo "$FILE_PATH" | grep -qE '^/tmp/' && exit 0
echo "$FILE_PATH" | grep -qE '^/var/tmp/' && exit 0

# ============================================================
# DENYLIST — sensitive file patterns
# ============================================================

# Cryptographic keys and certificates
echo "$FILE_PATH" | grep -qE '\.(pem|key|p12|pfx|ppk)$' && deny "Cryptographic key/certificate file"

# Encrypted secrets (workspace pattern: *.secret.enc.*)
echo "$FILE_PATH" | grep -qE '\.secret\.enc\.' && deny "Encrypted secrets file"

# Environment files with credentials
echo "$FILE_PATH" | grep -qE '\.envrc$' && deny ".envrc file (may contain AWS vault credentials)"
echo "$FILE_PATH" | grep -qE '(^|/)\.env$' && deny ".env file (likely contains API keys, DB URLs, or secrets)"

# Named credential files
echo "$FILE_PATH" | grep -qE '(^|/)(credentials|secrets)\.(json|yaml|yml|toml)$' && deny "Credentials/secrets file"

# AWS
echo "$FILE_PATH" | grep -qE '\.aws/(credentials|config)$' && deny "AWS credentials file"

# Kubernetes
echo "$FILE_PATH" | grep -qE '\.kube/config$' && deny "Kubernetes config file"

# SSH
echo "$FILE_PATH" | grep -qE '\.ssh/(id_rsa|id_ed25519|id_ecdsa|id_dsa|authorized_keys)$' && deny "SSH key/config file"

# Package manager auth
echo "$FILE_PATH" | grep -qE '\.(netrc|npmrc|pypirc|pgpass)$' && deny "Package manager auth file"

# GPG
echo "$FILE_PATH" | grep -qE '\.gnupg/' && deny "GPG keyring directory"

exit 0
