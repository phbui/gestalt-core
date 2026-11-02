#!/usr/bin/env bash
# Stop hook: warn (never block) when this turn edited a document under ~/artifacts or personal/documents with no ```diff block shown in chat
# and no approval word in the user's last message. Enforces rules/diff-then-apply.md. Warn-only by design: the check is fuzzy.
INPUT=$(cat)
TP=$(jq -r '.transcript_path // empty' <<< "$INPUT" 2>/dev/null)
[ -f "$TP" ] || exit 0
python3 -I - "$TP" <<'PY'
import json, re, sys
rows=[]
for line in open(sys.argv[1], errors="replace"):
    try: rows.append(json.loads(line))
    except Exception: pass
def texts(m):
    c=m.get("message",{}).get("content",[])
    if isinstance(c,str): return [("text",c)]
    return [(b.get("type"), b.get("text") or "") for b in c if isinstance(b,dict)]
def human(m):
    if m.get("type")!="user" or m.get("isSidechain") or m.get("isMeta"): return False
    c=m.get("message",{}).get("content",[])
    if isinstance(c,str): return not c.startswith("<")
    return any(b.get("type")=="text" and not b.get("text","").startswith("<") for b in c if isinstance(b,dict))
last=max((i for i,m in enumerate(rows) if human(m)), default=-1)
if last<0: sys.exit(0)
user_text=" ".join(t for k,t in texts(rows[last]) if k=="text").lower()
approved=re.search(r"\b(apply|go ahead|go|do it|fix|execute|y|yes|run)\b", user_text) is not None
doc=re.compile(r"(/artifacts/|/personal/documents/|\.tex$)")
edited=[]; diff_shown=False
for m in rows[last+1:]:
    if m.get("type")!="assistant": continue
    c=m.get("message",{}).get("content",[])
    if not isinstance(c,list): continue
    for b in c:
        if b.get("type")=="text" and "```diff" in (b.get("text") or ""): diff_shown=True
        if b.get("type")=="tool_use" and b.get("name") in ("Edit","Write","NotebookEdit"):
            p=b.get("input",{}).get("file_path","")
            if doc.search(p) and p.endswith((".tex",".md",".txt",".html")): edited.append(p)
if edited and not diff_shown and not approved:
    print(json.dumps({"systemMessage":"diff-then-apply: this turn edited "+", ".join(sorted(set(edited))[:3])+" with no diff block shown and no approval word in the last message. Show the diff and wait for the go (rules/diff-then-apply.md)."}))
PY
exit 0
