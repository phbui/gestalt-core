"""Fetch, seal, open and verify the sealed held-out retrieval sets (litsearch, techqa, longmemeval-s).

    heldout.py fetch <name> [--dest DIR] [--dry-run]
    heldout.py seal <name> [--dir DIR]
    heldout.py open <name> --reason TEXT [--reopen] [--dir DIR]
    heldout.py verify <name> [--dir DIR]

Data sits in <dir>/<name>/. The manifest <dir>/<name>.manifest.json sits beside it. The tool never deletes data.
A sealed set cannot be fetched over. A set is opened once. A second open needs --reopen and a reason, and both are logged.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I does not put the script folder on the path
import heldout_datasets as hd

URL = "https://huggingface.co/datasets/{repo}/resolve/{commit}/{path}"


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_path(base: Path, name: str) -> Path:
    return base / f"{name}.manifest.json"


def fetch_record_path(base: Path, name: str) -> Path:
    return base / f"{name}.fetch.json"


def write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def default_downloader(url: str, repo: str, commit: str, path: str, out: Path) -> None:
    """Download one pinned file. Uses huggingface_hub when installed and plain urllib otherwise."""
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        with urllib.request.urlopen(url) as r, open(out, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        return
    with tempfile.TemporaryDirectory(dir=out.parent) as tmp:  # keeps hub cache files out of the sealed folder
        shutil.move(hf_hub_download(repo_id=repo, filename=path, repo_type="dataset", revision=commit, local_dir=tmp), out)


def cmd_fetch(name: str, dest: Path, dry_run: bool = False, downloader=default_downloader) -> int:
    spec = hd.SPECS[name]
    commits = {s["repo"]: s["commit"] for s in spec["sources"]}
    if manifest_path(dest, name).exists():
        print(f"Refusing: {name} is sealed ({manifest_path(dest, name)}). A sealed set is never fetched over.", file=sys.stderr)
        return 1
    plan = [(repo, commits[repo], path, local, URL.format(repo=repo, commit=commits[repo], path=path)) for repo, path, local in spec["files"]]
    if dry_run:
        for repo, commit, path, local, url in plan:
            print(f"{url}\n  repo={repo} commit={commit} -> {dest / name / local}")
        return 0
    for repo, commit, path, local, url in plan:
        out = dest / name / local
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".part")
        downloader(url, repo, commit, path, tmp)
        os.replace(tmp, out)
        print(f"fetched {local}")
    write_json(fetch_record_path(dest, name), {"name": name, "sources": spec["sources"], "license": spec["license"], "fetched_at": now(),
                                                "files": [p[2] for p in plan]})
    print(f"Fetched {name} into {dest / name}. Next: heldout.py seal {name}")
    return 0


def file_hashes(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): sha256_file(p) for p in sorted(root.rglob("*")) if p.is_file()}


def cmd_seal(name: str, base: Path) -> int:
    if manifest_path(base, name).exists():
        print(f"Refusing: {name} is already sealed.", file=sys.stderr)
        return 1
    rec_path = fetch_record_path(base, name)
    if not rec_path.exists() or not (base / name).is_dir():
        print(f"Refusing: no fetch record for {name}. Run fetch first.", file=sys.stderr)
        return 1
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    ds = hd.lookup(name, base / name, gated=False)
    ids = ds.all_query_ids()
    counts = {"docs": ds.docs_count(), "queries": len({q.query_id for q in ds.queries_iter()}), "qrels": sum(1 for _ in ds.qrels_iter()),
              "unanswerable": len(ds.unanswerable_ids())}
    manifest = {
        "name": name, "hf_repo": rec["sources"][0]["repo"], "commit_sha": rec["sources"][0]["commit"], "sources": rec["sources"],
        "license": rec["license"], "fetched_at": rec["fetched_at"], "sealed_at": now(), "files": file_hashes(base / name),
        "query_ids": ids, "query_ids_sha256": hashlib.sha256("\n".join(ids).encode()).hexdigest(), "counts": counts, "opened": [],
    }
    write_json(manifest_path(base, name), manifest)
    print(f"Sealed {name}: {counts}")
    return 0


def load_manifest(base: Path, name: str) -> dict | None:
    p = manifest_path(base, name)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def problems(base: Path, name: str) -> list[str]:
    m = load_manifest(base, name)
    if m is None:
        return [f"{name} has no manifest"]
    now_hashes = file_hashes(base / name) if (base / name).is_dir() else {}
    out = [f"changed: {f}" for f, h in m["files"].items() if f in now_hashes and now_hashes[f] != h]
    out += [f"missing: {f}" for f in m["files"] if f not in now_hashes]
    out += [f"extra: {f}" for f in now_hashes if f not in m["files"]]
    return out


def cmd_verify(name: str, base: Path) -> int:
    bad = problems(base, name)
    for b in bad:
        print(b, file=sys.stderr)
    print(f"{name}: {'MISMATCH' if bad else 'ok'}")
    return 1 if bad else 0


def git_sha() -> str:
    try:
        return subprocess.run(["git", "-C", str(hd.REPO), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def cmd_open(name: str, base: Path, reason: str, reopen: bool) -> int:
    m = load_manifest(base, name)
    if m is None:
        print(f"Refusing: {name} is not sealed.", file=sys.stderr)
        return 1
    if not reason.strip():
        print("Refusing: --reason must say why.", file=sys.stderr)
        return 1
    if m["opened"] and not reopen:
        first = m["opened"][0]
        print(f"Refusing: {name} was opened at {first['at']} ({first['reason']}). A second look spends the set. Pass --reopen to log another.", file=sys.stderr)
        return 1
    bad = problems(base, name)
    if bad:
        print(f"Refusing: {name} no longer matches its seal: {bad[:3]}", file=sys.stderr)
        return 1
    m["opened"].append({"at": now(), "reason": reason, "git_sha": git_sha(), "reopen": bool(m["opened"])})
    write_json(manifest_path(base, name), m)
    print(f"Opened {name}. Run this line before beir_bench:")
    print(f"export GESTALT_HELDOUT_DIR={base} GESTALT_HELDOUT_OPEN={name}")
    return 0


def main(argv: list[str] | None = None, downloader=default_downloader) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("fetch", "seal", "open", "verify"):
        p = sub.add_parser(c)
        p.add_argument("name", choices=sorted(hd.SPECS))
        p.add_argument("--dest" if c == "fetch" else "--dir", dest="base", type=Path, default=None,
                       help="folder that holds the set folders and manifests (default GESTALT_HELDOUT_DIR or <repo>/.heldout)")
        if c == "fetch":
            p.add_argument("--dry-run", action="store_true", help="print the URLs and commit shas without downloading")
        if c == "open":
            p.add_argument("--reason", required=True)
            p.add_argument("--reopen", action="store_true")
    a = ap.parse_args(argv)
    base = a.base or hd.heldout_dir()
    if a.cmd == "fetch":
        return cmd_fetch(a.name, base, a.dry_run, downloader)
    if a.cmd == "seal":
        return cmd_seal(a.name, base)
    if a.cmd == "open":
        return cmd_open(a.name, base, a.reason, a.reopen)
    return cmd_verify(a.name, base)


if __name__ == "__main__":
    sys.exit(main())
