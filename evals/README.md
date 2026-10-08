# Gestalt Skill Evals

Eval harness for gestalt skills. Every skill's SKILL.md has an Evals table (trigger accuracy + expected output shape); this directory turns those tables into runnable checks.

## Approach (60/30/10)

- **60% deterministic** — schema/format validation of skill outputs: does the output contain the required sections, citation slots, verdict tables? Regex + structural checks, no LLM.
- **30% LLM-as-judge** — a judge model scores output quality against the skill's "Expected output shape" column.
- **10% human** — spot-review of judge disagreements and new failure modes.

## Pipeline

All tiers read from the same source of truth: `configs/<skill>.yaml`. There's nothing to keep in sync by hand — the LLM-judge tier is generated from the deterministic config, and the trigger-routing check reads the same `trigger_cases` list `validate` already parses.

```
configs/<skill>.yaml  (hand-written, source of truth)
        |
        |-- run_evals.py validate ---------> config shape check, CI hard gate
        |-- run_evals.py run <skill> <out> -> output_assertions against one captured output
        |-- run_evals.py triggers [skill] --> trigger_cases routing accuracy (deterministic, tier 1)
        |-- run_evals.py baseline save|check -> evals/baseline.json regression gate
        |
        `-- run_evals.py export <skill> ---> promptfoo/<skill>.yaml
                                                      |
                                              captured/<skill>.txt  (real transcript)
                                                      |
                                        npx promptfoo@latest eval -c promptfoo/<skill>.yaml
                                                      |
                                              LLM-judge tier (30%): same assertions +
                                              one llm-rubric derived from the assertion
                                              descriptions
```

### 1. Deterministic tier — `run_evals.py` (local + CI hard gate)

Pure Python (stdlib + `pyyaml`), no network, no promptfoo dependency.

```bash
python3 evals/run_evals.py list                    # list configs with case counts
python3 evals/run_evals.py validate [skill]         # validate config YAML + required fields
python3 evals/run_evals.py run <skill> <output.txt> # run output_assertions against a captured output
python3 evals/run_evals.py triggers [skill] [--llm] # score trigger_cases routing accuracy
python3 evals/run_evals.py baseline save            # write evals/baseline.json
python3 evals/run_evals.py baseline check           # exit 1 on regression from baseline.json
```

`run` loads `configs/<skill>.yaml`, reads `<output.txt>` as the skill's rendered output, runs every `output_assertions` entry (`contains_section` / `regex` / `not_contains`) against it, prints a PASS/FAIL/SKIP table, and exits 1 if anything fails. `validate` only checks config shape (no output required) — this is what CI runs, as a hard gate, on every PR touching `.claude/skills/**` or `evals/**`.

`triggers` executes every config's `trigger_cases`: for each `{prompt, should_trigger}` pair it reads the target skill's own `SKILL.md` frontmatter `description`, extracts keywords from both (lowercased, stopword-filtered, 5-char-prefix stem matching so "investigate"/"investigation" still count as one signal), and predicts a trigger if any keyword overlaps. No API calls, no model — this is still tier 1. Measured at 78.9% aggregate accuracy across the 30 configs' 90 trigger cases (`python3 evals/run_evals.py triggers`) as of the initial `baseline.json`; false positives cluster on skills whose descriptions share generic verbs with prompts that should route elsewhere (e.g. `commit`, `promote`, `system`) — a real limitation of a keyword heuristic, not a bug to silently paper over. Pass `--llm` to prefer a model-graded judge instead; today it prints an explicit note and falls back to deterministic, because `evals/provider.py` only replays pre-captured transcripts (see its docstring) and has no live-model call path for classifying an arbitrary prompt — wiring a real `--llm` judge is future work, not a broken flag.

### 2. LLM-judge tier — export to promptfoo + llm-rubric

```bash
python3 evals/run_evals.py export [skill]   # writes evals/promptfoo/<skill>.yaml (all skills if omitted)
```

Each exported config:
- `providers: [{id: "file://../provider.py"}]` — a Python provider, no live model call from promptfoo's side (see `evals/provider.py`).
- `prompts: ["{{prompt}}"]` — passthrough; the provider ignores the rendered prompt text.
- One `tests` entry with `vars.skill_case_id: "<skill>"`, and an `assert` list that transliterates every `output_assertions` entry (`contains_section` → `contains`, `regex` → `regex`, `not_contains` → `not-contains`) plus one appended `llm-rubric` assertion synthesized from the assertion descriptions: *"Output follows the /\<skill\> skill's mandated structure: \<descriptions\>"*.
- The `llm-rubric` assertion has no `provider` set on purpose — the grader model is controlled globally (`defaultTest.options.provider` in a wrapping config, or `--grader` on the CLI), not hardcoded per skill.

### 3. Capture workflow

promptfoo needs something to grade. Rather than re-running skills live inside promptfoo, we grade real transcripts:

1. Run a skill for real (e.g. `/investigate ...`) and get its full output.
2. Save that output verbatim to `evals/captured/<skill>.txt` (or `.md`).
3. `evals/provider.py`'s `call_api` reads `evals/captured/<skill_case_id>.{txt,md}` and returns it as the provider's output — no network call, no live inference from promptfoo. Missing capture → `{"error": "no captured output for <id> — run the skill and save its output to evals/captured/"}`.
4. Run promptfoo against the exported config:
   ```bash
   npx promptfoo@latest eval -c evals/promptfoo/<skill>.yaml -o results.json
   ```

promptfoo exits **100** on test failure, **1** on other errors (overridable via `PROMPTFOO_FAILED_TEST_EXIT_CODE`). CI treats a nonzero exit as a failed run; see `.github/workflows/evals.yaml`.

### 4. Human tier (10%)

Spot-review judge disagreements and new failure modes; feed anything systematic back into `output_assertions` on the config so it graduates into the deterministic tier.

## Layout

- `configs/<skill>.yaml` — per-skill eval config: trigger cases (should/shouldn't invoke) and output assertions. Hand-written, source of truth. 30 configs, one per skill under `.claude/skills/`.
- `baseline.json` — generated by `run_evals.py baseline save`; per-skill trigger accuracy + deterministic-tier results, diffed by `baseline check`. Diffable JSON, do not hand-edit — regenerate.
- `promptfoo/<skill>.yaml` — generated by `run_evals.py export`; do not hand-edit.
- `provider.py` — promptfoo custom Python provider implementing the pre-captured-output pattern.
- `captured/<skill>.txt` — real skill transcripts saved for LLM-judge grading. Public, non-private outputs only. Real runs go to `personal/eval-captures/` (see the last section).
- `run_evals.py` — the deterministic runner, trigger-routing scorer, baseline gate, and the promptfoo exporter.
- `Taskfile.yaml` — `task evals:list`, `task evals:validate -- [skill]`, `task evals:run -- <skill> <output.txt>`, `task evals:export -- [skill]`. (No `triggers`/`baseline` tasks yet — invoke those two subcommands directly via `python3 evals/run_evals.py`.)

## Writing a config

Derive cases directly from the skill's Evals table:
1. Each "Should trigger? YES" row → a trigger-accuracy case.
2. Each "Expected output shape" → deterministic assertions (contains-section, citation-slot-present, numbered-claim-format).
3. Add regression cases from real failures (see git log for the fix batches of 2026-07-02 — e.g. Explore misassignment, coverage-quota fabrication).

Trigger cases (`trigger_cases:`) are executed by `run_evals.py triggers` (deterministic keyword match against the skill's frontmatter description, see §1 above) and folded into `evals/baseline.json` by `baseline save`/`baseline check`. They still double as documentation for human/LLM review of expected routing behavior — a `should_trigger: false` case should carry `routes_to: "<skill>"` naming where the prompt should actually route (see `discuss.yaml`, `fix.yaml`, etc. for the pattern), even though the deterministic tier only scores whether the *current* skill's own description correctly predicts trigger/no-trigger, not whether `routes_to` itself is correct.

## Baseline discipline

`evals/baseline.json` stores, per skill: `trigger_total`/`trigger_correct`/`trigger_accuracy` from the deterministic trigger-routing check, and a `deterministic` block — `{"status": "captured", "assertions_total", "passed", "failed"}` once `evals/captured/<skill>.{txt,md}` exists, or `{"status": "not_captured", "assertions_total"}` before it does (the state for all 30 skills today, since `evals/captured/` only has `.gitkeep`). Regenerate it with `python3 evals/run_evals.py baseline save` whenever a config's trigger cases or output assertions change on purpose. `python3 evals/run_evals.py baseline check` recomputes both against the current configs/skills and exits 1 if any skill's trigger accuracy drops below its stored baseline, or (once a transcript exists) its deterministic-assertion failure count rises above the stored one. New skills with no baseline entry yet pass with a `[new]` note rather than failing — CI gates on regression from baseline, not absolute thresholds. Golden captured transcripts should come from real session output, not hypotheticals, when the capture workflow (§3 below) is used.

**2026-08-18 regeneration note:** `baseline.json` was regenerated (`generated_at` now `2026-08-18T21:02:43Z`) as part of wiring the three-tier CI/CD regression gate (`knowledge/gestalt.md` ^ci-tiers). The trigger-accuracy deltas it contains (`fix` 0.67→0.33, `investigate` 1.0→0.33, `requirements` 0.64→0.91, `research` 0.67→1.0) are the expected fallout of the deliberate skill-router description re-tune in commit `6a2f799` ("re-tune skill router descriptions (F16)"), not a regression — the config `trigger_cases` didn't change, only which descriptions the deterministic keyword matcher scores them against. `baseline check` was re-run against the regenerated file and passes (0 regressions across 21 skills).

## CI

`.github/workflows/evals.yaml` runs on PRs/pushes touching `.claude/skills/**` or `evals/**`:
- **`validate`** (always, hard gate) — `python3 evals/run_evals.py validate`. Fails the check if any config is malformed.
- **`triggers` and `baseline check`** (always, report-only today) — run inside the `validate` job right after config validation, each with `continue-on-error: true`, so a routing miss or baseline regression is visible in the check output without failing the build. A comment directly above both steps in `evals.yaml` marks where to flip `continue-on-error` to `false` once the baseline has proven itself across a few PRs. **`evals/baseline.json` must be committed** for `baseline check` to have anything to compare against in a fresh CI checkout — regenerate and commit it whenever trigger cases or output assertions change intentionally.
- **`promptfoo`** (only if `evals/captured/` has captured outputs) — exports every config and runs `npx promptfoo@latest eval` against each, per-skill. Skipped gracefully when there's nothing captured yet, so an empty `captured/` never fails CI.


## Naming and capture status (X13, 2026-10-06)

The "trigger accuracy" score is a trigger lint: keyword overlap between a prompt and the skill's frontmatter description. It is not a routing test of the model, so `baseline.json` stores `trigger_lint_total`, `trigger_lint_correct` and `trigger_lint_accuracy` (the check still reads the old key names from an older file).

The output assertions run on real captured runs. Captures hold private content, so they live in `personal/eval-captures/<skill>/<date>-<short id>.md`, which git-crypt encrypts (`.gitattributes` covers `personal/**`). Nothing private goes into `evals/`.

### Capture a run

```
tools/capture-skill-run.py --latest investigate            # newest run of /investigate in ~/.claude/projects
tools/capture-skill-run.py <transcript.jsonl> prompt --nth 1   # the second newest run in one transcript
```

The tool takes the assistant's final message of the run (the text after its last tool call, up to the next human message). `--all-text` keeps every assistant text block. Tool results, thinking, sidechains and system reminders are dropped. It redacts tokens, secret assignments, private keys, non-generic email addresses, tailnet hosts and addresses, and ntfy topics, then writes a header with the skill, date, source transcript id and redaction count. It refuses any destination inside `evals/`. Read each capture before you commit it.

### Score the captures

```
python3 evals/run_evals.py captures [skill] [--strict]
```

This prints passed and failed output assertions per skill and per capture. It exits 0 unless `--strict` is given. On a locked clone (ciphertext) or a clone without the directory it prints "No readable captures" and skips. `baseline save` and `baseline check` never read `personal/eval-captures/`, so CI behaves the same with or without a key. A failing assertion on a real run is a finding: either the assertion tests the wrong message (several target worker output, not the final reply) or the skill drifted from its template.
