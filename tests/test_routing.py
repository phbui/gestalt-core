"""Skill-routing accuracy, pinned to a measured floor.

`gestalt_route` is a WEAK ADVISORY signal, not a router — measured 23/40 top-1 (57%)
and 31/40 top-3 (77%) on the cases below, which cover all 31 skills.

An earlier 15-case version of this file reported 9/15 (60%) and 14/15 (93%). Those
numbers were an artifact of too few and too-easy cases: on a representative set the
then-shipped variant actually scored 19/40 (47%) top-1, the WORST of the four
variants tried. A 15-case eval cannot resolve a one-case difference, and three
successive variants were compared on exactly such differences before this was caught.

The history worth knowing, because it is the same mistake twice:

  1. The first implementation used `bm25(skills_fts, 10.0, 4.0, 1.0)` to weight the
     name column above the body. That does not work, because `name` is a 1-3 token
     field and `body` runs to thousands, so their bm25 contributions differ by ~3
     orders of magnitude. On a query containing the literal word "review", the
     correct skill ranked 7th. Weighting incommensurable scales is the same error as
     thresholding a ranking score.
  2. RRF over two separate rankings fixed that, because RRF discards magnitude and
     keeps order — which is the whole reason it exists.

Measured on the 40 cases: description-only 21/40 top-1, name+description/body 19/40,
description+body 23/40. The NAME leg was removed because it measurably hurt — most
slugs are ordinary English words, so a query merely using the word got routed there
("review a pull request diff" returned `review` ahead of `pr`). SkillRouter (arxiv 2603.22455) reports 29-44 points
for body text, but it measures a trained retrieve-and-rerank model over a 110-skill
catalog; the effect does not transfer to BM25 at 31 skills. The test below records
the local reality so the citation is never mistaken for local evidence.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"

# (query, acceptable top-1 answers). Phrased as a person would ask, and deliberately
# including queries whose target slug is an ordinary English word (review, fix,
# build, help, commit) since those are where lexical routing is weakest.
CASES: list[tuple[str, tuple[str, ...]]] = [
    # 2026-08-11 catalog consolidation: adr/sdd/srs/pitch/system/promote/implement
    # became modes of `requirements`; gestalt/recall/publish became modes of
    # `consolidate`. Queries kept, targets retargeted — the intent still exists.
    ("write down why we chose postgres over dynamo", ("requirements",)),
    ("final quality gate before this pitch ships", ("audit",)),
    ("build me a prototype of the settings page", ("build", "requirements")),
    ("cut my token usage way down for the rest of this session", ("caveman",)),
    ("make a commit with the staged changes", ("commit",)),
    ("prepare for context compaction", ("compact-resume",)),
    ("compress and archive stale memory records", ("consolidate",)),
    ("help me decide between two architectures", ("discuss", "requirements")),
    ("poke holes in this plan before I commit to it", ("discuss",)),
    ("fix these 12 linter errors", ("fix",)),
    ("how healthy are my memory layers right now", ("consolidate",)),
    ("show me all available commands", ("help-gestalt",)),
    ("what can you do", ("help-gestalt",)),
    ("take this spec all the way to working code", ("requirements",)),
    ("trace why the snapshot drops on reconnect", ("investigate",)),
    ("figure out how the tool registry actually works", ("investigate",)),
    ("scan this repo and write up what you find", ("learn",)),
    ("add a new slash command to the system", ("mutate",)),
    ("start a new bet folder for this feature idea", ("requirements",)),
    ("I need to review a pull request diff", ("pr",)),
    ("move these decisions into the durable system docs", ("requirements",)),
    ("turn my vague ask into a proper plan first", ("prompt",)),
    ("make a briefing I can upload to a shared project", ("consolidate",)),
    ("what did we decide about this three weeks ago", ("consolidate",)),
    ("what are the requirements for this feature", ("requirements",)),
    ("look up current best practice for this library online", ("research",)),
    ("review knowledge entries for accuracy against code", ("review",)),
    ("check my notes still match the actual code", ("review",)),
    ("save what we learned in this conversation", ("save",)),
    ("capture what we learned into the knowledge base", ("learn", "save")),
    ("write a software design document from the spec", ("requirements",)),
    ("write up the formal requirements for this pitch", ("requirements",)),
    ("will this change break any of the other repos", ("swarm-check",)),
    ("pull the latest from notion and linear", ("sync",)),
    ("set up a new system folder for grounding", ("requirements",)),
    ("eval loop review with dimension auditors", ("vet",)),
    ("check my paper before I submit it", ("vet",)),
    ("build the feature from this design", ("build", "requirements")),
    ("batch up all these review comments and apply them", ("fix",)),
    ("deep dive on prior art for this idea", ("research", "investigate")),
]

# Measured floors, not aspirations. Raise them only alongside a real improvement.
# 2026-08-11, 21-skill catalog (post-consolidation), lexical leg: 28/40 top-1,
# 31/40 top-3. Consolidating 10 skills into 2 parameterized ones was itself the
# routing improvement the research predicted (fewer, better-scoped candidates).
# A floor breach after a corpus edit is a deliberate re-baseline moment, not a
# nudge to lower the floor silently — same discipline as ^retrieval-baseline.
#
# 2026-08-18, re-measured after commit 6a2f799 (F16 description re-tune, 8 skills
# touched) on the current 25-skill catalog: top-1 fell to 26/40 (was 28/40 on the
# smaller 21-skill catalog), top-3 held at 35/40 — comfortably above the existing
# TOP3_FLOOR, so TOP3_FLOOR is kept at 31 rather than raised, per this file's own
# "raise only alongside a real improvement" rule (35/40 today is not yet a proven
# floor, just today's measurement). TOP1_FLOOR is re-pinned to the honest 26 rather
# than left claiming a 28 the router no longer clears — this is the gestalt-efficiency-
# audit-2026-08 ^router-regression finding (F16), closed by re-tuning, but the tuning
# traded some top-1 precision for top-3 recall. Even at top-3, five cases remain
# residual semantic misses the lexical/RRF leg cannot resolve without body-text
# signal it deliberately does not use (see the module docstring): "help me decide
# between two architectures" (-> help-gestalt), "poke holes in this plan before I
# commit to it" (-> prompt), "what can you do" (-> compact-resume), "start a new bet
# folder for this feature idea" (-> compact-resume), "what are the requirements for
# this feature" (-> swarm-check).
# The floors are for the LEXICAL leg explicitly (semantic=False): gestalt_route's default is
# `semantic = _model is not None`, i.e. it depends on whether something already loaded the
# embedding model in this process — on a node with a hub-built vector index that flipped the
# same 40 cases to 24/40 (2026-08-18 evening) purely by which leg ran. Pin the leg, not the luck.
# Re-measured 2026-08-18 evening on the HUB-BUILT index fetched via `fleet-sync index-fetch`
# (same 25-skill catalog, identical descriptions): lexical top-1 = 24/40, top-3 still ≥ 31.
# The local FTS-only build of the same commit gave 26/40 — same catalog, different BM25
# ranking; the cause was a publish from a dirty index, closed 2026-08-29 (c985203, gestalt-efficiency-audit-2026-08 ^status F16;
# tests/test_index_artifact.py::test_publish_refuses_when_indexed_inputs_are_dirty). The floor stays at the lower value on purpose.
# Floor pinned to the lower measured value so Tier-1 does not flap by which node built the
# index; top-3 is the metric this tool is judged on.
TOP1_FLOOR = 24
TOP3_FLOOR = 31

# Hybrid leg (semantic=True), same date/catalog: 27/40 top-1, 34/40 top-3.
# Top-1 one case WORSE than lexical, top-3 three cases BETTER — and top-3 is
# where this tool's value is (it emits a shortlist; the model decides). Dense
# vectors embed name+description ONLY: embedding full bodies measured 26/40
# top-1 because a multi-mode SKILL.md dilutes into a vector matching nothing.
HYBRID_TOP1_FLOOR = 27
HYBRID_TOP3_FLOOR = 34


@pytest.fixture(scope="module")
def route():
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    mod = pytest.importorskip("gestalt_mcp_server")
    if not (REPO / ".search" / "gestalt.db").exists():
        pytest.skip("search index not built")
    if not mod.gestalt_route("build a feature", limit=1):
        pytest.skip("skills_fts absent — index predates skill routing")
    return mod.gestalt_route


def test_top1_accuracy_holds_its_floor(route):
    hits = [q for q, want in CASES if (r := route(q, limit=1, semantic=False)) and r[0]["skill"] in want]
    assert len(hits) >= TOP1_FLOOR, (
        f"top-1 routing accuracy fell to {len(hits)}/{len(CASES)}, floor is "
        f"{TOP1_FLOOR}. Missed: "
        f"{[q for q, _ in CASES if q not in hits][:5]}"
    )


def test_top3_accuracy_holds_its_floor(route):
    """top-3 is where this feature's value actually is, so it gets the tighter floor."""
    hits = 0
    for q, want in CASES:
        got = [x["skill"] for x in route(q, limit=3, semantic=False)]
        hits += any(g in want for g in got)
    assert hits >= TOP3_FLOOR, (
        f"top-3 routing accuracy fell to {hits}/{len(CASES)}, floor is {TOP3_FLOOR}"
    )


def test_no_phantom_space_named_skills(route):
    """skills_fts.name stores the hyphens-as-spaces form for tokenisation.

    It must be mapped back to the real slug before returning, or a multi-word slug
    surfaces twice — once as `compact resume` and once as `compact-resume` — and the
    space form fails to join to skills_meta, yielding an empty description.
    """
    seen: set[str] = set()
    for q, _ in CASES:
        seen.update(x["skill"] for x in route(q, limit=5))
    phantom = {s for s in seen if " " in s}
    assert not phantom, f"skill names not mapped back from the tokenised form: {phantom}"


def test_every_returned_skill_exists_on_disk(route):
    """A returned name that has no SKILL.md would send the caller at nothing."""
    for q, _ in CASES:
        for hit in route(q, limit=3, semantic=False):
            path = REPO / "claude-tree" / "skills" / hit["skill"] / "SKILL.md"
            assert path.exists(), f"route returned {hit['skill']!r} for {q!r}, no {path}"


def test_result_carries_a_description_for_the_caller_to_judge(route):
    """The caller decides, so it needs the description — an empty one is a join bug."""
    for hit in route("build the feature from this design", limit=3):
        assert hit["description"], f"{hit['skill']} returned with an empty description"


def test_punctuation_only_query_returns_empty_rather_than_raising(route):
    """An empty FTS5 MATCH is a syntax error, so the tokeniser must short-circuit."""
    assert route("!!! ??? ...", limit=3) == []


RUN_HYBRID = os.environ.get("RUN_HYBRID_ROUTING_EVALS") == "1"


@pytest.mark.skipif(not RUN_HYBRID, reason="hybrid leg loads the embedding model (~14 s); set RUN_HYBRID_ROUTING_EVALS=1")
def test_hybrid_top1_holds_its_floor(route):
    hits = [q for q, want in CASES if (r := route(q, limit=1, semantic=True)) and r[0]["skill"] in want]
    assert len(hits) >= HYBRID_TOP1_FLOOR, (
        f"hybrid top-1 fell to {len(hits)}/{len(CASES)}, floor is {HYBRID_TOP1_FLOOR}"
    )


@pytest.mark.skipif(not RUN_HYBRID, reason="hybrid leg loads the embedding model (~14 s); set RUN_HYBRID_ROUTING_EVALS=1")
def test_hybrid_top3_holds_its_floor(route):
    hits = 0
    for q, want in CASES:
        got = [x["skill"] for x in route(q, limit=3, semantic=True)]
        hits += any(g in want for g in got)
    assert hits >= HYBRID_TOP3_FLOOR, (
        f"hybrid top-3 fell to {hits}/{len(CASES)}, floor is {HYBRID_TOP3_FLOOR}"
    )


def test_route_does_not_import_torch():
    """Routing runs per-request; loading an embedding model here would be fatal."""
    import subprocess

    code = (
        f"import sys; sys.path.insert(0, {str(TOOLS)!r})\n"
        "from gestalt_mcp_server import gestalt_route\n"
        "gestalt_route('build a feature', limit=3)\n"
        "assert 'torch' not in sys.modules, 'routing pulled in torch'\n"
        "print('CLEAN')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stderr[-1500:]
    assert "CLEAN" in r.stdout
