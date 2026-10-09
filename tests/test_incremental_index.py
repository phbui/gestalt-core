"""Incremental embedding: only re-embed chunks whose embedded text changed.

Embedding dominates index build cost — measured, import + encode are ~98% of the
~10.5 min full rebuild while all the SQLite writes together are under 10 ms. Since a
typical edit touches one entry, re-encoding all 571 chunks was recomputing vectors
that could not have changed. Rebuild time is also the binding scaling constraint:
projected linearly it crosses 30 minutes somewhere between ~55 and ~180 entries.

These tests assert the cache actually skips work, that a change invalidates exactly
what it should, and that reused vectors are indistinguishable from fresh ones. They
run against a fixture with a stubbed encoder, so they need no torch and no GPU and
finish in milliseconds.
"""

from __future__ import annotations

import sqlite3
import zlib

import pytest


@pytest.fixture
def encoder_calls():
    """Records how many texts each encode() call was given."""
    return []


@pytest.fixture
def stub_model(encoder_calls):
    """Deterministic fake encoder: identical text always yields identical bytes.

    Determinism is the point — it lets a test assert that a reused vector equals a
    freshly computed one, which is what makes the cache safe rather than merely fast.
    """
    np = pytest.importorskip("numpy")

    class StubModel:
        max_seq_length = 512

        def encode(self, texts, batch_size=None, show_progress_bar=False):
            # index_skills() calls encode() with a single string (like real
            # SentenceTransformer.encode, which returns a 1D vector for a bare
            # string and a 2D stack for a list) — mirror that here rather than
            # only supporting the batch call the section-embedding path uses.
            single = isinstance(texts, str)
            items = [texts] if single else texts
            encoder_calls.append(len(items))
            vecs = np.stack(
                [
                    np.full(768, (zlib.crc32(t.encode()) % 10_000) / 10_000.0, dtype=np.float32)
                    for t in items
                ]
            )
            return vecs[0] if single else vecs

    return StubModel()


@pytest.fixture
def build(tmp_path, monkeypatch, indexer, stub_model, encoder_calls):
    """Point the builder at a fixture corpus and run its locked build body."""
    sqlite_vec = pytest.importorskip("sqlite_vec")

    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    # F6 (^rules-unindexed): the builder now also walks RULES_DIR. Point it at an
    # empty tmp dir (mirroring the real ".claude/rules" relative path, so
    # file_path comes out the same shape as production) rather than leaving the
    # real .claude/rules/*.md of THIS repo in scope — otherwise every
    # exact-embed-count assertion below (e.g. `== 2`) would silently start
    # counting this repo's 19 real rule files too.
    rdir = tmp_path / ".claude" / "rules"
    rdir.mkdir(parents=True)
    sdir = tmp_path / ".search"

    monkeypatch.setattr(indexer, "GESTALT_DIR", tmp_path, raising=False)
    monkeypatch.setattr(indexer, "KNOWLEDGE_DIR", kdir, raising=False)
    monkeypatch.setattr(indexer, "RULES_DIR", rdir, raising=False)
    monkeypatch.setattr(indexer, "SEARCH_DIR", sdir, raising=False)
    monkeypatch.setattr(indexer, "DB_PATH", sdir / "gestalt.db", raising=False)
    monkeypatch.setattr(indexer, "BUILD_PATH", sdir / "gestalt.db.build", raising=False)

    # Bound to differently-named locals: a class body does not close over the
    # enclosing function scope, so `kdir = kdir` inside it raises NameError.
    _kdir, _rdir, _db, _vec = kdir, rdir, sdir / "gestalt.db", sqlite_vec

    class Harness:
        """Callable build harness. A class rather than function attributes so the
        fixture's shape is statically checkable."""

        kdir = _kdir
        rdir = _rdir
        db_path = _db
        vec = _vec

        def __call__(self) -> int:
            encoder_calls.clear()
            indexer._build_index_locked(sqlite_vec, lambda *a, **k: stub_model)
            return sum(encoder_calls)

    return Harness()


def _entry(*sections: str) -> str:
    head = "---\ntype: note\ntitle: Fixture\n---\n"
    return head + "".join(f"\n## {h}\n\n{b}\n" for h, b in (s.split("|") for s in sections))


def test_cold_build_embeds_everything(build):
    (build.kdir / "alpha.md").write_text(_entry("One|First body.", "Two|Second body."))
    assert build() == 2


def test_unchanged_corpus_embeds_nothing(build):
    (build.kdir / "alpha.md").write_text(_entry("One|First body.", "Two|Second body."))
    build()
    assert build() == 0, "an unchanged corpus must reuse every cached vector"


def test_single_edited_section_embeds_only_itself(build):
    (build.kdir / "alpha.md").write_text(_entry("One|First body.", "Two|Second body."))
    build()
    (build.kdir / "alpha.md").write_text(_entry("One|First body.", "Two|Second body EDITED."))
    assert build() == 1, "editing one section must not re-embed its neighbours"


def test_new_entry_embeds_only_its_own_sections(build):
    (build.kdir / "alpha.md").write_text(_entry("One|First body."))
    build()
    (build.kdir / "beta.md").write_text(_entry("Solo|Beta body."))
    assert build() == 1


def test_changing_the_model_invalidates_the_whole_cache(build, monkeypatch, indexer):
    """The hash covers MODEL_NAME, so vectors from two models can never mix.

    Without this, switching embedding models would silently leave the index holding
    vectors from two different spaces — a corruption with no error message.
    """
    (build.kdir / "alpha.md").write_text(_entry("One|First body.", "Two|Second body."))
    build()
    monkeypatch.setattr(indexer, "MODEL_NAME", "someone-else/model-v9", raising=False)
    assert build() == 2


def test_reused_vector_is_byte_identical_to_a_fresh_one(build, indexer, monkeypatch):
    """A cached vector must equal what a full re-encode would have produced."""
    (build.kdir / "alpha.md").write_text(_entry("One|First body.", "Two|Second body."))
    build()

    def read_vectors():
        db = sqlite3.connect(f"file:{build.db_path}?mode=ro", uri=True)
        db.enable_load_extension(True)
        build.vec.load(db)
        try:
            return {
                (r[0], r[1]): r[2]
                for r in db.execute(
                    "SELECT m.heading, m.content_hash, v.embedding FROM sections_meta m "
                    "JOIN sections_vec v ON v.id = m.id"
                )
            }
        finally:
            db.close()

    from_cache = read_vectors()
    # Force a full re-encode by invalidating the cache, then compare.
    monkeypatch.setattr(indexer, "load_embedding_cache", lambda _sv: {}, raising=False)
    assert build() == 2
    fresh = read_vectors()
    assert from_cache == fresh, "a reused vector differs from a freshly encoded one"


def test_every_row_gets_a_content_hash(build):
    """A row without a hash can never be cached, so it would silently re-embed forever."""
    (build.kdir / "alpha.md").write_text(_entry("One|First.", "Two|Second.", "Three|Third."))
    build()
    db = sqlite3.connect(f"file:{build.db_path}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT content_hash FROM sections_meta").fetchall()
    finally:
        db.close()
    assert len(rows) == 3
    assert all(r[0] for r in rows), "a section was written without a content_hash"
    assert len({r[0] for r in rows}) == 3, "distinct section text must hash distinctly"


def test_rules_dir_is_indexed_alongside_knowledge(build):
    """F6 (^rules-unindexed): .claude/rules/*.md must be searchable too — a rule
    like the Supabase schema-first migration rule used to have zero chunks in the
    index, so a search for it only ever surfaced knowledge/gestalt.md's
    self-description instead of the rule itself."""
    (build.kdir / "alpha.md").write_text(_entry("One|Knowledge body."))
    (build.rdir / "example-rule.md").write_text(
        "# Example Rule\n\nNever do the unsafe thing. ^unsafe-thing\n"
    )
    build()
    db = sqlite3.connect(f"file:{build.db_path}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "SELECT slug, block_id, file_path FROM sections_meta WHERE file_path LIKE '.claude/rules/%'"
        ).fetchall()
    finally:
        db.close()
    assert len(rows) > 0, "no chunks came from .claude/rules — rules are still unindexed"
    assert any(r[0] == "example-rule" for r in rows)
    # file_path is how a reader (CLI/MCP) tells a rule chunk from a knowledge chunk.
    assert all(r[2].startswith(".claude/rules/") for r in rows)


def test_skills_are_indexed_for_routing(build):
    """The routing corpus must be populated, or gestalt_route silently returns nothing.

    Routing on skill bodies rather than descriptions is worth 29-44 points of accuracy
    (SkillRouter, arxiv 2603.22455); an empty skills_fts makes /prompt fall back to
    reading the index file, which is the degraded path.
    """
    (build.kdir / "alpha.md").write_text(_entry("One|First."))
    build()
    db = sqlite3.connect(f"file:{build.db_path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "skills_fts" in tables and "skills_meta" in tables
        # The fixture GESTALT_DIR has no .claude/skills, so 0 rows is correct here —
        # what matters is that the schema exists and the query path works.
        n = db.execute("SELECT COUNT(*) FROM skills_meta").fetchone()[0]
        assert n == 0
    finally:
        db.close()


def test_chunks_record_every_anchor_they_contain(build):
    """A chunk must expose all its ^anchors, not just the one that named it.

    Entries mark sub-facts with a trailing inline `^anchor` mid-section, while
    block_id is derived only from headings and standalone anchor lines. Without an
    `anchors` column, 12 of 48 anchored eval targets had no chunk whose block_id could
    match, and correct retrievals were scored as block misses — block precision read
    64.6% when the unchanged system was actually achieving 83.3%.
    """
    (build.kdir / "alpha.md").write_text(
        "---\ntype: note\ntitle: Alpha\n---\n\n"
        "## Section One ^sec-one\n\n"
        "First fact here. ^inline-fact\n\n"
        "Second fact, different anchor. ^another-fact\n"
    )
    build()
    import sqlite3

    db = sqlite3.connect(f"file:{build.db_path}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT block_id, anchors FROM sections_meta").fetchall()
    finally:
        db.close()
    all_anchors = {a for _, anchors in rows for a in (anchors or "").split(",") if a}
    assert "inline-fact" in all_anchors, "an inline mid-section anchor was not recorded"
    assert "another-fact" in all_anchors


def test_anchors_exclude_wikilink_targets_and_code_spans(build):
    """An anchor cited via [[other#^theirs]] belongs to OTHER, not to this chunk.

    The same misattribution put five false block-ownership claims into MANIFEST.md, so
    the indexer strips wikilinks and code spans before extracting, exactly as
    tools/gestalt's extract_block_ids() does.
    """
    (build.kdir / "beta.md").write_text(
        "---\ntype: note\ntitle: Beta\n---\n\n"
        "## Only ^mine\n\n"
        "See [[other-entry#^not-mine]] and the pattern `^also-not-mine` here. ^really-mine\n"
    )
    build()
    import sqlite3

    db = sqlite3.connect(f"file:{build.db_path}?mode=ro", uri=True)
    try:
        anchors = {
            a
            for (row,) in db.execute("SELECT anchors FROM sections_meta")
            for a in (row or "").split(",")
            if a
        }
    finally:
        db.close()
    assert "really-mine" in anchors
    assert "not-mine" not in anchors, "a wikilink target was claimed as a local anchor"
    assert "also-not-mine" not in anchors, "a code-span token was treated as an anchor"


def test_skills_meta_one_row_per_dir_identical_regardless_of_build_mode(build, indexer):
    """gestalt-efficiency-audit-2026-08 F16: a hub-built (vector) index and a
    locally-built FTS-only index measured different lexical routing top-1
    accuracy (24/40 vs 26/40) on what was assumed to be the identical 25-skill
    catalog at the same commit sha.

    Root cause (verified 2026-08-18 by scp-fetching the actual hub-published
    `.search/gestalt.db` for that exact sha and diffing it row-for-row against
    a fresh local rebuild): NOT builder nondeterminism.
    `index_skills()`/`_build_index_locked()` are a single deterministic
    `sorted(skills_dir.glob("*/SKILL.md"))` loop, byte-identical whether a
    dense model is passed (full build) or not (`--fts-only`) — reproduced
    directly below. The two real indices disagreed because the LOCAL build
    read `.claude/skills/save/SKILL.md` off disk *after* a concurrent session
    had already edited it in place but *before* that edit was committed, while
    `.search/gestalt.db.stamp` records only `git rev-parse HEAD` — nothing
    checks the working tree was clean relative to that sha before stamping.
    The hub-published copy, gated by `cmd_index_publish` refusing to publish
    unless `stamp sha == git HEAD`, was a faithful snapshot of the committed
    tree; the local one silently was not, despite claiming the same sha. BM25
    is corpus-relative, so one skill's changed body shifted ranking for
    queries unrelated to that skill too — exactly the failure shape observed
    (multiple of the 40 cases flipped, not just save-routing ones). That
    working-tree/stamp gap lives in the fleet-sync publish/fetch layer, not in
    the skills step, and is out of this test's scope to fix.

    What IS this test's job: guard the one thing actually worth hardening in
    the skills step itself — it must walk `.claude/skills` only (a `.cursor/skills`
    mirror with the same directory names must never contribute rows or leak
    its body text; per dual-agent-sync.md's Provider-Specific Optimization,
    Cursor skill bodies legitimately diverge from Claude's and must stay out
    of the routing corpus), emit exactly one `skills_meta` row per skill
    directory, and be indifferent to fts-only vs full build mode.
    """
    claude_skills = build.kdir.parent / ".claude" / "skills"
    cursor_skills = build.kdir.parent / ".cursor" / "skills"
    fixtures = {
        "alpha-skill": ("Claude body for alpha, distinct wording.", "Cursor body for alpha — Task() pattern, DIFFERENT wording."),
        "beta-skill": ("Claude body for beta, distinct wording.", "Cursor body for beta — readonly=true, DIFFERENT wording."),
    }
    for name, (claude_body, cursor_body) in fixtures.items():
        (claude_skills / name).mkdir(parents=True)
        (claude_skills / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Fixture skill {name} (claude).\n---\n\n{claude_body}\n"
        )
        (cursor_skills / name).mkdir(parents=True)
        (cursor_skills / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Fixture skill {name} (cursor).\n---\n\n{cursor_body}\n"
        )
    (build.kdir / "alpha.md").write_text(_entry("One|First."))

    def snapshot() -> tuple[list[tuple], dict[str, str]]:
        db = sqlite3.connect(f"file:{build.db_path}?mode=ro", uri=True)
        try:
            meta = db.execute(
                "SELECT name, description, lines FROM skills_meta ORDER BY name"
            ).fetchall()
            bodies = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT s.name, f.body FROM skills_fts f JOIN skills_meta s ON s.id = f.rowid"
                )
            }
        finally:
            db.close()
        return meta, bodies

    build()  # full build: harness passes a stub embedding model
    full_meta, full_bodies = snapshot()

    # Same fixture tree, no dense model at all — the actual `--fts-only` path.
    indexer._build_index_locked(None, None)
    fts_meta, fts_bodies = snapshot()

    names = [row[0] for row in full_meta]
    assert names == sorted(fixtures), (
        f"expected exactly one skills_meta row per .claude/skills dir ({sorted(fixtures)}), "
        f"got {names} — a .cursor/skills mirror with matching dir names must not duplicate rows"
    )
    for name, (claude_body, cursor_body) in fixtures.items():
        assert claude_body in full_bodies[name], f"{name}: indexed body is missing its .claude/skills content"
        assert cursor_body not in full_bodies[name], f"{name}: .cursor/skills body leaked into the routing corpus"

    assert full_meta == fts_meta, "skills_meta diverged between a full build and an --fts-only build of the same tree"
    assert full_bodies == fts_bodies, "skills_fts bodies diverged between a full build and an --fts-only build of the same tree"
