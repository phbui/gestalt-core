"""N11: auto-promote proposes and never edits knowledge/; auto-consolidate defaults off (2026-10-06).

Spec 7 (2026-10-08): two sinks, `state` (default) and `inbox`, both under the same gates. Every behaviour test runs on both."""

import argparse
import os
import re
from pathlib import Path

import pytest

from conftest import REPO, _load

FACT = "The deploy pipeline requires the gamma-service endpoint to stay on port 9000."
INBOX_HEAD = "# Capture Inbox\n\n## Inbox\n\n- 2026-09-26 [commit] an older line\n\n## Commitments\n"
INBOX_RE = (r"- \d{4}-\d\d-\d\d \[promote\] target \[\[gamma-service\]\] \| confidence 0.6 \| source letta_block:project_context "
            r"\| sessions 1 \| first_seen \d{4}-\d\d-\d\d \| supersedes none \| ")
STATE_RE = r"- \d{4}-\d\d-\d\d \[proposal\] target \[\[gamma-service\]\] \| confidence 0.6 \| source letta_block:project_context \| "


@pytest.fixture(params=["state", "inbox"])
def sink(request):
    return request.param


@pytest.fixture
def world(tmp_path, monkeypatch, sink):
    monkeypatch.delenv("GESTALT_PROMOTE_MIN_CONF", raising=False)
    ap = _load("auto_promote_under_test", "tools/auto-promote.py")
    gdir, state = tmp_path / "gestalt", tmp_path / "state"
    (gdir / "knowledge").mkdir(parents=True)
    state.mkdir()
    entry = gdir / "knowledge" / "gamma-service.md"
    entry.write_text("---\ntags: [gamma]\nupdated: 2026-01-01\n---\n\n# Gamma\n\nBody.\n", encoding="utf-8")
    inbox = gdir / "knowledge" / "capture-inbox.md"
    inbox.write_text(INBOX_HEAD, encoding="utf-8")
    (gdir / "MANIFEST.md").write_text("### gamma-service\nreference | Gamma service\n", encoding="utf-8")
    block_fact = {"fact": FACT, "source": "letta_block:project_context", "confidence": 0.6, "session_id": "letta"}
    monkeypatch.setattr(ap, "read_letta_facts", lambda *a, **k: [dict(block_fact)])
    monkeypatch.setattr(ap, "check_graphiti_staleness", lambda *a, **k: 0)
    monkeypatch.setattr(ap, "_search_fts", lambda *a, **k: [])  # the server is never imported in tests
    for name in ("rebuild_indices", "feed_graphiti", "condense_letta", "update_entry"):
        monkeypatch.setattr(ap, name, lambda *a, **k: pytest.fail("must not be called in proposal mode"))
    args = argparse.Namespace(gestalt_dir=str(gdir), state_dir=str(state), letta_url="x", graphiti_url="y",
                              min_sessions=2, dry_run=False, sink=sink)
    return ap, args, gdir, state, entry


def sink_path(ap, args, gdir, state):
    return (gdir / "knowledge" / "capture-inbox.md") if args.sink == "inbox" else (state / ap.PROPOSALS_FILE)


def proposal_lines(path, tag):
    if not path.exists():
        return []
    return [l for l in path.read_text().splitlines() if l.startswith("- ") and f"[{tag}]" in l]


def tag_of(sink):
    return "promote" if sink == "inbox" else "proposal"


def health(state):
    return (state / "health.log").read_text()


def test_proposes_and_leaves_knowledge_untouched(world, sink):
    ap, args, gdir, state, entry = world
    before = (entry.read_bytes(), entry.stat().st_mtime_ns)
    stats = ap.run_promotion(args, ap.Log(state / "health.log"))
    assert (entry.read_bytes(), entry.stat().st_mtime_ns) == before
    assert sorted(p.name for p in (gdir / "knowledge").iterdir()) == ["capture-inbox.md", "gamma-service.md"]
    lines = proposal_lines(sink_path(ap, args, gdir, state), tag_of(sink))
    assert len(lines) == 1 and stats["promoted"] == 1
    assert re.match(INBOX_RE if sink == "inbox" else STATE_RE, lines[0])
    assert FACT in lines[0]


def test_state_sink_leaves_the_inbox_alone_and_inbox_sink_leaves_state_alone(world, sink):
    ap, args, gdir, state, _ = world
    ap.run_promotion(args, ap.Log(state / "health.log"))
    inbox = gdir / "knowledge" / "capture-inbox.md"
    if sink == "state":
        assert inbox.read_text() == INBOX_HEAD
    else:
        assert not (state / ap.PROPOSALS_FILE).exists()


def test_second_run_does_not_duplicate(world):
    ap, args, gdir, state, _ = world
    ap.run_promotion(args, ap.Log(state / "health.log"))
    stats = ap.run_promotion(args, ap.Log(state / "health.log"))
    assert stats["promoted"] == 0
    assert sink_path(ap, args, gdir, state).read_text().count(FACT) == 1


def test_dry_run_writes_nothing(world, sink):
    ap, args, gdir, state, entry = world
    args.dry_run = True
    target = sink_path(ap, args, gdir, state)
    before = (target.read_bytes(), target.stat().st_mtime_ns) if target.exists() else None
    ap.run_promotion(args, ap.Log(state / "health.log"))
    after = (target.read_bytes(), target.stat().st_mtime_ns) if target.exists() else None
    assert after == before
    assert "updated: 2026-01-01" in entry.read_text()
    assert "[dry-run] would propose for gamma-service conf=0.6 supersedes=none" in health(state)


def test_inbox_goes_under_the_inbox_heading_newest_first(world, sink):
    if sink != "inbox":
        pytest.skip("inbox only")
    ap, args, gdir, state, _ = world
    ap.run_promotion(args, ap.Log(state / "health.log"))
    text = (gdir / "knowledge" / "capture-inbox.md").read_text()
    assert text.index("## Inbox") < text.index("[promote]") < text.index("an older line") < text.index("## Commitments")
    assert "Wrote 1 promotion proposal(s) to capture-inbox.md" in health(state)


def test_inbox_file_is_created_when_missing(world, sink):
    if sink != "inbox":
        pytest.skip("inbox only")
    ap, args, gdir, state, _ = world
    (gdir / "knowledge" / "capture-inbox.md").unlink()
    ap.run_promotion(args, ap.Log(state / "health.log"))
    text = (gdir / "knowledge" / "capture-inbox.md").read_text()
    assert text.startswith("# Capture Inbox") and "## Inbox" in text and FACT in text


def test_state_sink_logs_its_own_write_line(world, sink):
    if sink != "state":
        pytest.skip("state only")
    ap, args, _, state, _ = world
    ap.run_promotion(args, ap.Log(state / "health.log"))
    assert "Wrote 1 promotion proposal(s) to promotion-proposals.md" in health(state)


def test_low_confidence_is_skipped_on_both_sinks(world, monkeypatch):
    ap, args, gdir, state, _ = world
    monkeypatch.setenv("GESTALT_PROMOTE_MIN_CONF", "0.7")
    stats = ap.run_promotion(args, ap.Log(state / "health.log"))
    assert stats["promoted"] == 0 and stats["skipped"] == 1
    target = sink_path(ap, args, gdir, state)
    assert FACT not in (target.read_text() if target.exists() else "")


def test_default_min_conf_is_half_and_a_bad_value_falls_back(world, monkeypatch):
    ap, *_ = world
    assert ap.min_confidence() == 0.5
    monkeypatch.setenv("GESTALT_PROMOTE_MIN_CONF", "nope")
    assert ap.min_confidence() == 0.5
    monkeypatch.setenv("GESTALT_PROMOTE_MIN_CONF", "0.9")
    assert ap.min_confidence() == 0.9


def test_fact_the_entry_already_says_is_skipped_as_known(world):
    ap, args, gdir, state, entry = world
    entry.write_text(entry.read_text() + f"\n{FACT}\n", encoding="utf-8")
    stats = ap.run_promotion(args, ap.Log(state / "health.log"))
    assert stats["promoted"] == 0
    assert "known: gamma-service" in health(state)


def test_same_sentence_with_a_new_number_is_news_not_a_duplicate(world):
    ap, args, gdir, state, entry = world
    entry.write_text(entry.read_text() + "\nThe deploy pipeline requires the gamma-service endpoint to stay on port 8000.\n", encoding="utf-8")
    stats = ap.run_promotion(args, ap.Log(state / "health.log"))
    assert stats["promoted"] == 1


def test_inbox_line_names_the_section_it_supersedes(world, sink, monkeypatch):
    if sink != "inbox":
        pytest.skip("inbox only")
    ap, args, gdir, state, entry = world
    asked = []

    def fake(query, limit=5):
        asked.append((query, limit))
        return [{"slug": "other-entry", "block_id": "nope", "content": "The deploy pipeline requires the gamma-service endpoint to stay on port 8000."},
                {"slug": "gamma-service", "block_id": "ports", "content": "Intro. The deploy pipeline requires the gamma-service endpoint to stay on port 8000."}]

    monkeypatch.setattr(ap, "_search_fts", fake)
    ap.run_promotion(args, ap.Log(state / "health.log"))
    line = proposal_lines(gdir / "knowledge" / "capture-inbox.md", "promote")[0]
    assert "supersedes [[gamma-service#^ports]]" in line
    assert asked == [(FACT, 5)]


def test_unrelated_hit_does_not_become_a_supersede_link(world, sink, monkeypatch):
    if sink != "inbox":
        pytest.skip("inbox only")
    ap, args, gdir, state, _ = world
    monkeypatch.setattr(ap, "_search_fts", lambda *a, **k: [
        {"slug": "gamma-service", "block_id": "x", "content": "Gamma was named after a river in 1999."}])
    ap.run_promotion(args, ap.Log(state / "health.log"))
    assert "supersedes none" in proposal_lines(gdir / "knowledge" / "capture-inbox.md", "promote")[0]


def test_provenance_counts_sessions_and_first_seen(world):
    ap, *_ = world
    q = [{"fact": FACT, "session_id": "a", "timestamp": "2026-10-03T10:00:00"},
         {"fact": FACT.upper(), "session_id": "b", "timestamp": "2026-10-01T09:00:00"},
         {"fact": "something else", "session_id": "c", "timestamp": "2026-09-01T09:00:00"}]
    assert ap.provenance({"fact": FACT}, q) == (2, "2026-10-01")


def test_default_sink_is_state():
    src = (REPO / "tools" / "auto-promote.py").read_text()
    assert "'--sink', choices=SINKS, default='state'" in src
    assert "--dry-run" in src


def test_run_promotion_without_a_sink_attribute_uses_state(world):
    ap, args, gdir, state, _ = world
    del args.sink
    ap.run_promotion(args, ap.Log(state / "health.log"))
    assert (state / ap.PROPOSALS_FILE).exists()
    assert (gdir / "knowledge" / "capture-inbox.md").read_text() == INBOX_HEAD


def test_dry_run_flag_exists_on_cli():
    assert "--dry-run" in (REPO / "tools" / "auto-promote.py").read_text()


def test_hook_defaults_off():
    hook = (REPO / "claude-tree" / "hooks" / "gestalt-session-start.sh").read_text()
    assert 'AUTO_CONSOLIDATE="${GESTALT_AUTO_CONSOLIDATE:-false}"' in hook


# ---- F3 fixes (2026-10-08) -------------------------------------------------

def test_session_list_makes_a_queue_row_stable_and_counts_two(world):
    ap, *_ = world
    row = {"fact": FACT, "source": "transcript", "session_id": "a", "sessions": ["a", "b"], "timestamp": "2026-10-03T10:00:00"}
    assert ap.is_stable(dict(row), [row], min_sessions=2) is True
    assert ap.provenance(dict(row), [row]) == (2, "2026-10-03")


def test_old_row_without_sessions_falls_back_to_session_id(world):
    ap, *_ = world
    old = {"fact": FACT, "source": "transcript", "session_id": "a"}
    assert ap.is_stable(dict(old), [old], min_sessions=2) is False
    assert ap.is_stable(dict(old), [old, {**old, "session_id": "b"}], min_sessions=2) is True


def test_negation_flip_is_news_not_a_duplicate(tmp_path):
    ap = _load("auto_promote_under_test", "tools/auto-promote.py")
    entry = tmp_path / "e.md"
    entry.write_text("The gamma-service cache is not enabled in production.\n", encoding="utf-8")
    assert ap.is_known("The gamma-service cache is enabled in production.", entry) is False
    assert ap.is_known("The gamma-service cache is not enabled in production.", entry) is True


def test_bad_confidence_counts_as_zero_and_the_run_survives(world):
    ap, args, gdir, state, _ = world
    ap.read_letta_facts = lambda *a, **k: [{"fact": FACT, "source": "letta_block:x", "confidence": "high", "session_id": "l"}]
    stats = ap.run_promotion(args, ap.Log(state / "health.log"))
    assert stats["promoted"] == 0 and stats["skipped"] >= 1
    assert proposal_lines(sink_path(ap, args, gdir, state), tag_of(args.sink)) == []


def _spy_server_load(ap, monkeypatch):
    import importlib.util
    calls = []

    def fake(*a, **k):
        calls.append(a)
        raise RuntimeError("no server in tests")

    monkeypatch.setattr(importlib.util, "spec_from_file_location", fake)
    monkeypatch.setattr(ap, "_SERVER", None)
    return calls


def test_dry_run_never_loads_the_server_module(world, sink, monkeypatch):
    if sink != "inbox":
        pytest.skip("inbox only")
    ap, args, gdir, state, _ = world
    monkeypatch.undo()  # drop the world's _search_fts stub, keep the real one
    ap = _load("auto_promote_under_test2", "tools/auto-promote.py")
    monkeypatch.setattr(ap, "read_letta_facts", lambda *a, **k: [{"fact": FACT, "source": "letta_block:project_context", "confidence": 0.6, "session_id": "l"}])
    monkeypatch.setattr(ap, "check_graphiti_staleness", lambda *a, **k: 0)
    calls = _spy_server_load(ap, monkeypatch)
    args.dry_run = True
    ap.run_promotion(args, ap.Log(state / "health.log"))
    assert calls == []
    args.dry_run = False
    ap.run_promotion(args, ap.Log(state / "health.log"))
    assert len(calls) == 1  # control: the spy does see a real load


def test_search_fts_turns_the_hit_log_off_before_loading(monkeypatch):
    ap = _load("auto_promote_under_test3", "tools/auto-promote.py")
    monkeypatch.delenv("GESTALT_HITS_LOG", raising=False)
    seen = []
    import importlib.util
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *a, **k: seen.append(os.environ.get("GESTALT_HITS_LOG")) or (_ for _ in ()).throw(RuntimeError("stop")))
    monkeypatch.setattr(ap, "_SERVER", None)
    assert ap._search_fts("q") == []
    assert seen == ["off"]


def test_inbox_append_keeps_a_concurrent_write(tmp_path):
    ap = _load("auto_promote_under_test4", "tools/auto-promote.py")
    inbox = tmp_path / "capture-inbox.md"
    inbox.write_text(INBOX_HEAD, encoding="utf-8")
    real = Path.read_text
    state = {"done": False}

    def racing(self, *a, **k):
        out = real(self, *a, **k)
        if self == inbox and not state["done"]:
            state["done"] = True
            inbox.write_text(inbox.read_text().replace("## Inbox\n\n", "## Inbox\n\n- 2026-10-08 [capture] written by someone else\n"), encoding="utf-8")
        return out

    Path.read_text = racing
    try:
        ap.append_inbox(inbox, [ap.inbox_line("s", {"fact": "A fact."}, "none", 2, "2026-10-01")], ap.Log(tmp_path / "h.log"), [("s", "A fact.")])
    finally:
        Path.read_text = real
    text = inbox.read_text()
    assert "written by someone else" in text and "| A fact." in text


def test_short_fact_inside_a_longer_line_is_still_added_and_other_slug_too(tmp_path):
    ap = _load("auto_promote_under_test5", "tools/auto-promote.py")
    inbox = tmp_path / "capture-inbox.md"
    inbox.write_text(INBOX_HEAD, encoding="utf-8")
    log = ap.Log(tmp_path / "h.log")
    mk = lambda slug, fact: (ap.inbox_line(slug, {"fact": fact}, "none", 2, "2026-10-01"), (slug, fact))
    l1, k1 = mk("s", "The port is 9000 on the gamma host today")
    assert ap.append_inbox(inbox, [l1], log, [k1]) == 1
    l2, k2 = mk("s", "The port is 9000")
    assert ap.append_inbox(inbox, [l2], log, [k2]) == 1
    assert ap.append_inbox(inbox, [l2], log, [k2]) == 0  # exact repeat, same slug
    l3, k3 = mk("t", "The port is 9000")
    assert ap.append_inbox(inbox, [l3], log, [k3]) == 1  # same fact, other slug
    assert not list(tmp_path.glob(".capture-inbox.md.*"))  # no temp leftovers


def test_missing_confidence_passes_the_gate_but_a_low_one_does_not(world):
    ap, args, gdir, state, _ = world
    assert ap._confidence({"fact": FACT}) is None
    assert ap._confidence({"fact": FACT, "confidence": "high"}) == 0.0
    ap.read_letta_facts = lambda *a, **k: [{"fact": FACT, "source": "letta_block:x", "session_id": "l"}]
    stats = ap.run_promotion(args, ap.Log(state / "health.log"))
    assert stats["promoted"] == 1
    assert sink_path(ap, args, gdir, state).read_text().count(FACT) == 1


def _lines(n, fact):
    return [f"- 2026-10-08 [promote] target [[gamma-service]] | confidence 0.6 | source s | sessions 1 | first_seen 2026-10-08 | supersedes none | {fact}"] * n


def test_identical_proposals_in_one_batch_are_written_once(tmp_path):
    ap = _load("auto_promote_dup", "tools/auto-promote.py")
    inbox = tmp_path / "capture-inbox.md"
    inbox.write_text(INBOX_HEAD, encoding="utf-8")
    log = ap.Log(tmp_path / "h.log")
    assert ap.append_inbox(inbox, _lines(2, "Fact one."), log) == 1
    assert inbox.read_text().count("Fact one.") == 1


def test_exhausted_retries_write_nothing_and_report_zero(tmp_path, monkeypatch):
    ap = _load("auto_promote_race", "tools/auto-promote.py")
    inbox = tmp_path / "capture-inbox.md"
    inbox.write_text(INBOX_HEAD, encoding="utf-8")
    log = ap.Log(tmp_path / "h.log")
    calls = {"n": 0}
    real = Path.read_text

    def racing(self, *a, **k):
        # Every read of the inbox after the first in a pass sees a different file, as an unlocked writer would cause.
        out = real(self, *a, **k)
        if self == inbox:
            calls["n"] += 1
            if calls["n"] % 2 == 0:
                out += f"- racer {calls['n']}\n"
        return out

    monkeypatch.setattr(Path, "read_text", racing)
    assert ap.append_inbox(inbox, _lines(1, "Fact two."), log) == 0
    monkeypatch.undo()
    assert "Fact two." not in inbox.read_text()


def test_search_fts_restores_the_hits_log_variable_after_a_failing_import(tmp_path, monkeypatch):
    ap = _load("auto_promote_env", "tools/auto-promote.py")
    monkeypatch.setattr(ap, "_SERVER", None)
    monkeypatch.setenv("GESTALT_HITS_LOG", "/keep/me.jsonl")
    import importlib.util

    def boom(*a, **k):
        raise SystemExit(3)

    monkeypatch.setattr(importlib.util, "spec_from_file_location", boom)
    assert ap._search_fts("anything") == []
    assert os.environ["GESTALT_HITS_LOG"] == "/keep/me.jsonl"
    monkeypatch.delenv("GESTALT_HITS_LOG")
    assert ap._search_fts("anything") == []
    assert "GESTALT_HITS_LOG" not in os.environ
