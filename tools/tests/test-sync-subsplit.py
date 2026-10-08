#!/usr/bin/env python3
"""The sync script's OWN embedded chunker must never plan a chunk over the limit.

This extracts and executes the heredoc out of tools/gestalt-graphiti-sync.sh rather than
reimplementing it, because a copy of the chunker in a test proves only that the copy is correct.
It also asserts the two heredocs stay in step: the knowledge-entry chunker and the session-digest
chunker had the same over-limit defect, and fixing one and forgetting the other is exactly how
they drift.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "gestalt-graphiti-sync.sh"
KNOWLEDGE = Path(os.environ.get("GESTALT_SUBSPLIT_KNOWLEDGE") or ROOT / "knowledge")
GIT_CRYPT_MAGIC = b"\x00GITCRYPT\x00"
LIMIT = 12000
fails = []


def check(label, ok, detail=""):
    print(("  ok: " if ok else "  FAIL: ") + label + (("  " + detail) if detail else ""))
    if not ok:
        fails.append(label)


src = SCRIPT.read_text()

# Both heredocs must carry the sub-split, and both must call it from their `## ` split.
check("both chunkers define the sub-splitter", src.count("def _sub(text, limit):") == 2,
      f"found {src.count('def _sub(text, limit):')}")
check("both chunkers expand over-limit sections before packing",
      src.count("_sub(p, limit) if len(p) > limit else [p]") == 2,
      f"found {src.count('_sub(p, limit) if len(p) > limit else [p]')}")
check("no chunker still splits on `## ` without sub-splitting",
      "parts = re.split(r'(?m)^(?=## )', body)\n" not in src)

# Run the real knowledge-entry chunker over the real corpus. In CI the corpus is git-crypt ciphertext
# (no key on the runner), so ciphertext files are skipped and, when none is readable, a synthetic entry
# with an over-limit section stands in, so the check still exercises the sub-split rather than passing
# vacuously or failing on a UnicodeDecodeError (CI red from 2026-09-06 03:39Z until this guard).
def corpus():
    files = [f for f in sorted(KNOWLEDGE.glob("*.md")) if not f.read_bytes().startswith(GIT_CRYPT_MAGIC)]
    if files:
        return files, None
    td = tempfile.TemporaryDirectory()
    synth = Path(td.name) / "synthetic-long-entry.md"
    synth.write_text("---\ntitle: Synthetic\n---\n\n" + "".join(
        f"## Section {i}\n" + ("a long paragraph line that repeats itself. " * 400 + "\n") * (5 if i == 2 else 1)
        for i in range(4)))
    print("  note: corpus is git-crypt ciphertext here; chunking a synthetic over-limit entry instead")
    return [synth], td

files, _keep = corpus()
body = re.split(r"_MANIFEST=\"\$CHUNKS_TMP\"[^\n]*python3 - <<'PYEOF'\n", src, 1)[1].split("\nPYEOF", 1)[0]
over, total = [], 0
with tempfile.TemporaryDirectory() as td:
    manifest = Path(td) / "m.tsv"
    for f in files:
        manifest.write_text("")
        env = dict(os.environ, _F=str(f), _N="kb-" + f.stem, _G="test", _C=str(LIMIT), _I="1",
                   _SKIP="", _CHUNKSTATE="{}", _RESEND="", _FORCE="1", _MANIFEST=str(manifest))
        r = subprocess.run([sys.executable, "-"], input=body, capture_output=True, text=True, env=env)
        if r.returncode != 0:
            check(f"chunker ran on {f.name}", False, r.stderr.strip()[:200])
            break
        import json
        for line in r.stdout.splitlines():
            if not line.strip():
                continue
            total += 1
            text = json.loads(line)["params"]["arguments"]["episode_body"]
            if len(text) > LIMIT:
                over.append((json.loads(line)["params"]["arguments"]["name"], len(text)))

check("the script's own chunker plans zero chunks over the limit", not over,
      f"{total} chunks planned, {len(over)} over: {over[:3]}")

print("PASS" if not fails else "FAIL: " + "; ".join(fails))
sys.exit(1 if fails else 0)
