"""GRAPH.md generator: Data Flow extraction edge cases (tools/gestalt regenerate_graph)."""
import subprocess
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "tools" / "gestalt"


def _rebuild(tmp_path: Path, body: str, slug: str = "entry") -> str:
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / f"{slug}.md").write_text(body)
    subprocess.run([str(TOOL), "rebuild"], env={"GESTALT_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"}, check=True, capture_output=True)
    return (tmp_path / "GRAPH.md").read_text()


def test_terminal_data_flow_section_keeps_body(tmp_path):
    # Data Flow as the LAST section of the file: the pre-2026-08-30 pipeline's
    # unconditional `sed '$d'` deleted the final content line, emptying the section.
    graph = _rebuild(tmp_path, "# Entry\n\nintro\n\n## Data Flow\n\nA -> B -> C pipeline line\n")
    assert "### entry" in graph
    assert "A -> B -> C pipeline line" in graph


def test_data_flow_followed_by_section_excludes_next_heading(tmp_path):
    graph = _rebuild(tmp_path, "# E\n\n## Data Flow\n\nflow body\n\n## Next Section\n\nother\n")
    assert "flow body" in graph
    assert "Next Section" not in graph


def test_nested_subheadings_demoted_below_slug_level(tmp_path):
    graph = _rebuild(tmp_path, "# E\n\n## Data Flow\n\n### Inputs\n\nx\n\n## Tail\n\ny\n")
    assert "#### Inputs" in graph
    assert "\n### Inputs" not in graph
