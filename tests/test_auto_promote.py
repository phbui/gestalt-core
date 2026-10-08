"""Behavioral tests for tools/auto-promote.py.

This script has ZERO prior tests and the highest blast radius in the repo: it
performs UNATTENDED APPENDS into knowledge/*.md, the human-curated source of
truth, with no human in the loop. It has already shipped broken twice, both
times invisibly:

  (a) read_manifest() parsed a pipe-table MANIFEST.md format that
      tools/gestalt's regenerate_manifest() never emitted, so it always
      returned {} and find_target_entry() never once selected a target —
      promotion silently no-opped from day one.
  (b) A relevance gate on the (now-removed) semantic fallback was set above
      the achievable RRF score ceiling, so its branch could never fire.

Both bugs were "the code runs, logs look clean, nothing writes" — the worst
kind of silent failure for a script with write access to curated knowledge.
Every test here calls the real functions from the real module. None of them
mock the thing under test, because a mock is exactly what would have hidden
both original bugs.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import _load


@pytest.fixture(scope="module")
def ap():
    """The real auto-promote module, imported without running its CLI."""
    return _load("auto_promote", "tools/auto-promote.py")


class _FakeLog:
    """A no-op sink so update_entry's log.info() call has somewhere to go.

    This is not a mock of anything under test — update_entry's log calls are
    a side channel, not the behavior being asserted. Using the real Log class
    would work identically (it just appends a line to a file); this avoids
    needing an extra tmp file per test.
    """

    def info(self, msg):
        pass

    def warn(self, msg):
        pass

    def error(self, msg):
        pass


# ---------------------------------------------------------------------------
# Producer/consumer contract: regenerate_manifest() -> read_manifest()
# ---------------------------------------------------------------------------
#
# This is the highest-priority test in the file. Bug (a) above existed because
# nothing ever fed a REAL generated MANIFEST.md through read_manifest() — a
# hand-written fixture string would have encoded the same wrong assumption
# that let the two formats drift apart in the first place. So this test
# invokes the actual generator (tools/gestalt rebuild) and feeds its literal
# output file to the real read_manifest().


def test_manifest_producer_consumer_contract(tmp_path, ap):
    """A real generated MANIFEST.md must be parseable by read_manifest().

    Source: tools/gestalt:35-75 (regenerate_manifest, emits `### slug` /
    `type | title` / `Links:` / `Blocks:` headings) and
    tools/auto-promote.py:428-462 (read_manifest, must parse that exact
    format).
    """
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (tmp_path / "rules").mkdir()
    (knowledge_dir / "widget-pipeline.md").write_text(
        "---\n"
        "type: reference\n"
        'title: "Widget Pipeline"\n'
        "tags: [widget, pipeline]\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "---\n\n"
        "## Architecture\n\n"
        "The widget pipeline ingests raw widgets.\n\n"
        "## Relationships\n\n"
        "- [[other-thing]]\n",
        encoding="utf-8",
    )

    gestalt_cli = REPO / "tools" / "gestalt"
    result = subprocess.run(
        ["bash", str(gestalt_cli), "rebuild"],
        env={"GESTALT_DIR": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"tools/gestalt rebuild failed (this test cannot fall back to a "
        f"hand-written fixture and still close the contract gap): "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    manifest_path = tmp_path / "MANIFEST.md"
    assert manifest_path.exists(), "generator did not write MANIFEST.md"

    entries = ap.read_manifest(tmp_path)

    # This is the exact assertion that would have caught bug (a): before the
    # fix, read_manifest() parsed a pipe-table format and always returned {}
    # against this real generator output.
    assert entries != {}, (
        "read_manifest() returned {} against a REAL generator-produced "
        "MANIFEST.md — this is bug (a) regressing: the two formats have "
        "drifted apart again"
    )
    assert "widget-pipeline" in entries, f"expected slug missing: {entries!r}"
    assert entries["widget-pipeline"]["title"] == "Widget Pipeline", entries["widget-pipeline"]
    # Tags are deliberately not written into MANIFEST.md (per read_manifest's
    # own docstring) — they come from the entry's own frontmatter instead.
    assert entries["widget-pipeline"]["tags"] == ["widget", "pipeline"], entries["widget-pipeline"]


# ---------------------------------------------------------------------------
# find_target_entry: slug matching, length/multiword guard, word boundaries
# ---------------------------------------------------------------------------


def test_find_target_entry_matches_multiword_slug_in_fact_text(ap):
    """A multiword slug found in the fact text (hyphen-or-space form) matches.

    Source: tools/auto-promote.py:136-155.
    """
    manifest = {"widget-pipeline": {"tags": [], "title": "Widget Pipeline"}}
    fact = {"fact": "The widget pipeline now writes directly to postgres."}
    assert ap.find_target_entry(fact, manifest) == "widget-pipeline"


def test_find_target_entry_rejects_generic_short_single_word(ap):
    """A short, single-word slug is too generic to match on alone.

    Source: tools/auto-promote.py:149-152 — `if not (multiword or
    len(candidate) >= 7): continue`. "mcp" is 3 characters and has no
    separator, so it must never be used as the sole basis for a write.
    """
    manifest = {"mcp": {"tags": [], "title": "MCP"}}
    fact = {"fact": "We configured mcp for the new tool integration today."}
    assert ap.find_target_entry(fact, manifest) is None


def test_find_target_entry_word_boundary_rejects_substring_match(ap):
    """A slug word must not fire when it is merely a substring of another word.

    Source: tools/auto-promote.py:142-147 documents the historical bug this
    guards: tag-matching used to let "invoice" match tag `voice` as a bare
    substring. The current slug-matching path uses a word-boundary regex
    (line 154); this test proves that regex actually rejects the embedded
    case. "session" is 7 characters (passes the length guard on its own,
    unlike "voice"), so this isolates the boundary check specifically rather
    than re-testing the length guard above.
    """
    manifest = {"session": {"tags": [], "title": "Session"}}
    embedded = {"fact": "We reviewed several long-running obsessions and sessions this week."}
    assert ap.find_target_entry(embedded, manifest) is None, (
        "'session' matched as a bare substring of 'obsessions'/'sessions' — "
        "the word-boundary regex regressed"
    )
    # Control: the same slug DOES match when it appears as its own word, so
    # the None above is the boundary check working, not the slug being
    # unreachable for some other reason.
    standalone = {"fact": "The session logged out early due to a timeout."}
    assert ap.find_target_entry(standalone, manifest) == "session"


def test_find_target_entry_has_no_semantic_or_search_fallback(ap):
    """There must be no relevance-scored fallback path after the slug loop.

    Source: tools/auto-promote.py:157-179. The comment there is explicit:
    gestalt_search's RRF score cannot express "relevant enough to write
    unattended", and re-adding a threshold on it reproduces bug (b) (a gate
    set above the achievable ceiling that can never fire, or below it that
    accepts noise). This function feeds an UNATTENDED write into curated
    knowledge, so ambiguous facts MUST return None and stay in the queue.

    This test asserts the absence two ways: statically, that the function
    body contains none of the machinery a search fallback would need; and
    behaviorally, that a fact with heavy lexical overlap on an UNRELATED
    entry's tags/title (the exact kind of thing a naive similarity match
    would accept) still returns None because its slug is never named.
    """
    src = inspect.getsource(ap.find_target_entry)
    # Call-shaped markers only — the function's own comment legitimately
    # discusses "score" and "gestalt_search" in prose to explain why no
    # fallback exists, so a bare substring check on those words would flag
    # the comment itself rather than a reintroduced call.
    for marker in ("asyncio.run(", "httpx.", "urllib.request", "gestalt_search("):
        assert marker not in src, (
            f"find_target_entry() now references {marker!r} — a semantic/"
            f"search fallback appears to have been re-added; see the "
            f"function's own comment on why the RRF score ceiling makes "
            f"that unsafe for an unattended write"
        )

    manifest = {
        "unrelated-entry": {"tags": ["api", "database", "endpoint"], "title": "Something Else"}
    }
    # Heavy lexical overlap with the tags/title above, but the slug itself
    # ("unrelated-entry") is never named in the text.
    fact = {"fact": "The api uses a database endpoint for storage of interface data."}
    assert ap.find_target_entry(fact, manifest) is None


# ---------------------------------------------------------------------------
# is_stable / is_significant gates
# ---------------------------------------------------------------------------


def test_is_stable_true_for_letta_block_facts(ap):
    """Letta block facts are inherently multi-session and always stable.

    Source: tools/auto-promote.py:92-102.
    """
    fact = {"fact": "The service uses redis for caching.", "source": "letta_block:project_context"}
    assert ap.is_stable(fact, [], min_sessions=2) is True


def test_is_stable_false_below_min_sessions(ap):
    """A queue fact seen in only one session is not yet stable."""
    fact = {"fact": "The service uses redis for caching.", "source": "queue", "session_id": "s1"}
    assert ap.is_stable(fact, [fact], min_sessions=2) is False


def test_is_stable_true_at_min_sessions(ap):
    """The same fact text seen across 2 distinct session_ids clears the gate."""
    fact = {"fact": "The service uses redis for caching.", "source": "queue", "session_id": "s1"}
    queue_facts = [
        {"fact": fact["fact"], "session_id": "s1"},
        {"fact": fact["fact"], "session_id": "s2"},
    ]
    assert ap.is_stable(fact, queue_facts, min_sessions=2) is True


def test_is_significant_true_for_architectural_fact(ap):
    """A fact naming a dependency/schema relationship is significant.

    Source: tools/auto-promote.py:105-129.
    """
    fact = {"fact": "The auth service depends on the users database schema."}
    assert ap.is_significant(fact) is True


def test_is_significant_false_for_transient_framing(ap):
    """Transient/investigation framing is excluded even if it mentions a schema.

    "currently debugging" matches the transient exclusion list and short-
    circuits to False before the significance patterns are even checked.
    """
    fact = {"fact": "I think we are currently debugging the schema migration issue."}
    assert ap.is_significant(fact) is False


def test_is_significant_false_for_no_architectural_content(ap):
    """A fact with no architectural pattern at all is not significant."""
    fact = {"fact": "The weather today is nice and calm outside."}
    assert ap.is_significant(fact) is False


# ---------------------------------------------------------------------------
# update_entry: insertion point, frontmatter bump, framing strip
# ---------------------------------------------------------------------------


def test_update_entry_inserts_under_heading_not_at_eof(tmp_path, ap):
    """Facts must land under the target heading, not appended after EOF.

    Source: tools/auto-promote.py:182-231. update_entry() searches for
    `## Architecture` / `## Data Flow` / `## Relationships` and inserts
    before the NEXT `## ` heading — a naive implementation could instead
    just do `content += insertion`, which would land after every existing
    section including one that follows Architecture.
    """
    entry_path = tmp_path / "widget-pipeline.md"
    entry_path.write_text(
        "---\n"
        "type: reference\n"
        'title: "Widget Pipeline"\n'
        "tags: [widget]\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "---\n\n"
        "## Architecture\n\n"
        "The pipeline ingests raw widgets.\n\n"
        "## Relationships\n\n"
        "- [[other-thing]]\n",
        encoding="utf-8",
    )
    facts = [{"fact": "We discovered that the widget pipeline connects to postgres."}]

    ap.update_entry(entry_path, facts, _FakeLog())

    content = entry_path.read_text()
    promo_idx = content.index("### Auto-Promoted Facts")
    relationships_idx = content.index("## Relationships")
    assert promo_idx < relationships_idx, (
        "facts were inserted after '## Relationships' instead of under "
        "'## Architecture' — this appends at/near EOF rather than under "
        "the intended heading"
    )
    # The original trailing section must survive intact after the insertion.
    assert "- [[other-thing]]" in content


def test_update_entry_bumps_frontmatter_updated_date(tmp_path, ap):
    """The frontmatter `updated:` date is rewritten to today.

    Source: tools/auto-promote.py:224-228.
    """
    from datetime import datetime

    entry_path = tmp_path / "widget-pipeline.md"
    entry_path.write_text(
        "---\n"
        "type: reference\n"
        'title: "Widget Pipeline"\n'
        "tags: [widget]\n"
        "created: 2026-01-01\n"
        "updated: 2020-01-01\n"
        "---\n\n"
        "## Architecture\n\n"
        "The pipeline ingests raw widgets.\n\n"
        "## Relationships\n\n"
        "- [[other-thing]]\n",
        encoding="utf-8",
    )
    facts = [{"fact": "The widget pipeline connects to postgres."}]

    ap.update_entry(entry_path, facts, _FakeLog())

    content = entry_path.read_text()
    # Mirrors the exact call in tools/auto-promote.py:226 (naive local time) so
    # the expected date matches what the code under test actually computes.
    expected = datetime.now().strftime("%Y-%m-%d")  # noqa: DTZ005
    assert f"updated: {expected}" in content, content.splitlines()[:8]
    assert "updated: 2020-01-01" not in content


def test_update_entry_strips_investigation_framing(tmp_path, ap):
    """Investigation framing prefixes are stripped and the result capitalized.

    Source: tools/auto-promote.py:200-206 (AKP-FR-023: write facts in
    authoritative voice, not investigation narration).
    """
    entry_path = tmp_path / "widget-pipeline.md"
    entry_path.write_text(
        "---\n"
        "type: reference\n"
        'title: "Widget Pipeline"\n'
        "tags: [widget]\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "---\n\n"
        "## Architecture\n\n"
        "The pipeline ingests raw widgets.\n\n"
        "## Relationships\n\n"
        "- [[other-thing]]\n",
        encoding="utf-8",
    )
    facts = [{"fact": "We discovered that the widget pipeline connects to postgres."}]

    ap.update_entry(entry_path, facts, _FakeLog())

    content = entry_path.read_text()
    assert "- The widget pipeline connects to postgres." in content, content


def test_update_entry_records_provenance_as_an_invisible_comment(tmp_path, ap):
    """Every appended fact carries its source, recoverable by grep, invisible when rendered.

    Source: tools/auto-promote.py:203-206 (audit 2026-09-08, finding A17/C). Before this,
    update_entry wrote a bare `- {text}` bullet with no source marker, so once a fact was
    promoted there was no way to tell whether it arrived via the letta_block exemption
    (AKP-FR-020 clause a, unconditional) or 2-session queue corroboration (clause b) --
    making the exemption's real-world effect permanently unauditable from entries alone.
    """
    entry_path = tmp_path / "widget-pipeline.md"
    entry_path.write_text(
        "---\n"
        "type: reference\n"
        'title: "Widget Pipeline"\n'
        "tags: [widget]\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "---\n\n"
        "## Architecture\n\n"
        "The pipeline ingests raw widgets.\n\n"
        "## Relationships\n\n"
        "- [[other-thing]]\n",
        encoding="utf-8",
    )
    facts = [
        {"fact": "The widget pipeline connects to postgres.", "source": "letta_block:project_context"},
        {"fact": "The widget pipeline retries on timeout.", "source": "transcript"},
    ]

    ap.update_entry(entry_path, facts, _FakeLog())

    content = entry_path.read_text()
    assert "- The widget pipeline connects to postgres. <!-- promoted:letta_block:project_context -->" in content, content
    assert "- The widget pipeline retries on timeout. <!-- promoted:transcript -->" in content, content
    # The two provenances must be distinguishable from each other, not just present.
    assert "letta_block:" in content and "promoted:transcript" in content
    assert "We discovered" not in content, "investigation framing was not stripped"


# ---------------------------------------------------------------------------
# is_factual_statement
# ---------------------------------------------------------------------------


def test_is_factual_statement_true_for_infrastructure_language(ap):
    assert ap.is_factual_statement("The service connects to the postgres database on port 5432.") is True


def test_is_factual_statement_false_for_ordinary_prose(ap):
    assert ap.is_factual_statement("The weather was nice today so we went for a walk.") is False


# ---------------------------------------------------------------------------
# read_queue / clear_queue
# ---------------------------------------------------------------------------


def test_read_queue_missing_file_returns_empty_list(ap, tmp_path):
    assert ap.read_queue(tmp_path / "no-such-queue.json", _FakeLog()) == []


def test_read_queue_corrupt_json_returns_empty_list(ap, tmp_path):
    p = tmp_path / "queue.json"
    p.write_text("{not valid json")
    assert ap.read_queue(p, _FakeLog()) == []


def test_read_queue_parses_a_real_list(ap, tmp_path):
    p = tmp_path / "queue.json"
    p.write_text('[{"fact": "x", "session_id": "s1"}]')
    assert ap.read_queue(p, _FakeLog()) == [{"fact": "x", "session_id": "s1"}]


def test_clear_queue_removes_only_promoted_facts(ap, tmp_path):
    p = tmp_path / "queue.json"
    p.write_text(
        '[{"fact": "The service uses redis for caching."}, '
        '{"fact": "The other service uses postgres for storage."}]'
    )
    ap.clear_queue(p, ["The service uses redis for caching."], _FakeLog())
    remaining = ap.read_queue(p, _FakeLog())
    assert len(remaining) == 1
    assert remaining[0]["fact"] == "The other service uses postgres for storage."


def test_clear_queue_missing_file_is_a_noop(ap, tmp_path):
    ap.clear_queue(tmp_path / "no-such-queue.json", ["x"], _FakeLog())  # must not raise


# ---------------------------------------------------------------------------
# read_letta_facts: agent-id file present/absent, Letta reachable/unreachable
# ---------------------------------------------------------------------------


def test_read_letta_facts_returns_empty_without_agent_id_file(ap, tmp_path):
    assert ap.read_letta_facts("http://localhost:8283/v1", tmp_path, _FakeLog()) == []


def test_read_letta_facts_returns_empty_when_letta_unreachable(ap, tmp_path, monkeypatch):
    (tmp_path / "letta-agent-id.txt").write_text("agent-123")

    import urllib.request

    def raise_conn_error(req, timeout=5):
        raise ConnectionRefusedError("letta not running")

    monkeypatch.setattr(urllib.request, "urlopen", raise_conn_error)
    assert ap.read_letta_facts("http://localhost:8283/v1", tmp_path, _FakeLog()) == []


def test_read_letta_facts_extracts_from_target_labels_only(ap, tmp_path, monkeypatch):
    (tmp_path / "letta-agent-id.txt").write_text("agent-123")

    agent = {
        "memory": {
            "blocks": [
                {"label": "project_context",
                 "value": "The service depends on the users database schema."},
                {"label": "human", "value": "The user prefers dark mode and short replies."},
            ]
        }
    }

    import io
    import json as jsonmod
    import urllib.request

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda req, timeout=5: _Resp(jsonmod.dumps(agent).encode()))

    facts = ap.read_letta_facts("http://localhost:8283/v1", tmp_path, _FakeLog())
    assert len(facts) == 1
    assert facts[0]["fact"] == "The service depends on the users database schema."
    assert facts[0]["source"] == "letta_block:project_context"
    assert facts[0]["confidence"] == 0.6
