---
name: read
description: "Read text aloud on this machine's speaker, in full, with a local neural voice (Kokoro-82M, Piper fallback). Input is a file, URL, PDF, pasted text, or the previous reply. Also pause|resume|stop|status. Chain after /prompt or a summary to hear the result."
disable-model-invocation: true
---

# Read

Speak the input aloud, in full, on the speaker of the node this seat runs on. The voice is local (Kokoro-82M ONNX on CPU, 3x faster than real time on the mobile laptop, Piper as the fast fallback). Nothing leaves the machine. Runbook and measurements: the-relevant-entry.

The tool is `gestalt/tools/the-relevant-entry`. It detaches by default and returns at once. A long document keeps playing after this turn ends.

## Input

`/read <what> [flags]` where `<what>` is one of:

- a file path (`.md`, `.txt`, `.html`, `.pdf`)
- a URL (article text is extracted first)
- a literal string
- `this`, `that`, `it`, `the above`, or nothing: the previous assistant reply in this conversation
- `pause`, `resume`, `stop`, `status`: control the running reader

Flags pass straight through: `--speed 1.2`, `--engine piper`, `--voice af_sky`, `--host laptop` (speak on another node), `--save out.wav`.

## Process (all steps mandatory)

1. Control words first. If the argument is `pause`, `resume`, `stop` or `status`, run `gestalt/tools/the-relevant-entry <word>` and print its one line. Done.
2. Resolve the input.
   - File or URL: pass it as-is.
   - Literal text under 500 characters: pass with `--text "..."`.
   - Longer text, or the previous reply: write it verbatim to `~/artifacts/the-relevant-entry/<slug>.md` (slug from the first heading or the first five words, kebab-case) and pass the path. Never summarize, trim or rephrase. "Read in full" means every sentence.
3. Run `gestalt/tools/the-relevant-entry <input> [flags]`. On the first use of a node it downloads about 420 MB of models to `~/.cache/the-relevant-entry/models` and says so.
4. Report the tool's own line (title, chunk count, word count, estimated minutes) plus the control hint: `/read pause|resume|stop`. One or two lines. No summary of the content.
5. If the tool reports an error, quote it and name the fix. Do not fall back to printing the text.

## Chaining

`/prompt summarize X and read it to me`: /prompt produces the summary in chat, then invokes this skill with `this`. The summary is what gets read, in full. The same holds for any skill whose result Phi wants spoken: produce the text, then `/read this`.

## Rules

- Read, never paraphrase. The only transformation is the tool's own markdown cleanup (headings become sentences, links keep their text, code blocks and display equations are announced as omitted, tables are read row by row).
- One reader at a time. Starting a new read stops the running one, and the tool says so.
- Speaking happens on the node where the seat runs. Use `--host <node>` when Phi asks for another speaker.
