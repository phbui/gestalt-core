#!/usr/bin/env python3
"""Read text aloud on this machine's PipeWire sink. Fully local.

Engine: Kokoro-82M (ONNX, af_heart) by default, Piper (en_US-lessac-medium) as
the fast fallback. Both measured on the mobile laptop 2026-10-16: Kokoro RTF
0.32, Piper RTF 0.04, so playback starts after the first sentence and never
waits on synthesis.

Pipeline: input -> speakable text -> pysbd sentences -> chunks -> synth thread
-> bounded queue -> one long-lived `pw-cat --raw` player. Pause is SIGSTOP on
the player, resume SIGCONT, stop SIGTERM on the reader. State lives under
$XDG_RUNTIME_DIR/<kb-entry> so a second invocation can control the first.

Usage:
  <kb-entry> [opts] FILE|URL|-            read a file, a web page, or stdin
  <kb-entry> --text "..."                 read a literal string
  <kb-entry> pause|resume|stop|status     control the running reader
  <kb-entry> --dry-run FILE               print the cleaned text, no audio
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

RUN_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or Path.home() / ".cache") / "<kb-entry>"
MODEL_DIR = Path(os.environ.get("READ_ALOUD_MODELS") or Path.home() / ".cache" / "<kb-entry>" / "models")
STATE = RUN_DIR / "state.json"
PAUSED = RUN_DIR / "paused"
LOG = RUN_DIR / "last.log"

MODELS = {
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.0.bin",
    "en_US-lessac-medium.onnx": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx",
    "en_US-lessac-medium.onnx.json": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json",
}
KOKORO_MAX_CHARS = 300  # phoneme cap is 510 tokens; 300 chars of English stays well under it
PIPER_MAX_CHARS = 600
SENTENCE_GAP_S = 0.12
PARAGRAPH_GAP_S = 0.45


# ---------------------------------------------------------------- text cleanup

def to_speakable(text: str) -> str:
    """Turn markdown / HTML / academic prose into something a voice can read."""
    t = text.replace("\r\n", "\n")
    t = re.sub(r"\A---\n.*?\n---\n", "", t, flags=re.S)  # YAML frontmatter
    t = re.sub(r"```.*?```", "\n(code block omitted)\n", t, flags=re.S)
    t = re.sub(r"~~~.*?~~~", "\n(code block omitted)\n", t, flags=re.S)
    t = re.sub(r"\$\$.*?\$\$", " (equation omitted) ", t, flags=re.S)
    t = re.sub(r"\\\[.*?\\\]", " (equation omitted) ", t, flags=re.S)
    t = re.sub(r"\$([^$\n]{1,80})\$", r"\1", t)  # inline math: speak the symbols as written
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", t, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", lambda m: f" image: {m.group(1)}. " if m.group(1) else " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)  # [text](url) -> text
    t = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", t)
    t = re.sub(r"^[ \t]*\[[^\]]+\]:\s*\S+.*$", "", t, flags=re.M)  # reference-style link defs
    t = re.sub(r"https?://\S+|www\.\S+", "", t)
    t = re.sub(r"\[\^[^\]]+\]", "", t)  # footnote marks
    t = re.sub(r"\[\d+(?:[,–\-\s]+\d+)*\]", "", t)  # numeric citations [12], [3-5]
    t = re.sub(r"`([^`\n]+)`", r"\1", t)
    t = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]*(.+?)[ \t]*#*[ \t]*$", lambda m: _as_sentence(m.group(1)) + "\n", t, flags=re.M)
    t = re.sub(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+", "", t, flags=re.M)  # list markers
    t = re.sub(r"^[ \t]*>[ \t]?", "", t, flags=re.M)  # blockquotes
    t = re.sub(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$\n?", "", t, flags=re.M)  # table rules
    t = re.sub(r"^[ \t]*\|(.+)\|[ \t]*$", lambda m: ", ".join(c.strip() for c in m.group(1).split("|") if c.strip()) + ".", t, flags=re.M)
    t = re.sub(r"^[ \t]*[-*_]{3,}[ \t]*$", "\n", t, flags=re.M)  # horizontal rules
    t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)
    t = re.sub(r"(?<!\w)(\*|_)(?!\s)(.+?)(?<!\s)\1(?!\w)", r"\2", t)
    t = re.sub(r"\s*(?:—|--)\s*", ", ", t).replace("–", " to ")
    t = re.sub(r"\s+([.,;:!?])", r"\1", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _as_sentence(s: str) -> str:
    s = s.strip()
    return s if not s or s[-1] in ".!?:" else s + "."


# ------------------------------------------------------------------- chunking

def split_sentences(text: str) -> list[str]:
    """Sentences, with '' markers at paragraph breaks."""
    import pysbd
    seg = pysbd.Segmenter(language="en", clean=False)
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = " ".join(para.split())
        if not para:
            continue
        out.extend(s.strip() for s in seg.segment(para) if s.strip())
        out.append("")
    while out and out[-1] == "":
        out.pop()
    return out


def _hard_split(s: str, limit: int) -> list[str]:
    """A sentence over the limit: cut at clause punctuation, then at words."""
    if len(s) <= limit:
        return [s]
    parts, cur = [], ""
    for piece in re.split(r"(?<=[,;:])\s+", s):
        if len(cur) + len(piece) + 1 <= limit:
            cur = f"{cur} {piece}".strip()
        else:
            if cur:
                parts.append(cur)
            cur = piece
    if cur:
        parts.append(cur)
    out = []
    for p in parts:
        if len(p) <= limit:
            out.append(p)
        else:
            words, cur = p.split(), ""
            for w in words:
                if len(cur) + len(w) + 1 <= limit:
                    cur = f"{cur} {w}".strip()
                else:
                    out.append(cur)
                    cur = w
            if cur:
                out.append(cur)
    return out


def make_chunks(sentences: list[str], limit: int) -> list[tuple[str, float]]:
    """Pack sentences into chunks under `limit` chars. Returns (text, gap_after_s)."""
    chunks: list[tuple[str, float]] = []
    cur = ""
    for s in sentences:
        if s == "":
            if cur:
                chunks.append((cur, PARAGRAPH_GAP_S))
                cur = ""
            elif chunks:
                chunks[-1] = (chunks[-1][0], PARAGRAPH_GAP_S)
            continue
        for piece in _hard_split(s, limit):
            if cur and len(cur) + len(piece) + 1 > limit:
                chunks.append((cur, SENTENCE_GAP_S))
                cur = piece
            else:
                cur = f"{cur} {piece}".strip()
    if cur:
        chunks.append((cur, SENTENCE_GAP_S))
    return chunks


# ------------------------------------------------------------------- engines

def ensure_models(names: list[str]) -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for n in names:
        p = MODEL_DIR / n
        if p.exists() and p.stat().st_size > 0:
            continue
        print(f"downloading {n} ...", file=sys.stderr, flush=True)
        tmp = p.with_suffix(p.suffix + ".part")
        urllib.request.urlretrieve(MODELS[n], tmp)
        tmp.rename(p)


class KokoroEngine:
    rate = 24000
    limit = KOKORO_MAX_CHARS

    def __init__(self, voice: str, speed: float):
        ensure_models(["kokoro-v1.0.onnx", "voices-v1.0.bin"])
        from kokoro_onnx import Kokoro
        self.k = Kokoro(str(MODEL_DIR / "kokoro-v1.0.onnx"), str(MODEL_DIR / "voices-v1.0.bin"))
        self.voice, self.speed = voice or "af_heart", speed

    def synth(self, text: str) -> bytes:
        import numpy as np
        samples, sr = self.k.create(text, voice=self.voice, speed=self.speed, lang="en-us")
        assert sr == self.rate, sr
        return (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()


class PiperEngine:
    limit = PIPER_MAX_CHARS

    def __init__(self, voice: str, speed: float):
        name = voice or "en_US-lessac-medium"
        ensure_models([f"{name}.onnx", f"{name}.onnx.json"])
        from piper import PiperVoice, SynthesisConfig
        self.v = PiperVoice.load(str(MODEL_DIR / f"{name}.onnx"))
        self.cfg = SynthesisConfig(length_scale=1.0 / speed)
        self.rate = self.v.config.sample_rate

    def synth(self, text: str) -> bytes:
        return b"".join(c.audio_int16_bytes for c in self.v.synthesize(text, self.cfg))


ENGINES = {"kokoro": KokoroEngine, "piper": PiperEngine}


# -------------------------------------------------------------------- player

def open_player(rate: int) -> subprocess.Popen:
    if shutil.which("pw-cat"):
        cmd = ["pw-cat", "--playback", "--raw", "--rate", str(rate), "--channels", "1",
               "--format", "s16", "--latency", "100ms", "-"]
    elif shutil.which("aplay"):
        cmd = ["aplay", "-q", "-r", str(rate), "-f", "S16_LE", "-c", "1", "-"]
    else:
        sys.exit("no pw-cat or aplay on PATH")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------- state

def write_state(**kw) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    cur = read_state() or {}
    cur.update(kw)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur))
    tmp.rename(STATE)


def read_state() -> dict | None:
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def running_reader() -> dict | None:
    st = read_state()
    if st and alive(st.get("pid", -1)):
        return st
    return None


def control(cmd: str) -> int:
    st = running_reader()
    if cmd == "status":
        if not st:
            print("idle")
            return 0
        cur = min(st["done"] + 1, st["total"])
        pct = 100 * st["done"] // max(st["total"], 1)
        state = "paused" if PAUSED.exists() else "reading"
        print(f"{state} {st['title']!r}: chunk {cur} of {st['total']} ({pct}% done), engine {st['engine']}, pid {st['pid']}")
        return 0
    if not st:
        print("nothing is being read", file=sys.stderr)
        return 1
    player = st.get("player_pid")
    if cmd == "pause":
        if player and alive(player):
            os.kill(player, signal.SIGSTOP)
        PAUSED.touch()
        print("paused")
    elif cmd == "resume":
        if player and alive(player):
            os.kill(player, signal.SIGCONT)
        PAUSED.unlink(missing_ok=True)
        print("resumed")
    elif cmd == "stop":
        os.kill(st["pid"], signal.SIGTERM)
        for _ in range(30):
            if not alive(st["pid"]):
                break
            time.sleep(0.1)
        if player and alive(player):
            os.kill(player, signal.SIGKILL)
        PAUSED.unlink(missing_ok=True)
        STATE.unlink(missing_ok=True)
        print("stopped")
    return 0


# --------------------------------------------------------------------- input

def fetch_url(url: str) -> str:
    import trafilatura
    html = trafilatura.fetch_url(url)
    if not html:
        sys.exit(f"could not fetch {url}")
    txt = trafilatura.extract(html, include_tables=True, include_comments=False)
    if not txt:
        sys.exit(f"no article text found at {url}")
    return txt


def read_clipboard() -> str:
    for cmd in (["wl-paste", "--no-newline"], ["xclip", "-o", "-selection", "clipboard"], ["xsel", "-ob"]):
        if shutil.which(cmd[0]):
            return subprocess.run(cmd, capture_output=True, text=True).stdout
    sys.exit("no clipboard tool (wl-paste/xclip/xsel) on PATH")


def load_input(args) -> tuple[str, str]:
    if args.text is not None:
        return args.text, "text"
    src = args.source
    if src == "clipboard":
        return read_clipboard(), "clipboard"
    if src in (None, "-"):
        return sys.stdin.read(), "stdin"
    if re.match(r"https?://", src):
        return fetch_url(src), src
    p = Path(src).expanduser()
    if not p.exists():
        sys.exit(f"no such file: {src}")
    if p.suffix.lower() == ".pdf":
        if not shutil.which("pdftotext"):
            sys.exit("pdftotext not on PATH (poppler-utils)")
        return subprocess.run(["pdftotext", "-layout", str(p), "-"], capture_output=True, text=True).stdout, p.name
    return p.read_text(errors="replace"), p.name


# ---------------------------------------------------------------------- read

def do_read(args, text: str, title: str) -> int:
    clean = to_speakable(text)
    if args.dry_run:
        print(clean)
        return 0
    if not clean:
        sys.exit("nothing to read after cleanup")
    eng_cls = ENGINES[args.engine]
    chunks = make_chunks(split_sentences(clean), eng_cls.limit)
    if not chunks:
        sys.exit("nothing to read after cleanup")
    words = len(clean.split())
    est_min = words / 150 / args.speed

    if args.daemon:
        if running_reader():
            control("stop")
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, __file__, "--engine", args.engine, "--speed", str(args.speed),
               "--title", title, "--no-daemon", "--text-stdin"]
        if args.voice:
            cmd += ["--voice", args.voice]
        if args.save:
            cmd += ["--save", args.save]
        log = open(LOG, "wb")
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log, stderr=log, start_new_session=True)
        p.stdin.write(clean.encode())
        p.stdin.close()
        est = f"{est_min:.0f} min" if est_min >= 1 else f"{est_min * 60:.0f} s"
        print(f"reading {title!r}: {len(chunks)} chunks, ~{words} words, about {est} at {args.speed}x "
              f"({args.engine}); control with <kb-entry> pause|resume|stop|status; log {LOG}")
        return 0

    # foreground reader
    engine = eng_cls(args.voice, args.speed)
    player = open_player(engine.rate)
    write_state(pid=os.getpid(), player_pid=player.pid, title=title, engine=args.engine,
                total=len(chunks), done=0, started=time.time(), words=words)
    PAUSED.unlink(missing_ok=True)
    q: queue.Queue = queue.Queue(maxsize=4)
    stop = threading.Event()
    save_f = open(args.save, "wb") if args.save else None

    def on_term(*_):
        stop.set()
        try:
            player.kill()
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    def producer():
        try:
            for text_chunk, gap in chunks:
                if stop.is_set():
                    break
                pcm = engine.synth(text_chunk)
                pcm += b"\0" * (2 * int(engine.rate * gap))
                q.put(pcm)
        except Exception as e:  # noqa: BLE001
            print(f"synth error: {e}", file=sys.stderr)
        finally:
            q.put(None)

    threading.Thread(target=producer, daemon=True).start()
    done = 0
    rc = 0
    try:
        while True:
            pcm = q.get()
            if pcm is None or stop.is_set():
                break
            try:
                player.stdin.write(pcm)
                player.stdin.flush()
            except BrokenPipeError:
                rc = 1
                break
            if save_f:
                save_f.write(pcm)
            done += 1
            write_state(done=done)
    finally:
        try:
            player.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        player.wait()
        if save_f:
            save_f.close()
            _wrap_wav(args.save, engine.rate)
        PAUSED.unlink(missing_ok=True)
        STATE.unlink(missing_ok=True)
    return rc


def _wrap_wav(path: str, rate: int) -> None:
    import wave
    raw = Path(path).read_bytes()
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(raw)


# ---------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", help="file, URL, 'clipboard', '-' for stdin, or pause|resume|stop|status")
    ap.add_argument("--text", help="read this literal string")
    ap.add_argument("--text-stdin", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--engine", choices=ENGINES, default=os.environ.get("READ_ALOUD_ENGINE", "kokoro"))
    ap.add_argument("--voice", default=os.environ.get("READ_ALOUD_VOICE"))
    ap.add_argument("--speed", type=float, default=float(os.environ.get("READ_ALOUD_SPEED", "1.0")))
    ap.add_argument("--title", default=None)
    ap.add_argument("--save", help="also write the audio to this .wav")
    ap.add_argument("--dry-run", action="store_true", help="print the cleaned text and exit")
    ap.add_argument("--no-daemon", dest="daemon", action="store_false", help="block until done (default: detach)")
    ap.add_argument("--host", help="speak on another fleet node over ssh (text is cleaned here first)")
    args = ap.parse_args(argv)

    if args.source in ("pause", "resume", "stop", "status"):
        return control(args.source)
    if args.text_stdin:
        text, title = sys.stdin.read(), args.title or "text"
        args.daemon = False
        return do_read(args, text, title)
    text, title = load_input(args)
    title = args.title or title
    if args.host:
        clean = to_speakable(text)
        remote = f"~/Documents/GitHub/gestalt/tools/<kb-entry> --engine {args.engine} --speed {args.speed} --title {json.dumps(title)} -"
        if args.voice:
            remote += f" --voice {args.voice}"
        r = subprocess.run(["ssh", args.host, remote], input=clean, text=True)
        return r.returncode
    return do_read(args, text, title)


if __name__ == "__main__":
    sys.exit(main())
