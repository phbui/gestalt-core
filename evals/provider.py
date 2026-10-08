#!/usr/bin/env python3
"""
promptfoo custom Python provider — pre-captured-output pattern.

No network call: promptfoo's Python provider contract is
`call_api(prompt, options, context) -> {"output": <string>}` (or `{"error": ...}`),
with `context["vars"]` carrying the test's vars. This provider ignores the
rendered prompt entirely and instead reads a previously captured skill
transcript from evals/captured/<skill_case_id>.txt (or .md) and returns it
verbatim. promptfoo's deterministic assertions (contains/regex/not-contains)
and the llm-rubric assertion then run against that captured text — this is
how the harness judges real skill output without re-running the skill or
making any live model call from promptfoo itself.

Capture workflow (see evals/README.md):
  1. Run the skill for real (e.g. `/investigate ...`).
  2. Save its full output to evals/captured/investigate.txt (or .md).
  3. `npx promptfoo@latest eval -c evals/promptfoo/investigate.yaml -o results.json`
"""
from pathlib import Path

CAPTURED_DIR = Path(__file__).parent / "captured"
CAPTURE_EXTENSIONS = (".txt", ".md")


def call_api(prompt, options, context):
    skill_case_id = (context or {}).get("vars", {}).get("skill_case_id")
    if not skill_case_id:
        return {"error": "test has no skill_case_id var — check the exported promptfoo config"}

    for ext in CAPTURE_EXTENSIONS:
        candidate = CAPTURED_DIR / f"{skill_case_id}{ext}"
        if candidate.exists():
            return {"output": candidate.read_text()}

    return {
        "error": (
            f"no captured output for {skill_case_id} — run the skill and save its output to "
            f"evals/captured/{skill_case_id}.txt (or .md)"
        )
    }
