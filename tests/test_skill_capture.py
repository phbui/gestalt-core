"""X13 (2026-10-06): tools/capture-skill-run.py and the `captures` mode of evals/run_evals.py.

The transcript is synthetic. Secret-shaped strings are assembled at run time so the repo's secret scan never sees a
literal one.
"""
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cap = _load("capture_skill_run", "tools/capture-skill-run.py")
ev = _load("run_evals_mod", "evals/run_evals.py")

FAKE_GH = "gh" + "p_" + "A1" * 18
FAKE_SK = "sk-" + "ant-" + "Zz9" * 8
FAKE_EMAIL = "pat.private" + "@" + "gmail.com"


def _u(text, **kw):
    return {"type": "user", "timestamp": "2026-10-01T10:00:00Z", "message": {"role": "user", "content": text}, **kw}


def _a(blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def _t(text):
    return {"type": "text", "text": text}


def _tool():
    return {"type": "tool_use", "name": "Bash", "id": "x", "input": {"command": "ls"}}


def _write(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path


@pytest.fixture
def transcript(tmp_path):
    return _write(tmp_path / "abcd1234-0000.jsonl", [
        _u("hello"),
        _a([_t("hi")]),
        _u("<command-message>prompt</command-message>\n<command-name>/prompt</command-name>\n<command-args>x</command-args>"),
        _u("Base directory for this skill: /x", isMeta=True),
        _a([_t("Working on it, scanning.")]),
        _a([_tool()]),
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "SECRET TOOL RESULT"}]}},
        _a([{"type": "thinking", "thinking": "private thoughts"}, _t("### Team Manifest\n- a\n\nExecute this? (y/n)")]),
        {"type": "assistant", "isSidechain": True, "message": {"role": "assistant", "content": [_t("sidechain noise")]}},
        _u("y"),
        _a([_t("later reply")]),
    ])


def test_extracts_final_message_only(transcript, tmp_path):
    out = cap.capture(transcript, "prompt", out=tmp_path / "out" / "c.md")
    body = out.read_text()
    assert "### Team Manifest" in body and "Execute this? (y/n)" in body
    for leak in ("scanning", "SECRET TOOL RESULT", "private thoughts", "sidechain noise", "later reply"):
        assert leak not in body
    assert "skill: prompt" in body and "abcd1234-0000" in body and "redactions: 0" in body


def test_all_text_keeps_earlier_messages_but_not_tool_results(transcript, tmp_path):
    body = cap.capture(transcript, "prompt", all_text=True, out=tmp_path / "c.md").read_text()
    assert "scanning" in body and "Team Manifest" in body
    assert "SECRET TOOL RESULT" not in body and "later reply" not in body


def test_skill_tool_call_starts_a_run(tmp_path):
    t = _write(tmp_path / "ffff0000.jsonl", [
        _u("please research it"),
        _a([{"type": "tool_use", "name": "Skill", "id": "s", "input": {"skill": "gestalt:research"}}]),
        _a([_t("### Overview\nanswer")]),
    ])
    assert "### Overview" in cap.capture(t, "research", out=tmp_path / "r.md").read_text()


def test_default_destination_is_the_captures_dir(transcript, tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "CAPTURES", tmp_path / "personal" / "eval-captures")
    out = cap.capture(transcript, "prompt")
    assert out == tmp_path / "personal" / "eval-captures" / "prompt" / "2026-10-01-abcd1234.md"


def test_refuses_to_write_into_evals(transcript):
    with pytest.raises(SystemExit):
        cap.capture(transcript, "prompt", out=REPO / "evals" / "captured" / "prompt.md")
    assert not (REPO / "evals" / "captured" / "prompt.md").exists()


def test_missing_run_exits(transcript, tmp_path):
    with pytest.raises(SystemExit):
        cap.capture(transcript, "audit", out=tmp_path / "x.md")


@pytest.mark.parametrize("secret,label", [
    (FAKE_GH, "token"),
    (FAKE_SK, "token"),
    ("Bearer " + "Q" * 30, "REDACTED"),
    ("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----", "private-key"),
    ("https://ntfy.sh/" + "my-topic-" + "abc123", "ntfy.sh/[REDACTED]"),
    ("NTFY_TOPIC=" + "secret-topic-1", "NTFY_TOPIC=[REDACTED]"),
    ("host box.tail1234" + ".ts.net up", "tailnet-host"),
    ("ip 100.64.12.9 up", "tailnet-ip"),
    ("MY_API_TOKEN=" + "hunter2hunter2", "MY_API_TOKEN=[REDACTED]"),
    (FAKE_EMAIL, "email"),
    ("blob " + "a1" * 25, "long-secret"),
])
def test_redacts_each_secret_kind(secret, label):
    out, n = cap.redact(f"before {secret} after")
    assert n >= 1
    assert label in out
    core = secret.split()[-1] if secret.startswith("Bearer") else secret
    assert core not in out


def test_generic_email_and_plain_prose_survive():
    text = "Co-Authored-By: Claude <noreply@anthropic.com> and you@example.com see tools/fleet/bin/fleet-check and 192 files"
    out, n = cap.redact(text)
    assert out == text and n == 0


def test_redaction_count_lands_in_header(tmp_path):
    t = _write(tmp_path / "eeee1111.jsonl", [_u("<command-name>/capture</command-name>"), _a([_t(f"captured idea: {FAKE_GH}")])])
    body = cap.capture(t, "capture", out=tmp_path / "c.md").read_text()
    assert "redactions: 1" in body and FAKE_GH not in body


def test_latest_finds_newest_run(transcript, tmp_path):
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    (proj / transcript.name).write_text(transcript.read_text())
    assert cap.latest_transcript("prompt", tmp_path / "projects") == proj / transcript.name
    with pytest.raises(SystemExit):
        cap.latest_transcript("audit", tmp_path / "projects")


# ---- runner behaviour -------------------------------------------------------------------------------------

GOOD_PROMPT = "### Team Manifest\n### Enhanced Request\nExecute this? (y/n)\n"


def test_captures_mode_counts_pass_and_fail(tmp_path, monkeypatch, capsys):
    d = tmp_path / "prompt"
    d.mkdir()
    (d / "a.md").write_text(GOOD_PROMPT)
    (d / "b.md").write_text("nothing useful")
    monkeypatch.setattr(ev, "PRIVATE_CAPTURES_DIR", tmp_path)
    ev.cmd_captures("prompt")
    out = capsys.readouterr().out
    assert "prompt: 3 passed, 3 failed over 2 capture(s)" in out
    assert "b.md: 0 passed, 3 failed" in out


def test_strict_exits_nonzero_on_failure(tmp_path, monkeypatch):
    d = tmp_path / "prompt"
    d.mkdir()
    (d / "b.md").write_text("nothing useful")
    monkeypatch.setattr(ev, "PRIVATE_CAPTURES_DIR", tmp_path)
    with pytest.raises(SystemExit):
        ev.cmd_captures("prompt", strict=True)


def test_missing_dir_skips_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ev, "PRIVATE_CAPTURES_DIR", tmp_path / "absent")
    ev.cmd_captures(None, strict=True)
    assert "No readable captures" in capsys.readouterr().out


def test_encrypted_files_are_skipped(tmp_path, monkeypatch, capsys):
    d = tmp_path / "prompt"
    d.mkdir()
    (d / "a.md").write_bytes(b"\x00GITCRYPT\x00" + b"\xff" * 40)
    monkeypatch.setattr(ev, "PRIVATE_CAPTURES_DIR", tmp_path)
    assert ev.private_captures("prompt") == []
    ev.cmd_captures("prompt", strict=True)
    assert "No readable captures" in capsys.readouterr().out


def test_baseline_ignores_private_captures(tmp_path, monkeypatch):
    d = tmp_path / "prompt"
    d.mkdir()
    (d / "a.md").write_text(GOOD_PROMPT)
    monkeypatch.setattr(ev, "PRIVATE_CAPTURES_DIR", tmp_path)
    assert ev._skill_deterministic_entry("prompt")["status"] == "not_captured"
