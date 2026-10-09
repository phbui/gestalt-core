"""PR review fixes for evals/memory: ENG-002, ENG-004, QAL-004, QAL-001 follow-up, MET-008."""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals" / "memory"))
pytest.importorskip("sqlite_vec")
pytest.importorskip("numpy")
import common as C  # noqa: E402
import locomo_bench as K  # noqa: E402
import longmemeval_bench as L  # noqa: E402
import merge_shards as M  # noqa: E402
from test_memory_bench import args_for, lme_file, locomo_file, stub_model  # noqa: E402,F401  (fixtures)

FP = {"benchmark": "x"}


def _scope(tmp_path, fallbacks):
    d = {"format": C.SCOPE_FORMAT, "fingerprint": FP, "meta": {}, "scope": "s1", "qids": ["q1"],
         "rows": {"session": {"hybrid": [], "hybrid_rerank": []}}, "abstain": {"session": {"hybrid": [[], []], "hybrid_rerank": [[], []]}},
         "lines": [], "rerank_fallbacks": fallbacks, "seconds": 1.0}
    C.atomic_write_text(C.scope_path(tmp_path, "s1"), json.dumps(d) + "\n")


def test_eng002_scope_with_fallbacks_is_recomputed_and_one_without_is_reused(tmp_path, capsys):
    systems = ["hybrid", "hybrid_rerank"]
    _scope(tmp_path, 3)
    assert C.load_scope(tmp_path, "s1", FP, ["q1"], systems, ["session"]) is None
    assert "reranker fell back on 3 queries" in capsys.readouterr().err
    _scope(tmp_path, 0)
    assert C.load_scope(tmp_path, "s1", FP, ["q1"], systems, ["session"])["scope"] == "s1"


def test_eng004_bm25_reports_no_abstention_auroc(lme_file, stub_model, tmp_path):
    s = L.run(args_for(tmp_path, lme_file, systems="bm25,dense", granularity="session"))
    auroc = s["levels"]["session"]["abstention_auroc"]
    assert "bm25" not in auroc and "dense" in auroc


def test_qal004_resume_and_discard_log_the_scope_file(tmp_path, capsys):
    _scope(tmp_path, 0)
    path = C.scope_path(tmp_path, "s1")
    C.load_scope(tmp_path, "s1", FP, ["q1"], ["hybrid", "hybrid_rerank"], ["session"])
    assert f"resuming from {path}" in capsys.readouterr().err
    path.write_text(path.read_text()[:30])
    assert C.read_scope_file(path) is None
    assert f"discarding {path}: it is unreadable" in capsys.readouterr().err


def test_qal004_merge_logs_each_scope_file(lme_file, stub_model, tmp_path, capsys):
    L.run(args_for(tmp_path, lme_file, systems="bm25", granularity="session"))
    capsys.readouterr()
    M.merge([tmp_path / "out"], tmp_path / "merged", lme_file)
    err = capsys.readouterr().err
    assert err.count("merging ") == 3 and str(tmp_path / "out" / "scopes") in err


def test_qal001_common_does_not_import_beir_bench_at_module_level():
    tree = ast.parse((REPO / "evals" / "memory" / "common.py").read_text())
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name for n in top if isinstance(n, ast.Import) for a in n.names} | {n.module for n in top if isinstance(n, ast.ImportFrom)}
    assert "beir_bench" not in names and "bench_stats" in names
    assert not hasattr(C, "beir") and not hasattr(C, "chunker")
    assert "evals/retrieval/bench_stats.py" in C.code_hashes(REPO / "evals" / "memory" / "locomo_bench.py")


def test_met008_locomo_summary_records_the_ci_method(locomo_file, stub_model, tmp_path):
    s = K.run(args_for(tmp_path, locomo_file, systems="bm25", granularity="session"))
    assert s["ci_method"].startswith("cluster percentile bootstrap, 95 percent, 2 clusters")
    assert "2^-1 = 0.500000" in s["ci_method"] and "under-cover" in s["ci_method"]
    assert "0.001953" in K.ci_method(10)


def test_sec010_a_bad_cached_file_is_quarantined_and_a_bad_download_is_refused(tmp_path, monkeypatch):
    """SEC-010. A cached file that fails the pin is moved aside with a message that says so, instead of failing for ever.
    A fresh download that fails the pin is refused outright and left for inspection."""
    import evals.memory.common as C

    name = next(iter(C.DATASETS))
    spec = C.DATASETS[name]
    cache = tmp_path / "cache"
    cache.mkdir()
    bad = cache / spec["file"]
    bad.write_bytes(b"not the dataset")
    with pytest.raises(SystemExit) as e:
        C.fetch_dataset(name, cache_dir=cache, download=False)
    assert "Moved it to" in str(e.value) and not bad.exists()
    assert any(p.name.startswith(spec["file"] + ".bad-") for p in cache.iterdir())

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            data, self.data = getattr(self, "data", b"wrong bytes"), b""
            return data

    seen = {}

    def fake_urlopen(url, timeout=None):
        seen["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr(C.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SystemExit) as e:
        C.fetch_dataset(name, cache_dir=cache, download=True)
    assert "download" in str(e.value) and seen["timeout"] == C.DOWNLOAD_TIMEOUT_S
    assert bad.exists(), "a refused download stays on disk for inspection"

