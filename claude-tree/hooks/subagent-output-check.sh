#!/usr/bin/env bash
# SubagentStop hook — advisory-only check for ungrounded subagent returns.
#
# Per code.claude.com/docs/en/hooks (fetched 2026-09-06), the SubagentStop
# payload carries session_id, prompt_id, transcript_path, cwd, permission_mode,
# hook_event_name, agent_id, agent_type, and last_assistant_message (the
# subagent's final assistant text, provided directly so callers do not need to
# re-read the transcript). This hook uses last_assistant_message first, and
# falls back to scanning transcript_path for the last assistant turn only if
# that field is absent (older harness build, or a payload shape this hook
# hasn't seen). NEEDS APPROVAL: last_assistant_message's exact key name could
# not be confirmed against a live payload capture (no SubagentStop fired during
# development) -- only against the docs page prose, which is not a JSON schema
# dump; if the harness names this field differently, the fallback below is what
# actually fires in practice.
#
# Per claude-tree/references/discipline-block.md RETURN DISCIPLINE, a subagent
# following the standard discipline block returns numbered single-claim rows
# each ending "... -- Source: {URL or file:line}". This hook flags a return
# that reads as substantial prose (>= WORD_COUNT_FLOOR words) but contains no
# Source: slot anywhere -- advisory only, never blocks SubagentStop, never
# exits nonzero.

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
source "$SCRIPT_DIR/lib.sh" && gestalt_init

INPUT=$(cat)

python3 -c "
import json, re, sys
from datetime import datetime

try:
    payload = json.loads(sys.stdin.read() or '{}')
except Exception:
    payload = {}

agent_type = payload.get('agent_type', 'unknown-agent')

# Primary source: last_assistant_message, provided directly on SubagentStop
# payloads so callers do not need to re-read the transcript.
output_text = str(payload.get('last_assistant_message', '') or '')

if not output_text:
    transcript_path = payload.get('transcript_path', '')
    try:
        if transcript_path:
            with open(transcript_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = [l.strip() for l in f if l.strip()]
            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if entry.get('type') != 'assistant':
                    continue
                message = entry.get('message', {})
                content = message.get('content', '') if isinstance(message, dict) else ''
                if isinstance(content, list):
                    content = ' '.join(
                        c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text'
                    )
                text = str(content).strip()
                if text:
                    output_text = text
                    break
    except Exception:
        output_text = ''

output_text = output_text.strip()

# Heuristic only, deliberately cheap: a short return (an abstain, a NO FINDINGS
# line, a one-word ack) is not the 'prose paragraphs with factual-sounding
# claims' this hook looks for -- skip it rather than nag on a legitimate short
# return.
WORD_COUNT_FLOOR = 40
has_source_slot = bool(re.search(r'\bSource\s*:', output_text, re.IGNORECASE))
is_abstain = bool(re.match(r'^\s*NO FINDINGS', output_text, re.IGNORECASE))
word_count = len(output_text.split())

if output_text and not is_abstain and word_count >= WORD_COUNT_FLOOR and not has_source_slot:
    print(f'{datetime.now().astimezone().isoformat()} ADVISORY: subagent ({agent_type}) return has {word_count} words of prose with no Source: line -- see discipline-block.md RETURN DISCIPLINE.')
" <<< "$INPUT" >> "$LOG" 2>&1 || true

exit 0
