"""Sampling and scoring of tools/graphiti-audit.py against a stub redis-cli (X7, 2026-10-06). Hermetic."""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import _load  # noqa: E402

audit = _load("graphiti_audit", "tools/graphiti-audit.py")

STUB = r'''#!/usr/bin/env python3
import json, sys
argv = sys.argv[1:]
assert argv[1] == "GRAPH.RO_QUERY", argv
open(sys.argv[0] + ".log", "a").write(argv[3] + "\n")
q = argv[3]
H = ["h"]
if "RETURN r.uuid" in q and "IN" not in q:
    rows = [["e%02d" % i] for i in range(30)]
elif "RELATES_TO" in q:
    ids = json.loads(q.split(" IN ")[1].split(" RETURN")[0])
    rows = [[i, "A", "REL", "B", "fact " + i, ["ep1"], None, None] for i in ids]
else:
    rows = [["ep1", "episode one", "the source text"]]
print(json.dumps([H, rows, []]))
'''


@pytest.fixture
def cli(tmp_path):
    p = tmp_path / "stub-cli"
    p.write_text(STUB)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return [str(p)]


def test_sampling_is_seeded_sized_and_read_only(cli):
    a = audit.sample_facts(cli, "g", 10, seed=1)
    b = audit.sample_facts(cli, "g", 10, seed=1)
    assert len(a) == 10 and [r["id"] for r in a] == [r["id"] for r in b]
    assert a[0]["episodes"][0]["text"] == "the source text" and a[0]["label"] is None
    log = Path(cli[0] + ".log").read_text()
    assert "CREATE" not in log and "DELETE" not in log and "SET" not in log.upper().replace("RELATES", "")


def test_main_writes_state_dir_file_and_refuses_repo(cli, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GESTALT_FALKORDB_CLI", cli[0])
    monkeypatch.setenv("GESTALT_STATE_DIR", str(tmp_path / "state"))
    assert audit.main(["-n", "5"]) == 0
    out = next((tmp_path / "state").glob("graphiti-audit-*.json"))
    assert len(json.loads(out.read_text())["rows"]) == 5
    assert audit.main(["-n", "5", "--out", str(audit.REPO / "evals" / "graphiti" / "x.json")]) == 2


def test_score_precision_and_wilson(tmp_path):
    rows = [{"id": str(i), "label": "correct"} for i in range(8)] + [
        {"id": "w", "label": "wrong"}, {"id": "u", "label": "unsupported"}, {"id": "n", "label": None}]
    s = audit.score({"rows": rows})
    assert s["labelled"] == 10 and s["unlabelled"] == 1 and s["precision"] == 0.8
    assert s["wilson95"][0] < 0.8 < s["wilson95"][1]
    with pytest.raises(ValueError):
        audit.score({"rows": [{"id": "x", "label": "maybe"}]})


def test_example_file_scores(capsys):
    ex = Path(audit.REPO / "evals" / "graphiti" / "audit-example.json")
    assert audit.score(json.loads(ex.read_text()))["labelled"] == 2
