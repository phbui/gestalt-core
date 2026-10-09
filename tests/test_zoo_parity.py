"""The zoo and beir_bench give the same numbers on the same inputs.

A fake encoder stands in for the model. Its vectors have unequal lengths, as the raw vectors of nomic-embed-text-v1.5 do.
No model loads and no dataset downloads. beir_bench is the reference. These tests fail when the zoo's dense leg or its fusion drifts from it.
"""
from __future__ import annotations

import inspect
import json
import sys
from collections import namedtuple
from pathlib import Path

import pytest
from conftest import HashEncoder, make_fake_ir_datasets

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
pytest.importorskip("sqlite_vec")
pytest.importorskip("ir_datasets")  # the harness imports it at module level; without the bench requirements these tests skip, as the README says
pytest.importorskip("yaml")
import evals.zoo  # noqa: E402,F401
import beir_bench as bb  # noqa: E402
import gestalt_embed_config as ec  # noqa: E402

from evals.zoo import dense_hf, zoo_run  # noqa: E402
from evals.zoo.bm25 import Bm25  # noqa: E402
from evals.zoo.fused import Fused  # noqa: E402

Doc = namedtuple("Doc", "doc_id title text")
Query = namedtuple("Query", "query_id text")
Qrel = namedtuple("Qrel", "query_id doc_id relevance")

WORDS = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu".split()
YAML = f"""
systems:
  bm25: {{adapter: bm25}}
  hash:
    adapter: dense_hf
    args: {{model: hash-test, block_size: 16, batch_size: 8, doc_prefix: "{ec.DOC_PREFIX}", query_prefix: "{ec.QUERY_PREFIX}"}}
"""


class Raw(HashEncoder):
    """Count vectors that are not normalised, like the raw output of a model without a Normalize module."""
    normalize = False


def corpus():
    """40 documents of unequal length over 12 words, so vector lengths differ and cosine and L2 order the documents differently."""
    docs = [Doc(f"d{i}", f"{WORDS[i % 12]} {WORDS[(i * 5) % 12]}", " ".join(WORDS[(i * j + j) % 12] for j in range(1 + i % 7))) for i in range(40)]
    queries = [Query(f"q{i}", " ".join(WORDS[(i + j * 3) % 12] for j in range(1 + i % 4))) for i in range(24)]
    qrels = [Qrel(f"q{i}", d, 1) for i in range(24) for d in (f"d{(i * 3) % 40}", f"d{(i * 7 + 1) % 40}", f"d{(i * 11 + 2) % 40}")]
    return docs, queries, qrels


def run_file(path: Path) -> dict[str, list[str]]:
    """The doc ids of a TREC run file per query, in file order."""
    out: dict[str, list[str]] = {}
    for line in path.read_text().splitlines():
        qid, _, doc, *_ = line.split()
        out.setdefault(qid, []).append(doc)
    return out


@pytest.fixture
def both(monkeypatch, tmp_path):
    """(bench result, bench dir, zoo) over one corpus and one encoder. zoo(*systems) runs zoo_run and returns (result, out dir)."""
    docs, queries, qrels = corpus()
    monkeypatch.setitem(sys.modules, "ir_datasets", make_fake_ir_datasets(docs, queries, qrels))
    monkeypatch.delenv("GESTALT_FUSION", raising=False)
    enc = Raw()
    monkeypatch.setattr(dense_hf, "load_model", lambda *a, **k: enc)
    monkeypatch.setattr(bb.harness, "_model", enc)
    bench = bb.run_dataset("beir/scifact", tmp_path / "bench", None, 8, enc, work_dir=tmp_path / "bwork", block_size=16, dense_mode="vec")
    cfg = tmp_path / "zoo.yaml"

    def zoo(*systems, normalize=False):
        cfg.write_text(YAML.replace("batch_size: 8,", "batch_size: 8, normalize: true,") if normalize else YAML)
        tag = "norm" if normalize else "raw"
        out = tmp_path / f"zoo-{tag}"
        code = zoo_run.main(["--dataset", "beir/scifact", "--systems", *systems, "--out", str(out), "--config", str(cfg),
                             "--work-dir", str(tmp_path / f"zwork-{tag}"), "--latency-queries", "1"])
        assert code == 0
        return json.loads((out / "beir-scifact.json").read_text()), out

    return bench, tmp_path / "bench", zoo


@pytest.mark.parametrize("zoo_name,bench_name", [("bm25", "bm25"), ("hash", "dense"), ("hash+bm25:rrf", "hybrid"), ("bm25+hash:rrf", "hybrid")])
def test_zoo_system_equals_the_bench_system_per_query_and_in_the_top_ten(both, zoo_name, bench_name):
    bench, bdir, zoo = both
    result, zdir = zoo(zoo_name)
    assert result["query_ids"] == bench["query_ids"]
    assert result["systems"][zoo_name]["per_query"] == bench["systems"][bench_name]["per_query"]
    assert run_file(zdir / f"beir-scifact.{zoo_name}.run") == run_file(bdir / f"beir-scifact.{bench_name}.run")


def test_the_fixture_is_sensitive_unit_vectors_change_the_dense_rankings(both):
    _, bdir, zoo = both
    _, zdir = zoo("hash", normalize=True)
    assert run_file(zdir / "beir-scifact.hash.run") != run_file(bdir / "beir-scifact.dense.run")


def test_dense_hf_default_keeps_the_models_own_vectors():
    assert inspect.signature(dense_hf.DenseHF).parameters["normalize"].default is False


def test_fused_puts_the_lexical_leg_first_whichever_side_it_stands_on(tmp_path):
    class Dense:
        parts, indexed, name = (), True, "dense"

        def search(self, queries, k):
            return {q: [("x", -0.1), ("y", -0.2)] for q, _ in queries}

    bm = Bm25("bm25", tmp_path)
    bm.search = lambda queries, k: {q: [("y", 3.0), ("x", 2.0)] for q, _ in queries}
    first = Fused("f", bm, Dense()).search([("q", "t")], 10)["q"]
    swapped = Fused("g", Dense(), bm).search([("q", "t")], 10)["q"]
    given = Fused("h", Dense(), bm, lexical_first=False).search([("q", "t")], 10)["q"]
    assert swapped == first and [d for d, _ in first] == ["y", "x"]  # equal rrf scores, the lexical leg's order wins the tie
    assert [d for d, _ in given] == ["x", "y"]
