"""Search-index publish wiring in the post-merge hook TEMPLATE (stage 7 item C).

Before this fix, `.search/gestalt.db` was only rebuilt and published from
claude-tree/hooks/gestalt-session-start.sh -- a SessionStart hook that never fires for a bare
`git pull` (or a hub `fleet-sync pull`) with no Claude session afterwards, so a hub rebuild
could sit unpublished indefinitely. tools/gestalt-git-setup.sh installs the post-merge git hook
from a heredoc TEMPLATE (not a standalone file) into .git/hooks/post-merge on every clone that
runs it; this test greps the TEMPLATE directly (not an installed hook, which would only exist on
a machine that has actually run the installer) so the wiring cannot silently regress.

Companion coverage lives in tools/fleet/bin/fleet-sync's own pull_one() path (fleet-sync's
"post-pull rebuild path"), which is not a template and is exercised live by
tests/test_index_artifact.py instead.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SETUP_SCRIPT = REPO / "tools" / "gestalt-git-setup.sh"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _post_merge_template() -> str:
    """Extract the post-merge heredoc body exactly as gestalt-git-setup.sh will write it to
    .git/hooks/post-merge (before the outer script's own \\$ / \\` escaping is resolved -- this
    test asserts on the template source, which is what a reviewer or a future edit actually
    touches)."""
    src = _text(SETUP_SCRIPT)
    m = re.search(r'install_hook post-merge "\$\(cat <<EOF\n(.*?)\nEOF\n\)"', src, re.S)
    assert m, "gestalt-git-setup.sh: could not find the post-merge heredoc template (cat <<EOF ... EOF)"
    return m.group(1)


TEMPLATE = _post_merge_template()


def test_post_merge_template_exists_and_is_nonempty() -> None:
    assert "#!/bin/sh" in TEMPLATE


def test_post_merge_template_publishes_the_index_on_the_hub() -> None:
    assert "fleet-sync index-publish" in TEMPLATE, (
        "post-merge hook template no longer calls `fleet-sync index-publish` -- a hub pull with "
        "no Claude session afterwards will leave .search/gestalt.db unpublished again"
    )


def test_post_merge_template_runs_the_index_builder_before_publishing() -> None:
    """The publish call must come after the builder, not before -- publishing a db that has not
    just been rebuilt would ship a stale artifact."""
    builder_pos = TEMPLATE.find("gestalt-index-builder.py")
    publish_pos = TEMPLATE.find("fleet-sync index-publish")
    assert builder_pos != -1, "post-merge hook template no longer runs tools/gestalt-index-builder.py"
    assert publish_pos != -1, "post-merge hook template no longer calls fleet-sync index-publish"
    assert builder_pos < publish_pos, (
        "fleet-sync index-publish must run AFTER gestalt-index-builder.py in the post-merge "
        "template, not before"
    )


def test_post_merge_template_gates_the_publish_on_being_the_hub() -> None:
    """'when and only when the node is the hub' -- the publish call must sit behind a hostname
    comparison against the configured hub name, not fire unconditionally on every clone."""
    m = re.search(r'if \[ "\\\$THIS_HOST" = "\\\$HUBN" \]; then(.*?)\n  fi', TEMPLATE, re.S)
    assert m, "post-merge hook template: no hub-only `if [ \"$THIS_HOST\" = \"$HUBN\" ]` guard found"
    assert "fleet-sync index-publish" in m.group(1), (
        "fleet-sync index-publish is not inside the hub-only guard -- it would fire on every node"
    )


def test_post_merge_template_does_not_force_a_full_reembed() -> None:
    """The builder call here must be the cheap, no-op-if-current default invocation
    (`gestalt-index-builder.py` with no args), not `--force` -- a --force re-embed on every merge
    is exactly the unbounded heavy-job pattern home-mesh ^hub-invariants warns against."""
    m = re.search(r"gestalt-index-builder\.py[^\n]*", TEMPLATE)
    assert m, "post-merge hook template: no gestalt-index-builder.py invocation found"
    assert "--force" not in m.group(0), (
        f"post-merge hook template must not pass --force to gestalt-index-builder.py: {m.group(0)!r}"
    )
