"""precision_study.py: pairing, the decision rule, rank flips, resume and dry-run. Synthetic result files only, no model and no subprocess."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals" / "retrieval"))
sys.path.insert(0, str(REPO / "tools"))
pytest.importorskip("numpy")
import precision_study as ps  # noqa: E402

DATASET = "beir/toy"
SYSTEM = "hybrid"
N = 60
BASE = [0.5 + 0.005 * (i % 20) for i in range(N)]


def write_arm(out: Path, name: str, scores: list[float], run: dict[str, list[str]] | None = None, docs: int = 1000, secs: float = 10.0) -> None:
    d = out / name
    d.mkdir(parents=True, exist_ok=True)
    ids = [f"q{i}" for i in range(N)]
    result = {"dataset": DATASET, "metric": "nDCG@10", "documents": docs, "queries": N, "query_ids": ids,
              "systems": {SYSTEM: {"ndcg10": sum(scores) / N, "per_query": scores}},
              "seconds": {"index_and_embed": secs, "search_all_queries": 1.0}}
    (d / "beir-toy.json").write_text(json.dumps(result))
    run = run or {q: [f"d{j}" for j in range(10)] for q in ids}
    (d / f"beir-toy.{SYSTEM}.run").write_text("".join(f"{q} Q0 {doc} {r} {10 - r} t\n" for q, docs_ in run.items() for r, doc in enumerate(docs_, 1)))


def shifted(delta: float, wobble: float = 0.0) -> list[float]:
    return [s + delta + (wobble if i % 2 else -wobble) for i, s in enumerate(BASE)]


def study(out: Path, arms: list[str]) -> dict:
    return ps.evaluate("embed", out, arms, [DATASET], SYSTEM)


def base_world(out: Path, noise: float = 0.0005) -> None:
    write_arm(out, "ref", BASE, secs=10)
    write_arm(out, "ref-b16", shifted(0, noise))
    write_arm(out, "ref-b64", shifted(0, -noise))


def test_a_close_arm_passes_and_a_worse_arm_fails(tmp_path):
    base_world(tmp_path)
    write_arm(tmp_path, "close", shifted(0, 0.0005), secs=4)
    write_arm(tmp_path, "worse", shifted(-0.01), secs=2)
    s = study(tmp_path, ["close", "worse"])
    assert s["margin"] == ps.MIN_MARGIN
    assert s["arms"]["close"]["pass"] is True and s["arms"]["worse"]["pass"] is False
    assert s["arms"]["worse"]["datasets"][DATASET]["ci95_bootstrap_difference"][0] == pytest.approx(-0.01, abs=1e-4)
    assert s["cheapest_passing_arm"] == "close" and "close" in s["verdict"] and "250.0 docs/s" in s["verdict"]


def test_the_cheapest_passing_arm_is_the_fastest_one(tmp_path):
    base_world(tmp_path)
    write_arm(tmp_path, "slowok", shifted(0, 0.0005), secs=8)
    write_arm(tmp_path, "fastok", shifted(0, 0.0005), secs=3)
    write_arm(tmp_path, "fastbad", shifted(-0.05), secs=1)
    assert study(tmp_path, ["slowok", "fastok", "fastbad"])["cheapest_passing_arm"] == "fastok"


def test_no_passing_arm_keeps_the_reference(tmp_path):
    base_world(tmp_path)
    write_arm(tmp_path, "bad", shifted(-0.05))
    s = study(tmp_path, ["bad"])
    assert s["cheapest_passing_arm"] is None and "Keep the reference" in s["verdict"]


def test_a_noise_floor_above_the_minimum_widens_the_margin(tmp_path):
    base_world(tmp_path, noise=0.03)
    floor, by = ps.noise_floor(tmp_path, [DATASET], SYSTEM)
    assert floor > ps.MIN_MARGIN and by[DATASET] == floor
    write_arm(tmp_path, "edge", shifted(-0.003))
    s = study(tmp_path, ["edge"])
    assert s["noise_floor"] == floor and s["margin"] == floor
    assert s["arms"]["edge"]["pass"] is True, "a lower bound of -0.003 passes a margin of the larger floor"
    # The same arm fails once the floor is small.
    base_world(tmp_path, noise=0.0005)
    assert study(tmp_path, ["edge"])["arms"]["edge"]["pass"] is False


def test_every_dataset_must_pass(tmp_path):
    base_world(tmp_path)
    write_arm(tmp_path, "ok", shifted(0, 0.0005))
    s = ps.evaluate("embed", tmp_path, ["ok"], [DATASET], SYSTEM)
    assert s["arms"]["ok"]["pass"] is True
    for n in ("ref", "ref-b16", "ref-b64", "ok"):  # a second dataset where the arm is worse
        src = json.loads((tmp_path / n / "beir-toy.json").read_text())
        src["dataset"] = "beir/other"
        if n == "ok":
            src["systems"][SYSTEM]["per_query"] = shifted(-0.05)
        (tmp_path / n / "beir-other.json").write_text(json.dumps(src))
        (tmp_path / n / f"beir-other.{SYSTEM}.run").write_text((tmp_path / n / f"beir-toy.{SYSTEM}.run").read_text())
    s = ps.evaluate("embed", tmp_path, ["ok"], [DATASET, "beir/other"], SYSTEM)
    assert s["arms"]["ok"]["datasets"][DATASET]["pass"] is True and s["arms"]["ok"]["datasets"]["beir/other"]["pass"] is False
    assert s["arms"]["ok"]["pass"] is False


def test_rank_flips_on_a_tiny_example(tmp_path):
    ref = tmp_path / "ref.run"
    arm = tmp_path / "arm.run"
    rows = {
        "same": (list("abcdefghij"), list("abcdefghij")),
        "swap": (list("abcdefghij"), list("bacdefghij")),
        "newdoc": (list("abcdefghij"), list("abcdefghik")),
    }
    ref.write_text("".join(f"{q} Q0 {d} {r} 0 t\n" for q, (a, _) in rows.items() for r, d in enumerate(a, 1)))
    arm.write_text("".join(f"{q} Q0 {d} {r} 0 t\n" for q, (_, b) in rows.items() for r, d in enumerate(b, 1)))
    f = ps.rank_flips(ref, arm)
    assert f["queries"] == 3
    assert f["order_differs"] == pytest.approx(2 / 3, abs=1e-4), "swap and newdoc differ in order"
    assert f["set_differs"] == pytest.approx(1 / 3, abs=1e-4), "only newdoc changes the set"
    assert ps.kendall_tau_b(list("abcdefghij"), list("abcdefghij")) == 1.0
    assert ps.kendall_tau_b(list("abc"), list("cba")) == -1.0
    swap_tau = ps.kendall_tau_b(*rows["swap"])
    assert swap_tau == pytest.approx(1 - 2 / 45, abs=1e-9), "one discordant pair of 45"
    assert f["mean_kendall_tau"] == pytest.approx((1.0 + swap_tau + ps.kendall_tau_b(*rows["newdoc"])) / 3, abs=1e-4)


def test_docs_per_second_comes_from_the_recorded_timings(tmp_path):
    base_world(tmp_path)
    write_arm(tmp_path, "fast", BASE, docs=1000, secs=5)
    assert study(tmp_path, ["fast"])["arms"]["fast"]["docs_per_s"] == 200.0
    rr = {"queries": 10, "query_log_header": {"rerank": {"depth": 40}}, "seconds": {"search_all_queries": 8.0}}
    assert ps.docs_per_second("rerank", [rr]) == 50.0
    assert ps.docs_per_second("embed", [{"documents": 0, "seconds": {}}]) is None


class Args:
    datasets = [DATASET]
    rerank_model = "qwen3-0.6b"
    rerank_depth = 40
    embed_profile = None
    max_docs = None
    dry_run = False


def test_resume_skips_a_complete_arm_and_runs_the_rest(tmp_path, monkeypatch):
    write_arm(tmp_path, "ref", BASE)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, env=None: calls.append((cmd, env)) or subprocess.CompletedProcess(cmd, 0))
    arms = ps.plan("embed", ["float16"])
    status = ps.run_arms("embed", tmp_path, arms, Args(), SYSTEM)
    assert status["ref"]["status"] == "skipped"
    assert status["ref-b16"]["status"] == "failed", "the stub wrote no result, so the arm does not validate"
    ran = [c[0][c[0].index("--out") + 1] for c in calls]
    assert str(tmp_path / "ref") not in ran and str(tmp_path / "float16") in ran
    env16 = next(e for c, e in calls if str(tmp_path / "float16") in c)
    assert env16["GESTALT_EMBED_DTYPE"] == "float16"


def test_an_invalid_result_is_not_complete(tmp_path):
    (tmp_path / "ref").mkdir()
    (tmp_path / "ref" / "beir-toy.json").write_text("{not json")
    assert ps.arm_complete(tmp_path / "ref", [DATASET], SYSTEM) is False
    write_arm(tmp_path, "void", BASE)
    p = tmp_path / "void" / "beir-toy.json"
    r = json.loads(p.read_text())
    r["rerank_fallback_queries"] = 3
    p.write_text(json.dumps(r))
    assert ps.arm_complete(tmp_path / "void", [DATASET], SYSTEM) is False


def test_child_env_clears_inherited_knobs_and_sets_the_arm_knobs():
    base = {"PATH": "/bin", "GESTALT_TF32": "1", "GESTALT_EMBED_DTYPE": "float16"}
    env = ps.child_env(base, {"GESTALT_RERANK_DTYPE": "bfloat16"}, {"GESTALT_RERANK_BATCH": "16"})
    assert env == {"PATH": "/bin", "GESTALT_RERANK_DTYPE": "bfloat16", "GESTALT_RERANK_BATCH": "16"}


def test_arms_run_in_order_and_bad_specs_are_refused():
    names = [n for n, _, _ in ps.plan("embed", ["float16", "t=GESTALT_TF32=1,GESTALT_EMBED_DTYPE=float32"])]
    assert names == ["ref", "ref-b16", "ref-b64", "float16", "t"]
    for bad in (["nonsense"], ["ref"], ["x=GESTALT_NOPE=1"], ["float16", "float16"]):
        with pytest.raises(ValueError):
            ps.plan("embed", bad)


def test_dry_run_prints_the_commands_and_runs_nothing(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("the dry run started a process")

    monkeypatch.setattr(subprocess, "run", boom)
    rc = ps.main(["--role", "rerank", "--out", str(tmp_path / "o"), "--arms", "bfloat16", "tf32", "--dry-run"])
    out = capsys.readouterr().out.splitlines()
    assert rc == 0 and len(out) == 5
    assert out[0].startswith("ref: ") and "--rerank on" in out[0] and "beir_bench.py" in out[0]
    assert out[1].startswith("ref-b16: GESTALT_RERANK_BATCH=16 ")
    assert out[3].startswith("bfloat16: GESTALT_RERANK_DTYPE=bfloat16 ")
    assert out[4].startswith("tf32: GESTALT_RERANK_DTYPE=float32 GESTALT_TF32=1 ")
    assert not (tmp_path / "o").exists()
    ps.main(["--role", "embed", "--out", str(tmp_path / "o"), "--dry-run"])
    emb = capsys.readouterr().out.splitlines()
    assert "--batch-size 16" in emb[1] and "--rerank" not in emb[0] and emb[3].startswith("float16: GESTALT_EMBED_DTYPE=float16 ")
    assert str(tmp_path / "o" / "float16" / "work") in emb[3], "embed arms get their own work directory"
