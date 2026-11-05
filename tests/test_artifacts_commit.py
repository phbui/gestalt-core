"""artifacts-commit selects exactly what the DOCUMENTS panel indexes, and commits it per topic.

Guard for the 2026-11-05 stale-panel incident: the hub's DOCUMENTS · ARTIFACTS panel showed nothing newer
than Oct 17 because renders were never committed from the laptops. The committer must stay in step with
docs.py's rules, skip nested clones and oversize files, and leave the tree clean for the panel's set."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "fleet" / "bin" / "artifacts-commit"
DOCS = ROOT / "tools" / "fleet" / "dashboard" / "docs.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    src = path.read_text()
    # docs.py imports fastapi-free stdlib only; the tool is a script. Both execute as plain modules.
    exec(compile(src, str(path), "exec"), mod.__dict__)
    return mod


def test_rules_match_docs_py():
    tool = _load(TOOL, "artifacts_commit")
    docs = _load(DOCS, "fleet_docs")
    assert tool.DOC_EXTS == docs.DOC_EXTS
    assert tool.SHALLOW_ONLY == docs.SHALLOW_ONLY
    assert tool.SKIP_DIRS == docs.SKIP_DIRS


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *a], text=True, capture_output=True, check=True).stdout


def _run(repo: Path, *a: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), str(repo), *a], text=True, capture_output=True)


def test_commits_panel_documents_only(tmp_path: Path):
    repo = tmp_path / "artifacts"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text("*.iso\nretrieval-upgrade/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "topic-a").mkdir()
    (repo / "topic-a" / "report.html").write_text("<p>x</p>")
    (repo / "topic-a" / "notes.md").write_text("n")
    (repo / "topic-a" / "source.tex").write_text("\\x")  # not a render
    (repo / "topic-a" / "big.pdf").write_bytes(b"0" * (2 * 1024 * 1024))  # over the test cap below
    (repo / "topic-b" / "deep" / "er" / "path" / "x").mkdir(parents=True)
    (repo / "topic-b" / "deep" / "er" / "path" / "x" / "noisy.csv").write_text("a,b")  # csv too deep
    (repo / "topic-b" / "fig.png").write_bytes(b"\x89PNG")
    (repo / "topic-b" / "disk.iso").write_bytes(b"i")  # ignored
    nested = repo / "overleaf-proj" / "proj"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    (nested / "paper.pdf").write_bytes(b"%PDF")  # inside a nested clone
    (repo / "retrieval-upgrade").mkdir()
    (repo / "retrieval-upgrade" / "bench.md").write_text("ignored dir")

    plan = _run(repo, "--dry-run", "--max-mb", "1")
    assert plan.returncode == 0, plan.stderr
    assert "topic-a" in plan.stdout and "topic-b" in plan.stdout
    assert "overleaf-proj" not in plan.stdout and "retrieval-upgrade" not in plan.stdout

    r = _run(repo, "--no-push", "--max-mb", "1")
    assert r.returncode == 0, r.stderr
    log = _git(repo, "log", "--format=%s")
    assert "artifacts: topic-a (+2 new" in log
    assert "artifacts: topic-b (+1 new" in log
    tracked = set(_git(repo, "ls-files").splitlines())
    assert {"topic-a/report.html", "topic-a/notes.md", "topic-b/fig.png"} <= tracked
    for never in ("topic-a/source.tex", "topic-a/big.pdf", "topic-b/deep/er/path/x/noisy.csv", "topic-b/disk.iso", "overleaf-proj/proj/paper.pdf", "retrieval-upgrade/bench.md"):
        assert never not in tracked, never

    again = _run(repo, "--no-push", "--max-mb", "1")
    assert again.returncode == 0 and "clean" in again.stdout

    # a tracked document that is deleted leaves the repo too
    os.remove(repo / "topic-b" / "fig.png")
    r = _run(repo, "--no-push", "--max-mb", "1")
    assert r.returncode == 0, r.stderr
    assert "topic-b/fig.png" not in _git(repo, "ls-files")
    assert "-1 removed" in _git(repo, "log", "-1", "--format=%s")


def test_repointed_tracked_symlink_is_committed(tmp_path: Path):
    """jarvis/briefing-latest.md is a tracked symlink repointed every day; left alone it keeps the tree dirty
    and fleet-sync never pulls (hub, 2026-11-05)."""
    repo = tmp_path / "artifacts"
    (repo / "jarvis").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "jarvis" / "briefing-1.md").write_text("one")
    (repo / "jarvis" / "briefing-2.md").write_text("two")
    os.symlink(str(repo / "jarvis" / "briefing-1.md"), repo / "jarvis" / "briefing-latest.md")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    os.remove(repo / "jarvis" / "briefing-latest.md")
    os.symlink(str(repo / "jarvis" / "briefing-2.md"), repo / "jarvis" / "briefing-latest.md")
    assert "jarvis/briefing-latest.md" in _git(repo, "status", "--porcelain")  # dirty before
    r = _run(repo, "--no-push")
    assert r.returncode == 0, r.stderr
    assert _git(repo, "status", "--porcelain").strip() == ""
