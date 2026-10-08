#!/usr/bin/env python3
"""Generate `.cursor/` from `claude-tree/` so the mirror is true by construction.

    tools/sync-cursor-tree.py --check   exit 1 and list missing, stale and extra files
    tools/sync-cursor-tree.py --write   regenerate the tree (idempotent)

Edit `claude-tree/` only. Never edit `.cursor/` by hand. The translation is
documented in `claude-tree/rules/dual-agent-sync.md`. Stdlib only.

Three files in `.cursor/` have no source in `claude-tree/` and are never read,
written or removed: hetslam.md, hooks.json, mcp.json.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLAUDE = REPO / "claude-tree"
CURSOR = REPO / ".cursor"

# Cursor-only files: no source in claude-tree/. Never touched.
CURSOR_ONLY = {"hetslam.md", "hooks.json", "mcp.json"}

# Everything in claude-tree/ that is deliberately not mirrored. Nothing is
# skipped silently: a path under claude-tree/ that is neither generated nor
# listed here makes --check fail.
SKIP_PREFIXES = {
    "hooks/": "Claude hooks. Cursor has no PreToolUse or PostToolUse and wires its two hooks through .cursor/hooks.json. Only hooks/builder-quality-gate.sh is mirrored.",
    "settings.json": "Claude Code settings. Cursor has no equivalent file.",
    "agent-memory/": "Claude agent memory (memory: project). Cursor has no equivalent.",
    "references/.sync-accepted": "Claude-side hash ledger for the old by-hand sync. Meaningless for a generated tree.",
    "gestalt/": "Runtime logs written by hooks (health.log). Not configuration.",
}
# Files that exist in claude-tree/ but are deliberately NOT generated. The existing `.cursor/` copy
# is Cursor-native adaptation (a different mechanism, not an older version), so it is kept as is and
# not checked. Key is the claude-tree/ path. Decided 2026-10-06 after reading the diffs. The owner
# decides whether to drop an entry (and let claude-tree/ overwrite it) or to keep it.
SKIP_FILES: dict[str, str] = {
    "rules/proactive-parallelism.md": "Cursor cap is 4 concurrent Task() calls and its subagent types are `explore` and `generalPurpose`. Claude says 6 and `Explore`/`general-purpose`.",
    "references/agent-design-citations.md": "Cites the Cursor cap of 2-4 Task() agents instead of Claude's 2-6.",
    "references/verification.md": "Cursor has the `ReadLints` tool. Claude runs the project linter through Bash.",
    "skills/audit/SKILL.md": "Cursor copy adds a Cloud Agent mode note that has no Claude equivalent.",
    "skills/build/SKILL.md": "Cursor copy spawns builders with Task(subagent_type=\"builder\") and omits the Claude Agent Teams and quality-gate notes.",
    "skills/discuss/SKILL.md": "Cursor copy uses Task(subagent_type=\"explore\", model=\"fast\").",
    "skills/fix/SKILL.md": "Cursor copy uses Task(subagent_type=\"general-purpose\") and omits the Claude quality-gate note.",
    "skills/learn/SKILL.md": "Cursor copy uses Task(model=\"fast\", readonly=true) and no `memory: project`.",
}

# Cursor-only `<Background>` context injection, placed after the frontmatter of the Cursor skill.
SKILL_BACKGROUND = {
    "audit": "<Background>\n@requirements-engineering\n</Background>",
    "build": "<Background>@gestalt-grounding</Background>",
    "discuss": "<Background>@gestalt-grounding</Background>",
    "fix": "<Background>@gestalt-grounding</Background>",
    "investigate": "<Background>@gestalt-grounding</Background>",
    "learn": "<Background>@gestalt-grounding</Background>",
    "swarm-check": "<Background>@gestalt-grounding</Background>",
}

# The one hook Cursor shares.
MIRRORED_HOOKS = ("hooks/builder-quality-gate.sh",)

# Cursor rules carry a `description:` that Claude rules lack (Claude has no agent-decided mode).
# Seeded 2026-10-06 from the hand-written Cursor copies. A rule missing here
# gets its first prose line.
RULE_DESCRIPTIONS = {
    'agent-autonomy': 'Agent autonomy — never instruct the user to perform actions the agent can do itself; bias toward doing over describing',
    'caveman-safeguard': 'Protects gestalt knowledge base entries from output compression when caveman mode or similar skills are active',
    'cite-before-claim': 'Enforce confidence tiers and search-before-claim discipline for any behavioral claim about external libraries, frameworks, or tools — applies to every conversation',
    'codify-repeated-requests': 'Codify repeated requests — a second substantially-identical ask triggers a one-line offer to make it a skill/rule/cron; suggest-only',
    'discrimination-tests': 'Discrimination tests — every bug-fix commit carries a test shown to FAIL with the fix reverted; vacuous tests guard nothing',
    'dual-agent-sync': 'The .cursor/ tree is generated from claude-tree/ by tools/sync-cursor-tree.py. Edit claude-tree/ only, then run --write',
    'gestalt-operations': 'Gestalt operational conventions — write philosophy, linking rules, agent teams, symlinks. Applies when working on gestalt files.',
    'gestalt': 'Gestalt knowledge base — learned rules and knowledge for this workspace. Always check gestalt before answering questions or working on tasks.',
    'memory-activation': 'Activates Letta memory blocks injected at session start and runs the staleness check when gestalt/STALENESS_REPORT.md exists',
    'mermaid-diagrams': 'Use Mermaid syntax for diagrams in files; ASCII art for terminal output',
    'no-hard-wrapping': 'One paragraph is one line; never hard-wrap prose source unless asked',
    'no-silent-deferral': 'No silent deferral — mid-task work ends done-with-proof or surfaced-for-approval; no quiet "later" bucket',
    'no-unverified-claims': 'No unverified claims — transport acknowledgments are not outcomes; VERIFIED / DISPATCHED / BLOCKED never blur',
    'proactive-parallelism': 'Requires spinning up parallel agents only when subtasks are genuinely independent and the value justifies the token cost; single agent is the default for coding-shaped work. Includes agent selection, model choice, and worker prompt structure guidelines.',
    'prompt-intelligence': 'Detect underspecified prompts and trigger proactive web research for external tech — applies to every conversation',
    'requirements-engineering': 'Requirements engineering workflow for pitches and systems using IEEE 29148 methodology',
    'resource-cleanup': 'Tear down background shells, subagents, monitors, worktrees, and cloud compute you created; never kill what you cannot prove is finished',
    'search-source-coverage': 'Search coverage gaps — WebSearch cannot see reddit.com or stackoverflow.com; route around via APIs and never present a coverage gap as coverage',
    'srs-flow-requirements': 'SRS must include flow requirements connecting UI components, not just individual component specs',
}

AGENT_DESCRIPTIONS = {
    'builder': 'Build specialist for implementing code from SDDs, verifying against SRS acceptance criteria, and maintaining living design documents.',
    'designer': 'Design specialist for producing SDDs from requirements and ADRs. Translates WHAT (SRS) and WHY (ADRs) into HOW (SDD).',
    'learner': 'Deep repo scanner for gestalt knowledge capture. Operates in one of four scanner roles (Infra, Code, Test/Docs, External) depending on assignment.',
    'researcher': 'Research specialist for exploring architectural options and producing ADR drafts. Use when requirements imply choices with lasting impact that need documented decisions.',
    'reviewer': 'Verification specialist for gestalt knowledge review. Audits gestalt entries against actual codebase state, checking every factual claim against code.',
    'staleness-detector': 'Monitors git history to detect potentially stale gestalt entries by cross-referencing changed files against knowledge entry topics.',
    'syncer': 'External knowledge reconciler for gestalt. Queries Notion, Linear, and Slack via MCP tools and reconciles findings with existing gestalt entries.',
    'verifier': 'Independent verification specialist that validates implementations against requirements. Runs tests, checks edge cases, confirms acceptance criteria are actually met.',
}


def rewrite_paths(text: str) -> str:
    """`.claude/` becomes `.cursor/`. A rule path `.claude/rules/x.md` becomes `.cursor/rules/x.mdc`."""
    text = re.sub(r"\.claude/rules/([^\s`'\")]*?)\.md\b", r".cursor/rules/\1.mdc", text)
    # A leading `~/` or `$HOME/` names the real Claude home directory. Leave it.
    return re.sub(r"(?<!~/)(?<!HOME/)\.claude/", ".cursor/", text)


def split_frontmatter(text: str) -> tuple[list[str], str]:
    """Return (raw frontmatter lines without the fences, body). No frontmatter gives ([], text)."""
    if not text.startswith("---\n"):
        return [], text
    end = text.index("\n---\n", 3)
    return text[4:end].split("\n"), text[end + 5 :]


def fm_keys(lines: list[str]) -> dict[str, list[str]]:
    """Group raw frontmatter lines by top-level key (continuation lines stay with their key)."""
    out: dict[str, list[str]] = {}
    cur = None
    for ln in lines:
        m = re.match(r"([A-Za-z][\w-]*):", ln)
        if m:
            cur = m.group(1)
            out[cur] = [ln]
        elif cur:
            out[cur].append(ln)
    return out


def yaml_scalar(s: str, quote_commas: bool = False) -> str:
    if (quote_commas and "," in s) or re.match(r"^[\s*{\[&!|>'\"%@,#-]", s) or re.search(r": |\s#", s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def paths_list(lines: list[str]) -> list[str]:
    return [m.group(1) for ln in lines if (m := re.match(r'\s+-\s+"?(.*?)"?\s*$', ln))]


def first_prose(body: str) -> str:
    for ln in body.split("\n"):
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            return ln[:200]
    return ""


def gen_rule(src: Path) -> str:
    fm, body = split_frontmatter(src.read_text(encoding="utf-8"))
    keys = fm_keys(fm)
    body = rewrite_paths(body.lstrip("\n"))
    stem = src.stem
    desc = RULE_DESCRIPTIONS.get(stem) or first_prose(body)
    head = ["---", f"description: {yaml_scalar(desc)}"]
    if "paths" in keys:
        globs = ",".join(dict.fromkeys(rewrite_paths(p) for p in paths_list(keys["paths"])))
        head += [f"globs: {yaml_scalar(globs, True)}", "alwaysApply: false"]
    else:
        head += ["alwaysApply: true"]
    return "\n".join(head) + "\n---\n\n" + body


def gen_skill(src: Path) -> str:
    fm, body = split_frontmatter(src.read_text(encoding="utf-8"))
    keys = fm_keys(fm)
    head = ["---"] + keys["name"] + keys["description"] + ["disable-model-invocation: true"]
    bg = SKILL_BACKGROUND.get(src.parent.name)
    body = rewrite_paths(body.lstrip("\n"))
    return "\n".join(head) + "\n---\n" + (bg + "\n\n" if bg else "\n") + body


def gen_agent(src: Path) -> str:
    fm, body = split_frontmatter(src.read_text(encoding="utf-8"))
    keys = fm_keys(fm)
    desc = AGENT_DESCRIPTIONS.get(src.stem)
    desc_lines = [f"description: {yaml_scalar(desc)}"] if desc else keys["description"]
    return "\n".join(["---"] + keys["name"] + desc_lines) + "\n---\n" + rewrite_paths(body)


def gen_plain(src: Path) -> str:
    return rewrite_paths(src.read_text(encoding="utf-8"))


def expected() -> dict[str, str]:
    """Map of `.cursor/`-relative path to its generated content."""
    out: dict[str, str] = {}
    for p in sorted((CLAUDE / "rules").glob("*.md")):
        out[f"rules/{p.stem}.mdc"] = gen_rule(p)
    for p in sorted((CLAUDE / "agents").glob("*.md")):
        out[f"agents/{p.name}"] = gen_agent(p)
    for p in sorted((CLAUDE / "skills").rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(CLAUDE).as_posix()
        out[rel] = gen_skill(p) if p.name == "SKILL.md" and p.parent.parent.name == "skills" else gen_plain(p)
    for p in sorted((CLAUDE / "references").glob("*.md")):
        out[f"references/{p.name}"] = gen_plain(p)
    for rel in MIRRORED_HOOKS:
        if (CLAUDE / rel).exists():
            out[rel] = (CLAUDE / rel).read_text(encoding="utf-8")
    return out


def skipped_targets() -> set[str]:
    """`.cursor/` paths that SKIP_FILES keeps untouched."""
    return {k[:-3] + ".mdc" if k.startswith("rules/") else k for k in SKIP_FILES}


def unlisted_sources() -> list[str]:
    """Files under claude-tree/ that are neither generated nor in the skip table."""
    generated = ("rules/", "agents/", "skills/", "references/")
    bad = []
    for p in sorted(CLAUDE.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(CLAUDE).as_posix()
        if rel in MIRRORED_HOOKS or rel in SKIP_FILES:
            continue
        if any(rel == k or (k.endswith("/") and rel.startswith(k)) for k in SKIP_PREFIXES):
            continue
        if rel.startswith(generated):
            continue
        bad.append(rel)
    return bad


def current() -> dict[str, str]:
    out = {}
    for p in sorted(CURSOR.rglob("*")):
        if p.is_file():
            rel = p.relative_to(CURSOR).as_posix()
            if rel not in CURSOR_ONLY:
                out[rel] = p.read_text(encoding="utf-8")
    return out


def diff(want: dict[str, str], have: dict[str, str]):
    missing = sorted(set(want) - set(have))
    extra = sorted(set(have) - set(want))
    stale = sorted(k for k in want if k in have and want[k] != have[k])
    return missing, stale, extra


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    unlisted = unlisted_sources()
    if unlisted:
        print("claude-tree files neither generated nor in the skip table:", file=sys.stderr)
        for u in unlisted:
            print(f"  {u}", file=sys.stderr)
        return 2
    skipped = skipped_targets()
    want = {k: v for k, v in expected().items() if k not in skipped}
    have = {k: v for k, v in current().items() if k not in skipped}
    missing, stale, extra = diff(want, have)
    if args.check:
        for label, items in (("missing", missing), ("stale", stale), ("extra", extra)):
            for i in items:
                print(f"{label}: .cursor/{i}")
        if missing or stale or extra:
            print(f"{len(missing)} missing, {len(stale)} stale, {len(extra)} extra. Run tools/sync-cursor-tree.py --write.")
            return 1
        print(f"ok: .cursor/ matches claude-tree/ ({len(want)} files)")
        return 0
    for rel in missing + stale:
        dst = CURSOR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(want[rel], encoding="utf-8")
        if rel.endswith(".sh") and (CLAUDE / rel).exists():
            dst.chmod((CLAUDE / rel).stat().st_mode & 0o777)
    for rel in extra:
        (CURSOR / rel).unlink()
    for d in sorted((p for p in CURSOR.rglob("*") if p.is_dir()), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    print(f"created {len(missing)}, rewritten {len(stale)}, removed {len(extra)}, unchanged {len(want) - len(missing) - len(stale)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
