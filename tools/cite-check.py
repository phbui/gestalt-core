#!/usr/bin/env python3
"""Mechanical citation check before a manuscript goes out (X10, 2026-10-06).

Given a .bib or .tex file, or a directory of them, it extracts the cited keys, resolves each to
knowledge/paper-<citekey>.md, and prints the access tier per key under claude-tree/rules/cite-with-access.md:
READ (the PDF is in assets/pdf/), ABSTRACT-ONLY (no PDF, the note holds a real abstract) or UNREAD.
A key with no note is flagged. With --online each DOI is also checked against Crossref: title and year
agreement, and retraction or correction notices. Responses are cached for a day under the state dir.

Exit 1 when a cited work is retracted, has no note, or its DOI does not resolve (HTTP 404).
Network errors only warn. Title or year mismatch only warns, unless --strict.

Usage: tools/cite-check.py paper.tex [refs.bib | dir ...] [--online] [--strict] [--knowledge-dir D] [--pdf-dir D]
Polite pool: set GESTALT_MAILTO, else the address in tools/zotero-import.py's User-Agent is used.
State dir: $GESTALT_STATE_DIR or ~/.fleet/cite-check.
"""
import argparse, difflib, hashlib, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIN_INTERVAL = 0.25          # 4 requests per second, under Crossref's polite 10 per second
CACHE_TTL = 86400
RETRACT_TYPES = {"retraction", "withdrawal", "removal"}
_last = [0.0]

TEX_CITE = re.compile(r"\\[A-Za-z]*cite[A-Za-z]*\*?(?:\s*\[[^\]]*\]){0,2}\s*\{([^}]*)\}")
BIB_KEY = re.compile(r"^\s*@(?!comment|string|preamble)\w+\s*\{\s*([^,\s]+)\s*,", re.I | re.M)


def state_dir() -> Path:
    return Path(os.environ.get("GESTALT_STATE_DIR") or Path.home() / ".fleet" / "cite-check")


def mailto() -> str | None:
    if os.environ.get("GESTALT_MAILTO"):
        return os.environ["GESTALT_MAILTO"]
    try:
        m = re.search(r"mailto:([^)\"]+)\)", (ROOT / "tools" / "zotero-import.py").read_text())
        return m.group(1) if m else None
    except OSError:
        return None


def extract_keys(path: Path) -> list[str]:
    """Cited keys of one file. A .bib lists every entry key, a .tex every key inside a cite command."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".bib":
        return BIB_KEY.findall(text)
    text = re.sub(r"(?<!\\)%.*", "", text)            # drop LaTeX comments
    return [k.strip() for g in TEX_CITE.findall(text) for k in g.split(",") if k.strip()]


def collect(paths: list[Path]) -> list[str]:
    files = []
    for p in paths:
        files += sorted(q for q in p.rglob("*") if q.suffix in (".tex", ".bib")) if p.is_dir() else [p]
    seen, out = set(), []
    for f in files:
        for k in extract_keys(f):
            if k not in seen:
                seen.add(k)
                out.append(k)
    return out


def frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    out = {}
    for line in (m.group(1) if m else "").split("\n"):
        mm = re.match(r"^([A-Za-z_][\w-]*):[ \t]*(.*?)\s*$", line)
        if mm:
            out[mm.group(1)] = mm.group(2).strip("\"'")
    return out


DOI_LINK = re.compile(r"doi\.org/(10\.[^\s>]+?)(?=\)?(?:\s|>|$))")   # keeps parentheses inside a DOI


def find_note(key: str, kdir: Path) -> Path | None:
    slug = re.sub(r"[^a-zA-Z0-9_-]", "", key)
    p = kdir / f"paper-{slug}.md"
    if p.is_file():
        return p
    low = f"paper-{slug}.md".lower()
    return next((q for q in kdir.glob("paper-*.md") if q.name.lower() == low), None)


def note_info(key: str, kdir: Path, pdfdir: Path) -> dict:
    note = find_note(key, kdir)
    if note is None:
        return {"key": key, "note": None, "tier": "NO-NOTE", "doi": "", "basis": ""}
    text = note.read_text(encoding="utf-8", errors="replace")
    fm = frontmatter(text)
    doi = fm.get("doi", "")
    if not doi:
        m = DOI_LINK.search(text)
        doi = m.group(1) if m else ""
    slug = note.stem.removeprefix("paper-")
    has_pdf = (pdfdir / f"{slug}.pdf").is_file() or (pdfdir / f"{key}.pdf").is_file()
    ab = re.search(r"## Abstract\s*\n+(.*?)(?=\n## |\Z)", text, re.S)
    ab = ab.group(1).strip() if ab else ""
    real_abs = bool(ab) and "(no abstract" not in ab.lower() and len(ab) > 40
    tier = "READ" if has_pdf else "ABSTRACT-ONLY" if real_abs else "UNREAD"
    return {"key": key, "note": note, "tier": tier, "doi": doi,
            "basis": fm.get("summary_basis", "") or "-", "read_by": fm.get("read_by", "") or "-"}


def http_get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": f"gestalt-cite-check/1.0 (mailto:{mailto() or 'unknown'})"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, ""


def crossref(doi: str, cache: Path | None = None, sleep=time.sleep) -> dict:
    """{"status": int, "message": dict}. Cached for a day. Backs off on 429. status 0 means a network error."""
    cache = cache or state_dir() / "cache"
    f = cache / (hashlib.sha1(doi.lower().encode()).hexdigest() + ".json")
    if f.is_file() and time.time() - f.stat().st_mtime < CACHE_TTL:
        return json.loads(f.read_text())
    # 2026-10-06: the single-work route rejects `select` with HTTP 400 (parameter-not-allowed), so the
    # whole record is fetched and trimmed to the fields below before caching.
    q = {}
    if mailto():
        q["mailto"] = mailto()
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi)}?{urllib.parse.urlencode(q)}"
    status, body = 0, ""
    for attempt in range(4):
        wait = MIN_INTERVAL - (time.monotonic() - _last[0])
        if wait > 0:
            sleep(wait)
        _last[0] = time.monotonic()
        try:
            status, body = http_get(url)
        except OSError:
            return {"status": 0, "message": {}}
        if status != 429:
            break
        sleep(2 ** attempt)
    if status not in (200, 404):
        return {"status": status, "message": {}}
    full = json.loads(body).get("message", {}) if status == 200 and body else {}
    keep = ("DOI", "title", "issued", "type", "update-to", "updated-by")
    res = {"status": status, "message": {k: full[k] for k in keep if k in full}}
    cache.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(res))
    return res


def _norm(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = re.sub(r"^\s*retracted\s*:?\s*", "", s, flags=re.I)
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", s).casefold()).strip()


def judge(res: dict, note_title: str = "", note_year: str = "") -> dict:
    """Turn a crossref() result into verdict fields: resolved, retracted, notices, title_ratio, year_delta."""
    m = res.get("message") or {}
    out = {"http": res["status"], "resolved": res["status"] == 200, "retracted": False,
           "notices": [], "title_ratio": None, "year_delta": None}
    if not out["resolved"]:
        return out
    title = (m.get("title") or [""])[0]
    for u in m.get("updated-by", []) or []:
        t = u.get("type", "")
        if t in RETRACT_TYPES:
            out["retracted"] = True
        out["notices"].append({"type": t, "doi": u.get("DOI", ""), "source": u.get("source", "")})
    if re.match(r"\s*retracted\b", title, re.I):
        out["retracted"] = True
    if note_title:
        out["title_ratio"] = round(difflib.SequenceMatcher(None, _norm(title), _norm(note_title)).ratio(), 3)
    parts = ((m.get("issued") or {}).get("date-parts") or [[None]])[0]
    if note_year.isdigit() and parts and parts[0]:
        out["year_delta"] = abs(int(parts[0]) - int(note_year))
    return out


def note_title_year(note: Path) -> tuple[str, str]:
    text = note.read_text(encoding="utf-8", errors="replace")
    t = frontmatter(text).get("title", "")
    y = re.search(r"\*\*Year:\*\*\s*(\d{4})", text)
    return t, y.group(1) if y else ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--strict", action="store_true", help="title or year mismatch also fails")
    ap.add_argument("--knowledge-dir", type=Path, default=ROOT / "knowledge")
    ap.add_argument("--pdf-dir", type=Path, default=ROOT / "assets" / "pdf")
    a = ap.parse_args(argv)
    keys = collect(a.paths)
    if not keys:
        print("no citations found")
        return 0
    bad = warn = 0
    w = min(max(len(k) for k in keys), 60)
    print(f"{'key':{w}s} {'tier':14s} {'basis':9s} online")
    for k in keys:
        info = note_info(k, a.knowledge_dir, a.pdf_dir)
        flags = []
        if info["note"] is None:
            flags.append("FAIL no note")
            bad += 1
        elif a.online:
            if not info["doi"]:
                flags.append("no DOI to check")
            else:
                t, y = note_title_year(info["note"])
                v = judge(crossref(info["doi"]), t, y)
                if v["http"] == 404:
                    flags.append("FAIL DOI does not resolve")
                    bad += 1
                elif not v["resolved"]:
                    flags.append(f"warn crossref unavailable (http {v['http']})")
                    warn += 1
                else:
                    if v["retracted"]:
                        flags.append("FAIL RETRACTED " + ",".join(n["doi"] for n in v["notices"] if n["type"] in RETRACT_TYPES))
                        bad += 1
                    elif v["notices"]:
                        flags.append("notice " + ",".join(sorted({n["type"] for n in v["notices"]})))
                    if v["title_ratio"] is not None and v["title_ratio"] < 0.9:
                        flags.append(f"title ratio {v['title_ratio']}")
                        warn += 1
                        bad += a.strict
                    if v["year_delta"] is not None and v["year_delta"] > 1:
                        flags.append(f"year off by {v['year_delta']}")
                        warn += 1
                        bad += a.strict
                    if not flags:
                        flags.append("ok")
        print(f"{k[:w]:{w}s} {info['tier']:14s} {info['basis']:9s} {'; '.join(flags)}")
    tiers = {t: sum(1 for k in keys if note_info(k, a.knowledge_dir, a.pdf_dir)["tier"] == t)
             for t in ("READ", "ABSTRACT-ONLY", "UNREAD", "NO-NOTE")}
    print(f"\n{len(keys)} keys: {tiers}  failures: {bad}  warnings: {warn}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
