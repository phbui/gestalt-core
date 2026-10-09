"""Direct tests for functions that the PR review (QAL-005) found with no test naming them.

Covered here: run_cqadupstack, fingerprint_diff, and the publish-public helpers rewrite_text, prune_settings, fill_results and prune_index_docs.
The hybrid_search FTS error branch already has tests in tests/test_pr_review_rank.py (fts_failed), so it is not repeated.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
from conftest import HashEncoder, make_fake_ir_datasets

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals" / "retrieval"))
sys.path.insert(0, str(REPO / "evals" / "memory"))
sys.path.insert(0, str(REPO / "tools"))
pytest.importorskip("sqlite_vec")
pytest.importorskip("ir_datasets")  # the harness imports it at module level; without the bench requirements these tests skip, as the README says
import beir_bench as bb  # noqa: E402
import common as mem_common  # noqa: E402

if not (REPO / "tools" / "publish-public.py").exists():
    pytest.skip("the exporter is not part of the public tree", allow_module_level=True)
_spec = importlib.util.spec_from_file_location("publish_public_cov", REPO / "tools" / "publish-public.py")
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


class CountEncoder(HashEncoder):
    """Unnormalised bag-of-words counts."""
    normalize = False


# run_cqadupstack

def _forum_datasets(forums: dict[str, int]):
    """A fake ir_datasets whose corpus size differs per forum. `forums` maps a forum name to its document count."""
    mods = {}
    for name, n in forums.items():
        docs = [(f"{name}{i}", f"title{i}", f"alpha{i} beta{i} gamma") for i in range(n)]
        queries = [("q1", "alpha3 beta3"), ("q2", "alpha5 beta5")]
        qrels = [("q1", f"{name}3", 1), ("q2", f"{name}5", 1)]
        mods[name] = make_fake_ir_datasets(docs, queries, qrels)
    mod = types.ModuleType("ir_datasets")
    mod.load = lambda dataset: mods[dataset.split("/")[2]].load(dataset)
    return mod


def test_run_cqadupstack_pools_forums_and_averages_their_means(monkeypatch, tmp_path):
    """Catches a pool that drops a forum, sums documents wrongly, or reports a headline that is not the mean of the forum means."""
    forums = {"android": 12, "gis": 16, "unix": 20}
    monkeypatch.setattr(bb, "CQADUPSTACK", list(forums))
    monkeypatch.setitem(sys.modules, "ir_datasets", _forum_datasets(forums))
    seen: dict[str, dict] = {}
    real = bb.run_dataset

    def spy(name, *a, **kw):
        seen[name.split("/")[2]] = real(name, *a, **kw)
        return seen[name.split("/")[2]]

    monkeypatch.setattr(bb, "run_dataset", spy)
    out = tmp_path / "out"
    out.mkdir()
    r = bb.run_cqadupstack(out, None, 8, CountEncoder(), None, None, work_dir=tmp_path / "work")

    assert set(seen) == set(forums)
    assert r["dataset"] == "beir/cqadupstack"
    assert r["subforums"] == sorted(forums)
    assert r["documents"] == sum(forums.values())
    assert r["queries"] == 2 * len(forums)
    for system in ("bm25", "dense", "hybrid"):
        means = [seen[f]["systems"][system]["ndcg10"] for f in forums]
        assert r["systems"][system]["ndcg10"] == round(sum(means) / len(means), 4)
        pooled = [x for f in forums for x in seen[f]["systems"][system]["per_query"]]
        assert len(pooled) == 2 * len(forums)
        assert r["systems"][system]["ndcg10_pooled"] == round(sum(pooled) / len(pooled), 4)
    assert json.loads((out / "beir-cqadupstack.json").read_text())["subforums"] == sorted(forums)


# fingerprint_diff

def test_fingerprint_diff_names_the_changed_field():
    """Catches a diff that misses a changed field, flags an equal one, or reports a field that is only present on one side."""
    a = {"seed": 1, "bootstrap": 100, "smoke_subset": False}
    assert mem_common.fingerprint_diff(a, dict(a)) == []
    assert mem_common.fingerprint_diff(a, {**a, "seed": 2}) == ["seed"]
    assert mem_common.fingerprint_diff(a, {**a, "extra": 1}) == ["extra"]


def test_fingerprint_diff_names_the_changed_code_file():
    """Catches a diff that reports code_sha256 as one blob instead of naming the file that changed."""
    a = {"seed": 1, "code_sha256": {"common.py": "aa", "bench.py": "bb"}}
    b = {"seed": 1, "code_sha256": {"common.py": "aa", "bench.py": "cc"}}
    assert mem_common.fingerprint_diff(a, b) == ["code_sha256.bench.py"]


# rewrite_text

def test_rewrite_text_rewrites_private_names_and_leaves_other_text_alone():
    """Catches a rewrite that leaves a private email, home path or tailnet address in place, or that mangles unrelated text."""
    text = "mail you@example.com and see /home/user/x on 100.80.1.2 here"
    out = pp.rewrite_text(text, ".txt", None, set(), "docs/a.txt")
    assert "you@example.com" not in out and "you@example.com" in out
    assert "/home/user" not in out and "/home/user/x" in out
    assert "100.80.1.2" not in out and "100.x.x.x" in out
    plain = "Nothing private lives in this sentence."
    assert pp.rewrite_text(plain, ".txt", None, set(), "docs/b.txt") == plain


def test_rewrite_text_unlinks_private_wikilinks_but_keeps_shipped_ones():
    """Catches a rewrite that ships a link to a private entry or breaks a link whose target ships."""
    text = "See [[private-entry]] and [[shipped-rule]] and [[hidden|the alias]]."
    out = pp.rewrite_text(text, ".md", None, {"shipped-rule"})
    assert "[[shipped-rule]]" in out
    assert "[[private-entry]]" not in out and "private-entry" in out
    assert "the alias" in out and "[[hidden" not in out


def test_rewrite_text_replaces_private_slugs_with_the_slug_pattern():
    """Catches a slug pattern that is built but never applied."""
    rx = pp.slug_regex({"secret-notes-entry", "shipped-rule-entry"}, {"shipped-rule-entry"})
    out = pp.rewrite_text("read secret-notes-entry and shipped-rule-entry", ".md", rx, set())
    assert "secret-notes-entry" not in out
    assert "shipped-rule-entry" in out


# prune_settings

def test_prune_settings_drops_fleet_hooks_and_plugins_and_keeps_public_ones(tmp_path):
    """Catches a prune that leaves a fleet hook or enabledPlugins in the export, or one that deletes a public hook."""
    path = tmp_path / "settings.json"
    settings = {
        "model": "keep-me",
        "enabledPlugins": {"private@x": True},
        "hooks": {
            "Stop": [{"hooks": [{"command": "bash fleet-publish.sh"}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"command": "bash resident-no-ask.sh"}, {"command": "bash safety-guard-bash.sh"}]}],
        },
    }
    path.write_text(json.dumps(settings))
    pp.prune_settings(path)
    out = json.loads(path.read_text())
    assert "enabledPlugins" not in out
    assert out["model"] == "keep-me"
    assert "Stop" not in out["hooks"]
    cmds = [h["command"] for g in out["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert cmds == ["bash safety-guard-bash.sh"]
    assert out["hooks"]["PreToolUse"][0]["matcher"] == "Bash"


# fill_results

def _summary():
    def sysrow(v):
        return {"ndcg10": v, "ci95_bootstrap": [v - 0.01, v + 0.01]}

    def dataset(base):
        return {"systems": {"bm25": sysrow(base), "dense": sysrow(base + 0.1), "hybrid": sysrow(base + 0.2),
                            "hybrid_rerank": sysrow(base + 0.25)},
                "tests": {"hybrid_vs_bm25": {"mean_difference": 0.2, "p_value": 0.00001},
                          "hybrid_vs_dense": {"mean_difference": 0.1, "p_value": 0.03},
                          "rerank_vs_hybrid": {"mean_difference": 0.05, "p_value": 0.002}}}
    return {"results": {"beir/scifact": dataset(0.5), "beir/nfcorpus": dataset(0.3)}}


def _export(tmp_path: Path, readme: str, bench: str = "plain text") -> Path:
    res = tmp_path / "evals" / "retrieval" / "results"
    res.mkdir(parents=True)
    (res / "summary.json").write_text(json.dumps(_summary()))
    (tmp_path / "README.md").write_text(readme)
    (tmp_path / "evals" / "retrieval" / "BENCHMARKS.md").write_text(bench)
    return tmp_path


def test_fill_results_replaces_tokens_from_the_summary(tmp_path):
    """Catches a fill that leaves a RESULT_ token in place, swaps datasets or systems, or loses the interval or the test line."""
    dest = _export(tmp_path, "S: RESULT_SCIFACT_HYBRID point RESULT_SCIFACT_HYBRID_POINT test RESULT_SCIFACT_TEST_BM25 and RESULT_NFCORPUS_BM25_POINT",
                   "dense RESULT_NFCORPUS_DENSE vs RESULT_NFCORPUS_TEST_DENSE")
    pp.fill_results(dest)
    readme = (dest / "README.md").read_text()
    assert "0.700 [0.690, 0.710]" in readme
    assert "point 0.700" in readme
    assert "+0.200, p < 0.0001" in readme
    assert "and 0.300" in readme
    bench = (dest / "evals" / "retrieval" / "BENCHMARKS.md").read_text()
    assert "0.400 [0.390, 0.410]" in bench
    assert "+0.100, p = 0.03" in bench
    assert "RESULT_" not in readme + bench


def test_fill_results_refuses_an_unknown_token(tmp_path):
    """Catches a fill that ships a document with a token it could not fill."""
    dest = _export(tmp_path, "keep RESULT_NOT_A_REAL_TOKEN here")
    with pytest.raises(SystemExit, match="unfilled RESULT_ token"):
        pp.fill_results(dest)


# prune_index_docs

def test_prune_index_docs_removes_dropped_entries_and_keeps_public_ones(tmp_path):
    """Catches a prune that leaves a dropped skill or rule in an index doc, or that removes a public row, bullet or section."""
    doc = tmp_path / "claude-tree" / "references" / "skill-index.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "# Index\n"
        "| `/briefing` | morning brief |\n"
        "| `/build` | build things |\n"
        "- `hub-invariants` keeps the hub up\n"
        "- `agent-autonomy` acts first\n"
        "## /spin-up\n"
        "spin up body line\n"
        "## Public section\n"
        "public body line\n"
    )
    pp.prune_index_docs(tmp_path)
    out = doc.read_text()
    for gone in ("briefing", "hub-invariants", "spin-up", "spin up body line"):
        assert gone not in out
    for kept in ("`/build`", "`agent-autonomy`", "## Public section", "public body line", "# Index"):
        assert kept in out


def test_prune_index_docs_skips_index_files_that_do_not_exist(tmp_path):
    """Catches a prune that crashes when the export lacks one of the index docs."""
    pp.prune_index_docs(tmp_path)
