#!/usr/bin/env python3
"""Capture one skill run from a Claude Code transcript for the output-assertion evals (X13, 2026-10-06).

A run starts at the user message holding `<command-name>/<skill></command-name>` or at a `Skill` tool call
for that skill. It ends at the next real human message. The capture is the assistant's FINAL message of the
run: the text blocks after its last tool call. That is the text the output assertions in evals/configs/ are
written against (a summary, a report, a one-line confirmation). `--all-text` keeps every assistant text block.
Tool results, thinking, sidechains and system reminders never enter the capture.

The capture is redacted (secrets, tokens, emails, tailnet addresses, ntfy topics) and written to
personal/eval-captures/<skill>/<date>-<short id>.md. That directory is git-crypt encrypted. The tool refuses
any other destination unless --out is given, and it never writes into evals/.

Usage:
  tools/capture-skill-run.py <transcript.jsonl> <skill> [--nth N] [--all-text] [--out FILE]
  tools/capture-skill-run.py --latest <skill> [--projects-dir DIR] [--out FILE]
"""
import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CAPTURES = REPO / "personal" / "eval-captures"
EVALS = REPO / "evals"

GENERIC_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "users.noreply.github.com"}
GENERIC_EMAIL_LOCALS = {"noreply", "no-reply", "user", "you", "name", "someone"}

# (label, regex). Order matters: specific token shapes first.
REDACTIONS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)),
    ("token", re.compile(r"\b(?:sk-ant-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                         r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|hf_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{15,}"
                         r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})")),
    ("bearer", re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("ntfy-topic", re.compile(r"(?i)(ntfy\.sh/|NTFY_TOPIC\s*[=:]\s*[\"']?)[A-Za-z0-9_-]{6,}")),
    ("tailnet-host", re.compile(r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.ts\.net\b")),
    ("tailnet-ip", re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b|\bfd7a:115c:a1e0:[0-9a-f:]+", re.I)),
    ("secret-assignment", re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL)[A-Z0-9_]*)\s*([=:])\s*[\"']?[^\s\"'`,;]{6,}")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b")),
    ("long-secret", re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{40,}\b")),
]
_SYSTEM_TAG = re.compile(r"<(system-reminder|local-command-stdout|local-command-caveat|command-name|command-message|command-args)>.*?</\1>", re.S)


def redact(text: str) -> tuple[str, int]:
    """Return (redacted text, number of replacements)."""
    count = 0

    def sub_for(label):
        def _sub(m):
            nonlocal count
            if label == "email":
                local, _, dom = m.group(0).lower().partition("@")
                if dom in GENERIC_EMAIL_DOMAINS or local in GENERIC_EMAIL_LOCALS:
                    return m.group(0)
            if label == "long-secret" and (m.group(0).count("-") > 6 or m.group(0).startswith(("http", "claude-"))):
                return m.group(0)
            count += 1
            if label == "secret-assignment":
                return f"{m.group(1)}{m.group(2)}[REDACTED]"
            if label == "ntfy-topic":
                return f"{m.group(1)}[REDACTED]"
            if label == "bearer":
                return f"{m.group(1)} [REDACTED]"
            return f"[REDACTED:{label}]"
        return _sub

    for label, rx in REDACTIONS:
        text = rx.sub(sub_for(label), text)
    return text, count


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_human(o: dict) -> bool:
    """A real human turn: a user message that is not a tool result, meta expansion or system reminder."""
    if o.get("type") != "user" or o.get("isMeta") or o.get("isSidechain"):
        return False
    c = (o.get("message") or {}).get("content")
    if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
        return False
    t = _SYSTEM_TAG.sub("", _text_of(c)).strip()
    return bool(t) or bool(re.search(r"<command-name>", _text_of(c)))


def _invokes(o: dict, skill: str) -> bool:
    c = (o.get("message") or {}).get("content")
    if o.get("type") == "user" and not o.get("isMeta"):
        m = re.search(r"<command-name>/?([^<]+)</command-name>", _text_of(c))
        return bool(m) and m.group(1).strip().split(":")[-1] == skill
    if o.get("type") == "assistant" and isinstance(c, list):
        return any(isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill"
                   and str((b.get("input") or {}).get("skill", "")).split(":")[-1] == skill for b in c)
    return False


def read_events(path: Path) -> list[dict]:
    out = []
    with open(path, errors="replace") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if isinstance(o, dict) and not o.get("isSidechain") and o.get("type") in ("user", "assistant"):
                out.append(o)
    return out


def find_runs(events: list[dict], skill: str) -> list[tuple[int, int]]:
    """Return (start, end) event index ranges, one per invocation; end is exclusive."""
    starts = [i for i, o in enumerate(events) if _invokes(o, skill)]
    runs = []
    for s in starts:
        if runs and s < runs[-1][1]:
            continue  # a Skill tool call inside the previous run
        e = len(events)
        for j in range(s + 1, len(events)):
            if _is_human(events[j]):
                e = j
                break
        runs.append((s, e))
    return runs


def extract_output(events: list[dict], start: int, end: int, all_text: bool = False) -> str:
    """Assistant text of one run. Default: the text blocks after the last tool call (the final message)."""
    pieces: list[str] = []
    for o in events[start:end]:
        if o.get("type") != "assistant":
            continue
        for b in (o.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and not all_text:
                pieces = []
            elif b.get("type") == "text" and b.get("text", "").strip():
                pieces.append(b["text"].strip())
    return _SYSTEM_TAG.sub("", "\n\n".join(pieces)).strip()


def short_id(path: Path, nth: int) -> str:
    return f"{path.stem[:8]}-{nth}" if nth else path.stem[:8]


def run_date(events: list[dict], start: int) -> str:
    return str(events[start].get("timestamp", ""))[:10] or "unknown"


def safe_target(out: Path | None, skill: str, date: str, sid: str) -> Path:
    if out is None:
        return CAPTURES / skill / f"{date}-{sid}.md"
    out = out.resolve()
    if EVALS in out.parents or out == EVALS:
        sys.exit(f"refusing to write into evals/: {out}")
    return out


def capture(transcript: Path, skill: str, nth: int = 0, all_text: bool = False, out: Path | None = None) -> Path:
    """nth counts from the last run: 0 is the newest run in the transcript, 1 the one before."""
    events = read_events(transcript)
    runs = find_runs(events, skill)
    if len(runs) <= nth:
        sys.exit(f"{transcript.name}: {len(runs)} run(s) of /{skill}, cannot take --nth {nth}")
    start, end = runs[-1 - nth]
    body = extract_output(events, start, end, all_text)
    if not body:
        sys.exit(f"{transcript.name}: run has no assistant text after the last tool call")
    body, n = redact(body)
    date = run_date(events, start)
    sid = short_id(transcript, nth)
    target = safe_target(out, skill, date, sid)
    header = (f"<!-- skill: {skill} | date: {date} | source transcript: {transcript.stem} | "
              f"redactions: {n} | extract: {'all-text' if all_text else 'final-message'} -->\n\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(header + body + "\n")
    return target


def latest_transcript(skill: str, projects_dir: Path) -> Path:
    cands = sorted(projects_dir.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    needle = (f"<command-name>/{skill}<", f"<command-name>{skill}<", f'"skill":"{skill}"')
    for p in cands:
        with open(p, errors="replace") as f:
            for line in f:
                if any(n in line for n in needle):
                    if find_runs(read_events(p), skill):
                        return p
                    break
    sys.exit(f"no transcript with a run of /{skill} under {projects_dir}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("transcript", nargs="?", help="transcript .jsonl path")
    ap.add_argument("skill", nargs="?")
    ap.add_argument("--latest", metavar="SKILL", help="find the newest transcript with a run of SKILL")
    ap.add_argument("--projects-dir", default=str(Path.home() / ".claude" / "projects"))
    ap.add_argument("--nth", type=int, default=0, help="0 = newest run in the transcript, 1 = the one before")
    ap.add_argument("--all-text", action="store_true", help="keep every assistant text block, not just the final message")
    ap.add_argument("--out", type=Path, help="write here instead of personal/eval-captures/ (never into evals/)")
    a = ap.parse_args(argv)
    if a.latest:
        skill, transcript = a.latest, latest_transcript(a.latest, Path(a.projects_dir))
    elif a.transcript and a.skill:
        skill, transcript = a.skill, Path(a.transcript)
    else:
        ap.error("give <transcript> <skill>, or --latest <skill>")
    print(capture(transcript, skill, a.nth, a.all_text, a.out))


if __name__ == "__main__":
    main()
