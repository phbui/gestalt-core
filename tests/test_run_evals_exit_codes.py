"""evals/run_evals.py exit codes (2026-10-11 line review): a typo, an empty assertion list and a skip-only run are failures, not passes."""
from __future__ import annotations

import sys

import pytest
from conftest import _load


@pytest.fixture
def ev(tmp_path, monkeypatch):
    mod = _load("run_evals_exit_codes", "evals/run_evals.py")
    monkeypatch.setattr(mod, "CONFIGS_DIR", tmp_path / "configs")
    (tmp_path / "configs").mkdir()
    return mod


def test_an_unknown_subcommand_exits_one(ev, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["run_evals.py", "baselin", "check"])
    with pytest.raises(SystemExit) as e:
        ev.main()
    assert e.value.code == 1 and "Usage" in capsys.readouterr().out


def test_a_config_with_no_assertions_is_a_failure_not_a_pass(ev, tmp_path):
    (ev.CONFIGS_DIR / "x.yaml").write_text("skill: x\n")
    out = tmp_path / "out.txt"
    out.write_text("hello\n")
    with pytest.raises(SystemExit) as e:
        ev.run_skill("x", str(out))
    assert e.value.code == 1


def test_a_run_where_every_assertion_was_skipped_is_a_failure(ev, tmp_path):
    (ev.CONFIGS_DIR / "x.yaml").write_text("skill: x\noutput_assertions:\n  - type: regexx\n    value: h\n    description: typo\n")
    out = tmp_path / "out.txt"
    out.write_text("hello\n")
    with pytest.raises(SystemExit) as e:
        ev.run_skill("x", str(out))
    assert e.value.code == 1
