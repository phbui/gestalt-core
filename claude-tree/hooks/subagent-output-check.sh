#!/usr/bin/env bash
# SubagentStop hook -- advisory-only checks on subagent returns.
#
# Per code.claude.com/docs/en/hooks (SubagentStop, fetched 2026-11-09) the payload
# carries session_id, transcript_path, agent_id, agent_type, agent_transcript_path
# and last_assistant_message ("the text content of the subagent's final response, so
# hooks can access it without parsing the transcript file"). This hook reads only
# last_assistant_message. It never opens a transcript.
#
# Second check: a verifier or reviewer return must end with one line
# "VERDICT: <PASS|FAIL|PARTIAL|BLOCKED> -- <one sentence>". A well-formed line is
# logged as one JSON record in $STATE_DIR/subagent-verdicts.jsonl. A missing or
# malformed line writes "VERDICT-MISSING" to health.log. Advisory only.
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

STATE_DIR_OUT="$STATE_DIR" python3 -c "
import json, os, re, sys
from datetime import datetime

try:
    payload = json.loads(sys.stdin.read() or '{}')
except Exception:
    payload = {}

agent_type = payload.get('agent_type', 'unknown-agent')

output_text = str(payload.get('last_assistant_message', '') or '')

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

VERDICT_RE = re.compile(r'^VERDICT:\s*(PASS|FAIL|PARTIAL|BLOCKED)\s+(?:\u2014|--?)\s+(\S.*)\$')
if str(agent_type).lower() in ('verifier', 'reviewer'):
    last = ''
    for ln in reversed(output_text.splitlines()):
        if ln.strip():
            last = ln.strip()
            break
    m = VERDICT_RE.match(last)
    now = datetime.now().astimezone().isoformat()
    if m:
        try:
            rec = {'ts': now, 'session_id': payload.get('session_id', ''), 'agent_type': agent_type,
                   'verdict': m.group(1), 'sentence': m.group(2)}
            with open(os.path.join(os.environ.get('STATE_DIR_OUT', ''), 'subagent-verdicts.jsonl'), 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec) + '\n')
        except Exception:
            pass
    else:
        print(f'{now} VERDICT-MISSING: subagent ({agent_type}) return lacks a well-formed final VERDICT line -- see agents/{agent_type}.md.')
" <<< "$INPUT" >> "$LOG" 2>&1 || true

exit 0
