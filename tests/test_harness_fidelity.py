"""The retrieval eval must measure the path that is actually served.

The eval runner deliberately mirrors `gestalt-mcp-server.py` rather than
importing it (the server module pulls in the MCP framework at import time).
A mirror silently drifts, and a drifted mirror makes every reported metric a
measurement of code nobody runs. These tests fail loudly on drift.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from conftest import _load

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "tools" / "gestalt-mcp-server.py"
RUNNER = REPO / "evals" / "retrieval" / "run_retrieval_evals.py"
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
    }


def test_rrf_constant_matches(sources):
    server_k = re.search(r"K\s*=\s*(\d+)", sources["server"])
    runner_k = re.search(r"K_RRF\s*=\s*(\d+)", sources["runner"])
    assert server_k and runner_k
    assert server_k.group(1) == runner_k.group(1), "RRF constant drifted"


def test_both_use_reciprocal_rank_fusion(sources):
    for name in ("server", "runner"):
        assert "1.0 / (" in sources[name], f"{name} no longer uses RRF-style scoring"


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
    """Both must over-fetch by the same factor before fusion."""
    assert sources["server"].count("limit * 2") >= 2
    assert sources["runner"].count("limit * 2") >= 2


def test_fts_tokenisation_matches(sources):
    """Per-token quoting, OR-joined. Quoting the whole query makes it a phrase
    query and silently kills the BM25 leg — a bug this corpus already hit."""
    for name in ("server", "runner"):
        assert re.search(r'OR.*join', sources[name]), f"{name} FTS join changed"
        assert 'split(r"\\W+", query)' in sources[name], f"{name} tokenisation changed"


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
    seed = abs(hash(text)) % (2**32 - 1)
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
    reuse the real `fts_tokens()` for tokenisation, since that IS the
    production tokeniser and re-deriving it here would itself risk drift.
    """
    db = mcp_server.get_db()
    try:
        score = 0.0
        fts_query = mcp_server.fts_tokens(query)
        if fts_query:
            rows = db.execute(
                "SELECT rowid FROM sections_fts WHERE sections_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, limit * 2),
            ).fetchall()
            for rank, row in enumerate(rows):
                if row["rowid"] == target_id:
                    score += 1.0 / (k + rank + 1)
        query_emb = _stub_vector("search_query: " + query)
        rows = db.execute(
            "SELECT id FROM sections_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (query_emb.tobytes(), limit * 2),
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
