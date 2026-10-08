#!/usr/bin/env python3
"""The "am I already in the venv?" check must use sys.prefix, not a resolved interpreter path.

A venv's bin/python3 is a symlink chain to the base interpreter, so
Path(venv_py).resolve() yields /usr/bin/python3.N. Comparing that against
Path(sys.executable).resolve() is therefore TRUE for any python3 on the system, so the guard
concluded it was already inside the venv and never re-executed. A hook calling plain python3
then built an FTS-only index while believing it had vectors, which is what happened on the hub
on 2026-09-04 and is the most likely mechanism behind the earlier two-week FTS-only period.

The test asserts the property that actually matters, in both directions, without needing to
run a build: outside the venv the guard must re-exec, inside it must not, or it would loop.
"""
import subprocess, sys
from pathlib import Path

VENV = Path("~/.claude/gestalt/venv/bin/python3").expanduser()
fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}: {label}: {got}" + ("" if ok else f"  wanted {want}"))
    if not ok:
        fails += 1


if not VENV.exists():
    print("  SKIP: no gestalt venv on this node"); sys.exit(0)

PROBE = (
    "import sys;from pathlib import Path;"
    "v=Path('~/.claude/gestalt/venv/bin/python3').expanduser();"
    "print('prefix', Path(sys.prefix).resolve()==v.parent.parent.resolve(),"
    "'resolved', Path(sys.executable).resolve()==v.resolve())"
)


def probe(interp):
    out = subprocess.run([interp, "-c", PROBE], capture_output=True, text=True, timeout=60).stdout.split()
    return {"prefix": out[1] == "True", "resolved": out[3] == "True"}


outside = probe("python3")
inside = probe(str(VENV))

print("--- the old resolved-path check is broken in the dangerous direction ---")
check("outside the venv, resolved-path check wrongly says 'in venv'", outside["resolved"], True)

print("--- sys.prefix answers correctly in both directions ---")
check("outside the venv, prefix check says 'not in venv'", outside["prefix"], False)
check("inside the venv, prefix check says 'in venv'", inside["prefix"], True)

print("--- and the shipped builder uses the prefix form ---")
src = Path(__file__).resolve().parents[1] / "gestalt-index-builder.py"
text = src.read_text()
check("builder compares sys.prefix", "Path(sys.prefix).resolve() == venv_py.parent.parent.resolve()" in text, True)
check("builder no longer compares resolved sys.executable to venv_py",
      "Path(sys.executable).resolve() == venv_py.resolve()" in text, False)

print("PASS" if fails == 0 else "FAIL")
sys.exit(1 if fails else 0)
