"""The retrieval eval must measure the path that is actually served.

The eval runner deliberately mirrors `gestalt-mcp-server.py` rather than
importing it (the server module pulls in the MCP framework at import time).
A mirror silently drifts, and a drifted mirror makes every reported metric a
measurement of code nobody runs. These tests fail loudly on drift.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest
from conftest import _load

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import gestalt_rank  # noqa: E402  the module the server and the runner both import
SERVER = REPO / "tools" / "gestalt-mcp-server.py"
RUNNER = REPO / "evals" / "retrieval" / "run_retrieval_evals.py"
RANK = REPO / "tools" / "gestalt_rank.py"
INDEXER = REPO / "tools" / "gestalt-index-builder.py"


@pytest.fixture(scope="module")
def sources() -> dict[str, str]:
    for p in (SERVER, RUNNER, INDEXER):
        if not p.exists():  # pragma: no cover - defensive
            pytest.skip(f"missing {p}")
    return {
        "server": SERVER.read_text(),
        "runner": RUNNER.read_text(),
        "indexer": INDEXER.read_text(),
        "rank": RANK.read_text(),
    }


def test_rrf_constant_matches(sources):
    """The constant lives in gestalt_rank. The server aliases it, and the runner either aliases it or still pins the same value."""
    assert gestalt_rank.RRF_K == 60
    assert re.search(r"K\s*=\s*gestalt_rank\.RRF_K", sources["server"]), "the server defines its own RRF constant"
    runner_k = re.search(r"K_RRF\s*=\s*(\d+|gestalt_rank\.RRF_K)", sources["runner"])
    assert runner_k is None or runner_k.group(1) in ("60", "gestalt_rank.RRF_K"), "RRF constant drifted"


def test_rrf_fuse_sums_reciprocal_ranks():
    scores = gestalt_rank.rrf_fuse([[1, 2], [2, 3]])
    k = gestalt_rank.RRF_K
    assert scores[1] == pytest.approx(1 / (k + 1))
    assert scores[2] == pytest.approx(1 / (k + 2) + 1 / (k + 1))
    assert scores[3] == pytest.approx(1 / (k + 2))


def test_query_prefix_present_in_both(sources):
    for name in ("server", "runner"):
        assert '"search_query: "' in sources[name], (
            f"{name} is missing the asymmetric query prefix; "
            "query and document embeddings would land in different spaces"
        )


def test_document_prefix_used_at_index_time(sources):
    assert 'DOC_PREFIX = "search_document: "' in sources["indexer"]
    # Tolerate line wrapping: the prefix is concatenated across lines.
    assert re.search(r"DOC_PREFIX\s*\+", sources["indexer"]), (
        "DOC_PREFIX is defined but never prepended to the embedded text"
    )


def test_prefixes_are_the_matched_pair(sources):
    """search_query must pair with search_document, not another task prefix."""
    assert 'QUERY_PREFIX = "search_query: "' in sources["indexer"]


def test_runner_reads_the_shared_embed_config(sources):
    """X14 (2026-10-06): the runner takes model, revision and prefixes from tools/gestalt_embed_config.py,
    as the builder does. It must not hold its own copy that could drift from the server."""
    r = sources["runner"]
    assert "import gestalt_embed_config as _ec" in r
    assert "_ec.QUERY_PREFIX + query" in r, "runner no longer prefixes queries from the shared config"
    assert "SentenceTransformer(_ec.MODEL_NAME" in r and "revision" in r
    assert not re.search(r'SentenceTransformer\("nomic', r), "runner hard-codes the model name again"


def test_runner_fallback_matches_the_shared_config(sources):
    """The inline fallback must carry the same values as the config module, or a lone copy embeds differently."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("ec_fid", REPO / "tools" / "gestalt_embed_config.py")
    ec = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ec)
    r = sources["runner"]
    assert f'MODEL_NAME = "{ec.MODEL_NAME}"' in r
    assert f'QUERY_PREFIX = "{ec.QUERY_PREFIX}"' in r
    assert f'DOC_PREFIX = "{ec.DOC_PREFIX}"' in r


def test_candidate_depth_matches(sources):
    """The pool depth comes from gestalt_rank.pool_size, so every caller over-fetches by the same factor before fusion."""
    assert gestalt_rank.pool_size(5, False) == 10, "off, the pool is twice the limit, as it always was"
    assert gestalt_rank.pool_size(5, True, 40) == 40, "a rerank reads at least its depth"
    assert gestalt_rank.pool_size(30, True, 40) == 60, "a large limit still gets twice itself"
    assert "pool_size(" in sources["rank"]
    assert "hybrid_search(" in sources["server"], "the server no longer ranks through gestalt_rank.hybrid_search"


def test_fts_tokenisation_matches(sources):
    """Per-token quoting, OR-joined, built in one place. Quoting the whole query makes it a phrase
    query and silently kills the BM25 leg — a bug this corpus already hit."""
    assert re.search(r'OR.*join', sources["rank"]), "gestalt_rank FTS join changed"
    assert 'split(r"\\W+", query)' in sources["rank"], "gestalt_rank tokenisation changed"
    for name in ("server", "runner"):
        assert 'split(r"\\W+", query)' not in sources[name], f"{name} grew its own copy of the tokeniser"
    assert "fts_match(query" in sources["rank"]


def test_runner_documents_the_mirror_relationship(sources):
    assert "gestalt-mcp-server" in sources["runner"], (
        "runner must name the module it mirrors so drift is discoverable"
    )


# ---------------------------------------------------------------------------
# Execution-level fidelity: run real queries through both implementations
# ---------------------------------------------------------------------------
#
# Every test above is a string-presence check: it passes if a literal
# substring survives anywhere in the file, including inside a comment or an
# unreachable branch. A refactor that renames a variable, extracts a helper,
# or reorders operations while preserving those substrings sails through all
# eight. None of them prove the two functions actually agree on OUTPUT.
#
# The tests below build a small synthetic index matching the production
# schema, run the SAME query through the real `gestalt_search` /
# `gestalt_search_fts` (tools/gestalt-mcp-server.py) and the real `search()`
# mirror (evals/retrieval/run_retrieval_evals.py), and assert on the returned
# ranked results. Only the embedding model is stubbed (an external
# dependency, deterministic so identical text always yields identical
# vectors); get_db, get_fts_db, get_sqlite_vec, fts_tokens, the FTS5 MATCH
# query, the sqlite-vec KNN query, and the RRF fusion are the real,
# unmodified production code in both files.


def _fixture_schema() -> str:
    """Pull the CREATE TABLE script straight out of the real index builder.

    Hand-copying the schema into this test file would itself be a drift risk
    -- the exact bug class this whole file exists to catch. Extracting it from
    the source means a future schema change (a new column, a new table) is
    reflected here automatically; if the builder ever stops using a single
    `executescript()` call, this fails loudly instead of silently testing a
    stale schema.
    """
    src = INDEXER.read_text()
    # F13 moved the vec0 CREATEs into a second, conditional executescript() (they
    # need the loaded extension). The fixture always loads sqlite_vec, so it wants
    # BOTH scripts — taking only the first silently dropped sections_vec/skills_vec
    # and every vec-dependent test here errored with "no such table" (2026-08-29).
    blocks = re.findall(r'db\.executescript\(\s*"""(.*?)"""\s*\)', src, re.DOTALL)
    assert blocks, "could not find any schema executescript() in gestalt-index-builder.py"
    return "\n".join(blocks)


def _stub_vector(text: str):
    """Deterministic FLOAT[768] vector: identical text -> identical bytes.

    Same pattern as test_incremental_index.py's StubModel (a hash-derived
    fill, not a real embedding). Determinism is the only property these tests
    need: the fixture's doc-side vectors are fixed ahead of time, and both
    code paths under test must derive the SAME query vector from the SAME
    input string for a ranking comparison between them to mean anything.
    """
    np = pytest.importorskip("numpy")
    import zlib
    seed = zlib.crc32(text.encode())  # hash() is salted per process, crc32 is not
    rng = np.random.default_rng(seed)
    return rng.random(768).astype(np.float32)


class _StubModel:
    """Encoder stand-in. `.encode()` is the only method either code path calls.

    Accepts and ignores extra kwargs: gestalt_search calls `model.encode(text)`
    while the harness calls `model.encode(text, convert_to_numpy=True)` --
    real SentenceTransformer accepts both; this must too, or the fidelity
    check would be comparing two different call signatures instead of two
    implementations of the same one.
    """

    def encode(self, text, **_kwargs):
        return _stub_vector(text)


def _build_fixture_db(path: Path, sections: list[dict], *, pin_fts_rowid: bool = True) -> None:
    """Build a synthetic index matching the production schema.

    `pin_fts_rowid=False` reproduces the historical bug verbatim: FTS5
    auto-assigns rowids starting at 1 while `sections_meta.id` (from
    `enumerate()`) starts at 0, so every row's FTS rowid sits one past its
    meta id (gestalt-index-builder.py:516-522, verified 2026-07-31: a MATCH
    hit rowid 594 and the tool displayed a different section). `sections_vec`
    is always pinned correctly here, since vec0 takes an explicit `id`.
    """
    sqlite_vec = pytest.importorskip("sqlite_vec")
    db = sqlite3.connect(str(path))
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.executescript(_fixture_schema())
    for s in sections:
        db.execute(
            "INSERT INTO sections_meta (id, slug, heading, block_id, content, file_path, content_hash, anchors) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                s["id"],
                s["slug"],
                s["heading"],
                s["block_id"],
                s["content"],
                s["file_path"],
                s.get("content_hash"),
                s.get("anchors", ""),
            ),
        )
        fts_rowid = s["id"] if pin_fts_rowid else s["id"] + 1
        db.execute(
            "INSERT INTO sections_fts(rowid, slug, heading, block_id, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (fts_rowid, s["slug"], s["heading"], s["block_id"], s["content"]),
        )
        db.execute(
            "INSERT INTO sections_vec (id, embedding) VALUES (?, ?)",
            (s["id"], _stub_vector(s["content"]).tobytes()),
        )
    db.commit()
    db.close()


_FIXTURE_SECTIONS = [
    {
        "id": 0,
        "slug": "retrieval-fusion",
        "heading": "Reciprocal rank fusion",
        "block_id": "^rrf-basics",
        "content": (
            "Reciprocal rank fusion combines BM25 and vector search scores by "
            "summing one over K plus rank for each ranked list, so a document "
            "that ranks highly in either leg outranks one that ranks nowhere."
        ),
        "file_path": "knowledge/retrieval-fusion.md",
        "content_hash": "hash-0",
    },
    {
        "id": 1,
        "slug": "fts-tokenisation",
        "heading": "FTS5 tokenisation",
        "block_id": "^tokenisation-rule",
        "content": (
            "FTS5 must OR-join per-token quoted terms, never wrap the whole "
            "query in one pair of quotes, because a phrase query requires that "
            "exact contiguous run of words and silently kills the BM25 leg."
        ),
        "file_path": "knowledge/fts-tokenisation.md",
        "content_hash": "hash-1",
    },
    {
        "id": 2,
        "slug": "embedding-prefixes",
        "heading": "Asymmetric embedding prefixes",
        "block_id": "^prefix-pairing",
        "content": (
            "nomic-embed-text-v1.5 requires a search_document prefix at index "
            "time and a search_query prefix at query time, or the two sides "
            "land in different regions of the embedding space."
        ),
        "file_path": "knowledge/embedding-prefixes.md",
        "content_hash": "hash-2",
    },
]

_SLUG_TO_ID = {s["slug"]: s["id"] for s in _FIXTURE_SECTIONS}


@pytest.fixture(scope="module")
def mcp_server():
    """The real gestalt-mcp-server.py, loaded without executing its CLI.

    Safe to load without stubbing anything at import time: the module's
    top-level code only imports `re`, `sqlite3`, `sys`, `pathlib`. Torch /
    sentence-transformers / the MCP framework are all lazy, inside
    get_model()/get_sqlite_vec()/build_mcp(), so loading the module costs
    nothing and pulls in no heavy dependency.
    """
    return _load("gestalt_mcp_server_fidelity", "tools/gestalt-mcp-server.py")


@pytest.fixture(scope="module")
def retrieval_harness():
    """The real evals/retrieval/run_retrieval_evals.py, loaded the same way."""
    return _load("run_retrieval_evals_fidelity", "evals/retrieval/run_retrieval_evals.py")


_KNOBS = ("GESTALT_RERANK", "GESTALT_RERANK_MODEL", "GESTALT_RERANK_DEPTH", "GESTALT_RERANK_MAXCHARS",
          "GESTALT_SLUG_DECAY", "GESTALT_FTS_STOPWORDS", "GESTALT_FUSION", "GESTALT_FUSION_ALPHA")


def _pin_knobs_off(monkeypatch) -> None:
    """Every ranking knob unset, except the rerank, which defaults to auto and would load a real model on the hub."""
    for k in _KNOBS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GESTALT_RERANK", "off")


@pytest.fixture
def fixture_index(tmp_path, mcp_server, retrieval_harness, monkeypatch):
    """Point both real modules at ONE synthetic index and stub only the model.

    Both DB_PATH globals point at the same file, so doc-side vectors are read
    from a single source of truth -- the two code paths can never disagree
    about what got indexed, only about how they QUERY it, which is exactly
    what these tests need isolated.
    """
    db_path = tmp_path / "gestalt.db"
    _build_fixture_db(db_path, _FIXTURE_SECTIONS)
    # These tests compare the HYBRID path against the harness's hybrid mirror.
    # Since the 2026-08-18 shared-MCP change the server defaults to fts on any
    # non-hub node (search_mode()), so without pinning the mode the comparison
    # silently ran fts-vs-hybrid and diverged on every non-hub machine.
    monkeypatch.setenv("GESTALT_SEARCH_MODE", "hybrid")
    _pin_knobs_off(monkeypatch)
    monkeypatch.setattr(mcp_server, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(mcp_server, "get_model", lambda: _StubModel(), raising=False)
    monkeypatch.setattr(retrieval_harness, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(retrieval_harness, "get_model", lambda: _StubModel(), raising=False)
    return db_path


FIXTURE_QUERIES = [
    # Multi-word natural language. Wrapping the whole string in one pair of
    # quotes (a phrase query) requires this exact contiguous run of words and
    # matches nothing -- the bug that killed the entire BM25 leg once already.
    "how does reciprocal rank fusion combine bm25 and vector scores",
    "what prefix pairing does asymmetric embedding retrieval need",
    # Single distinctive token, unique to one section's heading.
    "tokenisation",
    # Punctuation only: re.split(r"\W+", ...) yields no tokens, so the FTS5
    # MATCH expression would be empty -- and an empty MATCH is a syntax
    # error, not a miss, unless both implementations guard it.
    "???!!!",
]


@pytest.mark.parametrize("query", FIXTURE_QUERIES)
def test_server_and_harness_agree_on_ranking(fixture_index, mcp_server, retrieval_harness, query):
    """The core fidelity check: same query, same index, same ranked ids.

    A drift in tokenisation, candidate depth (limit*2), or the RRF constant
    would change WHICH ids come back or in WHAT order -- exactly the failure
    mode the string-presence tests above cannot see, because all of those
    literals could remain textually present while the actual arithmetic or
    control flow diverged.
    """
    server_results = mcp_server.gestalt_search(query, limit=5)
    assert not (server_results and "error" in server_results[0]), (
        f"gestalt_search errored on {query!r}: {server_results}"
    )

    harness_db = retrieval_harness.get_db()
    try:
        harness_results = retrieval_harness.search(harness_db, query, limit=5)
    finally:
        harness_db.close()

    server_order = [(r["slug"], r["block_id"]) for r in server_results]
    harness_order = [(r["slug"], r["block_id"]) for r in harness_results]
    assert server_order == harness_order, (
        f"query {query!r} diverged -- server={server_order} harness={harness_order}"
    )


def _expected_rrf_score(mcp_server, query: str, target_id: int, limit: int, k: int = 60) -> float:
    """Ground truth for gestalt_search's own reported score.

    Computed from the documented closed-form RRF formula (K=60, pinned by
    test_silent_failures.py::test_rrf_ceiling_is_what_we_think_it_is) applied
    to raw FTS5/vector query results -- NOT by calling gestalt_search or the
    harness's search(), so this is an independent check on the arithmetic
    rather than a comparison against another mock of the same logic. It does
    reuse the real `fts_match()` and `pool_size()` from gestalt_rank, since
    those ARE the production tokeniser and depth and re-deriving them here
    would itself risk drift.
    """
    db = mcp_server.get_db()
    try:
        score = 0.0
        pool = gestalt_rank.pool_size(limit, False)
        fts_query = gestalt_rank.fts_match(query)
        if fts_query:
            rows = db.execute(
                "SELECT rowid FROM sections_fts WHERE sections_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, pool),
            ).fetchall()
            for rank, row in enumerate(rows):
                if row["rowid"] == target_id:
                    score += 1.0 / (k + rank + 1)
        query_emb = _stub_vector("search_query: " + query)
        post = getattr(mcp_server._ec, "postprocess", None)  # the profile's post-processing, if the config has one
        if post:
            query_emb = post(query_emb)
        rows = db.execute(
            "SELECT id FROM sections_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (query_emb.tobytes(), pool),
        ).fetchall()
        for rank, row in enumerate(rows):
            if row["id"] == target_id:
                score += 1.0 / (k + rank + 1)
        return score
    finally:
        db.close()


@pytest.mark.parametrize(
    "query", ["how does reciprocal rank fusion combine bm25 and vector scores", "tokenisation"]
)
def test_gestalt_search_score_matches_closed_form_rrf(fixture_index, mcp_server, query):
    """gestalt_search's reported `score` must equal 1/(K+rank+1) summed over
    legs, within float tolerance -- not merely be present and sorted."""
    results = mcp_server.gestalt_search(query, limit=5)
    assert results, f"expected hits for {query!r}"
    for r in results:
        expected = round(_expected_rrf_score(mcp_server, query, _SLUG_TO_ID[r["slug"]], limit=5), 4)
        assert abs(r["score"] - expected) < 1e-9, (
            f"{r['slug']}: server reported {r['score']}, closed-form RRF gives {expected}"
        )


def test_pinned_fts_rowid_resolves_to_the_matched_section(tmp_path, mcp_server, monkeypatch):
    """gestalt_search_fts must return the CONTENT of the row that actually
    matched, not merely SOME result. A neighbour-row bug still returns a
    result with a plausible shape -- that is exactly what let it ship
    unnoticed through review and every test that only checks "a hit came back".
    """
    db_path = tmp_path / "pinned.db"
    _build_fixture_db(db_path, _FIXTURE_SECTIONS, pin_fts_rowid=True)
    monkeypatch.setattr(mcp_server, "DB_PATH", db_path, raising=False)

    hits = mcp_server.gestalt_search_fts("tokenisation", limit=5)
    assert hits, "expected a hit for a term unique to one section's heading"
    assert hits[0]["slug"] == "fts-tokenisation"
    assert "OR-join" in hits[0]["content"], "content must be the MATCHED row's own content"


def test_unpinned_fts_rowid_reproduces_the_2026_07_31_regression(tmp_path, mcp_server, monkeypatch):
    """Proof the previous test has teeth.

    Rebuilds the identical fixture with the FTS rowid shifted by one -- the
    exact shape FTS5's auto-assignment takes when an INSERT omits an explicit
    rowid (gestalt-index-builder.py:516-522; verified 2026-07-31 in
    production: MATCH 'normalize_advantage' hit rowid 594 and the tool
    displayed a different section, "Build complete" instead of "Build
    discipline"). If this test ever starts failing because the mismatch stops
    reproducing, that means gestalt_search_fts changed how it resolves rowid
    -> meta id, and `test_pinned_fts_rowid_resolves_to_the_matched_section`
    would no longer be exercising anything real -- investigate, don't delete.
    """
    db_path = tmp_path / "shifted.db"
    _build_fixture_db(db_path, _FIXTURE_SECTIONS, pin_fts_rowid=False)
    monkeypatch.setattr(mcp_server, "DB_PATH", db_path, raising=False)

    hits = mcp_server.gestalt_search_fts("tokenisation", limit=5)
    assert hits, "expected a result even with the id space shifted by one"
    assert hits[0]["slug"] != "fts-tokenisation", (
        "fixture no longer reproduces the rowid-shift bug -- "
        "gestalt_search_fts appears to have become immune to it"
    )
    assert hits[0]["slug"] == "embedding-prefixes", (
        "expected the match to resolve to its off-by-one NEIGHBOUR "
        "(id 1's fts rowid 2 collides with meta id 2) -- that collision IS "
        "the bug this test documents"
    )


# --------------------------------------------------------------------------
# Significance testing: the sign test is the wrong instrument
# --------------------------------------------------------------------------

def test_cluster_bootstrap_reports_no_effect_for_identical_runs(retrieval_harness):
    """A run compared with itself must show zero effect and p≈1."""
    same = {f"d{i}": [True, True, False, False] for i in range(5)}
    b = retrieval_harness.cluster_bootstrap(_synthetic_run(same), _synthetic_run(same))
    assert b is not None
    assert b["effect_size"] == 0.0
    assert b["p_value"] > 0.5


def test_cluster_bootstrap_detects_a_uniform_improvement(retrieval_harness):
    """A change that lifts every cluster must be distinguishable from noise."""
    worse = {f"d{i}": [False] * 4 for i in range(8)}
    better = {f"d{i}": [True] * 4 for i in range(8)}
    b = retrieval_harness.cluster_bootstrap(_synthetic_run(worse), _synthetic_run(better))
    assert b is not None
    assert b["effect_size"] > 0.9
    assert b["p_value"] < 0.05, "a corpus-wide improvement should be detectable"


def test_cluster_bootstrap_is_not_fooled_by_one_cluster(retrieval_harness):
    """The whole reason to resample by document rather than by query.

    Here 4 queries improve and none worsen — a per-query bootstrap or a sign test
    would call that significant. But all 4 belong to ONE target document, so the
    evidence is a single document, not four independent trials. Resampling whole
    clusters must decline to call it.
    """
    worse = {f"d{i}": [False] * 4 for i in range(8)}
    one_better = dict(worse)
    one_better["d0"] = [True] * 4
    b = retrieval_harness.cluster_bootstrap(_synthetic_run(worse), _synthetic_run(one_better))
    assert b is not None
    assert b["effect_size"] > 0, "the raw delta really is positive"
    assert b["p_value"] > 0.05, (
        "an improvement confined to one document cluster must NOT be reported as "
        "distinguishable from noise — that is the non-independence this test guards"
    )


def test_cluster_bootstrap_abstains_when_runs_are_not_comparable(retrieval_harness):
    """A changed golden set makes two runs different experiments, not two measurements."""
    a = {f"d{i}": [True] * 4 for i in range(8)}
    b = {f"d{i}": [True] * 7 for i in range(8)}  # different query counts per cluster
    assert retrieval_harness.cluster_bootstrap(_synthetic_run(a), _synthetic_run(b)) is None


def _synthetic_run(hits: dict) -> dict:
    """Build a runner-shaped result dict from {slug: [hit, ...]}."""
    return {
        "results": [
            {"query": f"{slug}-{i}", "expect_slug": slug, "rank": (1 if hit else None)}
            for slug, hs in hits.items()
            for i, hit in enumerate(hs)
        ]
    }


# --------------------------------------------------------------------------
# Shared ranking stages: the server and the runner agree with each knob on
# --------------------------------------------------------------------------
#
# A knob is only measured honestly if the runner moves with it exactly as the server does. Each test below sets
# the knob through the environment, runs the same queries through the real server and the real runner mirror, and
# compares the ranked (slug, block) lists. The model is the same stub as above. The reranker is a stub too.

_DUP_SECTIONS = [
    # Four chunks of one slug that all match "fusion", one chunk each of two other slugs.
    {"id": i, "slug": slug, "heading": head, "block_id": f"^b{i}", "content": text,
     "file_path": f"knowledge/{slug}.md", "content_hash": f"h{i}"}
    for i, (slug, head, text) in enumerate([
        ("retrieval-fusion", "Fusion overview", "Reciprocal rank fusion combines BM25 and vector search scores over ranked lists."),
        ("retrieval-fusion", "Fusion constant", "The constant K in rank fusion damps the top ranks of the vector search lists."),
        ("retrieval-fusion", "Fusion pitfalls", "Rank fusion over one list is only the order of that list, so score fusion differs."),
        ("retrieval-fusion", "Fusion history", "Fusion of ranked lists was introduced for metasearch before vector search existed."),
        ("fts-tokenisation", "FTS5 tokenisation", "FTS5 must OR-join quoted terms, so a vector search never silences the lexical leg."),
        ("embedding-prefixes", "Asymmetric prefixes", "The query prefix and the document prefix pair up in the vector search space."),
    ])
]

_DUP_QUERIES = [
    "how does rank fusion combine vector search lists",
    "what is the constant in fusion",
    "the query prefix for vector search",
    "tokenisation of quoted terms",
]


class _StubReranker:
    """Cross-encoder stand-in: the score is how many query tokens the pair's text holds. predict() is the only method used."""

    def predict(self, pairs, **_kw):
        out = []
        for q, doc in pairs:
            qt = {t for t in re.split(r"\W+", q.lower()) if t}
            out.append(float(len(qt & {t for t in re.split(r"\W+", doc.lower()) if t})))
        return out


class _ReverseReranker:
    """Scores the last pair highest, so the rerank visibly reverses the fusion order."""

    def predict(self, pairs, **_kw):
        return [float(i) for i in range(len(pairs))]


@pytest.fixture
def dup_index(tmp_path, mcp_server, retrieval_harness, monkeypatch):
    db_path = tmp_path / "dup.db"
    _build_fixture_db(db_path, _DUP_SECTIONS)
    monkeypatch.setenv("GESTALT_SEARCH_MODE", "hybrid")
    _pin_knobs_off(monkeypatch)
    for mod in (mcp_server, retrieval_harness):
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
        monkeypatch.setattr(mod, "get_model", lambda: _StubModel(), raising=False)
    return db_path


def _both(mcp_server, retrieval_harness, query, limit=5):
    server_rows = mcp_server.gestalt_search(query, limit=limit)
    assert not (server_rows and "error" in server_rows[0]), server_rows
    db = retrieval_harness.get_db()
    try:
        harness_rows = retrieval_harness.search(db, query, limit=limit)
    finally:
        db.close()
    return ([(r["slug"], r["block_id"]) for r in server_rows], [(r["slug"], r["block_id"]) for r in harness_rows], server_rows)


@pytest.mark.parametrize("query", _DUP_QUERIES)
def test_server_and_harness_agree_with_rerank(dup_index, mcp_server, retrieval_harness, monkeypatch, query):
    """Rerank on, with a stub cross-encoder, both paths read the same pool and order it the same way."""
    monkeypatch.setenv("GESTALT_RERANK", "on")
    monkeypatch.setattr(gestalt_rank, "get_reranker", lambda alias=None: _StubReranker())
    server_order, harness_order, rows = _both(mcp_server, retrieval_harness, query)
    assert server_order == harness_order
    assert all("rerank_score" in r for r in rows), "a reranked row carries rerank_score"


def test_rerank_changes_the_order_and_keeps_the_rrf_score(dup_index, mcp_server, retrieval_harness, monkeypatch):
    query = _DUP_QUERIES[0]
    off_order, _, off_rows = _both(mcp_server, retrieval_harness, query)
    monkeypatch.setenv("GESTALT_RERANK", "on")
    monkeypatch.setattr(gestalt_rank, "get_reranker", lambda alias=None: _ReverseReranker())
    on_order, harness_order, on_rows = _both(mcp_server, retrieval_harness, query)
    assert on_order == harness_order
    assert on_order != off_order, "the stub reverses the pool, so the order must move"
    off_scores = {(r["slug"], r["block_id"]): r["score"] for r in off_rows}
    for r in on_rows:
        assert r["score"] == off_scores.get((r["slug"], r["block_id"]), r["score"]), "score stays the RRF value"


def test_rerank_falls_back_to_fusion_order_when_the_model_fails(dup_index, mcp_server, retrieval_harness, monkeypatch, capsys):
    off_order, _, _ = _both(mcp_server, retrieval_harness, _DUP_QUERIES[0])
    monkeypatch.setenv("GESTALT_RERANK", "on")

    def boom(alias=None):
        raise ImportError("no sentence_transformers")

    monkeypatch.setattr(gestalt_rank, "get_reranker", boom)
    on_order, _, rows = _both(mcp_server, retrieval_harness, _DUP_QUERIES[0])
    assert on_order == off_order
    assert not any("rerank_score" in r for r in rows)
    assert "fell back to fusion order" in capsys.readouterr().err


@pytest.mark.parametrize("env", [
    {"GESTALT_SLUG_DECAY": "0.5"},
    {"GESTALT_SLUG_DECAY": "0"},
    {"GESTALT_FTS_STOPWORDS": "on"},
    {"GESTALT_FUSION": "convex", "GESTALT_FUSION_ALPHA": "0.3"},
    {"GESTALT_FUSION": "convex", "GESTALT_FUSION_ALPHA": "1"},
])
@pytest.mark.parametrize("query", _DUP_QUERIES)
def test_server_and_harness_agree_under_each_knob(dup_index, mcp_server, retrieval_harness, monkeypatch, env, query):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    server_order, harness_order, _ = _both(mcp_server, retrieval_harness, query)
    assert server_order == harness_order


def test_knobs_unset_leave_the_ranking_alone(dup_index, mcp_server, retrieval_harness):
    """With every knob unset, no row carries a new field and the pool is the old limit * 2."""
    rows = mcp_server.gestalt_search(_DUP_QUERIES[0], limit=3)
    assert rows and not any("rerank_score" in r for r in rows)
    assert gestalt_rank.config() == {
        "rerank": "off", "rerank_model": "bge", "rerank_depth": 40, "rerank_maxchars": 2000,
        "rerank_instruction": gestalt_rank.DEFAULT_QWEN_INSTRUCTION,
        "dedup": 1.0, "stopwords": False, "fusion": "rrf", "alpha": 0.5,
        "w_bm25": 0.5, "norm": "minmax", "missing": "zero", "abstain": False, "link_expand": False, "freshness": False,
    }


# --------------------------------------------------------------------------
# Runner side of the golden-set shapes: families, abstention, AUROC, config
# --------------------------------------------------------------------------

@pytest.fixture
def mini_db():
    d = sqlite3.connect(":memory:")
    d.row_factory = sqlite3.Row
    d.execute("CREATE VIRTUAL TABLE sections_fts USING fts5(slug, heading, block_id, body)")
    d.execute("CREATE TABLE sections_meta (id INTEGER PRIMARY KEY, slug, heading, block_id, anchors)")
    for i, (slug, body) in enumerate([("alpha", "zebra grazing habits"), ("beta", "quasar spectral lines"),
                                      ("alpha-log", "zebra grazing log entries"), ("gamma", "lichen on basalt")], 1):
        d.execute("INSERT INTO sections_fts(rowid, slug, heading, block_id, body) VALUES (?,?,?,?,?)", (i, slug, "h", "b", body))
        d.execute("INSERT INTO sections_meta VALUES (?,?,?,?,?)", (i, slug, "h", "b", ""))
    return d


def test_accept_set_unions_expect_any_and_families(retrieval_harness):
    acc = retrieval_harness.accept_set
    assert acc({"expect_slug": "a"}) == {"a"}
    assert acc({"expect_slug": "a", "expect_any": ["b"]}) == {"a", "b"}
    fams = [["a", "a-log"], ["x", "y"]]
    assert acc({"expect_slug": "a"}, fams) == {"a", "a-log"}
    assert acc({"expect_slug": "q", "expect_any": ["y"]}, fams) == {"q", "x", "y"}
    assert acc({"expect_slug": "z"}, fams) == {"z"}


def test_a_family_rescues_a_sibling_hit_and_is_counted(retrieval_harness, mini_db):
    cases = [{"query": "zebra grazing log", "expect_slug": "beta", "bucket": "ops"}]
    without = retrieval_harness.run_cases(mini_db, cases, "fts")
    withf = retrieval_harness.run_cases(mini_db, cases, "fts", [["beta", "alpha-log"]])
    assert without[0]["rank"] is None and withf[0]["rank"] == 1 and withf[0]["family_rescued"] is True
    assert without[0]["family_rescued"] is False


def test_abstention_rows_have_no_rank_and_stay_out_of_recall(retrieval_harness, mini_db):
    cases = [{"query": "zebra grazing", "expect_slug": "alpha", "bucket": "ops"},
             {"query": "quasar lines", "expect_slug": "beta", "bucket": "ops"},
             {"query": "cheese fondue recipe", "expect_none": True, "bucket": "abstain"},
             {"query": "lichen", "expect_none": True, "bucket": "abstain"}]
    res = retrieval_harness.run_cases(mini_db, cases, "fts")
    ab = [r for r in res if r.get("abstain")]
    assert len(ab) == 2 and all(r["rank"] is None and r["bucket"] == "abstain" and r["expect_slug"] is None for r in ab)
    m = retrieval_harness.metrics(res)
    assert m["n"] == 2 and m["recall@1"] == 1.0, "abstain rows are not misses"
    bm = retrieval_harness.bucket_metrics(res)
    assert bm["abstain"]["n"] == 2 and bm["abstain"]["answerable_n"] == 2
    # EVAL-008: an fts top score is raw bm25, which does not compare across queries, so the AUROC is withheld with the reason.
    assert bm["abstain"]["auroc"] is None and "bm25" in bm["abstain"]["auroc_note"]
    assert retrieval_harness.bucket_of({"expect_none": True, "bucket": "ops"}) == "abstain"


def test_auroc_matches_a_brute_force_pair_count():
    import random
    rng = random.Random(7)
    for _ in range(20):
        pos = [rng.choice([0.0, 1.0, 2.0, 3.0, 4.0]) for _ in range(rng.randint(1, 8))]
        neg = [rng.choice([0.0, 1.0, 2.0, 3.0, 4.0]) for _ in range(rng.randint(1, 8))]
        brute = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))
        assert gestalt_rank.auroc(pos, neg) == pytest.approx(brute)
    assert gestalt_rank.auroc([3, 4], [1, 2]) == 1.0 and gestalt_rank.auroc([1, 2], [3, 4]) == 0.0
    assert gestalt_rank.auroc([], [1]) is None


def test_bootstrap_and_paired_drop_skip_abstention_rows(retrieval_harness):
    rows = lambda hit: [{"query": f"q{i}", "expect_slug": f"s{i // 2}", "rank": 1 if hit else None} for i in range(12)] + \
                       [{"query": "none", "expect_slug": None, "rank": None, "abstain": True}]
    before, after = {"results": rows(False)}, {"results": rows(True)}
    assert retrieval_harness.cluster_bootstrap(before, after)["clusters"] == 6
    assert retrieval_harness.paired_drop(before, after)["n_shared"] == 12
    assert retrieval_harness.significance(before, after)["gained"] == 12
    assert retrieval_harness.effective_n(after["results"]) == 6


def test_paired_drop_on_block_hits(retrieval_harness):
    mk = lambda hits: {"results": [{"query": f"q{i}", "expect_slug": f"s{i}", "expect_block": "b", "rank": 1, "block_hit": h}
                                   for i, h in enumerate(hits)] + [{"query": "nb", "expect_slug": "z", "expect_block": None, "rank": 1}]}
    base = [True] * 20
    after = [True] * 16 + [False] * 4
    pd = retrieval_harness.paired_drop(mk(base), mk(after), metric="block")
    assert pd["n_shared"] == 20 and pd["net_lost"] == 4 and pd["threshold"] == 2 and pd["real_drop"] is True
    assert pd["metric"] == "block precision"
    assert retrieval_harness.paired_drop(mk(base), mk(base), metric="block")["real_drop"] is False


def test_config_diff_gates_only_on_a_changed_knob(retrieval_harness):
    opts = retrieval_harness.resolve_opts()
    cfg = retrieval_harness.run_config(opts)
    assert retrieval_harness.config_diff(cfg, None) == [], "knobs off equals the banked default"
    assert retrieval_harness.config_diff(cfg, {}) == []
    assert retrieval_harness.config_diff(retrieval_harness.run_config({**opts, "decay": 0.5}), None) == ["dedup"]
    assert "rerank" in retrieval_harness.config_diff(retrieval_harness.run_config({**opts, "rerank": True}), None)
    convex = retrieval_harness.run_config({**opts, "fusion": "convex", "alpha": 0.4})
    assert retrieval_harness.config_diff(convex, None) == ["fusion", "alpha"] or set(retrieval_harness.config_diff(convex, None)) == {"fusion", "alpha"}


def _gate_fixture(retrieval_harness, **cfg_over):
    """A run and a bank that are comparable except for the knobs in cfg_over."""
    opts = retrieval_harness.resolve_opts()
    rows = [{"qid": f"q{i}", "query": f"q{i}", "expect_slug": f"s{i % 4}", "rank": 1} for i in range(12)]
    base = {"config": retrieval_harness.run_config(opts), "mode": "hybrid", "split": "all", "golden_sha256": "g", "n_cases": 12}
    out = {"summary": {"mode": "hybrid", "split": "all", "config": retrieval_harness.run_config({**opts, **cfg_over})}, "results": rows}
    return out, base, {"results": rows}


def test_check_baseline_fails_when_the_config_differs_unless_rebank_is_allowed(retrieval_harness, capsys):
    out, base, prev = _gate_fixture(retrieval_harness, stopwords=True)
    lines = retrieval_harness.check_baseline(out, base, prev, golden_sha="g", n_golden=12)
    assert len(lines) == 1 and "config drift: stopwords=True" in lines[0]
    assert "not comparable" in capsys.readouterr().out
    assert retrieval_harness.check_baseline(out, base, prev, allow_rebank=True, golden_sha="g", n_golden=12) == []
    assert "SKIPPED" in capsys.readouterr().out


def test_baseline_check_exits_non_zero_on_config_drift(retrieval_harness, tmp_path, monkeypatch, capsys):
    out, base, prev = _gate_fixture(retrieval_harness, decay=0.5)
    bank = tmp_path / "baseline.json"
    bank.write_text(json.dumps({"schema": 2, "configs": {"default": base}}))
    (tmp_path / "baseline-results.json").write_text(json.dumps(prev))
    monkeypatch.setattr(retrieval_harness, "BASELINE", bank)
    monkeypatch.setattr(retrieval_harness, "evaluate", lambda *a, **k: out)
    monkeypatch.setattr(retrieval_harness, "report", lambda o: None)
    monkeypatch.setattr(retrieval_harness, "load_cases", lambda: ([{"query": "x"}] * 12, []))
    monkeypatch.setattr(retrieval_harness, "golden_sha256", lambda cases, fams: "g")
    monkeypatch.setattr(sys, "argv", ["run", "--baseline", "check"])
    with pytest.raises(SystemExit) as e:
        retrieval_harness.main()
    assert e.value.code == 1 and "config drift" in capsys.readouterr().out
    for flag in ("--allow-rebank", "--allow-config-drift"):  # the old flag name is an alias
        monkeypatch.setattr(sys, "argv", ["run", "--baseline", "check", flag])
        with pytest.raises(SystemExit) as e:
            retrieval_harness.main()
        assert e.value.code == 0 and "SKIPPED" in capsys.readouterr().out


def test_a_family_edit_is_a_config_change(retrieval_harness):
    opts = retrieval_harness.resolve_opts()
    fam = [["a", "b"]]
    bank = retrieval_harness.run_config(opts, fam)
    assert retrieval_harness.config_diff(retrieval_harness.run_config(opts, [["b", "a"]]), bank) == [], "member order is not an edit"
    assert retrieval_harness.config_diff(retrieval_harness.run_config(opts, [["a", "b", "c"]]), bank) == ["families"]
    assert retrieval_harness.config_diff(retrieval_harness.run_config(opts, []), bank) == ["families"]
    assert retrieval_harness.config_diff(retrieval_harness.run_config(opts, fam), {k: v for k, v in bank.items() if k != "families"}) == [], "an old bank has nothing to compare"


@pytest.mark.parametrize("flag,value", [("--dedup", "1.5"), ("--dedup", "-0.1"), ("--alpha", "2"), ("--alpha", "-1")])
def test_cli_rejects_dedup_and_alpha_outside_zero_one(retrieval_harness, monkeypatch, capsys, flag, value):
    monkeypatch.setattr(retrieval_harness, "evaluate", lambda *a, **k: pytest.fail("must reject before it evaluates"))
    monkeypatch.setattr(sys, "argv", ["run", flag, value])
    with pytest.raises(SystemExit) as e:
        retrieval_harness.main()
    assert e.value.code == 2 and flag in capsys.readouterr().err


@pytest.mark.parametrize("mode,hub,cuda,want", [
    ("off", True, True, None), ("on", False, False, "bge"), ("auto", False, True, None),
    ("auto", True, False, None), ("auto", True, True, "bge"),
])
def test_rerank_resolved_is_one_rule(monkeypatch, mode, hub, cuda, want):
    monkeypatch.setenv("GESTALT_RERANK", mode)
    monkeypatch.delenv("GESTALT_RERANK_MODEL", raising=False)
    assert gestalt_rank.rerank_resolved(hub, cuda) == want


def test_server_and_runner_resolve_auto_the_same_way_and_save_refuses_when_on(mcp_server, retrieval_harness, monkeypatch, capsys):
    monkeypatch.setenv("GESTALT_RERANK", "auto")
    monkeypatch.setattr(gestalt_rank, "is_hub", lambda: True)
    monkeypatch.setattr(gestalt_rank, "cuda_available", lambda: True)
    monkeypatch.setattr(mcp_server, "_is_hub", lambda: True)
    assert mcp_server._rerank_alias_for_search() == "bge"
    opts = retrieval_harness.resolve_opts()
    assert opts["rerank"] is True, "the runner agrees with the server"
    cfg = retrieval_harness.run_config(opts)
    assert cfg["rerank"] == "on" and cfg["rerank_env"] == "auto"
    monkeypatch.setattr(retrieval_harness, "evaluate", lambda *a, **k: {"summary": {"mode": "hybrid", "config": cfg}})
    monkeypatch.setattr(retrieval_harness, "report", lambda o: None)
    monkeypatch.setattr(sys, "argv", ["run", "--baseline", "save"])
    with pytest.raises(SystemExit) as e:
        retrieval_harness.main()
    assert e.value.code == 1 and "rerank on" in capsys.readouterr().out
    monkeypatch.setattr(gestalt_rank, "cuda_available", lambda: False)
    monkeypatch.setattr(mcp_server, "_is_hub", lambda: False)
    assert mcp_server._rerank_alias_for_search() is None and retrieval_harness.resolve_opts()["rerank"] is False


def test_evaluate_end_to_end_on_the_fts_leg(retrieval_harness, tmp_path, monkeypatch):
    db_path = tmp_path / "e2e.db"
    _build_fixture_db(db_path, _DUP_SECTIONS)
    golden = tmp_path / "golden.yaml"
    golden.write_text("""
families:
  - [retrieval-fusion, fts-tokenisation]
cases:
  - query: "how does rank fusion combine vector search lists"
    expect_slug: retrieval-fusion
    bucket: ops
    expect_block: b0
  - query: "metasearch history"
    expect_slug: fts-tokenisation
    bucket: ops
  - query: "zzzz qqqq"
    expect_none: true
    bucket: abstain
""")
    monkeypatch.setattr(retrieval_harness, "DB_PATH", db_path)
    monkeypatch.setattr(retrieval_harness, "GOLDEN", golden)
    _pin_knobs_off(monkeypatch)
    out = retrieval_harness.evaluate("fts")
    s = out["summary"]
    assert (s["n_cases"], s["n_answerable"], s["n_abstain"]) == (3, 2, 1)
    assert s["config"]["dedup"] == 1.0 and s["config"]["rerank"] == "off" and "dedup_block_displaced" not in s
    assert s["family_rescued"] >= 1 and s["buckets"]["abstain"]["n"] == 1
    out2 = retrieval_harness.evaluate("fts", decay=0.0)
    assert out2["summary"]["config"]["dedup"] == 0.0 and "dedup_block_displaced" in out2["summary"]


def test_compare_runs_prints_the_paired_bootstrap(retrieval_harness, tmp_path, capsys):
    mk = lambda hit: {"summary": {"mode": "fts", "config": {"dedup": 1.0}},
                      "results": [{"query": f"q{i}", "expect_slug": f"s{i // 2}", "rank": 1 if hit else None,
                                   "expect_block": "b", "block_hit": hit} for i in range(16)]}
    saved = tmp_path / "saved.json"
    saved.write_text(json.dumps(mk(False)))
    retrieval_harness.compare_runs(str(saved), {**mk(True), "fts_results": mk(True)["results"]})
    text = capsys.readouterr().out
    assert "recall@3 moved +100.0%" in text and "block precision: net +16 of 16" in text


# --- rerank plumbing in gestalt_rank and the server's unload -----------------------------------------

def test_reranker_import_failure_keeps_the_fusion_order(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # a leaf without the package: the import raises ImportError
    monkeypatch.setattr(gestalt_rank, "_rerankers", {})
    monkeypatch.setattr(gestalt_rank, "_load_failed", {})
    rows = [{"slug": "a", "heading": "h", "content": "x"}, {"slug": "b", "heading": "h", "content": "y"}]
    out, scores, info = gestalt_rank.rerank_rows("q", rows, "bge")
    assert out == rows and scores is None and info["fallback"] in ("ImportError", "ModuleNotFoundError")
    assert "rerank fell back to fusion order" in capsys.readouterr().err
    with pytest.raises(RuntimeError, match="failed to load less than"):
        gestalt_rank.get_reranker("bge")  # a broken load is not retried on every query


def test_qwen_4b_falls_back_to_bge_without_the_vram(monkeypatch, capsys):
    monkeypatch.setattr(gestalt_rank, "_alias_memo", {})
    monkeypatch.setitem(sys.modules, "torch", None)
    assert gestalt_rank.resolve_alias("qwen3-4b") == "bge"
    assert "using bge" in capsys.readouterr().err
    assert gestalt_rank.resolve_alias("qwen3-0.6b") == "qwen3-0.6b"


def test_the_fallback_line_is_logged_once_per_ttl_window(monkeypatch, capsys):
    now = [1000.0]
    monkeypatch.setattr(gestalt_rank.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(gestalt_rank, "_alias_memo", {})
    monkeypatch.setitem(sys.modules, "torch", None)
    for _ in range(5):
        assert gestalt_rank.resolve_alias("qwen3-4b") == "bge"
    assert capsys.readouterr().err.count("using bge") == 1
    now[0] += gestalt_rank.ALIAS_TTL_S + 1
    gestalt_rank.resolve_alias("qwen3-4b")
    assert capsys.readouterr().err.count("using bge") == 1, "a new window logs again"


def test_the_qwen_aliases_pass_prompt_to_a_predict_with_the_real_signature(monkeypatch):
    """The stub takes `prompt` as a keyword and nothing else, as CrossEncoder.predict does. A drift raises TypeError, and rerank_rows would then fall back without a word."""
    seen = {}

    class M:
        def predict(self, inputs, *, prompt=None, batch_size=32):
            seen["prompt"] = prompt
            return [1.0, 0.0]

    monkeypatch.setattr(gestalt_rank, "get_reranker", lambda alias=None: M())
    monkeypatch.setattr(gestalt_rank, "resolve_alias", lambda alias=None: alias)  # the VRAM check is not under test
    rows = [{"slug": "a", "heading": "h", "content": "x"}, {"slug": "b", "heading": "h", "content": "y"}]
    for alias in ("qwen3-0.6b", "qwen3-4b"):
        seen.clear()
        _, scores, info = gestalt_rank.rerank_rows("q", rows, alias)
        assert info["fallback"] == "none" and scores is not None, info
        assert seen["prompt"] == gestalt_rank.DEFAULT_QWEN_INSTRUCTION


def test_the_qwen_instruction_reaches_predict_only_for_qwen_aliases(monkeypatch):
    seen = []

    class M:
        def predict(self, pairs, **kw):
            seen.append({k: v for k, v in kw.items() if k != "batch_size"})  # the batch size has its own test
            return [1.0, 0.0]

    monkeypatch.setattr(gestalt_rank, "get_reranker", lambda alias=None: M())
    rows = [{"slug": "a", "heading": "h", "content": "x"}, {"slug": "b", "heading": "h", "content": "y"}]
    gestalt_rank.rerank_rows("q", rows, "bge")
    gestalt_rank.rerank_rows("q", rows, "qwen3-0.6b")
    assert seen == [{}, {"prompt": gestalt_rank.DEFAULT_QWEN_INSTRUCTION}]


def test_the_server_unloads_an_idle_reranker(mcp_server, monkeypatch, capsys):
    import time
    monkeypatch.setenv("GESTALT_MODEL_IDLE_S", "1")
    monkeypatch.setattr(mcp_server, "_model", None)
    monkeypatch.setattr(gestalt_rank, "_rerankers", {"bge": object()})
    monkeypatch.setattr(gestalt_rank, "_rerank_last_used", time.monotonic())
    assert mcp_server.unload_model() is False, "not idle yet"
    monkeypatch.setattr(gestalt_rank, "_rerank_last_used", time.monotonic() - 5)
    assert mcp_server.unload_model() is True and not gestalt_rank.rerank_loaded()
    assert "reranker unloaded after idle" in capsys.readouterr().err


def test_the_hit_log_names_the_reranker(dup_index, mcp_server, monkeypatch, tmp_path):
    import json as _json
    log = tmp_path / "hits.jsonl"
    monkeypatch.setenv("GESTALT_HITS_LOG", str(log))
    monkeypatch.setenv("GESTALT_RERANK", "on")
    monkeypatch.setattr(gestalt_rank, "get_reranker", lambda alias=None: _StubReranker())
    mcp_server.gestalt_search(_DUP_QUERIES[0], limit=3)
    assert _json.loads(log.read_text().splitlines()[-1])["rr"] == "bge"


def test_the_rerank_log_line_reports_every_field_of_the_info():
    info = {"model": "bge", "n": 17, "ms": 231, "top1_changed": True, "fallback": "none"}
    line = gestalt_rank.rerank_log_line(info)
    for value in info.values():
        assert str(value) in line


def test_hook_query_shapes():
    hook = _load("pi_hook_query", "claude-tree/hooks/prompt-intelligence.py")
    import os
    os.environ.pop("GESTALT_HOOK_QUERY", None)
    p = "I am setting up the index. Why does the fleet check fail on the hub?"
    assert hook.hook_query(p) == p
    os.environ["GESTALT_HOOK_QUERY"] = "last"
    try:
        assert hook.hook_query(p) == "Why does the fleet check fail on the hub?"
        assert hook.hook_query("???") == "???"
    finally:
        del os.environ["GESTALT_HOOK_QUERY"]


def test_hook_sets_src_hook_before_it_searches(monkeypatch):
    hook = _load("pi_hook_src", "claude-tree/hooks/prompt-intelligence.py")
    import os
    monkeypatch.delenv("GESTALT_HIT_SRC", raising=False)
    monkeypatch.setenv("GESTALT_HITS_LOG", "off")
    hook.retrieve_gestalt_sync("anything at all", str(REPO), 1)
    assert os.environ.get("GESTALT_HIT_SRC") == "hook"


def test_idf_query_keeps_the_rarest_tokens(tmp_path):
    db_path = tmp_path / "idf.db"
    _build_fixture_db(db_path, _DUP_SECTIONS)
    db = sqlite3.connect(str(db_path))
    try:
        got = gestalt_rank.idf_top_tokens(db, "how does the reciprocal fusion of the vector search work in tokenisation", k=2)
    finally:
        db.close()
    assert got.split() == ["reciprocal", "tokenisation"], "the two rarest tokens, in prompt order, with stopwords gone"
    assert gestalt_rank.idf_top_tokens(None, "the fusion", k=6) == "fusion", "short queries need no index"


# --------------------------------------------------------------------------
# The embed-config contract: device, remote code, kwargs, postprocess, side index
# --------------------------------------------------------------------------

def _meta_db(meta: dict):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)")
    for k, v in meta.items():
        db.execute("INSERT INTO index_meta VALUES (?, ?)", (k, v))
    return db


def _matching_meta(ec):
    return {"model_name": ec.MODEL_NAME, "model_revision": ec.MODEL_REVISION or "", "embed_dim": str(ec.EMBED_DIM),
            "doc_prefix": ec.DOC_PREFIX, "query_prefix": ec.QUERY_PREFIX}


def test_vector_leg_checks_text_format_and_profile_only_when_the_index_has_them(mcp_server, monkeypatch):
    ec = mcp_server._ec
    monkeypatch.setattr(ec, "TEXT_FORMAT", "v2-title", raising=False)
    monkeypatch.setattr(ec, "PROFILE", "nomic", raising=False)
    mcp_server._meta_logged.clear()
    assert mcp_server._vector_leg_ok(_meta_db(_matching_meta(ec))) is True, "an older index without the keys still passes"
    assert mcp_server._vector_leg_ok(_meta_db({**_matching_meta(ec), "text_format": "v2-title", "embed_profile": "nomic"})) is True
    assert mcp_server._vector_leg_ok(_meta_db({**_matching_meta(ec), "text_format": "v1"})) is False
    assert mcp_server._vector_leg_ok(_meta_db({**_matching_meta(ec), "embed_profile": "qwen3-4b"})) is False


def test_get_model_takes_device_remote_code_and_kwargs_from_the_config(mcp_server, monkeypatch):
    seen = {}

    class FakeST:
        def __init__(self, name, **kw):
            seen["name"], seen["kw"] = name, kw

    monkeypatch.setitem(sys.modules, "sentence_transformers", type("M", (), {"SentenceTransformer": FakeST}))
    monkeypatch.setattr(mcp_server, "_model", None)
    monkeypatch.setattr(mcp_server, "EMBED_DEVICE", "cuda")
    monkeypatch.setattr(mcp_server, "TRUST_REMOTE_CODE", False)
    monkeypatch.setattr(mcp_server, "MODEL_KWARGS", {"attn_implementation": "sdpa"})
    monkeypatch.setattr(mcp_server, "TOKENIZER_KWARGS", {"padding_side": "left"})
    mcp_server.get_model()
    mcp_server._model = None
    assert seen["kw"]["device"] == "cuda" and seen["kw"]["trust_remote_code"] is False
    assert seen["kw"]["model_kwargs"] == {"attn_implementation": "sdpa"} and seen["kw"]["tokenizer_kwargs"] == {"padding_side": "left"}


def test_the_query_vector_goes_through_postprocess(mcp_server, retrieval_harness, monkeypatch):
    marker = []

    def post(vec):
        marker.append(1)
        return vec * 0

    monkeypatch.setattr(mcp_server._ec, "postprocess", post, raising=False)
    monkeypatch.setattr(retrieval_harness._ec, "postprocess", post, raising=False)
    assert float(mcp_server._embed_query(_StubModel(), "x").sum()) == 0.0
    monkeypatch.setattr(retrieval_harness, "get_model", lambda: _StubModel())
    assert float(retrieval_harness._embed_query("x").sum()) == 0.0 and len(marker) == 2


def test_gestalt_search_dir_moves_the_server_and_the_runner_to_a_side_index(tmp_path, monkeypatch):
    monkeypatch.setenv("GESTALT_SEARCH_DIR", str(tmp_path))
    monkeypatch.delitem(sys.modules, "gestalt_embed_config", raising=False)  # the config reads the env at import, so it loads fresh here
    server = _load("gms_side_dir", "tools/gestalt-mcp-server.py")
    runner = _load("rre_side_dir", "evals/retrieval/run_retrieval_evals.py")
    assert server.DB_PATH == tmp_path / "gestalt.db" and runner.DB_PATH == tmp_path / "gestalt.db"
    assert runner.run_config(runner.resolve_opts())["search_dir"] == str(tmp_path / "gestalt.db")


def test_reranker_device_is_explicit(monkeypatch):
    """The loader names the device itself. CUDA when torch sees it, in float16, else cpu, and GESTALT_RERANK_DEVICE overrides both."""
    monkeypatch.delenv("GESTALT_RERANK_DEVICE", raising=False)
    monkeypatch.setattr(gestalt_rank, "cuda_available", lambda: False)
    assert gestalt_rank.rerank_device_kwargs() == ("cpu", {})
    monkeypatch.setattr(gestalt_rank, "cuda_available", lambda: True)
    device, kwargs = gestalt_rank.rerank_device_kwargs()
    assert device == "cuda"
    assert kwargs["model_kwargs"]["torch_dtype"] == "float16", "the dtype is a string, so naming it needs no torch"
    monkeypatch.setenv("GESTALT_RERANK_DEVICE", "cpu")
    assert gestalt_rank.rerank_device_kwargs() == ("cpu", {})


def test_compare_files_needs_no_model_and_reads_two_saved_runs(tmp_path, retrieval_harness, capsys, monkeypatch):
    """--compare-files compares two saved runs and evaluates nothing, so a comparison costs seconds and never loads a model or touches the GPU."""
    import json
    import sys

    base = {"summary": {"mode": "hybrid", "config": {}}, "fts_results": [],
            "results": [{"query": f"q{i}", "expect_slug": f"s{i % 5}", "rank": (1 if i % 2 else 4), "block_hit": bool(i % 2), "expect_block": "b", "hard": False, "bucket": "ops"} for i in range(40)]}
    better = json.loads(json.dumps(base))
    for r in better["results"]:
        r["rank"] = 1
        r["block_hit"] = True
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(base)); b.write_text(json.dumps(better))
    monkeypatch.setattr(sys, "argv", ["run_retrieval_evals.py", "--compare-files", str(a), str(b)])
    monkeypatch.setattr(retrieval_harness, "evaluate", lambda *x, **k: (_ for _ in ()).throw(AssertionError("must not evaluate")))
    retrieval_harness.main()
    out = capsys.readouterr().out
    assert "compare vs" in out and "recall@3" in out


def test_rerank_instruction_is_configurable_and_reaches_predict(monkeypatch):
    """GESTALT_RERANK_INSTRUCTION replaces the default task line for the Qwen rerankers and is recorded in the run config. The default names a personal knowledge base, which is the wrong task for a public benchmark."""
    seen = {}

    class _Stub:
        def predict(self, inputs, *, prompt=None, batch_size=32):
            seen["prompt"] = prompt
            return [0.0] * len(inputs)

    monkeypatch.setattr(gestalt_rank, "get_reranker", lambda alias=None: _Stub())
    monkeypatch.setattr(gestalt_rank, "resolve_alias", lambda alias=None: "qwen3-0.6b")
    monkeypatch.setenv("GESTALT_RERANK_INSTRUCTION", "Given a scientific claim, retrieve documents that support or refute the claim")
    rows = [{"slug": "a", "heading": "h", "content": "x", "title": "t"}, {"slug": "b", "heading": "h", "content": "y", "title": "t"}]
    gestalt_rank.rerank_rows("claim", rows, "qwen3-0.6b")
    assert seen["prompt"] == "Given a scientific claim, retrieve documents that support or refute the claim"
    assert gestalt_rank.config()["rerank_instruction"] == seen["prompt"]
    monkeypatch.delenv("GESTALT_RERANK_INSTRUCTION")
    assert gestalt_rank.config()["rerank_instruction"] == gestalt_rank.DEFAULT_QWEN_INSTRUCTION


def test_runner_model_device_follows_the_env_not_the_server_default(monkeypatch, retrieval_harness):
    """The eval runner passes a device only when GESTALT_EMBED_DEVICE is set, like the index builder. The server's cpu default must not reach the benchmarks."""
    import sys

    seen = {}

    class _ST:
        def __init__(self, name, **kw):
            seen.update(kw)

    monkeypatch.setitem(sys.modules, "sentence_transformers", type("m", (), {"SentenceTransformer": _ST}))
    monkeypatch.setattr(retrieval_harness, "_model", None)
    monkeypatch.delenv("GESTALT_EMBED_DEVICE", raising=False)
    retrieval_harness.get_model()
    assert "device" not in seen
    seen.clear()
    monkeypatch.setattr(retrieval_harness, "_model", None)
    monkeypatch.setenv("GESTALT_EMBED_DEVICE", "cuda")
    retrieval_harness.get_model()
    assert seen.get("device") == "cuda"


def test_rerank_batch_default_is_8_and_is_passed_to_predict(monkeypatch):
    """predict gets batch_size=8 unless GESTALT_RERANK_BATCH says otherwise. Eight was the fastest of 8..64 on the hub GPU, and the knob is clamped to 1..256."""
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("gr_batch", Path(__file__).resolve().parents[1] / "tools" / "gestalt_rank.py")
    gr = importlib.util.module_from_spec(spec); spec.loader.exec_module(gr)
    seen = {}

    class Model:
        def predict(self, pairs, *, batch_size=32, prompt=None):
            seen["bs"] = batch_size
            return [float(len(p[1])) for p in pairs]

    monkeypatch.setattr(gr, "get_reranker", lambda alias=None: Model())
    rows = [{"slug": "a", "title": "t", "heading": "h", "content": "x" * 5}, {"slug": "b", "title": "t", "heading": "h", "content": "y" * 50}]
    monkeypatch.delenv("GESTALT_RERANK_BATCH", raising=False)
    out, scores, info = gr.rerank_rows("q", rows, "bge")
    assert seen["bs"] == 8 and info["fallback"] == "none" and out[0]["slug"] == "b"
    monkeypatch.setenv("GESTALT_RERANK_BATCH", "16")
    gr.rerank_rows("q", rows, "bge"); assert seen["bs"] == 16
    monkeypatch.setenv("GESTALT_RERANK_BATCH", "9999")
    assert gr.rerank_batch() == 256
    monkeypatch.setenv("GESTALT_RERANK_BATCH", "0")
    assert gr.rerank_batch() == 1


# --- gestalt_rank.hybrid_search on a hand-built index ------------------------------------------------------------------
# Five chunks. The lexical leg for "alpha" ranks 0, 1, 2. The dense leg for the query vector (1, 0, 0) ranks 3, 2, 0, 1, 4.
# Reciprocal rank fusion with K=60 then orders 0, 2, 1, 3, 4, which no single leg does.

_TINY = [
    (0, "x", "alpha alpha alpha alpha", (0.5, 0.5, 0.0)),
    (1, "x", "alpha alpha", (0.0, 1.0, 0.0)),
    (2, "y", "alpha", (0.9, 0.1, 0.0)),
    (3, "z", "beta", (1.0, 0.0, 0.0)),
    (4, "z", "gamma", (0.0, 0.0, 1.0)),
]
_Q = (1.0, 0.0, 0.0)


def _tiny_embed(_query):
    import struct
    return struct.pack("3f", *_Q)


@pytest.fixture
def tiny(tmp_path, monkeypatch):
    sqlite_vec = pytest.importorskip("sqlite_vec")
    import struct
    for k in ("GESTALT_RERANK", "GESTALT_RERANK_MODEL", "GESTALT_RERANK_DEPTH", "GESTALT_SLUG_DECAY", "GESTALT_FTS_STOPWORDS",
              "GESTALT_FUSION", "GESTALT_FUSION_ALPHA"):
        monkeypatch.delenv(k, raising=False)
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.executescript(
        "CREATE TABLE sections_meta (id INTEGER PRIMARY KEY, slug TEXT, title TEXT, heading TEXT, block_id TEXT, anchors TEXT, content TEXT);"
        "CREATE VIRTUAL TABLE sections_fts USING fts5(slug, heading, block_id, content);"
        "CREATE VIRTUAL TABLE sections_vec USING vec0(id INTEGER PRIMARY KEY, embedding FLOAT[3]);"
    )
    for i, slug, content, vec in _TINY:
        db.execute("INSERT INTO sections_meta VALUES (?, ?, '', ?, ?, '', ?)", (i, slug, f"h{i}", f"^b{i}", content))
        db.execute("INSERT INTO sections_fts(rowid, slug, heading, block_id, content) VALUES (?, '', '', '', ?)", (i, content))
        db.execute("INSERT INTO sections_vec(id, embedding) VALUES (?, ?)", (i, struct.pack("3f", *vec)))
    yield db
    db.close()


def _ids(found):
    return [r["id"] for r in found.rows]


def test_rrf_orders_by_both_legs(tiny):
    found = gestalt_rank.hybrid_search(tiny, "alpha", 4, embed_query=_tiny_embed, rerank=False, decay=1.0)
    assert _ids(found) == [0, 2, 1, 3]
    k = gestalt_rank.RRF_K
    assert found.fused_scores[0] == pytest.approx(1 / (k + 1) + 1 / (k + 3))
    assert found.fused_scores[3] == pytest.approx(1 / (k + 1))
    assert found.scores == found.fused_scores and found.rerank_scores == {} and found.rerank is None
    assert found.top_score == pytest.approx(found.fused_scores[0])
    assert found.rerank_fallback is False


def test_convex_fusion_follows_alpha(tiny):
    lexical = gestalt_rank.hybrid_search(tiny, "alpha", 5, embed_query=_tiny_embed, rerank=False, decay=1.0, fusion="convex", alpha=1.0)
    dense = gestalt_rank.hybrid_search(tiny, "alpha", 5, embed_query=_tiny_embed, rerank=False, decay=1.0, fusion="convex", alpha=0.0)
    assert _ids(lexical)[:2] == [0, 1], "the worst lexical match ties with the non-matches at zero"
    assert _ids(dense) == [3, 2, 0, 1, 4]
    assert (lexical.fusion, lexical.alpha) == ("convex", 1.0)


def test_fusion_and_alpha_are_read_from_the_environment(tiny, monkeypatch):
    monkeypatch.setenv("GESTALT_FUSION", "convex")
    monkeypatch.setenv("GESTALT_FUSION_ALPHA", "0")
    found = gestalt_rank.hybrid_search(tiny, "alpha", 5, embed_query=_tiny_embed, rerank=False, decay=1.0)
    assert _ids(found) == [3, 2, 0, 1, 4]


def test_pool_size_follows_the_rerank(tiny, monkeypatch):
    monkeypatch.setattr(gestalt_rank, "get_reranker", lambda alias=None: _ReverseReranker())
    off = gestalt_rank.hybrid_search(tiny, "alpha", 4, embed_query=_tiny_embed, rerank=False)
    on = gestalt_rank.hybrid_search(tiny, "alpha", 4, embed_query=_tiny_embed, rerank=True, rerank_alias="bge")
    assert off.pool == 8 and on.pool == max(8, gestalt_rank.rerank_depth())


def test_rerank_reorders_and_reports_its_scores(tiny, monkeypatch):
    monkeypatch.setattr(gestalt_rank, "get_reranker", lambda alias=None: _ReverseReranker())
    found = gestalt_rank.hybrid_search(tiny, "alpha", 4, embed_query=_tiny_embed, rerank=True, rerank_alias="bge", decay=1.0)
    assert _ids(found) == [4, 3, 1, 2], "the stub scores the last fused row highest"
    assert found.rerank["model"] == "bge" and found.rerank["n"] == 5 and found.rerank_fallback is False
    assert found.rerank_scores[4] == 4.0 and found.scores[4] == 4.0
    assert found.fused_scores[4] < found.fused_scores[1], "the fused score is kept beside the rerank score"
    assert found.top_score == 4.0


def test_a_failed_rerank_keeps_the_fusion_order_and_says_so(tiny, monkeypatch):
    def broken(alias=None):
        raise RuntimeError("no weights")

    monkeypatch.setattr(gestalt_rank, "get_reranker", broken)
    found = gestalt_rank.hybrid_search(tiny, "alpha", 4, embed_query=_tiny_embed, rerank=True, rerank_alias="bge", decay=1.0)
    assert _ids(found) == [0, 2, 1, 3]
    assert found.rerank_fallback is True and found.rerank["fallback"] == "RuntimeError"
    assert found.rerank_scores == {} and found.top_score == pytest.approx(found.fused_scores[0])


def test_a_single_candidate_is_not_a_fallback(tiny, monkeypatch):
    monkeypatch.setattr(gestalt_rank, "get_reranker", lambda alias=None: _ReverseReranker())
    found = gestalt_rank.hybrid_search(tiny, "gamma", 1, embed_query=lambda q: __import__("struct").pack("3f", 0, 0, 1),
                                       rerank=True, rerank_alias="bge", decay=1.0)
    assert found.rerank_fallback is False


def test_slug_dedup_demotes_a_repeated_slug(tiny):
    found = gestalt_rank.hybrid_search(tiny, "alpha", 3, embed_query=_tiny_embed, rerank=False, decay=0.0)
    assert _ids(found) == [0, 2, 3], "ids 0 and 1 share a slug, so id 1 gives way to id 3"
    assert found.decay == {"pool": 5, "slugs_distinct": 3, "demoted": 1}


def test_fts_mode_never_embeds_and_scores_by_negated_bm25(tiny):
    def forbidden(_q):
        raise AssertionError("the dense leg ran in fts mode")

    found = gestalt_rank.hybrid_search(tiny, "alpha", 3, embed_query=forbidden, mode="fts", rerank=True, decay=1.0)
    assert _ids(found) == [0, 1, 2] and found.rerank is None
    bm25 = tiny.execute("SELECT rank FROM sections_fts WHERE sections_fts MATCH '\"alpha\"' AND rowid = 0").fetchone()[0]
    assert found.top_score == pytest.approx(-bm25) and found.top_score > 0


def test_nothing_matching_gives_an_empty_result(tiny):
    tiny.execute("DELETE FROM sections_meta")
    found = gestalt_rank.hybrid_search(tiny, "alpha", 3, embed_query=_tiny_embed, rerank=False)
    assert found.rows == [] and found.top_score is None and found.scores == {}


def test_metadata_columns_are_light_unless_the_rerank_or_the_caller_needs_the_text(tiny, monkeypatch):
    monkeypatch.setattr(gestalt_rank, "get_reranker", lambda alias=None: _StubReranker())
    light = gestalt_rank.hybrid_search(tiny, "alpha", 3, embed_query=_tiny_embed, rerank=False)
    full = gestalt_rank.hybrid_search(tiny, "alpha", 3, embed_query=_tiny_embed, rerank=False, full=True)
    ranked = gestalt_rank.hybrid_search(tiny, "alpha", 3, embed_query=_tiny_embed, rerank=True, rerank_alias="bge")
    assert "content" not in light.rows[0].keys()
    assert "content" in full.rows[0].keys() and "content" in ranked.rows[0].keys()


def test_rerank_none_resolves_as_the_server_does(tiny, monkeypatch):
    monkeypatch.setattr(gestalt_rank, "get_reranker", lambda alias=None: _ReverseReranker())

    def ran():
        return gestalt_rank.hybrid_search(tiny, "alpha", 4, embed_query=_tiny_embed, decay=1.0).rerank is not None

    monkeypatch.setenv("GESTALT_RERANK", "off")
    assert ran() is False
    monkeypatch.setenv("GESTALT_RERANK", "on")
    assert ran() is True
    monkeypatch.setenv("GESTALT_RERANK", "auto")
    monkeypatch.setattr(gestalt_rank, "is_hub", lambda: False)
    monkeypatch.setattr(gestalt_rank, "cuda_available", lambda: True)
    assert ran() is False, "auto reranks only on the hub"
    monkeypatch.setattr(gestalt_rank, "is_hub", lambda: True)
    assert ran() is True
    monkeypatch.setattr(gestalt_rank, "cuda_available", lambda: False)
    assert ran() is False, "auto reranks only with CUDA"


def test_the_server_serves_what_hybrid_search_returns(tiny, mcp_server, monkeypatch, tmp_path):
    """The server adds the snippet, the annotations and the hit log. The order and the scores are hybrid_search's."""
    monkeypatch.setenv("GESTALT_HITS_LOG", "off")
    monkeypatch.setenv("GESTALT_RERANK", "off")
    monkeypatch.setenv("GESTALT_SEARCH_MODE", "hybrid")
    tiny.execute("ALTER TABLE sections_meta ADD COLUMN file_path TEXT DEFAULT 'knowledge/x.md'")
    path = tmp_path / "tiny.db"
    tiny.commit()
    disk = sqlite3.connect(path)
    tiny.backup(disk)
    disk.close()
    monkeypatch.setattr(mcp_server, "DB_PATH", path)
    monkeypatch.setattr(mcp_server, "get_model", lambda: object())
    monkeypatch.setattr(mcp_server, "_vector_leg_ok", lambda db: True)
    monkeypatch.setattr(mcp_server, "_embed_query", lambda model, text: __import__("numpy").array(_Q, dtype="float32"))
    rows = mcp_server.gestalt_search("alpha", limit=4)
    assert [r["block_id"] for r in rows] == ["^b0", "^b2", "^b1", "^b3"]
    assert rows[0]["score"] == round(1 / 61 + 1 / 63, 4)


# --- reranker guards ---------------------------------------------------------------------------------------------------


def _two_rows():
    return [{"slug": "a", "heading": "h", "content": "x"}, {"slug": "b", "heading": "h", "content": "y"}]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_reranker_score_makes_the_whole_call_a_fallback(monkeypatch, bad):
    class M:
        def predict(self, pairs, **_kw):
            return [0.5, bad]

    monkeypatch.setattr(gestalt_rank, "get_reranker", lambda alias=None: M())
    rows = _two_rows()
    out, scores, info = gestalt_rank.rerank_rows("q", rows, "bge")
    assert out == rows and scores is None and info["fallback"] == "nonfinite-score"


def test_a_cuda_fault_in_predict_evicts_the_reranker(monkeypatch):
    class M:
        def predict(self, pairs, **_kw):
            raise RuntimeError("CUDA error: an illegal memory access was encountered")

    monkeypatch.setattr(gestalt_rank, "_rerankers", {"bge": M()})
    _, scores, info = gestalt_rank.rerank_rows("q", _two_rows(), "bge")
    assert scores is None and info["fallback"] == "RuntimeError"
    assert not gestalt_rank.rerank_loaded(), "the next call must reload the weights"


def test_an_ordinary_error_in_predict_keeps_the_reranker(monkeypatch):
    class M:
        def predict(self, pairs, **_kw):
            raise ValueError("bad pair")

    monkeypatch.setattr(gestalt_rank, "_rerankers", {"bge": M()})
    gestalt_rank.rerank_rows("q", _two_rows(), "bge")
    assert gestalt_rank.rerank_loaded()


# --- knob parsing ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name,value,read", [
    ("GESTALT_RERANK", "maybe", gestalt_rank.rerank_mode),
    ("GESTALT_FUSION", "rff", gestalt_rank.fusion_mode),
    ("GESTALT_FTS_STOPWORDS", "perhaps", gestalt_rank.stopwords_on),
    ("GESTALT_FUSION_ALPHA", "abc", gestalt_rank.fusion_alpha),
    ("GESTALT_RERANK_DEPTH", "forty", gestalt_rank.rerank_depth),
])
def test_an_unrecognised_knob_value_warns_once_and_keeps_the_default(monkeypatch, capsys, name, value, read):
    monkeypatch.setattr(gestalt_rank, "_warned", set())
    monkeypatch.delenv(name, raising=False)
    default = read()
    monkeypatch.setenv(name, value)
    assert read() == default and read() == default
    err = capsys.readouterr().err
    assert err.count(name) == 1, "one line per variable, however often the knob is read"


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_a_float_knob_rejects_non_finite_values(monkeypatch, value):
    monkeypatch.setattr(gestalt_rank, "_warned", set())
    monkeypatch.setenv("GESTALT_SLUG_DECAY", value)
    assert gestalt_rank.slug_decay() == 1.0
    monkeypatch.setenv("GESTALT_FUSION_ALPHA", value)
    assert gestalt_rank.fusion_alpha() == 0.5


def test_recognised_knob_values_stay_silent(monkeypatch, capsys):
    monkeypatch.setattr(gestalt_rank, "_warned", set())
    monkeypatch.setenv("GESTALT_FUSION", "Convex")
    monkeypatch.setenv("GESTALT_RERANK", "OFF")
    monkeypatch.setenv("GESTALT_FTS_STOPWORDS", "off")
    monkeypatch.setenv("GESTALT_FUSION_ALPHA", "7")
    assert (gestalt_rank.fusion_mode(), gestalt_rank.rerank_mode(), gestalt_rank.stopwords_on(), gestalt_rank.fusion_alpha()) == ("convex", "off", False, 1.0)
    assert capsys.readouterr().err == ""


# --- server ------------------------------------------------------------------------------------------------------------


def test_the_server_shares_the_hub_rule_and_has_no_dead_alias(mcp_server):
    assert mcp_server._is_hub is gestalt_rank.is_hub
    assert not hasattr(gestalt_rank, "QWEN_INSTRUCTION")


def test_unloading_waits_for_the_model_lock(mcp_server, monkeypatch):
    """A load in progress holds the lock. An unload must not null the model under it."""
    import threading, time
    monkeypatch.setattr(mcp_server, "_model", object())
    done = threading.Event()
    with mcp_server._model_lock:
        t = threading.Thread(target=lambda: (mcp_server.unload_model(force=True), done.set()), daemon=True)
        t.start()
        assert not done.wait(0.3), "unload_model ran while the lock was held"
        assert mcp_server._model is not None
    assert done.wait(5)
    assert mcp_server._model is None


def test_get_model_returns_the_loaded_model_even_if_unload_follows(mcp_server, monkeypatch):
    sentinel = object()
    monkeypatch.setattr(mcp_server, "_model", sentinel)
    got = mcp_server.get_model()
    mcp_server.unload_model(force=True)
    assert got is sentinel
