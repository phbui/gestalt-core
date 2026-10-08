"""Knowledge-corpus structural hygiene: links resolve, anchors don't collide,
frontmatter is complete, no entry crowds out the rest of the corpus, and each
resume-block workstream carries at most one live block.

`local`-only, and self-skips when `knowledge/**` is git-crypt encrypted, exactly
like `tests/test_golden_set.py::_corpus_readable` — copied rather than imported
since conftest.py doesn't export it and this file must not depend on another
writer's tests module.

Targets gestalt-efficiency-audit-2026-08 findings F7 (one entry could crowd out
the corpus — research-project-log-1 measured 45.9% pre-pruning), F8 (chunk re-splitting
demoting anchors — the anchor-first splitter needs anchors that are actually
`^anchor`-clean, i.e. don't collide), and the general "docs/knowledge drift
silently" class (F12) as it applies to the corpus itself rather than gestalt.md.

Anchor-resolution semantics follow `knowledge/gestalt.md`'s own documented
"Resolving Links" convention verbatim: `[[slug#^block-id]]` resolves if the
target file has a LINE containing that literal `^block-id` token (not
"is the chunk boundary for it" — that's a stricter, indexer-internal property
covered separately by the duplicate-anchor check below, which only cares about
`^anchor` tokens trailing a HEADING line, since two different headings sharing
one block-id is what actually makes a citation ambiguous).
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
KDIR = REPO / "knowledge"
RULES_DIR = REPO / "claude-tree" / "rules"

GITCRYPT_MAGIC = b"\x00GITCRYPT"

FENCE_TOGGLE_RE = re.compile(r"^\s*```")
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
CODESPAN_RE = re.compile(r"`[^`]*`")
LINE_TRAILING_ANCHOR_RE = re.compile(r"\^([a-z][a-z0-9-]*)\s*$")
REQUIRED_FRONTMATTER_KEYS = ("type", "title", "tags", "created", "updated")


def _corpus_readable() -> bool:
    """True only if entries are decrypted in this working copy. Copied from
    test_golden_set.py's helper of the same name — see module docstring."""
    entries = list(KDIR.glob("*.md")) if KDIR.exists() else []
    if not entries:
        return False
    return not entries[0].read_bytes().startswith(GITCRYPT_MAGIC)


pytestmark = [
    pytest.mark.local,
    pytest.mark.skipif(not _corpus_readable(), reason="knowledge/ is git-crypt encrypted here"),
]


def _iter_lines_outside_fences(raw: str):
    """Yield each line of `raw` that is not inside a fenced code block."""
    in_fence = False
    for line in raw.split("\n"):
        if FENCE_TOGGLE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        yield line


@pytest.fixture(scope="module")
def entries() -> dict[str, Path]:
    return {p.stem: p for p in KDIR.glob("*.md")}


@pytest.fixture(scope="module")
def rule_slugs() -> set[str]:
    return {p.stem for p in RULES_DIR.glob("*.md")} if RULES_DIR.exists() else set()


@pytest.fixture(scope="module")
def raw_by_slug(entries) -> dict[str, str]:
    return {slug: p.read_text(encoding="utf-8", errors="replace") for slug, p in entries.items()}


def _line_containing_anchor(raw: str, anchor: str) -> bool:
    """Per gestalt.md's own resolving convention: '`[[slug#^block-id]]` -> find
    the line containing `^block-id`' — a substring search over non-fenced lines,
    word-bounded so a short anchor can't false-positive inside a longer one."""
    pattern = re.compile(r"(?<![a-z0-9-])\^" + re.escape(anchor) + r"(?![a-z0-9-])")
    return any(pattern.search(line) for line in _iter_lines_outside_fences(raw))


def _headings(raw: str) -> set[str]:
    """Heading text (any '#' level), anchor suffix stripped, lowercased."""
    out = set()
    for line in _iter_lines_outside_fences(raw):
        s = line.strip()
        if s.startswith("#"):
            h = s.lstrip("#").strip()
            if "^" in h:
                h = h.rsplit("^", 1)[0].strip()
            out.add(h.lower())
    return out


def _heading_trailing_anchors(raw: str) -> list[str]:
    """Anchors defined by a heading line ending in `^anchor` — the only kind of
    duplication that makes a `[[slug#^anchor]]` citation genuinely ambiguous
    (which of two different sections does it name?). A bare `^anchor` cited
    mid-sentence elsewhere in the file is a citation, not a rival definition."""
    out = []
    for line in _iter_lines_outside_fences(raw):
        s = line.strip()
        if not s.startswith("#"):
            continue
        cleaned = WIKILINK_RE.sub("", CODESPAN_RE.sub("", s)).rstrip()
        m = LINE_TRAILING_ANCHOR_RE.search(cleaned)
        if m:
            out.append(m.group(1))
    return out


def _all_wikilinks(slug: str, raw: str):
    """Yield (raw_body, ) for every `[[...]]` outside fences and code spans."""
    for line in _iter_lines_outside_fences(raw):
        line_nocode = CODESPAN_RE.sub("", line)
        for m in WIKILINK_RE.finditer(line_nocode):
            if m.group(1).split('|')[0].split('#')[0].lower().endswith('.pdf'):
                continue  # ![[<citekey>.pdf]] embeds point at the untracked PDF store (assets/pdf/README.md), not at entries
            yield m.group(1)


# 2026-08-18: one genuinely dangling anchor citation found by this test's first
# run, left unfixed. `research-project.md` never got a `^llama-trl-template-traps-2026-08-12`
# anchor written — the trap note this cites (render-doc.py vs build_paper.py) does
# not exist anywhere in the corpus. Fixing the SLUG/anchor syntax isn't possible
# here (there is no near-miss to correct it to), and inventing the missing content
# is out of a test-writer's scope — so it's tracked here as a real, open hygiene
# violation rather than silently passed. See RETURN for the report-side callout.
KNOWN_DANGLING_LINKS: set[tuple[str, str]] = set()  # all resolved as of 2026-08-18


def test_every_wikilink_resolves(entries, rule_slugs, raw_by_slug):
    dangling = []
    for slug, raw in raw_by_slug.items():
        for body in _all_wikilinks(slug, raw):
            slugpart = body.split("#", 1)[0].split("|")[0].strip()
            fragpart = body.split("#", 1)[1].strip() if "#" in body else None
            target_stem = slugpart if slugpart else slug  # empty slug = same-file link
            if target_stem not in entries and target_stem not in rule_slugs:
                dangling.append((slug, body))
                continue
            if not fragpart:
                continue
            if fragpart.startswith("^"):
                anchor = fragpart[1:].strip()
                if target_stem not in entries:
                    continue  # rule files aren't chunked/anchored the same way; slug existence is the check
                if not _line_containing_anchor(raw_by_slug[target_stem], anchor):
                    dangling.append((slug, body))
            else:
                if target_stem in entries and fragpart.lower() not in _headings(raw_by_slug[target_stem]):
                    dangling.append((slug, body))

    unexpected = [d for d in dangling if d not in KNOWN_DANGLING_LINKS]
    assert not unexpected, f"dangling wikilinks (not in the documented known set): {unexpected}"


def test_no_new_known_dangling_links_go_stale(raw_by_slug):
    """If a documented dangling link gets fixed, this should notice so the
    allowlist above gets pruned rather than accreting forever."""
    for slug, body in KNOWN_DANGLING_LINKS:
        assert slug in raw_by_slug, f"{slug} no longer exists — prune KNOWN_DANGLING_LINKS"
        assert f"[[{body}]]" in raw_by_slug[slug], (
            f"[[{body}]] no longer appears verbatim in {slug}.md — it may have been fixed; "
            "prune it from KNOWN_DANGLING_LINKS"
        )


def test_no_heading_level_anchor_duplicated_within_a_file(raw_by_slug):
    violations = {}
    for slug, raw in raw_by_slug.items():
        anchors = _heading_trailing_anchors(raw)
        dupes = {a for a in anchors if anchors.count(a) > 1}
        if dupes:
            violations[slug] = dupes
    assert not violations, f"heading-level ^anchor collisions (ambiguous citation target): {violations}"


def test_every_entry_has_required_frontmatter_keys(raw_by_slug):
    """Required keys per `.claude/references/knowledge-write.md` lines 16-22
    (type, title, tags, created, updated).

    2026-08-18: five entries pre-date or slipped past that convention. Fixing
    frontmatter is a content edit, out of a test-writer's scope (knowledge/*.md
    is read-only here except unambiguous link/anchor fixes) — tracked as a dated
    ratchet so no NEW entry can silently join this set, and any of these getting
    fixed just makes its check pass (subset comparison), no maintenance needed.
    """
    KNOWN_FRONTMATTER_GAPS: dict[str, set[str]] = {
        "home-mesh": {"type", "tags", "created"},
        "hub-hub-runbook": {"type", "title", "created", "updated"},
        "research-project": {"tags", "created", "updated"},
        "large-entry": {"tags", "created", "updated"},
    }
    KNOWN_NO_FRONTMATTER_AT_ALL = {"kitty-setup"}

    unexpected = {}
    for slug, raw in raw_by_slug.items():
        if not raw.startswith("---"):
            if slug not in KNOWN_NO_FRONTMATTER_AT_ALL:
                unexpected[slug] = {"__no_frontmatter__"}
            continue
        end = raw.find("---", 3)
        fm = raw[3:end] if end != -1 else ""
        missing = {k for k in REQUIRED_FRONTMATTER_KEYS if not re.search(rf"^{k}:", fm, re.M)}
        allowed = KNOWN_FRONTMATTER_GAPS.get(slug, set())
        newly_missing = missing - allowed
        if newly_missing:
            unexpected[slug] = newly_missing
    assert not unexpected, f"entries missing required frontmatter keys beyond the documented ratchet: {unexpected}"


def test_no_entry_exceeds_a_quarter_of_the_corpus(entries):
    """F7 class: one oversized entry crowds out retrieval for everything else.
    research-project-log-1 measured 45.9% of the corpus pre-pruning (gestalt-efficiency-
    audit-2026-08 ^research-project-bloat); after the 2026-08-18 prune (commit 6a2f799)
    it measures ~22%. 25% is set as the ratchet ceiling now, deliberately above
    today's ~22% so routine growth of OTHER entries doesn't nuisance-fail this,
    while still catching a genuine re-bloat before it reaches the old 46%."""
    sizes = {slug: p.stat().st_size for slug, p in entries.items()}
    total = sum(sizes.values())
    assert total > 0
    offenders = {slug: b for slug, b in sizes.items() if b > 0.25 * total}
    assert not offenders, (
        f"entry(ies) exceed 25% of total corpus bytes ({total} B): "
        f"{ {s: f'{b} B ({b / total:.1%})' for s, b in offenders.items()} }"
    )


RESUME_ANCHOR_RE = re.compile(
    r"^compact-resume-(?:(?P<pre>[a-z0-9]+)-)?(?P<date>\d{4}-\d{2}-\d{2})[a-z]?(?:-(?P<post>[a-z0-9-]+))?$"
)


def _resume_anchor_groups(raw: str) -> dict[str, list[str]]:
    """Group this file's own DEFINED (heading- or paragraph-trailing, per the
    corpus convention) `^compact-resume-*` anchors by workstream suffix, e.g.
    `^compact-resume-2026-08-17-hub` and `^compact-resume-paper1-2026-07-30`
    both key on 'hub'/'paper1' regardless of which side of the date it sits on.
    A bare `^compact-resume-2026-08-18` (no suffix) is its own '' group."""
    by_suffix: dict[str, list[str]] = defaultdict(list)
    for line in _iter_lines_outside_fences(raw):
        cleaned = WIKILINK_RE.sub("", CODESPAN_RE.sub("", line)).rstrip()
        m = LINE_TRAILING_ANCHOR_RE.search(cleaned)
        if not m or not m.group(1).startswith("compact-resume-"):
            continue
        rm = RESUME_ANCHOR_RE.match(m.group(1))
        if not rm:
            continue
        suffix = rm.group("pre") or rm.group("post") or ""
        by_suffix[suffix].append(rm.group("date"))
    return by_suffix


# 2026-08-18: `research-project-log-1` and `research-project-log-2` both carried more than one
# live compact-resume block for the same (file, workstream-suffix) pair —
# superseded blocks never pruned after the newer one landed (B4, deferred
# pruning, gestalt-efficiency-audit-2026-08 ^research-project-bloat). Fixed the same
# day (the F7 deferred half): every `^compact-resume-*` anchor in both files
# was either renamed to a dated result anchor (durable facts lifted into
# prose, scaffolding deleted, citations repointed) or, where a still-open
# workstream needed a live pointer, left as the single newest block. Neither
# file has more than one compact-resume anchor per workstream suffix now, so
# this set is empty rather than removed outright — it stays as the place a
# future genuine violation gets recorded, matching the pattern above it.
KNOWN_RESUME_BLOCK_VIOLATIONS: set[str] = set()


def test_at_most_one_resume_block_per_workstream_per_entry(raw_by_slug):
    violations = {}
    for slug, raw in raw_by_slug.items():
        groups = _resume_anchor_groups(raw)
        bad = {suffix: dates for suffix, dates in groups.items() if len(dates) > 1}
        if bad:
            violations[slug] = bad

    unexpected = {s: v for s, v in violations.items() if s not in KNOWN_RESUME_BLOCK_VIOLATIONS}
    assert not unexpected, f"NEW multi-resume-block workstreams (not in the documented deferred set): {unexpected}"

    for slug in KNOWN_RESUME_BLOCK_VIOLATIONS:
        if slug in violations:
            pytest.xfail(f"{slug}: deferred pruning, B4 2026-08-18 — {violations[slug]}")


# --- X9 provenance keys and L7 sensitivity (2026-10-06) ---------------------------------------
# New paper notes (created 2026-10-07 or later) must carry the flat provenance keys. Older notes
# stay valid: rewriting 446 notes would re-send each one to the local graph ingest, so they are
# only counted. tools/paper-provenance-backfill.py can add the keys later, by an explicit decision.
PROVENANCE_REQUIRED_FROM = "2026-10-07"
PROVENANCE_REQUIRED = ("doi", "pdf_sha256", "summary_basis", "read_by")
ALLOWED_VALUES = {
    "summary_basis": {"abstract", "fulltext", "none"},
    "read_by": {"none", "human", "model", "both", "unknown"},
    "sensitivity": {"public", "unpublished", "restricted"},
}


def _fm_scalars(raw: str) -> dict[str, str]:
    """Flat `key: value` pairs of the frontmatter block, values unquoted. No YAML dependency."""
    if not raw.startswith("---"):
        return {}
    end = raw.find("\n---", 3)
    if end == -1:
        return {}
    out = {}
    for line in raw[3:end].split("\n"):
        m = re.match(r"^([A-Za-z_][\w-]*):[ \t]*(.*?)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2).strip("\"'")
    return out


def test_new_paper_notes_carry_provenance_keys(raw_by_slug):
    missing = {}
    for slug, raw in raw_by_slug.items():
        if not slug.startswith("paper-"):
            continue
        fm = _fm_scalars(raw)
        if str(fm.get("created", "")) < PROVENANCE_REQUIRED_FROM:
            continue
        gone = [k for k in PROVENANCE_REQUIRED if k not in fm]
        if gone:
            missing[slug] = gone
    assert not missing, f"paper notes created on or after {PROVENANCE_REQUIRED_FROM} lack provenance keys: {missing}"


def test_provenance_and_sensitivity_values_are_valid(raw_by_slug):
    bad, order = {}, []
    for slug, raw in raw_by_slug.items():
        fm = _fm_scalars(raw)
        for k, allowed in ALLOWED_VALUES.items():
            if k in fm and fm[k] not in allowed:
                bad.setdefault(slug, []).append(f"{k}={fm[k]!r}")
        if fm.get("read_by") in {"human", "model", "both"} and not fm.get("read_date"):
            order.append(f"{slug}: read_by {fm['read_by']} needs read_date")
        if fm.get("summary_basis") == "fulltext" and not fm.get("pdf_sha256"):
            order.append(f"{slug}: fulltext needs pdf_sha256")
    assert not bad, f"invalid provenance or sensitivity values: {bad}"
    assert not order, order


def test_report_older_paper_notes_without_provenance(raw_by_slug, capsys):
    """Report only. Run with -s to see the count."""
    old = [s for s, r in raw_by_slug.items() if s.startswith("paper-") and "summary_basis" not in _fm_scalars(r)]
    with capsys.disabled():
        print(f"\n[provenance] paper notes without summary_basis: {len(old)} (report only, not asserted)")
