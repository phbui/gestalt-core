"""The pure parts of evals/retrieval/qwen_rerank_check.py: pair picking, run reading, the card prompt, Spearman."""
from __future__ import annotations

import sys
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals" / "retrieval"))
import qwen_rerank_check as qc

Q = namedtuple("Q", "query_id text")


def test_pairs_take_the_top_relevant_and_the_best_ranked_non_relevant(tmp_path):
    run = tmp_path / "r.run"
    run.write_text("q1 Q0 d9 1 9.0 x\nq1 Q0 d1 2 8.0 x\nq2 Q0 d2 1 5.0 x\nq3 Q0 d7 1 1.0 x\n")
    qrels = {"q1": {"d1": 1, "d3": 2}, "q2": {"d2": 1}, "q3": {"d5": 1, "d7": 0}}
    pairs = qc.pick_pairs(qrels, [Q("q2", "b"), Q("q1", "a"), Q("q3", "c")], qc.read_run(run), 5)
    assert pairs == [("q1", "a", "d3", "d9"), ("q3", "c", "d5", "d7")]  # q2 has no ranked non-relevant document, so it is skipped
    assert qc.pick_pairs(qrels, [Q("q1", "a"), Q("q3", "c")], qc.read_run(run), 1) == [("q1", "a", "d3", "d9")]


def test_card_prompt_and_spearman():
    assert qc.card_body("I", "Q", "D") == "<Instruct>: I\n<Query>: Q\n<Document>: D"
    assert qc.CARD_SUFFIX.endswith("<think>\n\n</think>\n\n") and qc.CARD_PREFIX.startswith("<|im_start|>system\n")
    assert qc.spearman([1, 2, 3], [10, 20, 30]) == 1.0
    assert qc.spearman([1, 2, 3], [3, 2, 1]) == -1.0
