#!/usr/bin/env python3
"""Print a turn-by-turn table of Agent spawns for a Claude Code session JSONL.

Usage: python3 tools/agent-turn-audit.py <session.jsonl> [--width 90]

One row per user turn: index, UTC time, request snippet, Agent calls in the
reply, other tool calls. A follow-up turn with two or more independent asks
and zero Agent calls is the symptom the proactive-parallelism rule guards
against (2026-11-05, session 20107cc7).
"""
import argparse
import json
import sys


def user_text(msg):
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--width", type=int, default=90)
    a = ap.parse_args()
    rows = []
    cur = None
    with open(a.path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ = rec.get("type")
            msg = rec.get("message") or {}
            if typ == "user":
                txt = user_text(msg).strip()
                if not txt:
                    continue  # tool_result-only message
                cur = {"t": (rec.get("timestamp") or "")[11:16], "text": txt, "agents": 0, "other": 0}
                rows.append(cur)
            elif typ == "assistant" and cur is not None:
                for b in msg.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        if b.get("name") in ("Agent", "Task"):
                            cur["agents"] += 1
                        else:
                            cur["other"] += 1
    # A slash command arrives as two user records: the command tag, then the
    # skill body. Fold the body's tool counts into the command row.
    merged = []
    for r in rows:
        if merged and merged[-1]["text"].startswith("<command-message>") and r["text"].startswith("Base directory for this skill"):
            merged[-1]["agents"] += r["agents"]
            merged[-1]["other"] += r["other"]
            continue
        merged.append(r)
    rows = merged
    w = a.width
    print(f"{'turn':>4} {'utc':5} {'agents':>6} {'other':>5}  request")
    flagged = 0
    for i, r in enumerate(rows, 1):
        snippet = " ".join(r["text"].split())[:w]
        print(f"{i:>4} {r['t']:5} {r['agents']:>6} {r['other']:>5}  {snippet}")
        if r["agents"] == 0 and r["other"] >= 15:
            flagged += 1
    print(f"\n{len(rows)} user turns, {sum(r['agents'] for r in rows)} Agent calls, "
          f"{flagged} turns with 15+ tool calls and no agents")
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
