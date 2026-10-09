"""assign_splits: clusters, the deterministic dev/test tag, the bucket floor and the in-place write (EVAL-007). Hermetic."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import _load  # noqa: E402


@pytest.fixture(scope="module")
def sp():
    return _load("assign_splits_t", "evals/retrieval/assign_splits.py")


def test_components_join_cases_that_share_an_accepted_slug(sp):
    comps = sp.components([{"a"}, {"b", "c"}, {"c", "d"}, {"e"}, {"a"}])
    assert comps == [frozenset("a"), frozenset("bcd"), frozenset("e")]


def test_case_clusters_follow_expect_any_and_families(sp):
    cases = [
        {"query": "1", "expect_slug": "a"},
        {"query": "2", "expect_slug": "b", "expect_any": ["a"]},
        {"query": "3", "expect_slug": "c"},
        {"query": "4", "expect_slug": "d"},
        {"query": "5", "expect_none": True, "bucket": "abstain"},
    ]
    cl = sp.case_clusters(cases, [["c", "c-log"], ["d", "c-log"]])
    ids = [sp.runner.case_id(c) for c in cases]
    assert cl[ids[0]] == cl[ids[1]] == "a", "expect_any links b to a"
    assert cl[ids[2]] == cl[ids[3]] == "c", "two families that share c-log are one cluster"
    assert cl[ids[4]] == "abstain:" + ids[4]


def _many(n_slugs: int = 60):
    cases = []
    for i in range(n_slugs):
        bucket = ("ops", "paper", "voice")[i % 3]
        cases += [{"query": f"q{i}-{j}", "expect_slug": f"s{i}", "bucket": bucket} for j in range(1 + i % 3)]
    cases += [{"query": f"none {i}", "expect_none": True, "bucket": "abstain"} for i in range(12)]
    return cases


def test_assignment_is_deterministic_never_straddles_and_meets_the_bucket_floor(sp):
    cases = _many()
    a, _ = sp.assign(cases, [])
    b, _ = sp.assign(list(reversed(cases)), [])
    assert a == b, "the tag depends on the cluster, not on the case order"
    rep = sp.verify(cases, [], a)
    assert rep["straddling"] == [] and rep["under_floor"] == [] and rep["untagged"] == 0
    assert all(b["test_share"] >= sp.MIN_TEST_SHARE for b in rep["buckets"].values())
    for i in range(60):  # every case of one slug lands on one side
        assert len({a[sp.runner.case_id(c)] for c in cases if c.get("expect_slug") == f"s{i}"}) == 1


def test_the_hash_rule_is_sha1_of_sorted_members_mod_10(sp):
    import hashlib
    h = int(hashlib.sha1("a\nb".encode()).hexdigest(), 16)
    assert sp.cluster_hash(["b", "a"]) == h and sp.is_dev(["b", "a"]) == (h % 10 < 7)


def test_a_starved_bucket_gets_clusters_moved_to_test(sp):
    """Find a bucket the plain hash leaves entirely in dev, and check the adjustment moves the nearest clusters."""
    dev_slugs = [f"t{i}" for i in range(400) if sp.is_dev([f"t{i}"])][:10]
    cases = [{"query": f"q {s}", "expect_slug": s, "bucket": "small"} for s in dev_slugs]
    split, notes = sp.assign(cases, [])
    n_test = sum(v == "test" for v in split.values())
    assert n_test == 2, "10 cases at a 15 percent floor need 2 in test"
    order = sorted(dev_slugs, key=lambda s: (sp.cluster_hash([s]) % 10, sp.cluster_hash([s])), reverse=True)
    moved = {c["expect_slug"] for c in cases if split[sp.runner.case_id(c)] == "test"}
    assert moved == set(order[:2]), "the clusters nearest the test side of the cut move first"
    assert len(notes) == 2 and all("moved cluster" in n for n in notes)


def test_existing_tags_are_kept_and_new_cases_join_their_cluster(sp):
    cases = _many(20)
    first, _ = sp.assign(cases, [])
    tagged = [{**c, "split": first[sp.runner.case_id(c)]} for c in cases]
    flipped = [{**c, "split": "test" if c["split"] == "dev" else "dev"} for c in tagged]
    again, _ = sp.assign(flipped, [])
    assert all(again[sp.runner.case_id(c)] == c["split"] for c in flipped), "existing tags are never reassigned"
    newcomer = {"query": "new", "expect_slug": "s3", "bucket": "ops"}
    out, _ = sp.assign(flipped + [newcomer], [])
    side = {c["split"] for c in flipped if c.get("expect_slug") == "s3"}
    assert {out[sp.runner.case_id(newcomer)]} == side
    redone, _ = sp.assign(flipped, [], reassign=True)
    assert redone == first, "--reassign recomputes from the hash"


def test_straddling_tags_are_refused(sp):
    cases = [{"query": "a", "expect_slug": "s", "split": "dev"}, {"query": "b", "expect_slug": "s", "split": "test"}]
    with pytest.raises(ValueError, match="straddles"):
        sp.assign(cases, [])


GOLDEN_TEXT = """# header comment stays
families:
  - [s1, s1-log]
cases:
  # a section comment stays
  - query: "first question"
    expect_slug: s1
    bucket: ops
    expect_block: b
  - query: "second question"
    expect_slug: s1-log
    bucket: ops

  - query: "nothing answers this"
    expect_none: true
    bucket: abstain
"""


def test_write_tags_keeps_comments_and_is_idempotent(sp, tmp_path):
    g = tmp_path / "golden.yaml"
    g.write_text(GOLDEN_TEXT)
    assert sp.main(["--golden", str(g)]) in (0, 1)  # three cases cannot meet every floor, the write still happens
    text = g.read_text()
    assert "# header comment stays" in text and "# a section comment stays" in text
    data = yaml.safe_load(text)
    assert all(c["split"] in ("dev", "test") for c in data["cases"])
    assert data["cases"][0]["split"] == data["cases"][1]["split"], "one family, one side"
    assert text.replace("    split: dev\n", "").replace("    split: test\n", "") == GOLDEN_TEXT
    sp.main(["--golden", str(g)])
    assert g.read_text() == text, "a second run changes nothing"


def test_check_mode_writes_nothing_and_fails_on_untagged(sp, tmp_path, capsys):
    g = tmp_path / "golden.yaml"
    g.write_text(GOLDEN_TEXT)
    assert sp.main(["--golden", str(g), "--check"]) == 1
    assert g.read_text() == GOLDEN_TEXT and "untagged cases: 3" in capsys.readouterr().out
